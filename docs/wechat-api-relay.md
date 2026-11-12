# 微信 API 出口：SSH 隧道与 Nginx 转发

公众号后台的 API 只接受 **IP 白名单**内地址的请求。家用或办公宽带出口 IP 不固定时，
`wewrite publish` 会拿到 `errcode 40164`（invalid ip）。解法是把微信出口换到一台
IP 固定的服务器上，`wewrite` 侧只改一个配置项 `wechat.base_url`。

本文给两种接法：**SSH 隧道**（不暴露公网端口）与 **Nginx 反向代理**（一次配置、多机共用）。

## 先理解配置缝

`wewrite` 里所有微信 API 调用（取 token、正文图 `media/uploadimg`、封面 `material/add_material`、
草稿 `draft/add`、`draft/get`、数据分析 `datacube/*`）都从同一个 base 拼出 URL：

| 命令 | 是否走 `base_url` |
|------|------------------|
| `wewrite publish` | ✅ token / 正文图 / 封面 / 草稿 |
| `wewrite image-post` | ✅ token / 封面与图片素材 / 草稿 |
| `wewrite stats` | ✅ token / datacube |
| `wewrite learn-edits --from-wechat` | ✅ token / `draft/get` |
| `wewrite fetch-article` | ❌ 抓的是文章页 HTML，不调 API |

生效顺序（从高到低）：

1. 环境变量 `WECHAT_API_BASE`
2. `config.yaml` 里的 `wechat.base_url`
3. 默认 `https://api.weixin.qq.com`（不配置时的行为与旧版本完全一致）

例外：`learn-edits --from-wechat` 直接读 `config.yaml` 文件，请把它写在 `wechat.base_url` 里。

配置写法（`~/.wewrite/config.yaml`，或 `cp config.example.yaml` 后填）：

```yaml
wechat:
  appid: "wx..."
  secret: "..."
  base_url: "https://wx.example.com/relay-path"   # 留空 = 直连 api.weixin.qq.com
```

或：

```bash
export WECHAT_API_BASE="http://127.0.0.1:8080"
```

**只写 `scheme://host[/路径前缀]`**：不要带尾斜杠，更不要带 `?`。代码只做「去空白 + 去尾斜杠」，
路径前缀会原样保留并拼上 `/cgi-bin/...`；写了 query 会被拼进 URL 里变成坏地址。

检查当前解析结果：

```bash
python3 -c "from wewrite.toolkit.config import load_config; print(load_config().get('wechat', {}).get('base_url'))"
```

---

## 方式一：SSH 隧道

前提：你能 SSH 到一台服务器，**该服务器的公网 IP 已在公众号白名单里**，且它能访问
`api.weixin.qq.com`。

适合临时排障、单人使用、不想在公网开任何端口。两种形态按场景选：

### 1A. 隧道 + 服务器本地 HTTP 反代（推荐）

链路：`本机 HTTP → SSH 加密隧道 → 服务器 127.0.0.1:8080 → HTTPS 微信`。

**服务器侧**（nginx，只监听回环地址，公网访问不到）：

```nginx
server {
    listen 127.0.0.1:8080;
    server_name _;

    location / {
        proxy_pass https://api.weixin.qq.com;   # 不带 URI：$uri 原样拼到上游
        proxy_set_header Host api.weixin.qq.com;
        proxy_ssl_server_name on;               # 到微信的 TLS SNI，缺了会 502
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_read_timeout 60s;
        client_max_body_size 12m;               # 图片上传余量（微信单图上限 10MB）
    }
}
```

改完 `nginx -t && systemctl reload nginx`（Caddy 用 `caddy reload`）。

用 Caddy 则更短（同上，只绑回环）：

```
:8080 {
    bind 127.0.0.1
    reverse_proxy https://api.weixin.qq.com
}
```

**本机侧**：

```bash
ssh -N -L 8080:127.0.0.1:8080 user@your-server     # 前台；加 -f 后台，或用 autossh 保活
export WECHAT_API_BASE="http://127.0.0.1:8080"
wewrite publish article.md --cover cover.png
```

