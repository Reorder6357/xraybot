"""
اسکنر کانال با Telethon (اکانت شخصی مدیر).
- اسکن کامل تاریخچه کانال (فقط متادیتای فایل‌ها: اسم، حجم، مدت ویدیو)
- تشخیص تکراری: (اسم نرمال‌شده) یا (حجم + مدت)
- حذف تکراری‌ها بعد از تأیید (فقط قدیمی‌ترین هر گروه می‌مونه)
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
)
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    Message,
)

from app.core.config import settings, DATA_DIR
from app.core.database import db

logger = logging.getLogger(__name__)

# ریتم ملایم برای جلوگیری از FloodWait
FLOOD_SLEEP = 60
SCAN_BATCH_PROGRESS = 100


class ChannelScanner:
    def __init__(self):
        self._client: Optional[TelegramClient] = None
        self._session_path = DATA_DIR / "scanner.session"

    # ---------- client ----------
    def _ensure_client(self) -> TelegramClient:
        if self._client is None:
            api_id = int(settings.tg_api_id or 0)
            api_hash = settings.tg_api_hash or ""
            if not api_id or not api_hash:
                raise RuntimeError("api_id/api_hash تنظیم نشده")
            self._client = TelegramClient(
                str(self._session_path), api_id, api_hash
            )
        return self._client

    async def ensure_connected(self):
        client = self._ensure_client()
        if not client.is_connected():
            await client.connect()
        client.flood_sleep_threshold = FLOOD_SLEEP

    async def is_logged_in(self) -> bool:
        try:
            await self.ensure_connected()
            return await self._client.is_user_authorized()
        except Exception:
            return False

    # ---------- لاگین ----------
    async def request_code(self, phone: str) -> tuple[bool, str]:
        try:
            await self.ensure_connected()
            await self._client.send_code_request(phone)
            return True, f"کد تأیید به {phone} فرستاده شد. کد ۵ رقمی رو بفرست:"
        except Exception as e:
            return False, f"❌ خطا: {str(e)[:120]}"

    async def submit_code(self, phone: str, code: str) -> tuple[bool, str, bool]:
        """برمی‌گردونه: (موفق, پیام, need_password)"""
        try:
            await self.ensure_connected()
            await self._client.sign_in(phone, code.strip())
            return True, "✅ ورود موفق شد!", False
        except SessionPasswordNeededError:
            return True, "🔐 رمز دومرحله‌ای (پسورد) رو بفرست:", True
        except PhoneCodeInvalidError:
            return False, "❌ کد اشتباهه. دوباره بفرست:", False
        except PhoneCodeExpiredError:
            return False, "❌ کد منقضی شد. دوباره از اول (شماره) شروع کن:", False
        except Exception as e:
            return False, f"❌ خطا: {str(e)[:120]}", False

    async def submit_password(self, password: str) -> tuple[bool, str]:
        try:
            await self.ensure_connected()
            await self._client.sign_in(password=password)
            return True, "✅ ورود کامل شد!"
        except Exception as e:
            return False, f"❌ خطا: {str(e)[:120]}"

    async def logout(self) -> bool:
        try:
            await self.ensure_connected()
            await self._client.log_out()
            self._client = None
            return True
        except Exception:
            return False

    # ---------- اسکن ----------
    async def scan_channel(
        self,
        peer: str,
        progress_cb=None,
        max_messages: int = 100000,
    ) -> tuple[bool, str, int]:
        """
        اسکن کامل کانال و ذخیره متادیتای فایل‌ها در دیتابیس.
        برمی‌گردونه: (موفق, پیام, تعداد فایل پیدا شده)
        """
        try:
            await self.ensure_connected()
            if not await self._client.is_user_authorized():
                return False, "❌ اول باید با شماره وارد بشی (دکمه ورود)", 0

            entity = await self._resolve_entity(peer)
            channel_id = str(entity.id)

            files: list[dict] = []
            count = 0
            async for msg in self._client.iter_messages(entity, limit=max_messages):
                if isinstance(msg, Message) and msg.document:
                    filename = ""
                    duration = 0.0
                    is_video = False
                    for attr in msg.document.attributes:
                        if isinstance(attr, DocumentAttributeFilename):
                            filename = attr.file_name or ""
                        elif isinstance(attr, DocumentAttributeVideo):
                            duration = float(attr.duration or 0)
                            is_video = True
                    files.append({
                        "msg_id": msg.id,
                        "filename": filename,
                        "size": msg.document.size or 0,
                        "duration": duration,
                        "is_video": is_video,
                        "date": msg.date.timestamp() if msg.date else 0,
                    })
                count += 1
                if progress_cb and count % SCAN_BATCH_PROGRESS == 0:
                    await progress_cb(count)

            await db.add_scanned_files(channel_id, files)

            msg = (
                f"✅ اسکن تموم شد\n"
                f"• پیام‌های بررسی‌شده: {count}\n"
                f"• فایل/ویدیو پیدا شد: {len(files)}"
            )
            return True, msg, len(files)
        except Exception as e:
            logger.exception("scan_channel failed")
            return False, f"❌ خطا در اسکن: {str(e)[:150]}", 0

    async def _resolve_entity(self, peer: str):
        """پیدا کردن کانال با انعطاف: آیدی عددی، یوزرنیم، یا لینک t.me"""
        entity = None
        try:
            entity = await self._client.get_entity(peer)
        except Exception:
            # تبدیل لینک به یوزرنیم
            clean = peer.strip()
            if clean.startswith("https://t.me/"):
                clean = clean.replace("https://t.me/", "").split("/")[0]
            if not clean.startswith("@") and not clean.startswith("-"):
                clean = "@" + clean
            try:
                entity = await self._client.get_entity(clean)
            except Exception:
                # آخرین راه: resolve username
                from telethon.tl.functions.contacts import ResolveUsernameRequest
                clean2 = clean.lstrip("@")
                try:
                    res = await self._client(ResolveUsernameRequest(clean2))
                    entity = res.chats[0] if res.chats else None
                except Exception:
                    entity = None
        if entity is None:
            raise RuntimeError(
                "کانال پیدا نشد. مطمئن شو اکانت اسکنر توی اون کانال عضوه یا یه پیام ازش فوروارد کن."
            )
        return entity

    # ---------- تشخیص تکراری ----------
    @staticmethod
    def _norm_filename(name: str) -> str:
        """اسم فایل رو نرمال می‌کنه: حذف پسوند، حروف خاص، فاصله، حروف بزرگ"""
        if not name:
            return ""
        name = name.strip()
        # حذف پسوند
        if "." in name:
            name = name.rsplit(".", 1)[0]
        name = name.lower()
        name = re.sub(r"[\W_]+", "", name)
        return name

    async def find_duplicates(self, channel_id: str) -> list[dict]:
        """
        گروه‌های تکراری:
         - کلید ۱: اسم فایل نرمال‌شده یکسان
         - کلید ۲: حجم یکسان + مدت یکسان (برای فایل‌های تغییرنام‌داده)
        برمی‌گردونه: لیست گروه‌ها (هر گروه: {key, items})
        """
        rows = await db.get_scanned_files(channel_id)
        if len(rows) < 2:
            return []

        # union-find ساده برای ادغام
        parent = list(range(len(rows)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        # کلید ۱: اسم
        by_name: dict[str, list[int]] = {}
        for i, r in enumerate(rows):
            key = self._norm_filename(r["filename"] or "")
            if key:
                by_name.setdefault(key, []).append(i)
        for key, idxs in by_name.items():
            if len(idxs) > 1:
                first = idxs[0]
                for j in idxs[1:]:
                    union(first, j)

        # کلید ۲: حجم + مدت
        by_sd: dict[tuple, list[int]] = {}
        for i, r in enumerate(rows):
            size = int(r["size"] or 0)
            dur = float(r["duration"] or 0)
            if size > 0 and dur > 0:
                by_sd.setdefault((size, int(dur)), []).append(i)
        for key, idxs in by_sd.items():
            if len(idxs) > 1:
                first = idxs[0]
                for j in idxs[1:]:
                    union(first, j)

        # ساخت گروه‌ها
        groups_map: dict[int, list] = {}
        for i, r in enumerate(rows):
            groups_map.setdefault(find(i), []).append(r)

        groups = []
        for root, items in groups_map.items():
            if len(items) < 2:
                continue
            # فقط گروه‌هایی که واقعاً دلیل تکرار دارن:
            # یا اسم یکسان دارن، یا (حجم+مدت) یکسان
            names = {self._norm_filename(it["filename"] or "") for it in items}
            names.discard("")
            sds = {(int(it["size"] or 0), int(float(it["duration"] or 0))) for it in items}
            sds.discard((0, 0))
            has_reason = len(names) < len(items) or len(sds) < len(items)
            if not has_reason:
                continue
            items_sorted = sorted(items, key=lambda it: (it["date"] or 0, it["msg_id"]))
            groups.append({
                "items": items_sorted,
                "keep": items_sorted[0],  # قدیمی‌ترین می‌مونه
                "dups": items_sorted[1:],
            })

        # مرتب‌سازی گروه‌ها بر اساس تعداد تکراری (بیشترین اول)
        groups.sort(key=lambda g: len(g["dups"]), reverse=True)
        return groups

    # ---------- حذف ----------
    async def delete_duplicates(
        self, channel_id: str, groups: list[dict]
    ) -> tuple[bool, str, int]:
        """حذف تکراری‌ها؛ از هر گروه فقط قدیمی‌ترین می‌مونه"""
        try:
            await self.ensure_connected()
            if not await self._client.is_user_authorized():
                return False, "❌ وارد نشده‌ای", 0

            entity = await self._client.get_entity(int(channel_id))

            all_ids = []
            for g in groups:
                for it in g["dups"]:
                    all_ids.append(int(it["msg_id"]))

            if not all_ids:
                return False, "چیزی برای حذف نیست", 0

            # حذف در دسته‌های ۹۰تایی (ریتم ملایم)
            deleted = 0
            for i in range(0, len(all_ids), 90):
                batch = all_ids[i:i + 90]
                result = await self._client.delete_messages(entity, batch)
                if result:
                    deleted += len(batch)
                await asyncio.sleep(1)

            await db.delete_scanned_by_msg_ids(channel_id, all_ids)

            msg = f"🗑 {deleted} پیام تکراری حذف شد. از هر گروه فقط قدیمی‌ترین موند."
            return True, msg, deleted
        except Exception as e:
            logger.exception("delete_duplicates failed")
            return False, f"❌ خطا در حذف: {str(e)[:150]}", 0


import asyncio  # noqa: E402

scanner = ChannelScanner()
