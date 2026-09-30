from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..config import config
from ..database import database


def retention_seconds() -> float:
    return max(0, float(config.get("cleanup", {}).get("retention_minutes", 30))) * 60


@dataclass
class DownloadTask:
    task_id: str
    user_id: int
    ids: list[str]
    status: str = "pending"
    message: str = "等待执行"
    completed: int = 0
    total: int | None = None
    archive: str | None = None
    error: str | None = None
    events: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue, repr=False)
    state_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "status": self.status, "message": self.message,
                "completed": self.completed, "total": self.total, "archive": bool(self.archive),
                "error": self.error}

    def publish(self, **payload: Any) -> None:
        with self.state_lock:
            database.save_task(self, retention_seconds())
            data = self.snapshot()
            data.update({key: value for key, value in payload.items() if value is not None})
            self.events.put(data)


class TaskManager:
    def __init__(self, worker: Any) -> None:
        self._worker = worker
        self._tasks: dict[str, DownloadTask] = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._pending: list[DownloadTask] = []
        self._running_total = 0
        self._running_by_user: dict[int, int] = {}
        self._recovered = False
        limits = config.get("limits", {})
        self.max_running = max(1, int(limits.get("global_running_tasks", 2)))
        self.max_queued = max(0, int(limits.get("global_queued_tasks", 50)))
        self.per_user_running = max(1, int(limits.get("per_user_running_tasks", 1)))
        self.per_user_queued = max(0, int(limits.get("per_user_queued_tasks", 3)))
        self._executor = ThreadPoolExecutor(max_workers=self.max_running, thread_name_prefix="jm-download")
        threading.Thread(target=self._dispatch, name="task-dispatcher", daemon=True).start()

    def recover(self) -> None:
        if self._recovered:
            return
        self._recovered = True
        for row in database.recoverable_tasks():
            self._remember_and_submit(self._from_row(row))

    def create(self, user_id: int, ids: list[str]) -> DownloadTask:
        active = ("pending", "downloading", "packing")
        with self._lock:
            if database.count_tasks(None, active) >= self.max_running + self.max_queued:
                raise OverflowError("服务器任务队列已满")
            if database.count_tasks(user_id, active) >= self.per_user_running + self.per_user_queued:
                raise OverflowError("你的任务队列已满")
            task = DownloadTask(task_id=uuid.uuid4().hex, user_id=user_id, ids=ids)
            database.create_task(task, retention_seconds())
            self._remember_and_submit(task)
        task.events.put(task.snapshot())
        return task

    def get(self, task_id: str, user_id: int) -> DownloadTask | None:
        row = database.task_row(task_id, user_id)
        if row is None:
            return None
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                task = self._from_row(row)
                self._tasks[task_id] = task
            return task

    def _remember_and_submit(self, task: DownloadTask) -> None:
        with self._condition:
            self._tasks[task.task_id] = task
            self._pending.append(task)
            self._condition.notify_all()

    def _dispatch(self) -> None:
        while True:
            with self._condition:
                eligible_index = next(
                    (index for index, task in enumerate(self._pending)
                     if self._running_by_user.get(task.user_id, 0) < self.per_user_running),
                    None,
                )
                if self._running_total >= self.max_running or eligible_index is None:
                    self._condition.wait()
                    continue
                task = self._pending.pop(eligible_index)
                self._running_total += 1
                self._running_by_user[task.user_id] = self._running_by_user.get(task.user_id, 0) + 1
            self._executor.submit(self._run_and_release, task)

    def _run_and_release(self, task: DownloadTask) -> None:
        try:
            self._run(task)
        finally:
            with self._condition:
                self._running_total -= 1
                self._running_by_user[task.user_id] -= 1
                self._condition.notify_all()

    def _run(self, task: DownloadTask) -> None:
        try:
            self._worker(task)
        except Exception as exc:  # noqa: BLE001
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
            task.message = "任务失败"
            task.publish()

    @staticmethod
    def _from_row(row: Any) -> DownloadTask:
        return DownloadTask(task_id=row["id"], user_id=row["user_id"], ids=json.loads(row["album_ids"]),
                            status=row["status"], message=row["message"], completed=row["completed"],
                            total=row["total"], archive=row["archive_path"], error=row["error"])

    async def next_event(self, task: DownloadTask, timeout: float = 15) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(task.events.get, True, timeout)
        except queue.Empty:
            row = database.task_row(task.task_id, task.user_id)
            return self._from_row(row).snapshot() if row else task.snapshot()
