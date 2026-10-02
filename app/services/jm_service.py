from __future__ import annotations

import re
import threading
import zipfile
from typing import Any

import jmcomic
from jmcomic import Feature, JmMagicConstants
from jmcomic.jm_toolkit import JmcomicText

from ..config import config, resolve_path

DOWNLOAD_ROOT = resolve_path(config.get("download", {}).get("base_dir", "data/downloads"))
ARCHIVE_ROOT = resolve_path(config.get("download", {}).get("archive_dir", "data/archives"))
ID_RE = re.compile(r"^\d+$")


def normalize_ids(raw_ids: list[str]) -> list[str]:
    ids: list[str] = []
    for value in raw_ids:
        for item in value.split():
            if not ID_RE.fullmatch(item):
                raise ValueError(f"无效车号: {item}")
            if item not in ids:
                ids.append(item)
    if not ids:
        raise ValueError("至少需要一个车号")
    max_ids = max(1, int(config.get("download", {}).get("max_ids_per_task", 20)))
    if len(ids) > max_ids:
        raise ValueError(f"单次最多提交 {max_ids} 个车号")
    return ids


def preview(ids: list[str]) -> list[dict[str, Any]]:
    client = jmcomic.JmModuleConfig.option_class().default().build_jm_client()
    result: list[dict[str, Any]] = []
    for album_id in ids:
        album = client.get_album_detail(album_id)
        result.append({
            "id": str(album.id),
            "title": album.name,
            "authors": [str(author) for author in album.authors],
            "pages": album.page_count,
            "chapters": len(album),
        })
    return result


def search(
    query: str,
    page: int = 1,
    search_type: str = "site",
    order_by: str = JmMagicConstants.ORDER_BY_LATEST,
    time: str = JmMagicConstants.TIME_ALL,
    category: str = JmMagicConstants.CATEGORY_ALL,
) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise ValueError("请输入搜索关键词")
    if len(query) > 100:
        raise ValueError("搜索关键词不能超过 100 个字符")
    if page < 1 or page > 9999:
        raise ValueError("页码无效")

    search_methods = {
        "site": "search_site",
        "work": "search_work",
        "author": "search_author",
        "tag": "search_tag",
        "actor": "search_actor",
    }
    method_name = search_methods.get(search_type)
    if method_name is None:
        raise ValueError("搜索类型无效")

    client = jmcomic.JmModuleConfig.option_class().default().build_jm_client()
    search_page = getattr(client, method_name)(
        query,
        page=page,
        order_by=order_by,
        time=time,
        category=category,
    )
    return serialize_page(search_page, page)


def serialize_page(search_page: Any, fallback_page: int = 1) -> dict[str, Any]:
    """Share the card format between search results and category rankings."""
    items = []
    for album_id, info in search_page.content:
        image_url = str(info.get("image") or "")
        if not image_url:
            image_url = JmcomicText.get_album_cover_url(album_id, size="_3x4")
        items.append({
            "id": str(album_id),
            "title": str(info.get("name") or album_id),
            "author": str(info.get("author") or ""),
            "tags": [str(tag) for tag in (info.get("tags") or [])],
            "image": image_url,
            "category": str((info.get("category") or {}).get("title") or ""),
            "date": str(info.get("adddate") or ""),
        })
    return {
        "items": items,
        "total": int(search_page.total),
        "page": int(search_page.page_number or fallback_page),
        "page_count": int(search_page.page_count),
    }


class ProgressDownloader(jmcomic.JmDownloader):  # type: ignore[misc]
    def __init__(self, option: Any, task: Any) -> None:
        super().__init__(option)
        self.task = task
        self.pages = 0
        self._progress_lock = threading.Lock()

    def before_album(self, album: Any) -> None:
        super().before_album(album)
        self.task.status = "downloading"
        self.task.message = f"正在下载《{album.name}》"
        self.task.publish(title=album.name)

    def before_photo(self, photo: Any) -> None:
        super().before_photo(photo)
        with self._progress_lock:
            self.pages += len(photo)
            self.task.total = self.pages
        self.task.publish(completed=self.task.completed, total=self.task.total)

    def after_image(self, image: Any, img_save_path: str) -> None:
        super().after_image(image, img_save_path)
        with self._progress_lock:
            self.task.completed += 1
        self.task.publish(completed=self.task.completed, total=self.task.total)

    def after_album(self, album: Any) -> None:
        super().after_album(album)
        self.task.publish(completed=self.task.completed, total=self.task.total)


def download_task(task: Any) -> None:
    task_download_root = DOWNLOAD_ROOT / str(task.user_id) / task.task_id
    task_archive_root = ARCHIVE_ROOT / str(task.user_id) / task.task_id
    task_download_root.mkdir(parents=True, exist_ok=True)
    task_archive_root.mkdir(parents=True, exist_ok=True)
    task.status = "downloading"
    task.message = "准备下载"
    task.publish()

    # Feature.export_zip performs the local archive step after all images finish.
    for album_id in task.ids:
        option = jmcomic.JmModuleConfig.option_class().default()
        option.dir_rule.base_dir = str(task_download_root)
        extra = Feature.export_zip(zip_dir=str(task_archive_root), filename_rule="Aid")
        jmcomic.download_album(
            album_id,
            option=option,
            downloader=lambda current_option: ProgressDownloader(current_option, task),
            extra=extra,
        )

    archives = sorted(task_archive_root.glob("*.zip"))
    if not archives:
        raise RuntimeError("下载完成但没有找到 ZIP 文件")
    archive_name = "_".join(task.ids) + ".zip"
    final_archive = task_archive_root / archive_name
    if len(archives) == 1:
        if archives[0] != final_archive:
            archives[0].replace(final_archive)
    else:
        with zipfile.ZipFile(final_archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for archive in archives:
                bundle.write(archive, archive.name)
        for archive in archives:
            archive.unlink(missing_ok=True)
    task.archive = str(final_archive.resolve())
    task.status = "completed"
    task.message = "下载并打包完成"
    task.publish(archive=task.archive, completed=task.completed, total=task.total)
