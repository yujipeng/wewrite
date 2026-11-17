#!/usr/bin/env python3
"""
CLI entry point for WeWrite.

Usage:
    python cli.py preview article.md --theme professional-clean
    python cli.py publish article.md --appid wx123 --secret abc123
    python cli.py themes
"""

import argparse
import json
import sys
import webbrowser
from pathlib import Path

import yaml

from .converter import WeChatConverter, make_paste_safe, preview_html
from .theme import load_theme, list_themes
from .wechat_api import get_access_token, upload_image, upload_thumb
from .publisher import (
    build_image_post_body,
    check_image_post,
    create_draft,
    create_image_post,
    upload_image_assets,
)
from .config import load_config


def _print_validation(body_html: str) -> None:
    """产物合规自检：converter 自产应恒过 ERROR 级，命中即打印醒目告警（不阻断）。"""
    from ..commands.validate_html import validate_html
    issues = validate_html(body_html)
    for i in issues:
        print(f"  ⚠ [{i['level']}] {i['rule']}: {i['message']}", file=sys.stderr)


def cmd_preview(args):
    """Generate HTML preview and open in browser."""
    theme = load_theme(args.theme)
    converter = WeChatConverter(theme=theme)
    result = converter.convert_file(args.input)

    _print_validation(result.html)

    # 粘贴路径加固（preview 的用途就是复制进编辑器）；--no-paste-safe 关闭
    body = result.html if getattr(args, "no_paste_safe", False) else make_paste_safe(result.html)

    # Wrap in full HTML for browser preview
    full_html = preview_html(body, theme)

    # Write to temp file
    input_path = Path(args.input)
    output = args.output or str(input_path.with_suffix(".html"))
    Path(output).write_text(full_html, encoding="utf-8")

    print(f"Title: {result.title}")
    print(f"Digest: {result.digest}")
    print(f"Images: {len(result.images)}")
    print(f"Output: {output}")

    if not args.no_open:
        webbrowser.open(f"file://{Path(output).absolute()}")
        print("Opened in browser.")


def cmd_publish(args):
    """Convert, upload images, and create WeChat draft."""
    cfg = load_config()
    wechat_cfg = cfg.get("wechat", {})

    # Resolve from CLI args → config.yaml fallback
    appid = args.appid or wechat_cfg.get("appid")
    secret = args.secret or wechat_cfg.get("secret")
    theme_name = args.theme or cfg.get("theme", "professional-clean")
    author = args.author or wechat_cfg.get("author")
    # API 端点：nginx relay（留空 = 直连 api.weixin.qq.com）
    api_base = wechat_cfg.get("base_url")

    if not appid or not secret:
        print("Error: --appid and --secret required (or set in config.yaml)", file=sys.stderr)
        sys.exit(1)

    theme = load_theme(theme_name)
    converter = WeChatConverter(theme=theme)
    result = converter.convert_file(args.input)

    _print_validation(result.html)

    print(f"Title: {result.title}")
    print(f"Digest: {result.digest}")
    print(f"Images found: {len(result.images)}")

    # Get access token
    token = get_access_token(appid, secret, base_url=api_base)
    print("Access token obtained.")

    # Upload images referenced in article and replace src
    # Resolve relative paths against the markdown file's directory
    md_dir = Path(args.input).resolve().parent
    html = result.html
    for img_src in result.images:
        if img_src.startswith(("http://", "https://")):
            print(f"Skipping remote image: {img_src}")
            continue

        # Try: absolute → relative to CWD → relative to markdown file
        img_path = Path(img_src)
        if not img_path.is_absolute():
            if not img_path.exists():
                img_path = md_dir / img_src

        if img_path.exists():
            print(f"Uploading image: {img_src}")
            wechat_url = upload_image(token, str(img_path), base_url=api_base)
            html = html.replace(img_src, wechat_url)
            print(f"  -> {wechat_url}")
        else:
            print(f"Warning: image not found: {img_src} (searched {md_dir})")

    # Upload cover image if provided
    thumb_media_id = None
    if args.cover:
        print(f"Uploading cover: {args.cover}")
        thumb_media_id = upload_thumb(token, args.cover, base_url=api_base)
        print(f"  -> media_id: {thumb_media_id}")

    # Create draft
    title = args.title or result.title or Path(args.input).stem
    digest = args.digest or result.digest
    draft = create_draft(
        access_token=token,
        title=title,
        html=html,
        digest=digest,
        thumb_media_id=thumb_media_id,
        author=author,
        base_url=api_base,
    )

    print(f"\nDraft created! media_id: {draft.media_id}")


