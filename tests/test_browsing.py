from __future__ import annotations

import ast
import base64
import importlib
import io
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture(scope="module")
def application(tmp_path_factory):
    """Import the app against disposable config/storage, never the real user database."""
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    source = ast.parse((root / "app/config.py").read_text(encoding="utf-8"))
    template = next(ast.literal_eval(node.value) for node in source.body
                    if isinstance(node, ast.Assign) and any(getattr(target, "id", "") == "DEFAULT_CONFIG" for target in node.targets))
    settings = tomllib.loads(template)
    storage = tmp_path_factory.mktemp("browsing")
    settings["auth"]["admin_token"] = "test-admin-token-for-isolated-storage"
    settings["auth"]["token_encryption_key"] = base64.urlsafe_b64encode(b"a" * 32).decode()
    fake_config = ModuleType("app.config")
    fake_config.config = settings
    fake_config.resolve_path = lambda value: storage / value
    fake_config.cache_cleaner = SimpleNamespace(start=lambda: None, stop=lambda: None)
    fake_config.config_reloader = SimpleNamespace(start=lambda: None, stop=lambda: None)
    sys.modules["app.config"] = fake_config
    main = importlib.import_module("app.main")
    yield main
    main.browse.stop()
    main.manager._executor.shutdown(wait=True)
    sys.path.remove(str(root))
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


@pytest.fixture
def service(application, tmp_path, monkeypatch):
    from app.services.browse_service import BrowseService

    browse = BrowseService()
    browse.cache_dir = tmp_path / "reader"
    album = SimpleNamespace(id="123", name="测试 <漫画>", description="一段简介\n第二行", authors=["作者"],
                            tags=["标签"], page_count=3, views="100", likes="10", pub_date="2026-01-01",
                            update_date="2026-02-01", episode_list=[("456", "1", "第一章"), ("789", "2", "第二章")])

    class Photo:
        def __init__(self, photo_id):
            self.id = photo_id
            self.name = "第一章" if photo_id == "456" else "第二章"

        def __len__(self):
            return 3

        def __getitem__(self, index):
            return SimpleNamespace(index=index + 1)

    client = Mock()
    client.get_album_detail.return_value = album
    client.get_photo_detail.side_effect = lambda photo_id, **kwargs: Photo(photo_id)
    listing = SimpleNamespace(content=[("123", {"name": album.name, "tags": ["标签"], "author": "作者"})],
                              total=1, page_number=1, page_count=1)
    for method in ("day_ranking", "week_ranking", "month_ranking", "categories_filter"):
        getattr(client, method).return_value = listing

    def download(image, path, decode_image):
        assert decode_image is True
        Image.new("RGB", (16, 24), "navy").save(path, "JPEG")

    client.download_by_image_detail.side_effect = download
    monkeypatch.setattr(browse, "client", lambda: client)
    monkeypatch.setattr(application, "browse", browse)
    return browse, client, album


@pytest.fixture
def http(application, service):
    with TestClient(application.app) as client:
        client.headers["X-Access-Token"] = application.config["auth"]["admin_token"]
        yield client


def test_new_routes_require_authentication(application, service):
    with TestClient(application.app) as client:
        for path in ("/albums/123", "/api/recommendations", "/api/albums/123",
                     "/api/albums/123/chapters/456", "/api/albums/123/chapters/456/pages/1"):
            assert client.get(path).status_code == 401


@pytest.mark.parametrize("kind, method", [("day", "day_ranking"), ("week", "week_ranking"),
                                        ("month", "month_ranking"), ("latest", "categories_filter")])
def test_recommendations_use_jmcomic_and_cache(http, service, kind, method):
    _, upstream, _ = service
    first = http.get(f"/api/recommendations?kind={kind}")
    second = http.get(f"/api/recommendations?kind={kind}")
    assert first.status_code == 200
    assert first.json() == second.json()
    assert first.json()["items"][0]["image"].startswith("https://")
    assert getattr(upstream, method).call_count == 1


def test_detail_empty_description_and_chapter_membership(http, service):
    browse, upstream, album = service
    album.description = "None"
    response = http.get("/api/albums/123")
    assert response.status_code == 200
    assert response.json()["description"] == ""
    assert response.json()["chapters"][1]["id"] == "789"
    assert response.json()["preload_pages"] == 2
    assert http.get("/api/albums/123/chapters/999").status_code == 404
    assert http.get("/api/albums/123/chapters/999/pages/1").status_code == 404
    upstream.get_photo_detail.assert_not_called()
    assert http.get("/api/albums/123/chapters/456").json()["pages"] == 3


