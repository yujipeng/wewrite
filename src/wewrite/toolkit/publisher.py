import json
from pathlib import Path

import requests
from dataclasses import dataclass
from typing import Optional

from .wechat_api import resolve_api_base, upload_thumb


@dataclass
class DraftResult:
    media_id: str


@dataclass
class ImagePostResult:
    media_id: str
    image_count: int
    image_media_ids: list[str]


IMAGE_POST_MAX_IMAGES = 20
IMAGE_POST_TITLE_LIMIT = 32
IMAGE_POST_CONTENT_LIMIT = 20000
IMAGE_POST_COVER_RATIOS = ("1_1", "16_9", "2.35_1")
IMAGE_ALLOWED_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".gif")
IMAGE_MAX_BYTES = 10 * 1024 * 1024


def _normalize_cover_crop(cover_ratio: Optional[str], cover_crop: Optional[dict]) -> Optional[dict]:
    """Build one cover_info crop entry; default to the full frame."""
    if not cover_ratio and not cover_crop:
        return None
    crop = dict(cover_crop or {})
    if cover_ratio:
        if cover_ratio not in IMAGE_POST_COVER_RATIOS:
            raise ValueError(
                f"Unsupported cover ratio: {cover_ratio}. "
                f"Allowed: {', '.join(IMAGE_POST_COVER_RATIOS)}"
            )
        crop["ratio"] = cover_ratio
    for key in ("x1", "y1", "x2", "y2"):
        crop.setdefault(key, "0" if key in {"x1", "y1"} else "1")
        crop[key] = str(crop[key])
    return crop


def build_image_post_article(
    title: str,
    image_media_ids: list[str],
    content: str = "",
    cover_ratio: Optional[str] = None,
    cover_crop: Optional[dict] = None,
    open_comment: bool = False,
    fans_only_comment: bool = False,
) -> dict:
    """Build the `articles[0]` payload for a newspic draft (no network)."""
    if not title or not str(title).strip():
        raise ValueError("Image post title is required")
    if len(title) > IMAGE_POST_TITLE_LIMIT:
        raise ValueError(f"Title max {IMAGE_POST_TITLE_LIMIT} chars for image post, got {len(title)}")
    if not image_media_ids:
        raise ValueError("At least 1 image is required for image post")
    if len(image_media_ids) > IMAGE_POST_MAX_IMAGES:
        raise ValueError(
            f"Max {IMAGE_POST_MAX_IMAGES} images allowed, got {len(image_media_ids)}"
        )
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise ValueError("Image post content must be plain text")
    if len(content) > IMAGE_POST_CONTENT_LIMIT:
        raise ValueError(
            f"Image post content max {IMAGE_POST_CONTENT_LIMIT} chars, got {len(content)}"
        )

    article = {
        "article_type": "newspic",
        "title": title,
        "content": content,
        "image_info": {
            "image_list": [
                {"image_media_id": mid} for mid in image_media_ids
            ]
        },
        "need_open_comment": 1 if open_comment else 0,
        "only_fans_can_comment": 1 if fans_only_comment else 0,
    }
    crop = _normalize_cover_crop(cover_ratio, cover_crop)
    if crop:
        article["cover_info"] = {"crop_percent_list": [crop]}
    return article


def build_image_post_body(
    title: str,
    image_media_ids: list[str],
    content: str = "",
    cover_ratio: Optional[str] = None,
    cover_crop: Optional[dict] = None,
    open_comment: bool = False,
    fans_only_comment: bool = False,
) -> dict:
    """Build the full draft/add request body; used by --dry-run too."""
    return {
        "articles": [
            build_image_post_article(
                title=title,
                image_media_ids=image_media_ids,
                content=content,
                cover_ratio=cover_ratio,
                cover_crop=cover_crop,
                open_comment=open_comment,
                fans_only_comment=fans_only_comment,
            )
        ]
    }


def create_draft(
    access_token: str,
    title: str,
    html: str,
    digest: str,
    thumb_media_id: Optional[str] = None,
    author: Optional[str] = None,
    base_url: Optional[str] = None,
) -> DraftResult:
    """
    Create a draft in WeChat.
    API: POST https://api.weixin.qq.com/cgi-bin/draft/add
    Returns DraftResult.
    Raise ValueError on error (errcode present and != 0).

    base_url overrides the endpoint (e.g. an nginx relay).
    """
    if not thumb_media_id:
        raise ValueError("A cover image is required to create a WeChat draft")

    article = {
        "title": title,
        "author": author or "",
        "digest": digest,
        "content": html,
        "show_cover_pic": 0,
    }

    article["thumb_media_id"] = thumb_media_id

    body = {"articles": [article]}

    # MUST use ensure_ascii=False — otherwise Chinese becomes \uXXXX
    # and WeChat stores the escape sequences literally, causing title
    # length overflow and garbled content.
    resp = requests.post(
        f"{resolve_api_base(base_url)}/cgi-bin/draft/add",
        params={"access_token": access_token},
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=30,
    )

    data = resp.json()

    errcode = data.get("errcode", 0)
    if errcode != 0:
        errmsg = data.get("errmsg", "unknown error")
        raise ValueError(f"WeChat create_draft error: errcode={errcode}, errmsg={errmsg}")

    if "media_id" not in data:
        raise ValueError(f"WeChat create_draft error: missing media_id in response: {data}")

    return DraftResult(media_id=data["media_id"])


