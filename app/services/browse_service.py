from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

import jmcomic
from jmcomic import JmMagicConstants
from jmcomic.jm_toolkit import JmcomicText

from ..config import config, resolve_path
from .jm_service import serialize_page


class BrowseNotFound(ValueError):
    pass


class BrowseBusy(RuntimeError):
    pass


class BrowseService:
    """Use jmcomic for metadata and decoded pages, with bounded shared caches."""

    def __init__(self) -> None:
        settings = config.get("reader", {})
        self.cache_dir = resolve_path(settings.get("cache_dir", "data/reader"))
        self.retention = max(1, float(settings.get("retention_minutes", 30))) * 60
        self.max_bytes = max(1, int(settings.get("max_cache_mb", 512))) * 1024 * 1024
        self.metadata_ttl = max(1, int(settings.get("metadata_ttl_seconds", 300)))
        self.preload_pages = min(3, max(0, int(settings.get("preload_pages", 2))))
        self._metadata: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self._stripes = [threading.RLock() for _ in range(64)]
        self._active: set[Path] = set()
        self._slots = threading.BoundedSemaphore(4)
        self._local = threading.local()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def client(self) -> Any:
        if not hasattr(self._local, "client"):
            option = jmcomic.JmModuleConfig.option_class().default()
            option.client.retry_times = 2
            option.client.postman.meta_data["timeout"] = 20
            self._local.client = option.build_jm_client()
        return self._local.client

    def cached(self, key: tuple, loader: Callable[[], Any]) -> Any:
        with self._stripes[hash(key) % len(self._stripes)]:
            with self._lock:
                entry = self._metadata.get(key)
                if entry and entry[0] > time.monotonic():
                    self._metadata.move_to_end(key)
                    return entry[1]
                self._metadata.pop(key, None)
            value = loader()
            with self._lock:
                self._metadata[key] = (time.monotonic() + self.metadata_ttl, value)
                while len(self._metadata) > 256:
                    self._metadata.popitem(last=False)
            return value

    def recommendations(self, kind: str, page: int, category: str) -> dict:
        def load() -> dict:
            client = self.client()
            if kind == "latest":
                result = client.categories_filter(
                    page, JmMagicConstants.TIME_ALL, category, JmMagicConstants.ORDER_BY_LATEST,
                )
            else:
                method = {"day": "day_ranking", "week": "week_ranking", "month": "month_ranking"}[kind]
                result = getattr(client, method)(page, category=category)
            return serialize_page(result, page)

        return self.cached(("recommendations", kind, page, category), load)

    def album(self, album_id: str) -> Any:
        return self.cached(("album", album_id), lambda: self.client().get_album_detail(album_id))

    def album_detail(self, album_id: str) -> dict:
        album = self.album(album_id)
        description = album.description
        return {
            "id": str(album.id), "title": album.name,
            "description": "" if description in (None, "None", "null") else str(description),
            "authors": [str(value) for value in album.authors],
            "tags": [str(value) for value in album.tags],
            "pages": album.page_count, "views": str(album.views), "likes": str(album.likes),
            "pub_date": str(album.pub_date or ""), "update_date": str(album.update_date or ""),
            "image": JmcomicText.get_album_cover_url(album.id, size="_3x4"),
            "chapters": [{"id": str(entry[0]), "title": str(entry[2]), "index": index + 1}
                         for index, entry in enumerate(album.episode_list)],
            "preload_pages": self.preload_pages,
        }

    def chapter(self, album_id: str, photo_id: str) -> Any:
        album = self.album(album_id)
        if photo_id not in {str(entry[0]) for entry in album.episode_list}:
            raise BrowseNotFound("该章节不属于当前漫画")
        return self.cached(("photo", photo_id), lambda: self.client().get_photo_detail(photo_id, fetch_album=False))

    def chapter_detail(self, album_id: str, photo_id: str) -> dict:
        photo = self.chapter(album_id, photo_id)
        return {"id": str(photo.id), "title": photo.name, "pages": len(photo)}

    def page_image(self, album_id: str, photo_id: str, page: int) -> bytes:
        # Validate chapter ownership even on cache hits; never accept an arbitrary URL/path.
        photo = self.chapter(album_id, photo_id)
        if page < 1 or page > len(photo):
            raise BrowseNotFound("漫画页码不存在")
        path = self.cache_dir / f"{photo_id}-{page}.jpg"
        with self._stripes[hash(str(path)) % len(self._stripes)]:
            with self._lock:
                if path.is_file() and time.time() - path.stat().st_mtime < self.retention:
                    data = path.read_bytes()
                    path.touch()
                    return data
                self._active.add(path)
            temporary = path.with_name(f"{path.stem}-{uuid.uuid4().hex}.tmp.jpg")
            acquired = False
            try:
                acquired = self._slots.acquire(timeout=1)
                if not acquired:
                    raise BrowseBusy("阅读请求较多，请稍后重试")
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self.client().download_by_image_detail(photo[page - 1], str(temporary), decode_image=True)
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise RuntimeError("上游未返回有效图片")
                if temporary.stat().st_size > self.max_bytes:
                    raise RuntimeError("单页图片超过阅读缓存容量")
                with self._lock:
                    temporary.replace(path)
                    data = path.read_bytes()
                return data
            finally:
                temporary.unlink(missing_ok=True)
                if acquired:
                    self._slots.release()
                with self._lock:
                    self._active.discard(path)
                self.clean_cache()

    def clean_cache(self) -> None:
        with self._lock:
            now = time.time()
            files = []
            for path in self.cache_dir.glob("*.jpg"):
                if path in self._active:
                    continue
                try:
                    stat = path.stat()
                    if ".tmp." in path.name:
                        # A crash can leave an incomplete temporary file behind.
                        if now - stat.st_mtime >= self.retention:
                            path.unlink(missing_ok=True)
                        continue
                    if now - stat.st_mtime >= self.retention:
                        path.unlink(missing_ok=True)
                    else:
                        files.append((stat.st_mtime, stat.st_size, path))
                except OSError:
                    continue
            total = sum(size for _, size, _ in files)
            for _, size, path in sorted(files):
                if total <= self.max_bytes:
                    break
                try:
                    path.unlink(missing_ok=True)
                    total -= size
                except OSError:
                    continue

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def maintain() -> None:
            while not self._stop.is_set():
                try:
                    self.clean_cache()
                except OSError:
                    logging.exception("阅读缓存清理失败")
                self._stop.wait(60)

        self._thread = threading.Thread(target=maintain, name="reader-cleaner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


browse = BrowseService()