def test_image_is_decoded_cached_and_bounds_checked(http, service):
    _, upstream, _ = service
    first = http.get("/api/albums/123/chapters/456/pages/1")
    second = http.get("/api/albums/123/chapters/456/pages/1")
    assert first.status_code == 200
    assert first.content == second.content
    assert first.headers["content-type"] == "image/jpeg"
    assert first.headers["cache-control"] == "private, no-store"
    assert Image.open(io.BytesIO(first.content)).size == (16, 24)
    assert upstream.download_by_image_detail.call_count == 1
    assert http.get("/api/albums/123/chapters/456/pages/4").status_code == 404
    assert http.get("/api/albums/123/chapters/456/pages/0").status_code == 422


def test_parallel_requests_download_same_page_once(service):
    browse, upstream, _ = service
    with ThreadPoolExecutor(max_workers=4) as pool:
        images = list(pool.map(lambda _: browse.page_image("123", "456", 1), range(4)))
    assert all(image == images[0] for image in images)
    assert upstream.download_by_image_detail.call_count == 1


def test_cache_expires_and_enforces_capacity(service):
    browse, _, _ = service
    browse.cache_dir.mkdir()
    old = browse.cache_dir / "old.jpg"
    recent = browse.cache_dir / "recent.jpg"
    old.write_bytes(b"x" * 8)
    recent.write_bytes(b"x" * 8)
    import os
    os.utime(old, (time.time() - browse.retention - 1,) * 2)
    browse.clean_cache()
    assert not old.exists() and recent.exists()
    newer = browse.cache_dir / "newer.jpg"
    newer.write_bytes(b"y" * 8)
    os.utime(recent, (time.time() - 10,) * 2)
    browse.max_bytes = 8
    browse.clean_cache()
    assert not recent.exists() and newer.exists()


def test_reader_concurrency_limit_can_retry(http, service):
    browse, upstream, _ = service
    for _ in range(4):
        browse._slots.acquire()
    try:
        assert http.get("/api/albums/123/chapters/456/pages/1").status_code == 429
        upstream.download_by_image_detail.assert_not_called()
    finally:
        for _ in range(4):
            browse._slots.release()
    assert http.get("/api/albums/123/chapters/456/pages/1").status_code == 200


def test_failed_image_is_retryable_and_leaves_no_partial_file(service):
    browse, upstream, _ = service
    download = upstream.download_by_image_detail.side_effect

    def fail(image, path, decode_image):
        Path(path).write_bytes(b"partial")
        raise RuntimeError("temporary upstream failure")

    upstream.download_by_image_detail.side_effect = fail
    with pytest.raises(RuntimeError):
        browse.page_image("123", "456", 1)
    assert list(browse.cache_dir.glob("*")) == []
    upstream.download_by_image_detail.side_effect = download
    assert browse.page_image("123", "456", 1)


def test_cache_refreshes_metadata_after_ttl(service):
    browse, upstream, _ = service
    browse.album_detail("123")
    with browse._lock:
        browse._metadata[("album", "123")] = (time.monotonic() - 1, upstream.get_album_detail.return_value)
    browse.album_detail("123")
    assert upstream.get_album_detail.call_count == 2


def test_invalid_parameters_do_not_reach_upstream(http, service):
    _, upstream, _ = service
    for path in ("/api/recommendations?kind=invalid", "/api/recommendations?page=0",
                 "/api/recommendations?category=invalid", "/api/albums/not-a-number"):
        assert http.get(path).status_code == 422
    upstream.get_album_detail.assert_not_called()


def test_upstream_failure_returns_retryable_error_without_leaking_details(http, service):
    _, upstream, _ = service
    upstream.get_album_detail.side_effect = RuntimeError("private upstream diagnostics")
    response = http.get("/api/albums/123")
    assert response.status_code == 502
    assert "private" not in response.text


def test_cookie_login_can_open_reader(http, application):
    token = http.headers.pop("X-Access-Token")
    assert http.post("/api/session", headers={"X-Access-Token": token}).status_code == 204
    assert http.get("/albums/123").status_code == 200
    assert http.get("/api/albums/123").status_code == 200
    assert http.delete("/api/session").status_code == 204
    assert http.get("/api/albums/123").status_code == 401