def get_draft(access_token: str, media_id: str, base_url: Optional[str] = None) -> str:
    """
    Get draft content from WeChat by media_id.
    API: POST https://api.weixin.qq.com/cgi-bin/draft/get
    Returns the HTML content of the first article.
    """
    resp = requests.post(
        f"{resolve_api_base(base_url)}/cgi-bin/draft/get",
        params={"access_token": access_token},
        json={"media_id": media_id},
        timeout=30,
    )
    resp.encoding = "utf-8"
    data = resp.json()

    errcode = data.get("errcode", 0)
    if errcode != 0:
        errmsg = data.get("errmsg", "unknown error")
        raise ValueError(f"WeChat get_draft error: errcode={errcode}, errmsg={errmsg}")

    articles = data.get("news_item", [])
    if not articles:
        raise ValueError(f"WeChat get_draft: no articles in draft {media_id}")

    return articles[0].get("content", "")


def html_to_plaintext(html: str) -> str:
    """Extract plain text from WeChat HTML, stripping all tags and styles."""
    import re
    # Remove script/style blocks
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Replace block-level tags with newlines
    text = re.sub(r"<(br|p|div|section|h[1-6])[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Remove all remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities
    import html as html_module
    text = html_module.unescape(text)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def create_image_post(
    access_token: str,
    title: str,
    image_media_ids: list[str],
    content: str = "",
    open_comment: bool = False,
    fans_only_comment: bool = False,
    base_url: Optional[str] = None,
    cover_ratio: Optional[str] = None,
    cover_crop: Optional[dict] = None,
) -> ImagePostResult:
    """
    Create a WeChat image post (小绿书/图片帖) draft.

    This uses article_type="newspic" which displays as a horizontal
    swipe carousel (3:4 ratio), similar to Xiaohongshu.

    Args:
        access_token: WeChat access token.
        title: Post title, max 32 characters.
        image_media_ids: List of permanent media_ids from upload_thumb().
                        Min 1, max 20. First image becomes the cover.
        content: Plain text description, max ~1000 chars. No HTML.
        open_comment: Allow comments.
        fans_only_comment: Only followers can comment.
        cover_ratio: Optional cover crop ratio: 1_1 / 16_9 / 2.35_1.
        cover_crop: Optional explicit crop box (x1/y1/x2/y2 strings).

    Returns ImagePostResult with media_id of created draft.
    """
    body = build_image_post_body(
        title=title,
        image_media_ids=image_media_ids,
        content=content,
        cover_ratio=cover_ratio,
        cover_crop=cover_crop,
        open_comment=open_comment,
        fans_only_comment=fans_only_comment,
    )

    resp = requests.post(
        f"{resolve_api_base(base_url)}/cgi-bin/draft/add",
        params={"access_token": access_token},
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=30,
    )

    data = resp.json()

    errcode = data.get("errcode", 0)
    if errcode != 0:
        errmsg = data.get("errmsg", "unknown error")
        raise ValueError(f"WeChat create_image_post error: errcode={errcode}, errmsg={errmsg}")

    if "media_id" not in data:
        raise ValueError(f"WeChat create_image_post: missing media_id in response: {data}")

    return ImagePostResult(
        media_id=data["media_id"],
        image_count=len(image_media_ids),
        image_media_ids=list(image_media_ids),
    )


def _load_media_cache(cache_path: Optional[Path]) -> dict:
    if not cache_path or not cache_path.is_file():
        return {"version": 1, "images": {}}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "images": {}}
    if not isinstance(data, dict) or not isinstance(data.get("images"), dict):
        return {"version": 1, "images": {}}
    data.setdefault("version", 1)
    return data


def _write_media_cache(cache_path: Optional[Path], data: dict) -> None:
    if not cache_path:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(cache_path)