这里 base 必须是 **http**：隧道替你加密，服务器上的反代是明文监听，本机到 8080 也只是本机回环。
本机发起 HTTPS（`https://127.0.0.1:8080`）会因证书主机名不匹配直接失败。

### 1B. SSH 动态转发（SOCKS5）+ 环境变量代理

服务器上什么都不用装，只要它的 IP 在白名单、能出网到 `api.weixin.qq.com`。
链路是进程级代理：`wewrite` 的 HTTPS 请求经隧道由服务器发出。

```bash
ssh -N -D 1080 user@your-server                     # SOCKS5，默认只监听 localhost
pip install "requests[socks]"                       # requests 走 SOCKS 需要 PySocks
# pipx 安装的：pipx inject wewrite "requests[socks]"
# uv tool 安装的：uv tool install wewrite --with "requests[socks]"

HTTPS_PROXY=socks5h://127.0.0.1:1080 wewrite publish article.md --cover cover.png
```

要点：

- `socks5h` 里的 `h` 让 **服务器侧做 DNS 解析**，避免本机 DNS 结果或污染影响转发。
- 这个形态不改 `wechat.base_url`（仍是 `https://api.weixin.qq.com`，只是 TCP 走了隧道），
  所以不需要在 config 里留任何痕迹。
- 代理是进程级的：同一进程里所有 HTTPS 请求（生图 API、搜索等）都会穿过隧道。只想让微信流量
  走隧道、其它直连，请用 1A。
- 隧道断了表现为 `ConnectionError` / `ProxyError`，重连命令即可。

### 为什么不直接把隧道指到 443

`ssh -L 8443:api.weixin.qq.com:443` + `base_url: https://127.0.0.1:8443` 看着最直觉，但
TLS 证书是签给 `api.weixin.qq.com` 的，而 URL 的 host 是 `127.0.0.1`，`requests` 会以
主机名不匹配拒绝（要么改 hosts、要么自签信任，两边都不值得）。1A 用「本机明文 + 隧道 + 服务器
终结 TLS」绕开了这个问题。

---

## 方式二：Nginx 反向代理（公网 relay）

前提：一台有固定公网 IP 的服务器。做成公网 relay 后，多台机器、多个人都能共用；
代价是必须至少加一层访问控制，否则会被扫描器发现并盗用你的 appid 配额。

### 步骤

**1. 域名与证书**（`certbot --nginx` 或你自己的证书流程）：

```nginx
server {
    listen 443 ssl;
    server_name wx.example.com;
    ssl_certificate     /etc/letsencrypt/live/wx.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/wx.example.com/privkey.pem;
    # ...
}
```

**2. 把路径前缀当口令**。`wewrite` 只做「地址替换」，**不会附加 `Authorization` 头**，
所以只能用 path secret 形态（bearer 形态会让所有请求 401）。生成口令：

```bash
openssl rand -hex 16        # 32 位十六进制；下面示例串只是占位，换成你自己生成的
```

`location` 匹配前缀、`proxy_pass` 末尾的 `/` 负责剥掉前缀，原 query string（`appid`/`secret`/
`access_token`）自动原样带上：

```nginx
# http {} 层：日志脱敏（见下方「日志」）
map $uri $wx_relay_path {
    ~^/3f8a1c9e5b7d2f60a4c7e9b1d3502f68(/.*)?$  $1;
    default                     $uri;
}
log_format wxrelay '$remote_addr [$time_local] "$request_method $wx_relay_path" $status';

server {
    listen 443 ssl;
    server_name wx.example.com;
    # ssl_certificate / ssl_certificate_key 同上

    access_log /var/log/nginx/wxrelay.log wxrelay;   # 不写默认 combined，它会把 secret 落盘

    # 三个必须点之一：location 必须以 / 结尾
    location ^~ /3f8a1c9e5b7d2f60a4c7e9b1d3502f68/ {
        # 三个必须点之二：proxy_pass 必须带 URI（末尾 /），否则前缀会被整段发给微信 → 404
        proxy_pass https://api.weixin.qq.com/;
        proxy_set_header Host api.weixin.qq.com;
        proxy_ssl_server_name on;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_read_timeout 60s;
        client_max_body_size 12m;
    }

    location / {
        return 404;      # 前缀不符一律 404，不暴露 relay 存在
    }
}
```

