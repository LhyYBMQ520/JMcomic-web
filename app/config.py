from __future__ import annotations

import os
import secrets
import threading
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.toml"
DEFAULT_CONFIG = """# JMComic Web Downloader 运行配置
# 此文件在首次启动时自动生成。管理员令牌支持热重载；其他配置修改后需要重启程序。
# TOML 字符串必须保留引号，布尔值使用 true 或 false。

[server]
# Web 服务监听地址。127.0.0.1 仅允许本机访问；如需局域网访问可改为 0.0.0.0。
host = "127.0.0.1"
# Web 服务监听端口，范围为 1-65535。
port = 8000

[download]
# 单次网页提交允许包含的最大车号数量。
max_ids_per_task = 20
# 图片缓存目录。相对路径以项目根目录为基准，也可以填写绝对路径。
base_dir = "data/downloads"
# 下载完成后生成的 ZIP 保存目录。路径规则同 base_dir。
archive_dir = "data/archives"

[database]
# SQLite 数据库文件路径。相对路径以项目根目录为基准。
path = "data/app.db"

[auth]
# 管理员令牌。普通用户请在 /admin 页面中创建和管理。
# 公网部署前必须替换，并使用足够长的随机字符串。
admin_token = "B67XB6_JorFnIKSMHQ8UyU3sX8oi15qg"
# 普通用户令牌的 AES-GCM 加密密钥。首次生成后请勿修改，否则已有令牌无法解密。
token_encryption_key = "__GENERATED_TOKEN_ENCRYPTION_KEY__"
# 使用 HTTPS 公网部署时必须改为 true；本地 HTTP 调试保持 false。
secure_cookie = false

[config_reload]
# 是否热重载管理员令牌。修改后旧管理员令牌立即失效，新令牌立即生效。
enabled = true
# 检查 config.toml 修改的间隔，单位为秒，最小值为 1 秒。
interval_seconds = 2
# 端口、目录、任务配额和清理策略等其他配置仍需重启程序生效。

[limits]
# 全站同时运行的下载任务数，以及等待队列容量。
# 每个任务内部还会由 jmcomic 并发下载图片，运行数不宜过高。
global_running_tasks = 2
global_queued_tasks = 50
# 单个用户同时运行的任务数，以及等待队列容量。
per_user_running_tasks = 1
per_user_queued_tasks = 3

[cleanup]
# 是否启用下载缓存和 ZIP 的定时清理。
enabled = true
# 清理器检查过期文件的时间间隔，单位为分钟，最小值为 1 分钟。
interval_minutes = 2
# 文件或目录最后修改时间超过该分钟数后会被删除。
# 设为 0 时，启动和每次检查都会清理现有内容，请谨慎使用。
retention_minutes = 30
"""


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        generated = DEFAULT_CONFIG.replace("__GENERATED_TOKEN_ENCRYPTION_KEY__", secrets.token_urlsafe(32))
        CONFIG_PATH.write_text(generated, encoding="utf-8")
    with CONFIG_PATH.open("rb") as handle:
        return tomllib.load(handle)


config = load_config()


class ConfigReloader:
    def __init__(self) -> None:
        section = config.get("config_reload", {})
        self.enabled = bool(section.get("enabled", True))
        self.interval = max(1, float(section.get("interval_seconds", 2)))
        self._mtime_ns = CONFIG_PATH.stat().st_mtime_ns
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="config-reloader", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                mtime_ns = CONFIG_PATH.stat().st_mtime_ns
                if mtime_ns == self._mtime_ns:
                    continue
                with CONFIG_PATH.open("rb") as handle:
                    latest = tomllib.load(handle)
                self._mtime_ns = mtime_ns
                latest_auth = latest.get("auth", {})
                latest_auth["token_encryption_key"] = config.get("auth", {}).get("token_encryption_key", "")
                config["auth"] = latest_auth

                from .database import database
                database.sync_admin_token()
            except (OSError, tomllib.TOMLDecodeError) as exc:
                print(f"config.toml 热重载失败，将继续使用原配置: {exc}")

    def stop(self) -> None:
        self._stop.set()


config_reloader = ConfigReloader()


def resolve_path(value: str) -> Path:
    path = Path(os.path.expandvars(value))
    return path if path.is_absolute() else ROOT / path


class CacheCleaner:
    def __init__(self) -> None:
        self.enabled = bool(config.get("cleanup", {}).get("enabled", True))
        self.interval = max(60, float(config.get("cleanup", {}).get("interval_minutes", 2)) * 60)
        self.retention = max(0, float(config.get("cleanup", {}).get("retention_minutes", 30))) * 60
        self.paths = [
            resolve_path(config.get("download", {}).get("base_dir", "data/downloads")),
            resolve_path(config.get("download", {}).get("archive_dir", "data/archives")),
        ]
        self._stop = threading.Event()

    def clean_once(self) -> None:
        from .database import database
        import shutil

        for row in database.expired_tasks(time.time()):
            try:
                archive = row["archive_path"]
                if archive:
                    Path(archive).unlink(missing_ok=True)
                for root in self.paths:
                    task_dir = root / str(row["user_id"]) / row["id"]
                    if task_dir.is_dir():
                        shutil.rmtree(task_dir)
            except OSError:
                # A ZIP may still be held open by an active response on Windows.
                continue
            database.delete_task(row["id"])

    def start(self) -> None:
        if not self.enabled:
            return
        self.clean_once()
        threading.Thread(target=self._run, name="cache-cleaner", daemon=True).start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.clean_once()

    def stop(self) -> None:
        self._stop.set()


cache_cleaner = CacheCleaner()