def _media_cache_key(image_path: Path, cache_path: Optional[Path]) -> str:
    resolved = image_path.resolve()
    if cache_path:
        try:
            return str(resolved.relative_to(cache_path.resolve().parent))
        except ValueError:
            pass
    return str(resolved)


def upload_image_assets(
    access_token: str,
    image_paths: list[str],
    cache_path: Optional[str] = None,
    base_url: Optional[str] = None,
) -> list[dict]:
    """Upload images as permanent materials, reusing a local media_id cache.

    Returns one dict per image: {path, media_id, cached}. The cache file is
    keyed by path + size + mtime so interrupted runs can resume without
    re-uploading images (permanent materials count against the account quota).
    """
    if not image_paths:
        raise ValueError("At least 1 image is required for image post")
    if len(image_paths) > IMAGE_POST_MAX_IMAGES:
        raise ValueError(f"Max {IMAGE_POST_MAX_IMAGES} images allowed, got {len(image_paths)}")

    cache_file = Path(cache_path).expanduser() if cache_path else None
    cache = _load_media_cache(cache_file)
    results = []

    for raw in image_paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ValueError(f"Image not found: {raw}")
        stat = path.stat()
        key = _media_cache_key(path, cache_file)
        cached = cache["images"].get(key)
        if (
            isinstance(cached, dict)
            and cached.get("media_id")
            and cached.get("size") == stat.st_size
            and cached.get("mtime") == int(stat.st_mtime)
        ):
            results.append({"path": str(path), "media_id": cached["media_id"], "cached": True})
            continue

        media_id = upload_thumb(access_token, str(path), base_url=base_url)
        cache["images"][key] = {
            "media_id": media_id,
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
            "source": str(path.resolve()),
        }
        _write_media_cache(cache_file, cache)
        results.append({"path": str(path), "media_id": media_id, "cached": False})

    return results


def check_image_post(
    title: str,
    image_paths: list[str],
    content: str = "",
    *,
    max_images: int = IMAGE_POST_MAX_IMAGES,
    ratio_range: tuple[float, float] = (0.70, 0.80),
) -> dict:
    """Deterministic preflight for an image post; never calls WeChat."""
    errors: list[str] = []
    warnings: list[str] = []
    images: list[dict] = []

    title = (title or "").strip()
    if not title:
        errors.append("标题不能为空")
    elif len(title) > IMAGE_POST_TITLE_LIMIT:
        errors.append(f"标题 {len(title)} 字，超过 {IMAGE_POST_TITLE_LIMIT} 字上限")

    if content is None:
        content = ""
    if not isinstance(content, str):
        errors.append("正文必须是纯文本")
        content = ""
    elif len(content) > IMAGE_POST_CONTENT_LIMIT:
        errors.append(f"正文 {len(content)} 字符，超过 {IMAGE_POST_CONTENT_LIMIT} 字符上限")
    elif len(content) > 1000:
        warnings.append(f"正文 {len(content)} 字符，贴图建议控制在 1000 字以内")
    if "<" in content and ">" in content:
        warnings.append("正文疑似包含 HTML 标签，贴图正文只支持纯文本")

    if not image_paths:
        errors.append("至少需要 1 张图片")
    elif len(image_paths) > max_images:
        errors.append(f"图片 {len(image_paths)} 张，超过上限 {max_images} 张")

    from PIL import Image  # local import keeps the CLI light for non-image commands

    for index, raw in enumerate(image_paths, start=1):
        path = Path(raw).expanduser()
        if not path.is_file():
            errors.append(f"第 {index} 张图片不存在: {raw}")
            continue
        size = path.stat().st_size
        if path.suffix.lower() not in IMAGE_ALLOWED_SUFFIXES:
            errors.append(f"第 {index} 张图片格式不支持: {path.suffix}")
        if size > IMAGE_MAX_BYTES:
            errors.append(f"第 {index} 张图片 {size / 1024 / 1024:.1f}MB，超过 10MB 上限")
        try:
            with Image.open(path) as image:
                width, height = image.size
                image_format = image.format
        except Exception as exc:  # noqa: BLE001 - report unreadable files as errors
            errors.append(f"第 {index} 张图片无法打开: {exc}")
            continue
        ratio = width / height if height else 0
        if ratio and not (ratio_range[0] <= ratio <= ratio_range[1]):
            warnings.append(
                f"第 {index} 张图片 {width}x{height}，比例 {ratio:.2f} 偏离 3:4(0.75)，可能被裁切"
            )
        images.append({
            "index": index,
            "path": str(path),
            "width": width,
            "height": height,
            "ratio": round(ratio, 4),
            "format": image_format,
            "bytes": size,
        })

    return {"ok": not errors, "errors": errors, "warnings": warnings, "images": images}
