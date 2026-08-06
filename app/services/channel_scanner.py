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
        client.flood_sleep_threshold = FLOOD_SLEEP  # 60s

    async def is_logged_in(self) -> bool:
        try:
            await self.ensure_connected()
            return await self._client.is_user_authorized()
        except Exception:
            return False

    async def get_login_info(self) -> str:
        """اطلاعات اکانت واردشده (برای نمایش: اسم + شماره)"""
        try:
            await self.ensure_connected()
            if not await self._client.is_user_authorized():
                return ""
            me = await self._client.get_me()
            if me is None:
                return ""
            parts = [me.first_name or ""]
            if getattr(me, "username", None):
                parts.append("@" + me.username)
            phone = getattr(me, "phone", None)
            if phone:
                parts.append("+" + str(phone))
            return " ".join(x for x in parts if x)
        except Exception:
            return ""

    async def disconnect(self):
        """بستن اتصال و ذخیره سشن (قبل از خاموشی)"""
        try:
            if self._client is not None and self._client.is_connected():
                await self._client.disconnect()
                self._client = None
        except Exception:
            pass

    # ---------- لاگین ----------
    async def request_code(self, phone: str) -> tuple[bool, str]:
        try:
            await self.ensure_connected()
            await self._client.send_code_request(phone)
            # ذخیره شماره برای دفعات بعد
            try:
                from app.core.config import settings as _s
                from app.core.database import db as _db
                _s.scanner_phone = phone
                await _db.set_setting("scanner_phone", phone)
            except Exception:
                pass
            return True, f"کد تأیید به {phone} فرستاده شد. کد ۵ رقمی رو بفرست:"
        except Exception as e:
            return False, f"❌ خطا: {str(e)[:120]}"

    async def submit_code(self, phone: str, code: str) -> tuple[bool, str, bool]:
        """برمی‌گردونه: (موفق, پیام, need_password)"""
        try:
            await self.ensure_connected()
            await self._client.sign_in(phone, code.strip())
            try:
                await self._client.session.save()
            except Exception:
                pass
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
        hints: Optional[dict] = None,
    ) -> tuple[bool, str, int]:
        """
        اسکن کامل کانال و ذخیره متادیتای فایل‌ها در دیتابیس.
        برمی‌گردونه: (موفق, پیام, تعداد فایل پیدا شده)
        """
        try:
            await self.ensure_connected()
            if not await self._client.is_user_authorized():
                return False, "❌ اول باید با شماره وارد بشی (دکمه ورود)", 0

            entity = await self._resolve_entity(peer, hints)
            channel_id = str(entity.id)
            chan_title = getattr(entity, "title", "") or ""
            chan_uname = getattr(entity, "username", "")
            chan_ref = f"@{chan_uname}" if chan_uname else str(entity.id)
            chan_display = f"{chan_title} ({chan_ref})" if chan_title else chan_ref

            # ---- بررسی دسترسی: تعداد کل پیام‌های قابل‌دسترس ----
            total = 0
            try:
                one = await self._client.get_messages(entity, limit=1)
                total = getattr(one, "total", 0) or 0
            except Exception:
                pass

            files: list[dict] = []
            count = 0
            stats = {"video": 0, "doc": 0, "gif": 0, "photo": 0, "text": 0, "other": 0}
            async for msg in self._client.iter_messages(entity, limit=max_messages, wait_time=0):
                media = None
                if msg.document:
                    media = ("doc", msg.document)
                elif msg.video:
                    media = ("video", msg.video)
                elif getattr(msg, "gif", None):
                    media = ("gif", msg.gif)
                elif msg.photo:
                    media = ("photo", msg.photo)

                if media:
                    kind, m = media
                    stats[kind] = stats.get(kind, 0) + 1
                    filename = ""
                    duration = 0.0
                    is_video = kind in ("video", "gif")
                    size = 0

                    if kind == "doc":
                        for attr in m.attributes:
                            if isinstance(attr, DocumentAttributeFilename):
                                filename = attr.file_name or ""
                            elif isinstance(attr, DocumentAttributeVideo):
                                duration = float(attr.duration or 0)
                                is_video = True
                        size = m.size or 0
                    elif kind in ("video", "gif"):
                        # ویدیوهای ارسال‌شده به‌صورت ویدیو اسم فایل مستقیم ندارن
                        filename = ""
                        duration = float(getattr(m, "duration", 0) or 0)
                        size = getattr(m, "size", 0) or 0
                    elif kind == "photo":
                        # سایز عکس از بزرگترین سایزش
                        try:
                            sizes = getattr(m, "sizes", [])
                            if sizes:
                                size = getattr(sizes[-1], "size", 0) or 0
                        except Exception:
                            pass

                    files.append({
                        "msg_id": msg.id,
                        "filename": filename,
                        "size": size,
                        "duration": duration,
                        "is_video": is_video,
                        "date": msg.date.timestamp() if msg.date else 0,
                    })
                else:
                    # بدون مدیا: متن یا سایر
                    if msg.text:
                        stats["text"] += 1
                    else:
                        stats["other"] += 1
                count += 1
                if progress_cb and count % SCAN_BATCH_PROGRESS == 0:
                    await progress_cb(count)
                if count % 500 == 0:
                    logger.info(f"Scan progress: {count} messages, {len(files)} files so far")

            await db.add_scanned_files(channel_id, files)

            # ---- گزارش + هشدار اگه دسترسی کامل نبود ----
            warn = ""
            if total > 0 and count < total:
                warn = (
                    f"\n\n⚠️ فقط {count} از {total} پیام قابل دسترسی بود!\n"
                    f"مطمئن شو اکانت اسکنر توی کانال «ادمین» باشه و تیک «خواندن پیام‌ها/Read messages» فعال باشه.\n"
                    f"(اگه کانال خصوصیه، اکانت باید عضو/ادمین باشه تا تاریخچه کامل دیده بشه)"
                )

            # خلاصه انواع پیام‌ها (برای دیباگ)
            breakdown = " | ".join(f"{k}: {v}" for k, v in stats.items() if v > 0)
            with_name = sum(1 for f in files if f.get("filename"))
            with_dur = sum(1 for f in files if (f.get("duration") or 0) > 0)
            msg = (
                f"✅ اسکن تموم شد — 📡 `{chan_display}`\n"
                f"• کل پیام‌های کانال: {total if total > 0 else 'نامشخص'}\n"
                f"• پیام‌های بررسی‌شده: {count}\n"
                f"• ویدیو پیدا شد: {stats['video']}\n"
                f"• فایل پیدا شد: {stats['doc']}\n"
                f"• عکس: {stats['photo']} | گیف: {stats['gif']}\n"
                f"• متن: {stats['text']} | سایر: {stats['other']}\n"
                f"• مجموع مدیا: {len(files)}\n"
                f"• فایل با اسم: {with_name} | با مدت: {with_dur}"
                + warn
            )
            logger.info(f"Scan finished: total={total} checked={count} files={len(files)} breakdown={breakdown}")
            return True, msg, len(files)
        except Exception as e:
            logger.exception("scan_channel failed")
            return False, f"❌ خطا در اسکن: {str(e)[:150]}", 0

    @staticmethod
    def _norm_id(raw) -> Optional[int]:
        """نرمال‌سازی آیدی کانال (حذف -100 پیشوند)"""
        try:
            return int(str(raw).replace("-100", "").replace("-", ""))
        except Exception:
            return None

    async def _resolve_entity(self, peer: str, hints: Optional[dict] = None):
        """
        پیدا کردن کانال با هر روش ممکن:
        ۱) get_entity مستقیم (آیدی/یوزرنیم/لینک)
        ۲) ResolveUsername برای کانال‌های عمومی
        ۳) جستجو در گفتگوهای اکانت اسکنر (برای کانال‌های خصوصی که ادمینشه)
        """
        entity = None

        # ۱) مستقیم
        try:
            entity = await self._client.get_entity(peer)
        except Exception:
            entity = None

        # ۲) یوزرنیم / لینک
        if entity is None and peer:
            clean = peer.strip()
            if clean.startswith("https://t.me/"):
                clean = clean.replace("https://t.me/", "").split("/")[0]
            if clean and not clean.startswith("@") and not clean.lstrip("-").isdigit():
                clean = "@" + clean
            try:
                entity = await self._client.get_entity(clean)
            except Exception:
                entity = None
            if entity is None and clean.lstrip("@") and not clean.lstrip("-").isdigit():
                try:
                    from telethon.tl.functions.contacts import ResolveUsernameRequest
                    res = await self._client(ResolveUsernameRequest(clean.lstrip("@")))
                    if res.chats:
                        entity = res.chats[0]
                except Exception:
                    entity = None

        # ۳) جستجو در گفتگوهای اکانت (پوشش کانال‌های خصوصی)
        if entity is None:
            target_id = None
            target_title = None
            if hints:
                target_id = self._norm_id(hints.get("id"))
                target_title = str(hints.get("title") or "").strip()
            if peer:
                p = peer.strip()
                if p.lstrip("-").isdigit():
                    target_id = self._norm_id(p)
                elif p.startswith("@"):
                    pass  # یوزرنیم — با اسم تطبیق می‌دیم
            try:
                async for d in self._client.iter_dialogs():
                    ent = d.entity
                    eid = getattr(ent, "id", None)
                    eid_norm = self._norm_id(eid)
                    uname = getattr(ent, "username", None)
                    title = str(getattr(ent, "title", "") or "")
                    if target_id is not None and eid_norm == target_id:
                        entity = ent
                        break
                    if target_title and title.strip() == target_title:
                        entity = ent
                        break
                    if peer and peer.startswith("@") and uname and ("@" + uname) == peer:
                        entity = ent
                        break
            except Exception as e:
                logger.warning(f"dialog search failed: {e}")

        if entity is None:
            name = str((hints or {}).get("title") or "") or peer or "؟"
            raise RuntimeError(
                f"کانال «{name}» پیدا نشد. مطمئن شو اکانت اسکنر توی اون کانال عضوه/ادمینه "
                f"و با همون اکانت یه بار توی تلگرام کانال رو باز کرده باشی."
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

    async def find_duplicates(self, channel_id: str) -> dict:
        """
        تشخیص تکراری در دو سطح:
         - sure:    اسم نرمال‌شده یکسان، یا (حجم + مدت) یکسان → تقریباً قطعی
         - suspect: فقط حجم یکسان (بدون اسم/مدت یکسان) → باید خودت چک کنی
        برمی‌گردونه: {"sure": [گروه‌ها], "suspect": [گروه‌ها]}
        """
        rows = await db.get_scanned_files(channel_id)
        sure_groups: list[dict] = []
        suspect_groups: list[dict] = []
        if len(rows) < 2:
            return {"sure": sure_groups, "suspect": suspect_groups}

        n = len(rows)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        # کلید ۱ (قطعی): اسم نرمال‌شده یکسان
        by_name: dict[str, list[int]] = {}
        for i, r in enumerate(rows):
            key = self._norm_filename(r["filename"] or "")
            if key:
                by_name.setdefault(key, []).append(i)
        for key, idxs in by_name.items():
            if len(idxs) > 1:
                for j in idxs[1:]:
                    union(idxs[0], j)

        # کلید ۲ (قطعی): حجم + مدت یکسان (هر دو > 0)
        by_sd: dict[tuple, list[int]] = {}
        for i, r in enumerate(rows):
            size = int(r["size"] or 0)
            dur = float(r["duration"] or 0)
            if size > 0 and dur > 0:
                by_sd.setdefault((size, int(dur)), []).append(i)
        for key, idxs in by_sd.items():
            if len(idxs) > 1:
                for j in idxs[1:]:
                    union(idxs[0], j)

        # ساخت گروه‌ها و تشخیص سطح
        groups_map: dict[int, list] = {}
        for i, r in enumerate(rows):
            groups_map.setdefault(find(i), []).append(r)

        # فایل‌هایی که توی هیچ گروه قطعی نیفتادن
        in_sure = set()
        for root, items in groups_map.items():
            if len(items) < 2:
                continue
            items_sorted = sorted(items, key=lambda it: (it["date"] or 0, it["msg_id"]))
            names = {self._norm_filename(it["filename"] or "") for it in items}
            names.discard("")
            sds = {(int(it["size"] or 0), int(float(it["duration"] or 0))) for it in items}
            sds.discard((0, 0))
            same_name = len(names) < len(items)
            same_sd = len(sds) < len(items)
            if same_name or same_sd:
                group = {
                    "items": items_sorted,
                    "keep": items_sorted[0],
                    "dups": items_sorted[1:],
                }
                sure_groups.append(group)
                for it in items:
                    in_sure.add(it["msg_id"])

        # سطح مشکوک: فقط هم‌حجم (duration ثبت نشده) — جدا از گروه‌های قطعی
        remaining = [r for r in rows if r["msg_id"] not in in_sure]
        by_size: dict[int, list] = {}
        for r in remaining:
            size = int(r["size"] or 0)
            if size > 0:
                by_size.setdefault(size, []).append(r)
        for size, items in by_size.items():
            if len(items) < 2:
                continue
            # اگه اسم‌ها هم یکسان بودن، قطعی بود؛ پس اینجا حتماً متفاوتن → مشکوک
            items_sorted = sorted(items, key=lambda it: (it["date"] or 0, it["msg_id"]))
            suspect_groups.append({
                "items": items_sorted,
                "keep": items_sorted[0],
                "dups": items_sorted[1:],
            })

        sure_groups.sort(key=lambda g: len(g["dups"]), reverse=True)
        suspect_groups.sort(key=lambda g: len(g["dups"]), reverse=True)
        return {"sure": sure_groups, "suspect": suspect_groups}

    # ---------- لینک و پیش‌نمایش ----------
    @staticmethod
    def msg_link(channel, msg_id: int) -> str:
        """لینک مستقیم به پیام (کاربر با کلیک می‌بینه)"""
        try:
            username = getattr(channel, "username", None)
            if username:
                return f"https://t.me/{username}/{msg_id}"
            cid = str(getattr(channel, "id", ""))
            if cid.startswith("-100"):
                cid = cid[4:]
            elif cid.startswith("-"):
                cid = cid[1:]
            return f"https://t.me/c/{cid}/{msg_id}"
        except Exception:
            return ""

    async def forward_preview(self, channel_id, msg_ids: list[int], to_chat_id: int) -> tuple[bool, str]:
        """فوروارد کردن خود پیام‌ها (ویدیو/فایل) به چت مدیر برای بررسی دستی"""
        try:
            await self.ensure_connected()
            if not await self._client.is_user_authorized():
                return False, "❌ وارد نشده‌ای"
            entity = await self._client.get_entity(int(channel_id))
            if not msg_ids:
                return False, "چیزی برای فوروارد نیست"
            await self._client.forward_messages(to_chat_id, msg_ids, entity)
            return True, f"📤 {len(msg_ids)} پیام فوروارد شد — خودت چک کن."
        except Exception as e:
            logger.exception("forward_preview failed")
            return False, f"❌ خطا در فوروارد: {str(e)[:120]}"

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
