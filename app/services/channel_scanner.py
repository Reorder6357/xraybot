"""
اسکنر کانال با Telethon (اکانت شخصی مدیر).
- اسکن کامل تاریخچه کانال (فقط متادیتای فایل‌ها: اسم، حجم، مدت ویدیو)
- تشخیص تکراری: (اسم نرمال‌شده) یا (حجم + مدت)
- حذف تکراری‌ها بعد از تأیید (فقط قدیمی‌ترین هر گروه می‌مونه)
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
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
            try:
                await asyncio.wait_for(client.connect(), timeout=30)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "اتصال به تلگرام برقرار نشد (مشکل شبکه/سرور). چند دقیقه دیگه دوباره تلاش کن."
                )
        # flood_sleep_threshold=1: هر محدودیتی به‌صورت خطا سطح می‌شه و ما خودمون
        # مدیریتش می‌کنیم (با پیام به کاربر) — نه صبر بی‌صدای Telethon
        client.flood_sleep_threshold = 1

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

            if progress_cb:
                try:
                    await progress_cb("🔌 متصل شد — در حال پیدا کردن کانال...")
                except Exception:
                    pass

            try:
                entity = await asyncio.wait_for(self._resolve_entity(peer, hints), timeout=60)
            except asyncio.TimeoutError:
                return False, "❌ پیدا کردن کانال طول کشید (مشکل شبکه). دوباره تلاش کن.", 0
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

            # اطلاع‌رسانی اولیه: چند تا پیام/کلیپ اسکن می‌شه
            if progress_cb:
                try:
                    if total > 0:
                        await progress_cb(f"📊 {total} پیام/کلیپ در کانال پیدا شد — شروع اسکن...")
                    else:
                        await progress_cb("📊 در حال دریافت اطلاعات کانال...")
                except Exception:
                    pass

            files: list[dict] = []
            count = 0
            stats = {"video": 0, "doc": 0, "gif": 0, "photo": 0, "text": 0, "other": 0}

            # ---- صفحه‌بندی دستی (مقاوم در برابر FloodWait و قفل‌شدن) ----
            SCAN_TIME_CAP = 20 * 60  # سقف ۲۰ دقیقه — هیچ‌وقت بی‌نهایت صبر نمی‌کنه
            CHUNK = 100
            t_start = time.time()
            offset_id = 0
            capped = False
            retries = 0

            while True:
                # سقف زمانی
                if time.time() - t_start > SCAN_TIME_CAP:
                    capped = True
                    break
                # سقف تعداد
                if max_messages and count >= max_messages:
                    break

                try:
                    batch = await asyncio.wait_for(
                        self._client.get_messages(entity, limit=CHUNK, offset_id=offset_id),
                        timeout=30,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"get_messages timeout at {count}")
                    retries += 1
                    if retries >= 3:
                        logger.warning("Too many timeouts, stopping scan")
                        break
                    if progress_cb:
                        try:
                            await progress_cb(
                                f"⚠️ اتصال کند — تلاش مجدد... (تا الان {count} پیام)"
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(3)
                    continue
                except FloodWaitError as e:
                    secs = int(getattr(e, "seconds", 30) or 30)
                    logger.warning(f"FloodWait {secs}s while scanning (at {count})")
                    if secs >= 10 and progress_cb:
                        try:
                            await progress_cb(
                                f"⚠️ محدودیت تلگرام — {secs} ثانیه صبر کن... (تا الان {count} پیام)"
                            )
                        except Exception:
                            pass
                    # صبر + یه استراحت اضافه که دوباره flood نگیره
                    await asyncio.sleep(min(secs, 120) + 2)
                    continue
                except Exception as e:
                    logger.warning(f"get_messages failed at {count}: {e}")
                    break

                retries = 0  # موفق شد → ریست تلاش‌ها

                if not batch:
                    break

                for msg in batch:
                    if not isinstance(msg, Message):
                        continue
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
                            filename = ""
                            duration = float(getattr(m, "duration", 0) or 0)
                            size = getattr(m, "size", 0) or 0
                        elif kind == "photo":
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
                        if msg.text:
                            stats["text"] += 1
                        else:
                            stats["other"] += 1
                    count += 1

                # آفست برای صفحه بعد
                offset_id = batch[-1].id
                if len(batch) < CHUNK:
                    break

                if progress_cb and count % SCAN_BATCH_PROGRESS == 0:
                    if total > 0:
                        await progress_cb(f"⏳ مرحله {count} از {total}...")
                    else:
                        await progress_cb(count)
                if count % 500 == 0:
                    logger.info(f"Scan progress: {count} messages, {len(files)} files so far")

                # ریتم ملایم (جلوگیری از FloodWait)
                # ۱.۵ ثانیه بین هر ۱۰۰ پیام → ~۶۷ پیام/ثانیه — تلگرام محدودیت نمیزنه
                await asyncio.sleep(1.5)

            if capped:
                logger.warning("Scan capped by time limit")

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

    @staticmethod
    def _dur_round(v) -> int:
        """مدت رو به نزدیک‌ترین ثانیه گرد می‌کنه (نه truncate!)"""
        try:
            return int(float(v) + 0.5)
        except Exception:
            return 0

    @staticmethod
    def _size_bucket(size) -> int:
        """حجم رو به سطل ۱ مگابایتی می‌بره — اختلاف چند کیلوبایتی (متادیتا) رو نادیده می‌گیره"""
        try:
            return int(size) // (1024 * 1024)
        except Exception:
            return 0

    async def find_duplicates(self, channel_id: str) -> dict:
        """
        تشخیص تکراری فقط بر اساس (حجم + مدت ویدیو) — بدون مقایسه اسم:
         - قطعی (sure): حجم دقیقاً یکسان + مدت تا ۲ ثانیه اختلاف
         - مشکوک (suspect): حجم تا ۳۰MB اختلاف + مدت تا ۲ ثانیه اختلاف
        خروجی: {"sure": [...], "suspect": [...], "debug": "..."}
        """
        rows = await db.get_scanned_files(channel_id)
        sure_groups: list[dict] = []
        suspect_groups: list[dict] = []
        debug = ""
        if len(rows) < 2:
            return {"sure": sure_groups, "suspect": suspect_groups, "debug": debug}

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

        # ---------- ادغام قطعی: حجم دقیق یکسان + مدت نزدیک (تا ۲ ثانیه) ----------
        by_size: dict[int, list[int]] = {}
        for i, r in enumerate(rows):
            sz = int(r["size"] or 0)
            if sz > 0:
                by_size.setdefault(sz, []).append(i)
        for sz, idxs in by_size.items():
            if len(idxs) < 2:
                continue
            # مرتب‌سازی بر اساس مدت، بعد همسایه‌ها
            idxs_sorted = sorted(idxs, key=lambda i: float(rows[i]["duration"] or 0))
            for k in range(len(idxs_sorted) - 1):
                a = idxs_sorted[k]
                for m in range(k + 1, len(idxs_sorted)):
                    b = idxs_sorted[m]
                    if float(rows[b]["duration"] or 0) - float(rows[a]["duration"] or 0) > 2.0:
                        break
                    union(a, b)

        groups_map: dict[int, list] = {}
        for i, r in enumerate(rows):
            groups_map.setdefault(find(i), []).append(r)

        in_sure = set()
        for root, items in groups_map.items():
            if len(items) < 2:
                continue
            items_sorted = sorted(items, key=lambda it: (it["date"] or 0, it["msg_id"]))
            # مطمئن شو حداقل یه جفت با حجم دقیق یکسان + مدت نزدیک هست
            same_exact = False
            for x in range(len(items)):
                for y in range(x + 1, len(items)):
                    sx, sy = int(items[x]["size"] or 0), int(items[y]["size"] or 0)
                    dx, dy = float(items[x]["duration"] or 0), float(items[y]["duration"] or 0)
                    if sx > 0 and sx == sy and abs(dx - dy) <= 2.0:
                        same_exact = True
                        break
                if same_exact:
                    break
            if same_exact:
                sure_groups.append({
                    "items": items_sorted,
                    "keep": items_sorted[0],
                    "dups": items_sorted[1:],
                })
                for it in items:
                    in_sure.add(it["msg_id"])

        # ---------- مشکوک: جفت‌های مستقیم (هر فایل یک‌بار مصرف) ----------
        # حجم تا ۳۰MB اختلاف + مدت تا ۲ ثانیه اختلاف
        remaining = [r for r in rows if r["msg_id"] not in in_sure]
        if len(remaining) >= 2:
            order = sorted(range(len(remaining)), key=lambda i: float(remaining[i]["duration"] or 0))
            used = set()
            for oi in range(len(order)):
                i = order[oi]
                if i in used:
                    continue
                base_dur = float(remaining[i]["duration"] or 0)
                base_size = int(remaining[i]["size"] or 0)
                if base_dur <= 0 or base_size <= 0:
                    continue
                group_idx = [i]
                for oj in range(oi + 1, len(order)):
                    j = order[oj]
                    if j in used:
                        continue
                    dj = float(remaining[j]["duration"] or 0)
                    if dj - base_dur > 2.0:
                        break
                    sj = int(remaining[j]["size"] or 0)
                    if sj <= 0:
                        continue
                    if abs(sj - base_size) <= 30 * 1024 * 1024:
                        group_idx.append(j)
                        if len(group_idx) >= 4:
                            break
                if len(group_idx) >= 2:
                    items = [remaining[k] for k in group_idx]
                    items_sorted = sorted(items, key=lambda it: (it["date"] or 0, it["msg_id"]))
                    suspect_groups.append({
                        "items": items_sorted,
                        "keep": items_sorted[0],
                        "dups": items_sorted[1:],
                        "reason": "حجم+مدت نزدیک",
                    })
                    used.update(group_idx)

        sure_groups.sort(key=lambda g: len(g["dups"]), reverse=True)
        suspect_groups.sort(key=lambda g: len(g["dups"]), reverse=True)

        # ---------- دیباگ وقتی چیزی پیدا نشد ----------
        if not sure_groups and not suspect_groups:
            parts = ["\n\n🔍 دیباگ — نمونه فایل‌های ثبت‌شده (از ۵۰۰ اول):"]
            sample = rows[:500]
            for r in sample[:3]:
                nm = (r["filename"] or "بدون اسم")[:30]
                mb = int(r["size"] or 0) / (1024 * 1024)
                dr = float(r["duration"] or 0)
                parts.append(f"• `{nm}` | {mb:.0f}MB | {int(dr // 60)}:{int(dr % 60):02d}")
            # نزدیک‌ترین جفت‌ها بر اساس (حجم + مدت)
            pairs = []
            for i in range(len(sample)):
                for j in range(i + 1, min(i + 50, len(sample))):
                    a, b = sample[i], sample[j]
                    da, db_ = float(a["duration"] or 0), float(b["duration"] or 0)
                    sa, sb = int(a["size"] or 0), int(b["size"] or 0)
                    if da <= 0 or db_ <= 0 or sa <= 0 or sb <= 0:
                        continue
                    dd = abs(da - db_)
                    ds = abs(sa - sb) / (1024 * 1024)
                    pairs.append((dd + ds, a, b, dd, ds))
            pairs.sort(key=lambda x: x[0])
            parts.append("\nنزدیک‌ترین جفت‌ها (حجم+مدت):")
            for score, a, b, dd, ds in pairs[:5]:
                na = (a["filename"] or "بدون اسم")[:20]
                nb = (b["filename"] or "بدون اسم")[:20]
                parts.append(f"• `{na}` vs `{nb}` | Δمدت={dd:.1f}s | Δحجم={ds:.0f}MB")
            debug = "\n".join(parts)

        return {"sure": sure_groups, "suspect": suspect_groups, "debug": debug}

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


    async def reset_admin_filter(self, peer: str, hints: Optional[dict] = None) -> tuple[bool, str]:
        """تلاش برای روشن کردن «فیلتر اکشن ادمین» با جابه‌جایی ادمین اسکنر:
        - ادمین رو بردار (EditAdmin با rights=0)
        - دوباره اضافه کن (با همون دسترسی‌ها) → تلگرام فیلتر رو پیش‌فرض روشن می‌کنه
        برمی‌گردونه: (موفق, پیام)"""
        try:
            await self.ensure_connected()
            if not await self._client.is_user_authorized():
                return False, "❌ وارد نشده‌ای"
            entity = await self._resolve_entity(peer, hints)

            me = await self._client.get_me()
            if me is None:
                return False, "❌ اکانت اسکنر پیدا نشد"

            from telethon.tl.functions.channels import (
                GetParticipantRequest, EditAdminRequest,
            )
            from telethon.tl.types import ChatAdminRights, InputUser

            # دسترسی‌های فعلی ادمین اسکنر
            try:
                part = await self._client(GetParticipantRequest(
                    channel=entity, participant=InputUser(me.id, me.access_hash)
                ))
                rights = part.participant.admin_rights
            except Exception as e:
                return False, f"❌ اکانت اسکنر ادمین کانال نیست: {str(e)[:80]}"

            # ۱) حذف ادمین (با rights خالی)
            try:
                await self._client(EditAdminRequest(
                    channel=entity,
                    user_id=InputUser(me.id, me.access_hash),
                    admin_rights=ChatAdminRights(),
                    rank="",
                ))
            except Exception as e:
                return False, f"❌ حذف ادمین نشد: {str(e)[:80]}"

            # ۲) دوباره اضافه کن (با دسترسی‌های قبلی)
            try:
                await self._client(EditAdminRequest(
                    channel=entity,
                    user_id=InputUser(me.id, me.access_hash),
                    admin_rights=rights,
                    rank="",
                ))
            except Exception as e:
                return False, f"❌ اضافه دوباره نشد (دستی اضافه کن): {str(e)[:80]}"

            return True, (
                "✅ ادمین اسکنر جابه‌جا شد — فیلتر اکشنش باید روشن شده باشه.\n"
                "حالا «♻️ بازیابی» رو دوباره بزن."
            )
        except Exception as e:
            logger.exception("reset_admin_filter failed")
            return False, f"❌ خطا: {str(e)[:120]}"

    # ---------- بازیابی فیلم‌های پاک‌شده (از Recent Actions / Admin Log) ----------
    async def scan_deleted_media(self, peer: str, hints: Optional[dict] = None, max_events: int = 300):
        """بررسی Admin Log برای پیام‌های حذف‌شده با مدیا (فیلم/فایل).
        برمی‌گردونه: (موفق, پیام, list[dict]) هر آیتم: msg_id/name/size/caption"""
        try:
            await self.ensure_connected()
            if not await self._client.is_user_authorized():
                return False, "❌ وارد نشده‌ای", []

            entity = await self._resolve_entity(peer, hints)

            from telethon.tl.functions.channels import GetAdminLogRequest
            from telethon.tl.types import (
                ChannelAdminLogEventsFilter,
                ChannelAdminLogEventActionDeleteMessage,
            )

            events = []
            try:
                # بدون فیلتر — همه اکشن‌ها رو بگیر، خودمون delete رو جدا می‌کنیم
                res = await self._client(GetAdminLogRequest(
                    channel=entity, q="", max_id=0, min_id=0, limit=max_events,
                ))
                events = res.events
            except Exception as e:
                logger.warning(f"GetAdminLogRequest failed: {e}")
                return False, f"❌ دسترسی به Recent Actions نیست: {str(e)[:100]}", []

            found = []
            for ev in events:
                if not isinstance(ev.action, ChannelAdminLogEventActionDeleteMessage):
                    continue
                msg = ev.action.message
                media = getattr(msg, "document", None) or getattr(msg, "video", None)
                if media is None:
                    continue
                name = ""
                for attr in getattr(media, "attributes", []):
                    if isinstance(attr, DocumentAttributeFilename):
                        name = attr.file_name or ""
                found.append({
                    "msg_id": msg.id,
                    "name": name or "بدون اسم",
                    "size": getattr(media, "size", 0) or 0,
                    "caption": msg.message or "",
                })

            total_mb = sum(f["size"] for f in found) / (1024 * 1024)
            msg_out = (
                f"🔍 {len(found)} فیلم حذف‌شده در Recent Actions پیدا شد"
                + (f" (مجموعاً {total_mb:.0f}MB)" if found else "")
            )
            return True, msg_out, found
        except Exception as e:
            logger.exception("scan_deleted_media failed")
            return False, f"❌ خطا: {str(e)[:150]}", []

    async def recover_deleted(self, peer: str, hints: Optional[dict] = None,
                               progress_cb=None, max_events: int = 300):
        """بازیابی فیلم‌های پاک‌شده با قابلیت ادامه از جایی که قطع شد.
        - پیشرفت (msg_id های موفق) توی دیتابیس ذخیره می‌شه
        - اگه وسط کار قطع بشه، دفعه بعد از همونجا ادامه می‌ده
        - فایل‌های نیمه‌دانلود توی recover_tmp می‌مونن و دوباره دانلود نمی‌شن
        برمی‌گردونه: (موفق, پیام, تعداد موفق, تعداد ناموفق)"""
        try:
            await self.ensure_connected()
            if not await self._client.is_user_authorized():
                return False, "❌ وارد نشده‌ای", 0, 0

            entity = await self._resolve_entity(peer, hints)
            channel_id = str(entity.id)

            from telethon.tl.functions.channels import GetAdminLogRequest
            from telethon.tl.types import (
                ChannelAdminLogEventsFilter,
                ChannelAdminLogEventActionDeleteMessage,
            )

            events = []
            try:
                # بدون فیلتر — همه اکشن‌ها رو بگیر
                res = await self._client(GetAdminLogRequest(
                    channel=entity, q="", max_id=0, min_id=0, limit=max_events,
                ))
                events = res.events
            except Exception as e:
                logger.warning(f"GetAdminLogRequest failed: {e}")
                return False, f"❌ دسترسی به Recent Actions نیست: {str(e)[:100]}", 0, 0

            deleted_msgs = [
                e.action.message for e in events
                if isinstance(e.action, ChannelAdminLogEventActionDeleteMessage)
                and (e.action.message.document or e.action.message.video)
            ]

            if not deleted_msgs:
                return True, "چیزی برای بازیابی نیست.", 0, 0

            # ---------- پیشرفت ذخیره‌شده (از دیتابیس) ----------
            state = await db.get_recover_state(channel_id) or {}
            done_ids = set(state.get("done_ids") or [])      # msg_id های موفق قبلی (دیگه تکرار نمی‌شن)
            fail_ids = set(state.get("fail_ids") or [])      # msg_id های ناموفق قبلی (دوباره تلاش می‌شن)
            tmp_dir = DATA_DIR / "recover_tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)

            # فقط پیام‌هایی که هنوز موفق نشدن (ناموفق‌ها دوباره تلاش می‌شن — شاید خطا موقتی بود)
            todo = [m for m in deleted_msgs if m.id not in done_ids]
            skipped = len(done_ids)  # تعداد از قبل موفق

            # فایل‌های نیمه‌دانلود از اجرای قبلی: چون ممکنه ناقص باشن (قطعی وسط دانلود)،
            # پاکشون می‌کنیم تا از نو دانلود بشن — ویدیوی خراب آپلود نشه
            for m in todo:
                existing = tmp_dir / f"recover_{m.id}"
                if existing.exists():
                    try:
                        existing.unlink()
                    except Exception:
                        pass

            # شمارش جدیدِ همین اجرا (نه مجموع قبلی)
            recovered = 0
            failed = 0
            total = len(deleted_msgs)
            prev_done = len(done_ids)
            prev_fail = len(fail_ids)

            for i, msg in enumerate(todo, 1):
                if progress_cb:
                    done_so_far = prev_done + recovered + failed
                    await progress_cb(f"⏳ [{done_so_far + i}/{total}] دانلود و آپلود دوباره...")
                try:
                    path = await self._client.download_media(
                        msg, file=str(tmp_dir / f"recover_{msg.id}")
                    )
                    if not path:
                        fail_ids.add(msg.id)
                        failed += 1
                        await db.save_recover_state(channel_id, done_ids, fail_ids)
                        continue
                    # پست دوباره با کپشن اصلی
                    await self._client.send_file(entity, path, caption=msg.message or "")
                    done_ids.add(msg.id)
                    recovered += 1
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"recover msg {msg.id} failed: {e}")
                    fail_ids.add(msg.id)
                    failed += 1
                # 💾 ذخیره پیشرفت بعد از هر پیام — اگه قطع بشه، از اینجا ادامه می‌ده
                try:
                    await db.save_recover_state(channel_id, done_ids, fail_ids)
                except Exception:
                    pass

            # پاکسازی نهایی: هر فایل باقی‌مانده در tmp (ناقص یا قدیمی) رو حذف کن
            try:
                for leftover in tmp_dir.glob("recover_*"):
                    try:
                        leftover.unlink()
                    except Exception:
                        pass
            except Exception:
                pass

            state = await db.get_recover_state(channel_id)
            resume_note = ""
            if state and state.get("updated_at"):
                resume_note = f"\n🕒 آخرین اجرا: {time.strftime('%Y-%m-%d %H:%M', time.localtime(state['updated_at']))}"

            total_ok = prev_done + recovered
            msg_out = (
                f"♻️ بازیابی: این بار {recovered} موفق، {failed} ناموفق"
                + (f" — مجموع موفق: {total_ok} از {total}" if total > 0 else "")
                + resume_note
            )
            return True, msg_out, recovered, failed
        except Exception as e:
            logger.exception("recover_deleted failed")
            return False, f"❌ خطا: {str(e)[:150]}", 0, 0

import asyncio  # noqa: E402

scanner = ChannelScanner()
