---
name: wewrite-imagepost
description: |
  微信公众号贴图（图片消息 / 小绿书）创作模块：从主题、文案、图片清单到草稿箱，
  产出 1-20 张竖图的 newspic 草稿。触发词：贴图、小绿书、图片帖、九宫格、图文卡片。
  常规长文、主题排版、群发不触发。
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - WebSearch
  - WebFetch
---

# wewrite-imagepost — 贴图创作流程

## 定位

贴图在微信后台叫“图片消息”，API 为 `article_type: newspic`：标题 + 纯文本正文 +
1–20 张图片，首图即封面。它与常规图文是两条并列流程：

- 不套排版主题、不生成 HTML、不调用 `wewrite preview/publish`；
- 正文只支持纯文本和少量商品标签，外链与 HTML 会被过滤；
- 只创建草稿，不自动发表、不走群发（群发 `msgtype` 没有 `newspic`）。

`{home}` 是 `wewrite home` 返回的状态目录，`{skill_dir}` 是本 skill 目录。

## 前置

1. 运行 `wewrite diagnose --json`。命令缺失才引导安装；缺风格文件属正常首次设置，
   转 `wewrite-style`。
2. 已有贴图任务时先 `wewrite run show`，用 `wewrite run resume <run_id>` 继续，
   不要新建任务。
3. 新建任务：

```bash
wewrite run start --topic "{主题，可空}" --content-type image_post --mode draft
```

`--max-images` 默认 9，上限 20；用户明确说“推到草稿箱”时用 `--mode publish`，
否则保持 `draft`，发布权限在第 6 步单独授予。

## 流程

### 1. 主题与图片叙事（plan）

先确定目标读者、一句话主张，以及每张图承担什么信息。把结果写到
`artifacts.image_plan`（`image-plan.md`），包含：

- 图片数量与顺序，首图为什么适合当封面；
- 每张图的画面主体、景别、构图与文字留白；
- 统一视觉：色板、光线、质感、是否出现人物；
- 哪些图用户会提供，哪些需要生成。

不要为了凑数加图；3–9 张通常比 20 张更有节奏。

```bash
wewrite run step plan completed
```

### 2. 文案（copy）

贴图文案短，但信息要完整：

- 标题 ≤32 字，具体、可核对，不标题党；
- 正文为纯文本，建议 200–800 字，可换行分段；不放 HTML、外链、复杂表格；
- 每张图配一句说明，写进图片条目，方便发布前核对顺序。

同时维护两份产物：

- `artifacts.image_post`（`image-post.md`）：人读稿，标题 + 正文 + 图片顺序；
- `artifacts.post_meta`（`image-post.yaml`）：机读稿，供 CLI 使用。

```yaml
version: 1
title: "标题（≤32 字）"
content: |
  第一段正文。
  第二段正文。
images:
  - path: images/01.png
    caption: "首图：封面"
  - path: images/02.png
    caption: "第二张：细节"
cover_ratio: "1_1"        # 可选：1_1 / 16_9 / 2.35_1
open_comment: true
```

图片路径相对于 `image-post.yaml` 所在目录，文件必须真实存在。

```bash
wewrite run step copy completed
```

### 3. 图片（images）

用户提供图片时优先使用；不足的部分按 `image-plan.md` 生成，统一用 3:4 竖图：

```bash
wewrite image-gen --manifest {run_dir}/images.json --max-images {max_images} --max-cost {max_cost}
```

`max_cost` 为空时省略该参数；不得绕过任务里的数量和费用上限。图片要求：

- 1–20 张，单张 ≤10MB，格式 jpg/jpeg/png/bmp/gif；
- 统一比例接近 3:4（0.75），首图信息最强；
- 生成失败时保留提示词，不要用低质图凑数。

最终顺序写入 `image-post.yaml` 后跑预检：

```bash
wewrite image-post-check --post {run_dir}/image-post.yaml --json
```

预检不通过就修图片或文案，不要跳过。

```bash
wewrite run step images completed
```

### 4. 轻量审稿（review）

贴图不走长文五维审稿，但仍要过这五项：

1. **准确**：标题和正文里的数字、人名、地点、价格可核对；不能核实的删掉。
2. **不虚构**：没有用户提供的经历，不写“我亲身经历”。
3. **合规**：无医疗/金融等违规承诺，无导流外链，无侵权图片。
4. **一致**：文案说的信息在图片里能看到，图片顺序与正文节奏一致。
5. **封面**：首图在 1:1 与 3:4 裁切下主体仍完整。

需要外部事实时用 `wewrite sources add` 记账，不得把模型记忆标为 `verified`。
把结论写进任务状态：

```bash
wewrite run update --patch '{"review": {"decision": "pass", "publishable": true, "notes": "简短结论"}}'
```

不通过就直接改稿并复审，最多两轮。

```bash
wewrite run step review completed
```

### 5. 封存

```bash
wewrite run finish
```

只有 `image-post.yaml` 可解析、标题 ≤32 字、至少 1 张图片存在且
`review.publishable=true` 时才会封存。封存后不再改写正文；换图或改文案请新建任务。

### 6. 推草稿箱（仅明确授权）

用户本轮明确说“推草稿箱 / 传到草稿箱 / 建草稿”时才执行：

```bash
wewrite run permission publish allow
wewrite image-post --post {run_dir}/image-post.yaml \
  --media-cache {run_dir}/media-cache.json --json
```

- 先上传永久图片素材，再调 `draft/add`；
- `media-cache.json` 让重试跳过已上传图片，避免重复占用素材库；
- 成功后把 `media_id` 写入 `publish.media_id`，再 `wewrite run step publish completed`；
- 失败时保留本地稿和预览信息，不自动重试外部动作。

用户没有明确授权时，流程止于第 5 步，只交付本地 `image-post.md` 和图片。

## 失败与恢复

```bash
wewrite run step <plan|copy|images|review|publish> failed --error "简短原因"
```

- 图片生成失败：保留 `image-plan.md` 和提示词，任务保持可恢复；
- 素材上传失败：`media-cache.json` 已记录成功项，重跑只补失败项；
- 草稿创建失败：检查标题长度、图片数量、素材 `media_id` 与 API 出口配置；
- 发布失败不改变任务的完成状态。

## 禁止

- 不得调用 `wewrite publish`、`wewrite preview` 或任何主题排版命令；
- 不得把长文的 `article.md` 当作贴图正文；
- 不得在正文里塞 HTML、外链或超过 20 张图片；
- 不得自动调用 `freepublish/submit` 或群发接口；
- 不得在没有发布权限时上传素材或创建草稿。
