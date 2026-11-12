"""微信 API base_url 可配置性的契约测试。

覆盖 resolve_api_base 的优先级（显式参数 > WECHAT_API_BASE > 默认值）、
空白/尾斜杠归一化，以及默认端点保持不变（不配置 = 直连）。
"""

import pytest

from wewrite.toolkit import publisher, wechat_api


def test_resolve_api_base_defaults_to_direct_endpoint(monkeypatch):
    monkeypatch.delenv("WECHAT_API_BASE", raising=False)
    assert wechat_api.resolve_api_base() == "https://api.weixin.qq.com"
    assert wechat_api.resolve_api_base(None) == "https://api.weixin.qq.com"
    assert wechat_api.DEFAULT_API_BASE == "https://api.weixin.qq.com"


def test_resolve_api_base_explicit_argument_wins(monkeypatch):
    monkeypatch.setenv("WECHAT_API_BASE", "https://env.example.com")
    assert wechat_api.resolve_api_base("https://arg.example.com") == "https://arg.example.com"


def test_resolve_api_base_env_overrides_default(monkeypatch):
    monkeypatch.setenv("WECHAT_API_BASE", "https://env.example.com/relay")
    assert wechat_api.resolve_api_base() == "https://env.example.com/relay"


def test_resolve_api_base_strips_whitespace_and_trailing_slash(monkeypatch):
    monkeypatch.delenv("WECHAT_API_BASE", raising=False)
    assert wechat_api.resolve_api_base("  https://relay.example.com/path/  ") == (
        "https://relay.example.com/path"
    )
    monkeypatch.setenv("WECHAT_API_BASE", " https://env.example.com/relay/// ")
    assert wechat_api.resolve_api_base() == "https://env.example.com/relay"


def test_resolve_api_base_blank_value_falls_through(monkeypatch):
    """config.example.yaml 里 base_url: "" 表示直连，不能被当成有效 base。"""
    monkeypatch.delenv("WECHAT_API_BASE", raising=False)
    assert wechat_api.resolve_api_base("   ") == "https://api.weixin.qq.com"
    monkeypatch.setenv("WECHAT_API_BASE", "")
    assert wechat_api.resolve_api_base("") == "https://api.weixin.qq.com"


def test_token_and_publish_requests_use_configured_base(monkeypatch, tmp_path):
    """配置 base_url 后，token / 上传 / 草稿请求都发往该 base。"""
    monkeypatch.setenv("WECHAT_API_BASE", "https://relay.example.com/kX9mP2qR8vT4wN6z")
    called_urls = []

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        called_urls.append(url)
        return Response({"access_token": "tok", "expires_in": 7200})

    def fake_post(url, **kwargs):
        called_urls.append(url)
        if url.endswith("/cgi-bin/draft/add"):
            return Response({"media_id": "m1"})
        if url.endswith("/cgi-bin/media/uploadimg"):
            return Response({"url": "https://img.example.com/a.png"})
        return Response({"media_id": "thumb1"})

    monkeypatch.setattr(wechat_api.requests, "get", fake_get)
    monkeypatch.setattr(wechat_api.requests, "post", fake_post)
    monkeypatch.setattr(publisher.requests, "post", fake_post)

    image = tmp_path / "cover.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    token = wechat_api.get_access_token("appid", "secret", force_refresh=True)
    wechat_api.upload_image(token, str(image))
    thumb = wechat_api.upload_thumb(token, str(image))
    publisher.create_draft(token, "title", "<p>x</p>", "digest", thumb)

    assert called_urls == [
        "https://relay.example.com/kX9mP2qR8vT4wN6z/cgi-bin/token",
        "https://relay.example.com/kX9mP2qR8vT4wN6z/cgi-bin/media/uploadimg",
        "https://relay.example.com/kX9mP2qR8vT4wN6z/cgi-bin/material/add_material",
        "https://relay.example.com/kX9mP2qR8vT4wN6z/cgi-bin/draft/add",
    ]


def test_explicit_base_url_beats_env_in_mutating_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("WECHAT_API_BASE", "https://env.example.com")
    urls = []

    class Response:
        def json(self):
            return {"media_id": "m1"}

    def fake_post(url, **kwargs):
        urls.append(url)
        return Response()

    monkeypatch.setattr(publisher.requests, "post", fake_post)
    publisher.create_draft("tok", "title", "<p>x</p>", "digest", "thumb", base_url="https://arg.example.com/")
    assert urls == ["https://arg.example.com/cgi-bin/draft/add"]


def test_direct_endpoint_when_unconfigured(monkeypatch):
    monkeypatch.delenv("WECHAT_API_BASE", raising=False)
    urls = []

    class Response:
        def json(self):
            return {"media_id": "m1", "news_item": [{"content": "<p>x</p>"}]}

    def fake_post(url, **kwargs):
        urls.append(url)
        return Response()

    monkeypatch.setattr(publisher.requests, "post", fake_post)
    publisher.create_draft("tok", "title", "<p>x</p>", "digest", "thumb")
    publisher.get_draft("tok", "draft1")
    publisher.create_image_post("tok", "title", ["m1"])
    base = "https://api.weixin.qq.com"
    assert urls == [
        f"{base}/cgi-bin/draft/add",
        f"{base}/cgi-bin/draft/get",
        f"{base}/cgi-bin/draft/add",
    ]
