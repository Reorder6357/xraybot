import aiosqlite
import json
import time
from pathlib import Path
from typing import Any, Optional
from app.core.config import settings


class Database:
    def __init__(self, db_path: Path = settings.db_path):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._create_tables()

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def _create_tables(self):
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- کانال‌های ثبت‌شده (برای اسکن سریع)
            CREATE TABLE IF NOT EXISTS channels (
                chat_id TEXT PRIMARY KEY,
                title TEXT,
                username TEXT,
                added_at REAL
            );

            -- نتیجه آخرین اسکن هر کانال (تا دوباره اسکن نشه)
            CREATE TABLE IF NOT EXISTS scan_results (
                channel_id TEXT PRIMARY KEY,
                scanned_at REAL,
                files_count INTEGER DEFAULT 0,
                sure_count INTEGER DEFAULT 0,
                suspect_count INTEGER DEFAULT 0,
                groups_json TEXT,
                debug TEXT
            );

            -- فایل‌های اسکن‌شده در کانال (برای تشخیص تکراری)
            CREATE TABLE IF NOT EXISTS scanned_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                msg_id INTEGER NOT NULL,
                filename TEXT,
                size INTEGER DEFAULT 0,
                duration REAL DEFAULT 0,
                is_video INTEGER DEFAULT 0,
                date REAL,
                UNIQUE(channel_id, msg_id)
            );
            """
        )
        await self._conn.commit()

    # ---------- generic settings ----------
    async def get_setting(self, key: str, default: Any = None) -> Any:
        cur = await self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]

    async def set_setting(self, key: str, value: Any):
        await self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        await self._conn.commit()

    # ---------- channels (کانال‌های ثبت‌شده) ----------
    async def add_channel(self, chat_id: str, title: str = "", username: str = ""):
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO channels (chat_id, title, username, added_at)
            VALUES (?, ?, ?, ?)
            """,
            (str(chat_id), title, username, time.time()),
        )
        await self._conn.commit()

    async def list_channels(self) -> list:
        cur = await self._conn.execute(
            "SELECT * FROM channels ORDER BY added_at DESC LIMIT 50"
        )
        return await cur.fetchall()

    async def remove_channel(self, chat_id: str):
        await self._conn.execute(
            "DELETE FROM channels WHERE chat_id = ?", (str(chat_id),)
        )
        await self._conn.commit()

    async def get_channel(self, chat_id: str):
        cur = await self._conn.execute(
            "SELECT * FROM channels WHERE chat_id = ?", (str(chat_id),)
        )
        return await cur.fetchone()

    # ---------- scan results (نتیجه اسکن ذخیره‌شده) ----------
    async def save_scan_result(self, channel_id: str, files_count: int, found: dict):
        import json as _json
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO scan_results
                (channel_id, scanned_at, files_count, sure_count, suspect_count, groups_json, debug)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id, time.time(), int(files_count),
                len(found.get("sure") or []), len(found.get("suspect") or []),
                _json.dumps(found, ensure_ascii=False, default=str),
                found.get("debug") or "",
            ),
        )
        await self._conn.commit()

    async def get_scan_result(self, channel_id: str):
        cur = await self._conn.execute(
            "SELECT * FROM scan_results WHERE channel_id = ?", (channel_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        import json as _json
        result = dict(row)
        try:
            result["groups"] = _json.loads(result.get("groups_json") or "{}")
        except Exception:
            result["groups"] = {}
        return result

    # ---------- scanned files (اسکن کانال) ----------
    async def add_scanned_files(self, channel_id: str, items: list[dict]):
        """items: [{msg_id, filename, size, duration, is_video, date}]"""
        if not items:
            return
        await self._conn.executemany(
            """
            INSERT OR REPLACE INTO scanned_files
                (channel_id, msg_id, filename, size, duration, is_video, date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    channel_id, it["msg_id"], it.get("filename"),
                    int(it.get("size") or 0), float(it.get("duration") or 0),
                    1 if it.get("is_video") else 0, it.get("date") or 0,
                )
                for it in items
            ],
        )
        await self._conn.commit()

    async def get_scanned_files(self, channel_id: str, limit: int = 200000) -> list:
        cur = await self._conn.execute(
            "SELECT * FROM scanned_files WHERE channel_id = ? ORDER BY date ASC, msg_id ASC LIMIT ?",
            (channel_id, limit),
        )
        return await cur.fetchall()

    async def clear_scanned_files(self, channel_id: str = ""):
        if channel_id:
            await self._conn.execute(
                "DELETE FROM scanned_files WHERE channel_id = ?", (channel_id,)
            )
        else:
            await self._conn.execute("DELETE FROM scanned_files")
        await self._conn.commit()

    async def delete_scanned_by_msg_ids(self, channel_id: str, msg_ids: list[int]):
        if not msg_ids:
            return
        placeholders = ",".join("?" * len(msg_ids))
        await self._conn.execute(
            f"DELETE FROM scanned_files WHERE channel_id = ? AND msg_id IN ({placeholders})",
            [channel_id] + msg_ids,
        )
        await self._conn.commit()


db = Database()
