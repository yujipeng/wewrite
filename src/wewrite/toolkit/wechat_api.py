import os
import time
import mimetypes
import requests
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

# Token cache
_token_cache: dict = {}

# Unified timeout for WeChat API calls
API_TIMEOUT = 30

# Default WeChat API endpoint (direct connection)
DEFAULT_API_BASE = "https://api.weixin.qq.com"

# Environment variable that overrides the API endpoint
API_BASE_ENV = "WECHAT_API_BASE"


def resolve_api_base(base_url: Optional[str] = None) -> str:
    """
    Resolve the WeChat API base URL.

    Priority: explicit argument > env var WECHAT_API_BASE > DEFAULT_API_BASE.
    Whitespace is stripped and a trailing "/" is removed, so callers can
    safely build f"{base}/cgi-bin/...".

    Empty/whitespace-only values fall through to the next source, which keeps
    the "leave it blank in config.yaml to connect directly" contract working.
    """
    for candidate in (base_url, os.environ.get(API_BASE_ENV)):
        if candidate is None:
            continue
        cleaned = str(candidate).strip().rstrip("/")
        if cleaned:
            return cleaned
    return DEFAULT_API_BASE


@dataclass
class TokenResult:
    access_token: str
    expires_at: float  # unix timestamp
    appid: str
    secret: str


def get_access_token(
    appid: str,
    secret: str,
    force_refresh: bool = False,
    base_url: Optional[str] = None,
) -> str:
    """
    Get access_token with caching and auto-refresh.
    Cache key: appid
    API: GET https://api.weixin.qq.com/cgi-bin/token
    Cache until expires_in - 300 seconds (5 min buffer).
    Raise ValueError on API error.

    base_url overrides the endpoint (e.g. an nginx relay); see resolve_api_base.
    """
    now = time.time()

    if not force_refresh and appid in _token_cache:
        cached: TokenResult = _token_cache[appid]
        if now < cached.expires_at:
            return cached.access_token

    resp = requests.get(
        f"{resolve_api_base(base_url)}/cgi-bin/token",
        params={
            "grant_type": "client_credential",
            "appid": appid,
            "secret": secret,
        },
        timeout=API_TIMEOUT,
    )
    data = resp.json()

    if "access_token" not in data:
        errcode = data.get("errcode", "unknown")
        errmsg = data.get("errmsg", "unknown error")
        raise ValueError(f"WeChat API error: errcode={errcode}, errmsg={errmsg}")

    access_token = data["access_token"]
    expires_in = data.get("expires_in", 7200)

    _token_cache[appid] = TokenResult(
        access_token=access_token,
        expires_at=now + expires_in - 300,
        appid=appid,
        secret=secret,
    )

    return access_token


def ensure_valid_token(
    appid: str,
    secret: str,
    base_url: Optional[str] = None,
) -> str:
    """Get a valid token, force-refreshing if near expiry.

    Use this instead of get_access_token for long-running operations
    where the token may have expired since initial acquisition.
    """
    now = time.time()
    if appid in _token_cache:
        cached = _token_cache[appid]
        if now >= cached.expires_at:
            return get_access_token(appid, secret, force_refresh=True, base_url=base_url)
        return cached.access_token
    return get_access_token(appid, secret, base_url=base_url)


def _guess_content_type(file_path: str) -> str:
    """Detect content type from file extension."""
    content_type, _ = mimetypes.guess_type(file_path)
    return content_type or "application/octet-stream"


def upload_image(access_token: str, image_path: str, base_url: Optional[str] = None) -> str:
    """
    Upload image for use inside article content.
    API: POST https://api.weixin.qq.com/cgi-bin/media/uploadimg
    Returns the url string.
    Raise ValueError on error.
    """
    path = Path(image_path)
    content_type = _guess_content_type(image_path)

    with open(path, "rb") as f:
        resp = requests.post(
            f"{resolve_api_base(base_url)}/cgi-bin/media/uploadimg",
            params={"access_token": access_token},
            files={"media": (path.name, f, content_type)},
            timeout=API_TIMEOUT,
        )

    data = resp.json()

    if "url" not in data:
        errcode = data.get("errcode", "unknown")
        errmsg = data.get("errmsg", "unknown error")
        raise ValueError(f"WeChat upload_image error: errcode={errcode}, errmsg={errmsg}")

    return data["url"]


def upload_thumb(access_token: str, image_path: str, base_url: Optional[str] = None) -> str:
    """
    Upload cover image as permanent material.
    API: POST https://api.weixin.qq.com/cgi-bin/material/add_material
    Returns media_id string.
    Raise ValueError on error.
    """
    path = Path(image_path)
    content_type = _guess_content_type(image_path)

    with open(path, "rb") as f:
        resp = requests.post(
            f"{resolve_api_base(base_url)}/cgi-bin/material/add_material",
            params={"access_token": access_token, "type": "image"},
            files={"media": (path.name, f, content_type)},
            timeout=API_TIMEOUT,
        )

    data = resp.json()

    if "media_id" not in data:
        errcode = data.get("errcode", "unknown")
        errmsg = data.get("errmsg", "unknown error")
        raise ValueError(f"WeChat upload_thumb error: errcode={errcode}, errmsg={errmsg}")

    return data["media_id"]
