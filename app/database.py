from __future__ import annotations

import hashlib
import base64
import json
import sqlite3
import threading
import time
from typing import Any

from .config import config, resolve_path


class Database:
    def __init__(self) -> None:
        self.path = resolve_path(config.get("database", {}).get("path", "data/app.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    token_cipher BLOB,
                    token_nonce BLOB,
                    token_tag BLOB,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    album_ids TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    total INTEGER,
                    archive_path TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status);
                CREATE INDEX IF NOT EXISTS idx_tasks_expiry ON tasks(expires_at, status);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
            if "is_admin" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
            if "note" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            if "token_cipher" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN token_cipher BLOB")
            if "token_nonce" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN token_nonce BLOB")
            if "token_tag" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN token_tag BLOB")
        self.sync_admin_token()

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _encryption_key() -> bytes:
        encoded = str(config.get("auth", {}).get("token_encryption_key", ""))
        try:
            key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except ValueError as exc:
            raise RuntimeError("token_encryption_key 格式无效") from exc
        if len(key) != 32:
            raise RuntimeError("token_encryption_key 必须是 32 字节的 URL-safe Base64 密钥")
        return key

    def encrypt_token(self, token: str) -> tuple[bytes, bytes, bytes]:
        from Crypto.Cipher import AES

        cipher = AES.new(self._encryption_key(), AES.MODE_GCM)
        ciphertext, tag = cipher.encrypt_and_digest(token.encode("utf-8"))
        return ciphertext, cipher.nonce, tag

    def decrypt_token(self, ciphertext: bytes | None, nonce: bytes | None, tag: bytes | None) -> str | None:
        if not ciphertext or not nonce or not tag:
            return None
        from Crypto.Cipher import AES

        try:
            cipher = AES.new(self._encryption_key(), AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    def sync_admin_token(self) -> None:
        token = str(config.get("auth", {}).get("admin_token", "")).strip()
        now = time.time()
        with self._lock, self.connect() as db:
            db.execute("UPDATE users SET enabled = 0 WHERE is_admin = 1")
            if token:
                db.execute(
                    """INSERT INTO users(token_hash, enabled, is_admin, note, created_at)
                       VALUES (?, 1, 1, '配置管理员', ?)
                       ON CONFLICT(token_hash) DO UPDATE SET enabled=1, is_admin=1, note='配置管理员'""",
                    (self.token_hash(token), now),
                )

    def authenticate(self, token: str) -> int | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM users WHERE token_hash = ? AND enabled = 1",
                (self.token_hash(token),),
            ).fetchone()
        return int(row["id"]) if row else None

    def is_admin(self, user_id: int) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT is_admin FROM users WHERE id=? AND enabled=1", (user_id,)).fetchone()
        return bool(row and row["is_admin"])

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT id,note,enabled,created_at,token_cipher,token_nonce,token_tag,
                   (SELECT COUNT(*) FROM tasks WHERE tasks.user_id=users.id) AS task_count
                   FROM users WHERE is_admin=0 ORDER BY created_at DESC"""
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["token"] = self.decrypt_token(item.pop("token_cipher"), item.pop("token_nonce"), item.pop("token_tag"))
            result.append(item)
        return result

    def create_user(self, token: str, note: str, enabled: bool = True) -> int:
        token = token.strip()
        if not token:
            raise ValueError("用户令牌不能为空")
        ciphertext, nonce, tag = self.encrypt_token(token)
        try:
            with self._lock, self.connect() as db:
                cursor = db.execute(
                    """INSERT INTO users
                       (token_hash,enabled,is_admin,note,token_cipher,token_nonce,token_tag,created_at)
                       VALUES (?,?,0,?,?,?,?,?)""",
                    (self.token_hash(token), int(enabled), note.strip(), ciphertext, nonce, tag, time.time()),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError("该令牌已存在") from exc

    def update_user(self, user_id: int, note: str, enabled: bool, token: str | None = None) -> bool:
        values: list[Any] = [note.strip(), int(enabled)]
        sql = "UPDATE users SET note=?,enabled=?"
        if token is not None and token.strip():
            clean_token = token.strip()
            ciphertext, nonce, tag = self.encrypt_token(clean_token)
            sql += ",token_hash=?,token_cipher=?,token_nonce=?,token_tag=?"
            values.extend((self.token_hash(clean_token), ciphertext, nonce, tag))
        sql += " WHERE id=? AND is_admin=0"
        values.append(user_id)
        try:
            with self._lock, self.connect() as db:
                return db.execute(sql, values).rowcount == 1
        except sqlite3.IntegrityError as exc:
            raise ValueError("该令牌已存在") from exc

    def delete_user(self, user_id: int) -> bool:
        active = ("pending", "downloading", "packing")
        if self.count_tasks(user_id, active):
            raise ValueError("该用户仍有运行中或等待中的任务")
        with self._lock, self.connect() as db:
            return db.execute("DELETE FROM users WHERE id=? AND is_admin=0", (user_id,)).rowcount == 1

    def create_task(self, task: Any, retention_seconds: float) -> None:
        now = time.time()
        with self._lock, self.connect() as db:
            db.execute(
                """INSERT INTO tasks
                   (id,user_id,album_ids,status,message,completed,total,archive_path,error,created_at,updated_at,expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task.task_id, task.user_id, json.dumps(task.ids), task.status, task.message, 0, None,
                 None, None, now, now, now + retention_seconds),
            )

    def save_task(self, task: Any, retention_seconds: float) -> None:
        now = time.time()
        with self._lock, self.connect() as db:
            db.execute(
                """UPDATE tasks SET status=?,message=?,completed=?,total=?,archive_path=?,error=?,
                   updated_at=?,expires_at=? WHERE id=?""",
                (task.status, task.message, task.completed, task.total, task.archive, task.error,
                 now, now + retention_seconds, task.task_id),
            )

    def task_row(self, task_id: str, user_id: int | None = None) -> sqlite3.Row | None:
        query, args = "SELECT * FROM tasks WHERE id=?", [task_id]
        if user_id is not None:
            query += " AND user_id=?"
            args.append(user_id)
        with self.connect() as db:
            return db.execute(query, args).fetchone()

    def recoverable_tasks(self) -> list[sqlite3.Row]:
        with self._lock, self.connect() as db:
            db.execute("UPDATE tasks SET status='pending', message='服务重启，等待恢复' WHERE status IN ('downloading','packing')")
            return list(db.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY created_at"))

    def count_tasks(self, user_id: int | None, statuses: tuple[str, ...]) -> int:
        marks = ",".join("?" for _ in statuses)
        query = f"SELECT COUNT(*) AS n FROM tasks WHERE status IN ({marks})"
        args: list[Any] = list(statuses)
        if user_id is not None:
            query += " AND user_id=?"
            args.append(user_id)
        with self.connect() as db:
            return int(db.execute(query, args).fetchone()["n"])

    def expired_tasks(self, now: float) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(db.execute(
                "SELECT * FROM tasks WHERE expires_at <= ? AND status IN ('completed','failed','interrupted')", (now,)
            ))

    def delete_task(self, task_id: str) -> None:
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM tasks WHERE id=?", (task_id,))


database = Database()
