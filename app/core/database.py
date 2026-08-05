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

            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE NOT NULL,
                title TEXT,
                username TEXT,
                is_active INTEGER DEFAULT 1,
                added_at REAL
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                name TEXT,
                is_active INTEGER DEFAULT 1,
                last_fetch REAL,
                added_at REAL
            );

            CREATE TABLE IF NOT EXISTS healthy_history (
                config_hash TEXT PRIMARY KEY,
                config_line TEXT NOT NULL,
                remark TEXT,
                country TEXT,
                latency REAL,
                first_seen REAL,
                last_seen REAL
            );

            -- کانفیگ‌های استخراج‌شده که هنوز تست نشدن
            CREATE TABLE IF NOT EXISTS pending_configs (
                config_hash TEXT PRIMARY KEY,
                config_line TEXT NOT NULL,
                protocol TEXT,
                address TEXT,
                port TEXT,
                remark TEXT,
                source TEXT,              -- message / file / forward / channel / subscription
                source_detail TEXT,       -- مثلاً نام کانال یا فایل
                added_at REAL,
                last_seen REAL
            );

            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER DEFAULT 0,
                times TEXT DEFAULT '[]',          -- JSON list of "HH:MM"
                last_run REAL
            );

            CREATE TABLE IF NOT EXISTS channel_tag (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                tag TEXT DEFAULT '',
                enabled INTEGER DEFAULT 0
            );

            INSERT OR IGNORE INTO schedule (id, enabled, times) VALUES (1, 0, '[]');
            INSERT OR IGNORE INTO channel_tag (id, tag, enabled) VALUES (1, '', 0);
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

    # ---------- channels ----------
    async def add_channel(self, chat_id: str, title: str = "", username: str = ""):
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO channels (chat_id, title, username, added_at)
            VALUES (?, ?, ?, ?)
            """,
            (str(chat_id), title, username, time.time()),
        )
        await self._conn.commit()

    async def remove_channel(self, chat_id: str):
        await self._conn.execute(
            "DELETE FROM channels WHERE chat_id = ?", (str(chat_id),)
        )
        await self._conn.commit()

    async def list_channels(self, only_active: bool = True):
        q = "SELECT * FROM channels"
        if only_active:
            q += " WHERE is_active = 1"
        cur = await self._conn.execute(q)
        return await cur.fetchall()

    # ---------- subscriptions ----------
    async def add_subscription(self, url: str, name: str = ""):
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO subscriptions (url, name, added_at)
            VALUES (?, ?, ?)
            """,
            (url, name, time.time()),
        )
        await self._conn.commit()

    async def remove_subscription(self, url: str):
        await self._conn.execute(
            "DELETE FROM subscriptions WHERE url = ?", (url,)
        )
        await self._conn.commit()

    async def list_subscriptions(self, only_active: bool = True):
        q = "SELECT * FROM subscriptions"
        if only_active:
            q += " WHERE is_active = 1"
        cur = await self._conn.execute(q)
        return await cur.fetchall()

    # ---------- healthy history (24h anti-duplicate) ----------
    async def add_healthy(self, config_hash: str, config_line: str, remark: str, country: str, latency: float):
        now = time.time()
        await self._conn.execute(
            """
            INSERT INTO healthy_history (config_hash, config_line, remark, country, latency, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_hash) DO UPDATE SET
                last_seen = excluded.last_seen,
                latency = excluded.latency,
                remark = excluded.remark
            """,
            (config_hash, config_line, remark, country, latency, now, now),
        )
        await self._conn.commit()

    async def is_recently_sent(self, config_hash: str, ttl_hours: int = 24) -> bool:
        cutoff = time.time() - (ttl_hours * 3600)
        cur = await self._conn.execute(
            "SELECT 1 FROM healthy_history WHERE config_hash = ? AND last_seen > ?",
            (config_hash, cutoff),
        )
        return await cur.fetchone() is not None

    async def cleanup_old_history(self, ttl_hours: int = 24):
        cutoff = time.time() - (ttl_hours * 3600)
        await self._conn.execute(
            "DELETE FROM healthy_history WHERE last_seen < ?", (cutoff,)
        )
        await self._conn.commit()

    # ---------- schedule ----------
    async def get_schedule(self) -> dict:
        cur = await self._conn.execute("SELECT * FROM schedule WHERE id = 1")
        row = await cur.fetchone()
        if not row:
            return {"enabled": False, "times": [], "last_run": None}
        return {
            "enabled": bool(row["enabled"]),
            "times": json.loads(row["times"] or "[]"),
            "last_run": row["last_run"],
        }

    async def set_schedule(self, enabled: bool, times: list[str]):
        await self._conn.execute(
            "UPDATE schedule SET enabled = ?, times = ? WHERE id = 1",
            (1 if enabled else 0, json.dumps(times)),
        )
        await self._conn.commit()

    async def update_last_run(self):
        await self._conn.execute(
            "UPDATE schedule SET last_run = ? WHERE id = 1", (time.time(),)
        )
        await self._conn.commit()

    # ---------- channel tag ----------
    async def get_channel_tag(self) -> dict:
        cur = await self._conn.execute("SELECT * FROM channel_tag WHERE id = 1")
        row = await cur.fetchone()
        if not row:
            return {"tag": "", "enabled": False}
        return {"tag": row["tag"] or "", "enabled": bool(row["enabled"])}

    async def set_channel_tag(self, tag: str, enabled: bool):
        await self._conn.execute(
            "UPDATE channel_tag SET tag = ?, enabled = ? WHERE id = 1",
            (tag, 1 if enabled else 0),
        )
        await self._conn.commit()

    # ---------- pending configs (extracted, not yet tested) ----------
    async def add_pending_configs(
        self,
        links: list[str],
        source: str = "message",
        source_detail: str = "",
    ) -> tuple[int, int]:
        """
        لیست لینک‌ها رو به جدول pending اضافه می‌کنه.
        برمی‌گردونه: (تعداد جدید, تعداد تکراری)
        """
        from app.services.config_extractor import (
            config_hash, parse_basic_info, get_remark
        )

        new_count = 0
        dup_count = 0
        now = time.time()

        for link in links:
            h = config_hash(link)
            info = parse_basic_info(link)

            # چک وجود قبلی
            cur = await self._conn.execute(
                "SELECT 1 FROM pending_configs WHERE config_hash = ?", (h,)
            )
            exists = await cur.fetchone() is not None

            await self._conn.execute(
                """
                INSERT INTO pending_configs
                    (config_hash, config_line, protocol, address, port, remark,
                     source, source_detail, added_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_hash) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    config_line = excluded.config_line,
                    remark = excluded.remark
                """,
                (
                    h,
                    link,
                    info.get("protocol") or "",
                    info.get("address") or "",
                    info.get("port") or "",
                    info.get("remark") or get_remark(link),
                    source,
                    source_detail,
                    now,
                    now,
                ),
            )

            if exists:
                dup_count += 1
            else:
                new_count += 1

        await self._conn.commit()
        return new_count, dup_count

    async def count_pending(self) -> int:
        cur = await self._conn.execute("SELECT COUNT(*) AS c FROM pending_configs")
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def get_pending_configs(self, limit: int = 5000) -> list:
        # oldest-first تا کانفیگ‌های قدیمی‌تر همیشه نوبتشون برسه (جلوگیری از گرسنگی)
        cur = await self._conn.execute(
            "SELECT * FROM pending_configs ORDER BY added_at ASC, last_seen ASC LIMIT ?",
            (limit,),
        )
        return await cur.fetchall()

    async def clear_pending(self):
        await self._conn.execute("DELETE FROM pending_configs")
        await self._conn.commit()

    async def delete_pending_by_hashes(self, hashes: list[str]):
        if not hashes:
            return
        placeholders = ",".join("?" * len(hashes))
        await self._conn.execute(
            f"DELETE FROM pending_configs WHERE config_hash IN ({placeholders})",
            hashes,
        )
        await self._conn.commit()


db = Database()