def cmd_themes(args):
    """List available themes."""
    names = list_themes()
    for name in names:
        theme = load_theme(name)
        print(f"  {name:24s} {theme.description}")


def _parse_crop(value):
    if not value:
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError("--crop 需要 x1,y1,x2,y2 四个 0-1 之间的数字")
    keys = ("x1", "y1", "x2", "y2")
    return {key: str(float(part)) for key, part in zip(keys, parts)}


def _load_post_file(path):
    """Read image-post.yaml and resolve image paths relative to the file."""
    post_path = Path(path).expanduser()
    data = yaml.safe_load(post_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("image-post.yaml 必须是映射结构")
    base = post_path.parent
    images = []
    for item in data.get("images") or []:
        raw = item.get("path") if isinstance(item, dict) else item
        if not raw:
            raise ValueError("image-post.yaml 中存在缺少 path 的图片")
        candidate = Path(str(raw)).expanduser()
        images.append(str(candidate if candidate.is_absolute() else base / candidate))
    return {
        "path": post_path,
        "title": str(data.get("title") or ""),
        "content": data.get("content") or "",
        "images": images,
        "cover_ratio": data.get("cover_ratio"),
        "open_comment": bool(data.get("open_comment", True)),
        "fans_only_comment": bool(data.get("only_fans_can_comment", False)),
        "media_cache": data.get("media_cache"),
        "media_id_out": data.get("media_id_out"),
    }


def _post_inputs(args):
    post = _load_post_file(args.post) if getattr(args, "post", None) else {}
    title = getattr(args, "title", None) or post.get("title") or ""
    content = getattr(args, "content", None)
    if content is None:
        content = post.get("content", "")
    images = list(getattr(args, "images", None) or post.get("images") or [])
    return post, title, content, images


def _print_check(result, as_json):
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    for warning in result["warnings"]:
        print(f"  ⚠ {warning}", file=sys.stderr)
    for error in result["errors"]:
        print(f"  ✗ {error}", file=sys.stderr)
    if result["ok"]:
        print(f"预检通过：{len(result['images'])} 张图片")


def cmd_image_post(args):
    """Create a WeChat image post (小绿书) from a post file or image paths."""
    post, title, content, images = _post_inputs(args)
    cover_ratio = args.cover_ratio or post.get("cover_ratio")
    crop = _parse_crop(args.crop)
    open_comment = post.get("open_comment", True) and not args.no_comments
    fans_only_comment = post.get("fans_only_comment", False)

    check = check_image_post(title, images, content)
    _print_check(check, as_json=False)
    if not check["ok"]:
        sys.exit(1)

    if args.dry_run:
        media_ids = [f"DRY_RUN_MEDIA_ID_{index}" for index in range(1, len(images) + 1)]
        body = build_image_post_body(
            title=title,
            image_media_ids=media_ids,
            content=content,
            cover_ratio=cover_ratio,
            cover_crop=crop,
            open_comment=open_comment,
            fans_only_comment=fans_only_comment,
        )
        payload = {"dry_run": True, "images": check["images"], "body": body}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("Dry run：未调用微信 API。将提交的 draft/add 请求体：")
            print(json.dumps(body, ensure_ascii=False, indent=2))
        return

    cfg = load_config()
    wechat_cfg = cfg.get("wechat", {})
    appid = args.appid or wechat_cfg.get("appid")
    secret = args.secret or wechat_cfg.get("secret")
    api_base = wechat_cfg.get("base_url")
    if not appid or not secret:
        print("Error: --appid and --secret required (or set in config.yaml)", file=sys.stderr)
        sys.exit(1)

    cache_path = args.media_cache or post.get("media_cache")
    if not cache_path and args.post:
        cache_path = str(post["path"].parent / "media-cache.json")

    token = get_access_token(appid, secret, base_url=api_base)
    print(f"Uploading {len(images)} images as permanent materials...")
    uploads = upload_image_assets(token, images, cache_path=cache_path, base_url=api_base)
    for item in uploads:
        suffix = " (cached)" if item["cached"] else ""
        print(f"  {Path(item['path']).name} -> {item['media_id']}{suffix}")

    result = create_image_post(
        access_token=token,
        title=title,
        image_media_ids=[item["media_id"] for item in uploads],
        content=content,
        open_comment=open_comment,
        fans_only_comment=fans_only_comment,
        base_url=api_base,
        cover_ratio=cover_ratio,
        cover_crop=crop,
    )

    media_id_out = args.media_id_out or post.get("media_id_out")
    if not media_id_out and args.post:
        media_id_out = str(post["path"].parent / "image-post-media-id.txt")
    if media_id_out:
        out_path = Path(media_id_out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result.media_id + "\n", encoding="utf-8")

    payload = {
        "media_id": result.media_id,
        "image_count": result.image_count,
        "image_media_ids": result.image_media_ids,
        "title": title,
        "images": [item["path"] for item in uploads],
        "cover_ratio": cover_ratio,
        "media_id_out": media_id_out or None,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("\nImage post draft created!")
        print(f"  media_id: {result.media_id}")
        print(f"  images: {result.image_count}")
        print(f"  title: {title}")
        print("  请到公众号后台草稿箱检查并发布")


def cmd_image_post_check(args):
    """Preflight an image post without calling WeChat."""
    _, title, content, images = _post_inputs(args)
    result = check_image_post(title, images, content)
    _print_check(result, as_json=args.json)
    if not result["ok"]:
        sys.exit(1)


def cmd_gallery(args):
    """Render all themes side by side in a browser gallery."""
    from concurrent.futures import ThreadPoolExecutor

    # Use provided markdown or a built-in sample
    if args.input:
        md_text = Path(args.input).read_text(encoding="utf-8")
    else:
        md_text = _gallery_sample_markdown()

    names = list_themes()
    results = {}

    def render_theme(name):
        theme = load_theme(name)
        converter = WeChatConverter(theme=theme)
        result = converter.convert(md_text)
        return name, theme.description, result.html

    # Parallel rendering
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, desc, html in pool.map(lambda n: render_theme(n), names):
            results[name] = (desc, html)

    # Build gallery HTML
    gallery_html = _build_gallery_html(results, names)
    output = args.output or "/tmp/wewrite-gallery.html"
    Path(output).write_text(gallery_html, encoding="utf-8")
    print(f"Gallery: {output} ({len(names)} themes)")

    if not args.no_open:
        webbrowser.open(f"file://{Path(output).absolute()}")


def cmd_learn_theme(args):
    """Learn a theme from a WeChat article URL."""
    from ..commands import learn_theme as learn_theme_mod
    sys.argv = ["wewrite learn-theme", args.url, "--name", args.name]
    learn_theme_mod.main()


def _gallery_sample_markdown():
    return """# 示例文章标题

## 第一部分

这是一段正常的文章内容，用来展示不同主题的排版效果。WeWrite 支持多种排版主题，每种都有独特的视觉风格。

说实话，选主题这件事——看截图永远不如看实际渲染效果。

## 关键数据

| 指标 | 数值 | 变化 |
|------|------|------|
| 阅读量 | 12,580 | +23% |
| 分享率 | 4.7% | +0.8% |
| 完读率 | 68% | -2% |

## 代码示例

```python
def hello():
    print("Hello, WeWrite!")
```

> 好的排版不是让读者注意到设计，而是让读者忘记设计，只记住内容。

## 列表展示

- 第一个要点：简洁是设计的灵魂
- 第二个要点：一致性比创意更重要
- 第三个要点：移动端体验优先

**加粗文本**和*斜体文本*的样式也需要关注。

最后这段用来展示文章结尾的留白和间距效果。一篇好文章的结尾，应该像一首好歌的最后一个音符——恰到好处地收束。
"""


def _join_newline(items):
    """Join items with comma + newline (workaround for f-string limitation)."""
    return ",\n".join(items)


def _build_gallery_html(results, names):
    cards = []
    for name in names:
        desc, html = results[name]
        # Escape for embedding in JS
        escaped_html = html.replace('\\', '\\\\').replace('`', '\\`').replace('$', '\\$')
        cards.append(f"""
        <div class="theme-card" onclick="selectTheme('{name}')">
          <div class="theme-name">{name}</div>
          <div class="theme-desc">{desc}</div>
          <div class="phone-frame">
            <div class="phone-content" id="preview-{name}">{html}</div>
          </div>
          <button class="copy-btn" onclick="event.stopPropagation(); copyHTML('{name}')">复制 HTML</button>
        </div>""")

    # Store HTML data for copy
    data_entries = []
    for name in names:
        desc, html = results[name]
        safe = html.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n')
        data_entries.append(f"  '{name}': '{safe}'")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WeWrite 主题画廊</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f0f0f; color: #fff; }}
.header {{ text-align: center; padding: 40px 20px 20px; }}
.header h1 {{ font-size: 28px; font-weight: 700; }}
.header p {{ color: #888; margin-top: 8px; font-size: 15px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 24px; padding: 24px; max-width: 1440px; margin: 0 auto; }}
.theme-card {{ background: #1a1a1a; border-radius: 12px; padding: 16px; cursor: pointer; transition: transform 0.2s, box-shadow 0.2s; }}
.theme-card:hover {{ transform: translateY(-4px); box-shadow: 0 8px 24px rgba(0,0,0,0.4); }}
.theme-name {{ font-size: 16px; font-weight: 700; margin-bottom: 4px; }}
.theme-desc {{ font-size: 13px; color: #888; margin-bottom: 12px; }}
.phone-frame {{ background: #fff; border-radius: 8px; overflow: hidden; max-height: 480px; overflow-y: auto; }}
.phone-content {{ padding: 16px; font-size: 14px; transform: scale(0.85); transform-origin: top left; width: 118%; }}
.copy-btn {{ margin-top: 12px; width: 100%; padding: 8px; background: #333; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }}
.copy-btn:hover {{ background: #555; }}
.toast {{ position: fixed; bottom: 40px; left: 50%; transform: translateX(-50%); background: #333; color: #fff; padding: 10px 24px; border-radius: 8px; font-size: 14px; display: none; z-index: 999; }}
</style>
</head>
<body>
<div class="header">
  <h1>WeWrite 主题画廊</h1>
  <p>{len(names)} 个主题 · 点击卡片查看大图 · 点击「复制 HTML」直接粘贴到公众号编辑器</p>
</div>
<div class="grid">
{''.join(cards)}
</div>
<div class="toast" id="toast">已复制到剪贴板</div>
<script>
const themeData = {{
{_join_newline(data_entries)}
}};
function copyHTML(name) {{
  const html = themeData[name];
  if (html) {{
    navigator.clipboard.writeText(html).then(() => {{
      const t = document.getElementById('toast');
      t.style.display = 'block';
      setTimeout(() => t.style.display = 'none', 1500);
    }});
  }}
}}
function selectTheme(name) {{
  localStorage.setItem('wewrite-theme', name);
  // Scroll to card for visual feedback
  const el = document.getElementById('preview-' + name);
  if (el) el.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
}}
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(
        prog="wewrite",
        description="Markdown to WeChat HTML converter and publisher",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # preview
    p_preview = sub.add_parser("preview", help="Generate HTML and open in browser")
    p_preview.add_argument("input", help="Markdown file path")
    p_preview.add_argument("-t", "--theme", default="professional-clean", help="Theme name")
    p_preview.add_argument("-o", "--output", help="Output HTML file path")
    p_preview.add_argument("--no-open", action="store_true", help="Don't open browser")
    p_preview.add_argument("--no-paste-safe", action="store_true", help="不做粘贴加固（leaf 包裹）")

    # publish
    p_publish = sub.add_parser("publish", help="Convert and publish as WeChat draft")
    p_publish.add_argument("input", help="Markdown file path")
    p_publish.add_argument("-t", "--theme", default=None, help="Theme name")
    p_publish.add_argument("--appid", default=None, help="WeChat AppID (or set in config.yaml)")
    p_publish.add_argument("--secret", default=None, help="WeChat AppSecret (or set in config.yaml)")
    p_publish.add_argument("--cover", help="Cover image file path")
    p_publish.add_argument("--title", help="Override article title")
    p_publish.add_argument("--author", default=None, help="Article author")
    p_publish.add_argument("--digest", default=None, help="Override article digest (≤120 UTF-8 bytes)")

    # themes
    sub.add_parser("themes", help="List available themes")

    # image-post (小绿书)
    p_imgpost = sub.add_parser("image-post", help="Create WeChat image post (小绿书)")
    p_imgpost.add_argument("images", nargs="*", help="Image file paths (1-20, first = cover)")
    p_imgpost.add_argument("-t", "--title", default=None, help="Post title (max 32 chars)")
    p_imgpost.add_argument("-c", "--content", default=None, help="Plain text description (max ~1000 chars)")
    p_imgpost.add_argument("--post", default=None, help="image-post.yaml 路径（标题/正文/图片/裁剪）")
    p_imgpost.add_argument(
        "--cover-ratio", choices=["1_1", "16_9", "2.35_1"], default=None,
        help="封面裁剪比例（写入 cover_info）",
    )
    p_imgpost.add_argument("--crop", default=None, help="显式裁剪框 x1,y1,x2,y2（0-1）")
    p_imgpost.add_argument("--media-cache", default=None, help="永久素材 media_id 缓存（重试幂等）")
    p_imgpost.add_argument("--media-id-out", default=None, help="草稿 media_id 输出文件")
    p_imgpost.add_argument("--dry-run", action="store_true", help="只校验并输出请求体，不调用微信 API")
    p_imgpost.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    p_imgpost.add_argument("--no-comments", action="store_true", help="关闭评论")
    p_imgpost.add_argument("--appid", default=None, help="WeChat AppID")
    p_imgpost.add_argument("--secret", default=None, help="WeChat AppSecret")

    # image-post-check (贴图预检)
    p_postcheck = sub.add_parser("image-post-check", help="Preflight an image post (no WeChat calls)")
    p_postcheck.add_argument("images", nargs="*", help="Image file paths")
    p_postcheck.add_argument("-t", "--title", default=None, help="Post title")
    p_postcheck.add_argument("-c", "--content", default=None, help="Plain text description")
    p_postcheck.add_argument("--post", default=None, help="image-post.yaml 路径")
    p_postcheck.add_argument("--json", action="store_true", help="结构化 JSON 输出")

    # gallery
    p_gallery = sub.add_parser("gallery", help="Open theme gallery in browser")
    p_gallery.add_argument("input", nargs="?", default=None, help="Markdown file (optional, uses sample if omitted)")
    p_gallery.add_argument("-o", "--output", help="Output HTML file path")
    p_gallery.add_argument("--no-open", action="store_true", help="Don't open browser")

    # learn-theme
    p_learn = sub.add_parser("learn-theme", help="Learn formatting theme from a WeChat article URL")
    p_learn.add_argument("url", help="WeChat article URL")
    p_learn.add_argument("--name", required=True, help="Theme name")

    args = parser.parse_args()

    try:
        if args.command == "preview":
            cmd_preview(args)
        elif args.command == "publish":
            cmd_publish(args)
        elif args.command == "themes":
            cmd_themes(args)
        elif args.command == "image-post":
            cmd_image_post(args)
        elif args.command == "image-post-check":
            cmd_image_post_check(args)
        elif args.command == "gallery":
            cmd_gallery(args)
        elif args.command == "learn-theme":
            cmd_learn_theme(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