三个必须点，任一处写错都会「配置看着没问题、请求全 404」：前两条已标在上面配置的注释里
（`location` 以 `/` 结尾、`proxy_pass` 带 URI）；第三条是路径前缀**区分大小写**，
`location` 与 `base_url` 必须逐字符一致——`wewrite` 只去空白和尾斜杠，不做大小写归一。

用 Caddy 等价写法：

```
wx.example.com {
    handle_path /3f8a1c9e5b7d2f60a4c7e9b1d3502f68/* {
        reverse_proxy api.weixin.qq.com
    }
    respond 404
}
```

**3. 服务器 IP 进白名单**：公众号后台 →「设置与开发」→「基本配置」→「公众号开发信息」→
「IP 白名单」→「修改」→ 填 relay 服务器的公网 IP（多行分隔）→ 扫码确认。

**4. `wewrite` 侧**：

```yaml
wechat:
  base_url: "https://wx.example.com/3f8a1c9e5b7d2f60a4c7e9b1d3502f68"
```

**5. 验证**（`40164` 说明 relay 通了但白名单没生效，`access_token` 说明全链路正常）：

```bash
export APPID=... SECRET=...     # 或用 read -s，别让凭据进 shell 历史
S=3f8a1c9e5b7d2f60a4c7e9b1d3502f68
B=https://wx.example.com

# 前缀正确 → 微信返回 access_token 或 40164（两者都证明 nginx 侧配置正确）
curl -s "$B/$S/cgi-bin/token?grant_type=client_credential&appid=$APPID&secret=$SECRET"

# 前缀错误 → 必须是 nginx 的 404
curl -s -o /dev/null -w '%{http_code}\n' "$B/cgi-bin/token"
```

### 日志

nginx 默认的 `combined` 格式会把完整 `$request`（含 query 里的 `appid`/`secret`）和 path secret
写进 access.log。上面的 `map` + `log_format` 抹掉前缀、只记 `$uri`；不想留任何访问日志就直接
`access_log off;`。

### 可选加固

- nginx 到上游默认不校验证书。要收紧可加 `proxy_ssl_verify on;` 与
  `proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;`（Debian/Ubuntu 路径）。
- 用 `certbot --webroot` 签发时，`location / { return 404; }` 会挡掉 ACME 校验，需放行
  `location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }`（`certbot --nginx` 会自动插入）。

---

## 故障对照表

| 现象 | 多半是什么 |
|------|-----------|
| `errcode 40164` | relay 链路已通，白名单里缺服务器出口 IP（或 IP 填错未保存） |
| `401` | 用了 bearer 形态鉴权；`wewrite` 不发 `Authorization` 头，请改 path secret |
| nginx 的 `404` 页面 | `location` 前缀与 `base_url` 不一致；或 `proxy_pass` 少了末尾 `/`，含 secret 的路径被整段发给微信 |
| `502` / `504` | relay 到 `api.weixin.qq.com` 失败：`proxy_ssl_server_name on` 缺失、DNS 过期、服务器出网被拦 |
| `413` | `client_max_body_size` 太小，图片上传被 nginx 直接拒掉 |
| 本机 `ConnectionError` | SSH 隧道断了 / 端口被占 / `-L` 目标写错 |
| `https://127.0.0.1:xxxx` 证书错误 | 隧道形态用了 https 的本地 base，见上文「为什么不直接把隧道指到 443」 |
| 换 IP 后重新出现 `40164` | 服务器换 IP 了，同步改公众号白名单 |

## 运维要点

- relay 无状态、不落盘，可随时销毁重建；唯一的「配置」是白名单里的 IP。
- `/cgi-bin/token` 有每日调用额度：冒烟验证一次即可，别写进循环脚本。
- 上传接口返回的图片地址仍是微信自己的 CDN 域名（`mmbiz.qpic.cn`），与 `base_url` 无关，属于正常。
- 不想自建、也无法固定出口 IP：这是微信侧的硬约束（白名单只认 IP），`wewrite` 不提供也不销售代理服务；
  可以先用「本地排版 + 手动粘贴」完成发布，等有白名单条件时再接。
