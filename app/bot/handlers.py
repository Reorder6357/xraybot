"""
هندلرهای ربات — نسخه تمیز
فقط: اسکن کانال (تکراری‌یاب) + دیپلوی گیت‌هاب + مدیریت ادمین‌ها
(بخش xray/کانفیگ به‌طور کامل حذف شد)
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import zipfile
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from app.core.config import settings, DATA_DIR
from app.core.database import db
from app.bot.keyboards import (
    main_menu, scanner_menu, settings_menu, github_menu, back_only, confirm_keyboard,
)
from app.services.github_deploy import github_deployer
from app.services.channel_scanner import scanner

logger = logging.getLogger(__name__)


def _esc(s: str) -> str:
    """فرار از کاراکترهای خاص Markdown (برای متن‌های کاربر/کانال که نباید کرش کنه)"""
    if not s:
        return ""
    for ch in ("_", "*", "`", "[", "]"):
        s = s.replace(ch, "\\" + ch)
    return s


# -------------------- آپدیت از فایل ZIP --------------------
ZIP_MAX_SIZE = 10 * 1024 * 1024          # 10MB فایل zip
ZIP_MAX_UNCOMPRESSED = 30 * 1024 * 1024  # حداکثر حجم کل بعد از باز شدن
ZIP_MAX_FILES = 300                      # حداکثر تعداد فایل
UPDATE_ROOT_FILES = {"Dockerfile", "requirements.txt", "README.md", ".gitignore", "railway.toml"}
UPDATE_EXT = {".py", ".txt", ".md", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini"}
UPDATE_SKIP_DIRS = {"__pycache__", "data", "outputs", ".venv", "venv", "node_modules", ".git"}


def parse_update_zip(data: bytes) -> tuple[bool, str, dict]:
    """
    استخراج امن فایل‌های کد از ZIP آپدیت.
    فقط فایل‌های app/ (متن‌ی) + چند فایل ریشه (Dockerfile و...) قبول می‌شه.
    برمی‌گردونه: (موفق?, پیام, {path_in_repo: content})
    """
    if len(data) > ZIP_MAX_SIZE:
        return False, "حجم فایل زیپ بیشتر از حد مجاز (۱۰MB) است.", {}
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return False, "این فایل یک ZIP معتبر نیست.", {}

    files: dict = {}
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        # ضد zip-slip و مسیرهای عجیب
        norm = name.replace("\\", "/")
        parts = [x for x in norm.split("/") if x not in ("", ".")]
        if not parts or norm.startswith("/") or ".." in parts or ":" in parts[0]:
            continue
        if any(s in parts for s in UPDATE_SKIP_DIRS):
            continue
        # فقط app/ و چند فایل ریشه
        if parts[0] == "app":
            if Path(norm).suffix.lower() not in UPDATE_EXT:
                continue
        else:
            if norm not in UPDATE_ROOT_FILES:
                continue
        total += info.file_size
        if total > ZIP_MAX_UNCOMPRESSED or len(files) >= ZIP_MAX_FILES:
            return False, "فایل زیپ بیش از حد بزرگ است یا تعداد فایل‌هایش زیاد است.", {}
        try:
            content = zf.read(info).decode("utf-8")
        except UnicodeDecodeError:
            return False, f"فایل {norm} متنی نیست (فرمت پشتیبانی نمی‌شود).", {}
        files[norm] = content

    if not files:
        return False, "هیچ فایل قابل آپدیتی در زیپ پیدا نشد (باید فایل‌های app/ باشند).", {}
    return True, f"{len(files)} فایل از ZIP استخراج شد.", files


# -------------------- فوروارد کانال (برای اسکن) --------------------

def _forward_source_detail(message) -> str:
    """نام کانال/کاربر مبدأ فوروارد رو با forward_origin (ساختار جدید PTB v21) برمی‌گردونه."""
    origin = message.forward_origin
    if not origin:
        return ""
    try:
        if origin.type == "channel":
            # اول یوزرنیم (قابل resolve)، بعد آیدی عددی، بعد اسم (برای نمایش)
            if origin.chat.username:
                return "@" + origin.chat.username
            if origin.chat.id:
                return str(origin.chat.id)
            return origin.chat.title or ""
        if origin.type == "chat":
            if origin.sender_chat.username:
                return "@" + origin.sender_chat.username
            if origin.sender_chat.id:
                return str(origin.sender_chat.id)
            return origin.sender_chat.title or ""
        if origin.type == "user":
            return origin.sender_user.full_name or str(origin.sender_user.id)
        if origin.type == "hidden_user":
            return origin.sender_user_name or "کاربر ناشناس"
    except Exception:
        pass
    return ""


def _forward_hints(message) -> dict:
    """از فوروارد، آیدی و اسم کانال مبدأ رو برمی‌گردونه (برای resolve مطمئن)"""
    origin = message.forward_origin
    if not origin:
        return {}
    ch = None
    try:
        if origin.type == "channel":
            ch = origin.chat
        elif origin.type == "chat":
            ch = origin.sender_chat
    except Exception:
        ch = None
    if ch is None:
        return {}
    return {
        "id": str(getattr(ch, "id", "")),
        "title": str(getattr(ch, "title", "") or ""),
        "username": str(getattr(ch, "username", "") or ""),
    }


# -------------------- دکوریتورها --------------------

def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None:
            return
        if not settings.is_owner(user.id):
            if update.callback_query:
                await update.callback_query.answer("⛔ فقط مدیر اصلی دسترسی دارد", show_alert=True)
            else:
                await update.effective_message.reply_text("⛔ فقط مدیر اصلی دسترسی دارد.")
            return
        return await func(update, context)
    return wrapper


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None:
            return
        if not settings.is_admin(user.id):
            if update.callback_query:
                await update.callback_query.answer("⛔ دسترسی ندارید", show_alert=True)
            else:
                await update.effective_message.reply_text("⛔ دسترسی ندارید.")
            return
        return await func(update, context)
    return wrapper


# -------------------- /start --------------------
@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_owner = settings.is_owner(user.id)

    # حذف دکمه‌های ثابت قدیمی (ReplyKeyboard) که از نسخه‌های قبل مونده
    from telegram import ReplyKeyboardRemove
    try:
        await update.effective_message.reply_text(
            "⌨️ پاک‌سازی دکمه‌های قدیمی...", reply_markup=ReplyKeyboardRemove()
        )
    except Exception:
        pass

    # مستقیم منوی اسکن
    logged = await scanner.is_logged_in()
    info = ""
    if logged:
        who = await scanner.get_login_info()
        info = f"\n👤 وارد شده با: `{who}`" if who else ""
    status = "✅ وارد شده‌اید" if logged else "❌ هنوز وارد نشده‌اید"
    await update.effective_message.reply_text(
        f"📡 اسکن کانال برای پیدا کردن فایل‌های تکراری\n\n{status}{info}\n\n"
        f"۱) کانال رو ثبت کن (با آیدی/یوزرنیم یا فوروارد)\n"
        f"۲) از لیست کانال‌های ثبت‌شده انتخاب کن و اسکن بزن\n"
        f"۳) گزارش تکراری‌ها + تأیید حذف",
        parse_mode="Markdown",
        reply_markup=scanner_menu(logged),
    )


# -------------------- Callback router --------------------
@admin_only
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    is_owner = settings.is_owner(user_id)

    if data == "back_main":
        await query.edit_message_text("منوی اصلی:", reply_markup=main_menu(is_owner))
        return

    # ---- Scanner ----
    if data == "menu_scanner":
        logged = await scanner.is_logged_in()
        info = ""
        if logged:
            who = await scanner.get_login_info()
            info = f"\n👤 وارد شده با: `{who}`" if who else ""
        status = "✅ وارد شده‌اید" if logged else "❌ هنوز وارد نشده‌اید"
        await query.edit_message_text(
            f"📡 اسکن کانال برای پیدا کردن فایل‌های تکراری\n\n{status}{info}\n\n"
            f"۱) ورود با شماره (اکانت ادمین کانال)\n"
            f"۲) از کانال یه پیام/ویدیو فوروارد کن و «اسکن این کانال» رو بزن\n"
            f"۳) گزارش تکراری‌ها + تأیید حذف",
            parse_mode="Markdown",
            reply_markup=scanner_menu(logged),
        )
        return

    if data == "scan_login":
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        # اگه از قبل وارد شده → نیازی به لاگین مجدد نیست
        if await scanner.is_logged_in():
            who = await scanner.get_login_info()
            await query.edit_message_text(
                f"✅ شما قبلاً وارد شده‌اید ({who}).\n"
                f"لازم نیست دوباره لاگین کنی — مستقیم فوروارد کن و اسکن بزن.\n"
                f"اگه می‌خوای حساب عوض کنی، اول «🚪 خروج از حساب اسکنر» رو بزن.",
                reply_markup=scanner_menu(True),
            )
            return
        # چک api_id/api_hash (از حافظه یا دیتابیس)
        api_id = settings.tg_api_id or await db.get_setting("tg_api_id")
        api_hash = settings.tg_api_hash or await db.get_setting("tg_api_hash")
        if not api_id or not api_hash:
            await query.edit_message_text(
                "🔑 اول api_id و api_hash لازمه (از my.telegram.org می‌گیری).\n"
                "api_id رو بفرست:",
                reply_markup=back_only(),
            )
            return WAIT_SCAN_API_ID
        settings.tg_api_id = int(api_id)
        settings.tg_api_hash = api_hash
        saved_phone = settings.scanner_phone or await db.get_setting("scanner_phone")
        hint = f"\n(شماره قبلی: `{saved_phone}` — همون رو بفرست یا جدید)" if saved_phone else ""
        await query.edit_message_text(
            f"📱 شماره تلفن اکانت اسکنر رو با فرمت بین‌المللی بفرست:{hint}\n"
            f"مثال: `+989123456789`",
            parse_mode="Markdown",
            reply_markup=back_only(),
        )
        return WAIT_SCAN_PHONE

    if data == "act_scan_forward":
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        peer = context.user_data.get("last_forward_peer", "")
        hints = context.user_data.get("last_forward_hints") or {}
        if not peer:
            await query.answer("اول یه پیام از کانال فوروارد کن", show_alert=True)
            return
        # تبدیل callback به یک پیام جدید تا reply_text کار کنه
        await query.edit_message_text(f"⏳ شروع اسکن کانال «{hints.get('title') or peer}»...")
        await _start_scan_for_peer(update, context, peer, hints)
        return

    if data == "scan_channels_list":
        rows = await db.list_channels()
        if not rows:
            await query.edit_message_text(
                "📭 هنوز کانالی ثبت نشده.\n\n"
                "برای ثبت:\n"
                "• از کانال یه پیام/ویدیو فوروارد کن (خودکار ثبت می‌شه)\n"
                "• یا آیدی/یوزرنیم کانال رو بفرست: `@channel` یا `-1001234567890`",
                parse_mode="Markdown",
                reply_markup=back_only(),
            )
            return

        lines = [f"📋 {len(rows)} کانال ثبت‌شده — یکی رو انتخاب کن:", ""]
        for i, r in enumerate(rows[:10], 1):
            name = r["title"] or r["chat_id"]
            uname = f" (@{r['username']})" if r["username"] else ""
            has_result = await db.get_scan_result(r["chat_id"]) is not None
            mark = " ✅" if has_result else ""
            lines.append(f"{i}. {_esc(name)}{uname}{mark}")
        lines.append("")
        lines.append("با دکمه‌های پایین انتخاب کن. «➕» برای ثبت کانال جدید.")

        kb_rows = []
        for i in range(min(len(rows), 10)):
            has_result = await db.get_scan_result(rows[i]["chat_id"]) is not None
            if has_result:
                kb_rows.append([
                    InlineKeyboardButton(f"📊 نتیجه {i+1}", callback_data=f"scan_result_{i}"),
                    InlineKeyboardButton(f"🔄 اسکن {i+1}", callback_data=f"scan_choose_{i}"),
                ])
            else:
                kb_rows.append([
                    InlineKeyboardButton(f"📡 اسکن {i+1}", callback_data=f"scan_choose_{i}"),
                ])
        kb_rows.append([
            InlineKeyboardButton("➕ ثبت کانال جدید", callback_data="scan_add_manual"),
            InlineKeyboardButton("🗑 حذف کانال", callback_data="scan_remove_channel"),
        ])
        kb_rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")])

        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return

    if data == "scan_choose_":
        return

    if data.startswith("scan_result_"):
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        try:
            idx = int(data.split("_")[2])
        except ValueError:
            await query.answer("نامعتبر", show_alert=True)
            return
        rows = await db.list_channels()
        if idx < 0 or idx >= len(rows):
            await query.answer("نامعتبر", show_alert=True)
            return
        ch = rows[idx]
        saved = await db.get_scan_result(ch["chat_id"])
        if saved is None:
            await query.edit_message_text(
                f"❌ هنوز نتیجه‌ای برای «{_esc(ch['title'] or ch['chat_id'])}» ذخیره نشده. اول یه بار اسکن کن.",
                reply_markup=scanner_menu(True),
            )
            return
        # نمایش نتیجه ذخیره‌شده
        found = saved["groups"] or {}
        msg = (
            f"✅ آخرین اسکن: 📡 `{_esc(ch['title'] or ch['chat_id'])}`\n"
            f"• زمان: {time.strftime('%Y-%m-%d %H:%M', time.localtime(saved['scanned_at']))}\n"
            f"• فایل‌های بررسی‌شده: {saved['files_count']}"
        )
        await show_duplicate_report(update, context, query.message, msg, ch["chat_id"], found)
        return

    if data.startswith("scan_choose_"):
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        try:
            idx = int(data.split("_")[2])
        except ValueError:
            await query.answer("نامعتبر", show_alert=True)
            return
        rows = await db.list_channels()
        if idx < 0 or idx >= len(rows):
            await query.answer("نامعتبر", show_alert=True)
            return
        ch = rows[idx]
        peer = ch["chat_id"]
        hints = {"id": ch["chat_id"], "title": ch["title"] or "", "username": ch["username"] or ""}
        if ch["username"]:
            peer = "@" + ch["username"]
        await query.edit_message_text(f"⏳ شروع اسکن کانال «{_esc(ch['title'] or peer)}»...")
        await _start_scan_for_peer(update, context, peer, hints)
        return

    if data == "scan_add_manual":
        await query.edit_message_text(
            "📡 آیدی عددی یا یوزرنیم کانال رو بفرست:\n"
            "مثال: `@mychannel` یا `-1001234567890` یا لینک `https://t.me/...`\n\n"
            "⚠️ اکانت اسکنر باید توی اون کانال ادمین/عضو باشه.",
            parse_mode="Markdown",
            reply_markup=back_only(),
        )
        return WAIT_SCAN_CHANNEL

    if data == "scan_remove_channel":
        rows = await db.list_channels()
        if not rows:
            await query.answer("کانالی ثبت نشده", show_alert=True)
            return
        lines = [f"🗑 کدوم کانال حذف بشه؟ ({len(rows)} کانال)", ""]
        for i, r in enumerate(rows[:10], 1):
            name = r["title"] or r["chat_id"]
            lines.append(f"{i}. {_esc(name)}")
        kb_rows = [
            [InlineKeyboardButton(f"🗑 {i+1}", callback_data=f"scan_delchan_{i}")]
            for i in range(min(len(rows), 10))
        ]
        kb_rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="scan_channels_list")])
        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return

    if data.startswith("scan_delchan_"):
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        try:
            idx = int(data.split("_")[2])
        except ValueError:
            await query.answer("نامعتبر", show_alert=True)
            return
        rows = await db.list_channels()
        if idx < 0 or idx >= len(rows):
            await query.answer("نامعتبر", show_alert=True)
            return
        await db.remove_channel(rows[idx]["chat_id"])
        await query.edit_message_text("🗑 کانال حذف شد.", reply_markup=scanner_menu(True))
        return

    if data == "scan_channel":
        await query.edit_message_text(
            "📡 آیدی عددی کانال یا یوزرنیمش رو بفرست:\n"
            "مثال: `@mychannel` یا `-1001234567890`\n\n"
            "⚠️ اکانتی که باهاش وارد شدی باید ادمین/عضو اون کانال باشه.",
            parse_mode="Markdown",
            reply_markup=back_only(),
        )
        return WAIT_SCAN_CHANNEL

    if data == "scan_clear_data":
        rows = await db.list_channels()
        total_files = await db.count_scanned_files()
        lines = [
            "🗑 پاک‌سازی داده‌های اسکن",
            "",
            f"• کل فایل‌های ثبت‌شده: {total_files}",
            "",
        ]
        if rows:
            lines.append("کدوم کانال پاک بشه؟ (فایل‌ها + نتیجه اسکنش):")
        else:
            lines.append("کانالی ثبت نشده — فقط می‌تونی همه رو پاک کنی.")
        kb_rows = []
        for i in range(min(len(rows), 8)):
            name = rows[i]["title"] or rows[i]["chat_id"]
            kb_rows.append([InlineKeyboardButton(f"🗑 {_esc(str(name))[:25]}", callback_data=f"scan_clear_chan_{i}")])
        kb_rows.append([InlineKeyboardButton("🧹 پاک کردن همه (همه کانال‌ها)", callback_data="scan_clear_all")])
        kb_rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="menu_scanner")])
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return

    if data.startswith("scan_clear_chan_"):
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        try:
            idx = int(data.split("_")[3])
        except ValueError:
            await query.answer("نامعتبر", show_alert=True)
            return
        rows = await db.list_channels()
        if idx < 0 or idx >= len(rows):
            await query.answer("نامعتبر", show_alert=True)
            return
        ch = rows[idx]
        await db.clear_channel_data(ch["chat_id"])
        await query.edit_message_text(
            f"🗑 داده‌های «{_esc(ch['title'] or ch['chat_id'])}» پاک شد (فایل‌ها + نتیجه).",
            reply_markup=scanner_menu(True),
        )
        return

    if data == "scan_clear_all":
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        await db.clear_scanned_files()
        await query.edit_message_text(
            "🧹 همه داده‌های اسکن (همه کانال‌ها) پاک شد — فضا آزاد شد.",
            reply_markup=scanner_menu(await scanner.is_logged_in()),
        )
        return

    if data == "scan_logout":
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        ok = await scanner.logout()
        await query.edit_message_text(
            "🚪 از حساب اسکنر خارج شدی." if ok else "⚠️ مشکلی پیش اومد (شاید از قبل خارج شده بودی).",
            reply_markup=scanner_menu(False),
        )
        return

    if data == "scan_back_to_result":
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        channel_id = context.user_data.get("scan_groups_channel", "")
        stored = context.user_data.get("scan_groups") or {}
        if not channel_id or not stored:
            await query.answer("اول اسکن کن", show_alert=True)
            return
        found = {"sure": stored.get("sure") or [], "suspect": stored.get("suspect") or [], "debug": ""}
        msg = f"✅ نتیجه اسکن — 📡 `{_esc(channel_id)}`"
        try:
            await show_duplicate_report(update, context, query.message, msg, channel_id, found)
        except Exception:
            pass
        return

    if data == "scan_recover":
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        rows = await db.list_channels()
        if not rows:
            await query.edit_message_text(
                "اول یه کانال ثبت کن (فوروارد یا آیدی) بعد بازیابی کن.",
                reply_markup=back_only(),
            )
            return
        kb_rows = [
            [InlineKeyboardButton(
                f"♻️ {_esc(str(rows[i]['title'] or rows[i]['chat_id']))[:25]}",
                callback_data=f"scan_rec_{i}"
            )]
            for i in range(min(len(rows), 8))
        ]
        kb_rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="menu_scanner")])
        await query.edit_message_text(
            "🗑 کدوم کانال؟ فیلم‌های پاک‌شده‌ش از Recent Actions بازیابی می‌شن (با کپشن اصلی).\n"
            "⚠️ هر چی زودتر — ارجاع فایل‌ها ممکنه منقضی بشه!",
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return

    if data.startswith("scan_rec_"):
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        try:
            idx = int(data.split("_")[2])
        except ValueError:
            await query.answer("نامعتبر", show_alert=True)
            return
        rows = await db.list_channels()
        if idx < 0 or idx >= len(rows):
            await query.answer("نامعتبر", show_alert=True)
            return
        ch = rows[idx]
        peer = ch["chat_id"]
        hints = {"id": ch["chat_id"], "title": ch["title"] or "", "username": ch["username"] or ""}
        if ch["username"]:
            peer = "@" + ch["username"]
        context.user_data["recover_peer"] = peer
        context.user_data["recover_hints"] = hints
        await query.edit_message_text("🔍 در حال بررسی Recent Actions کانال...")
        ok, msg, found = await scanner.scan_deleted_media(peer, hints)
        if not ok:
            await query.edit_message_text(msg, reply_markup=scanner_menu(True))
            return
        if not found:
            await query.edit_message_text(
                f"{msg}\n\n"
                f"چند تا راه که باید چک کنی:\n\n"
                f"۱️⃣ توی کانال: تنظیمات ← مدیران ← ادمین اسکنر ← تیک «فیلتر اکشن ادمین» رو روشن کن ← سیو\n"
                f"۲️⃣ اگه فیلم‌ها رو با اکانت اصلی پاک کردی، فیلتر اکشن اون ادمین هم باید روشن باشه\n"
                f"۳️⃣ حذف‌هایی که «بعد از روشن کردن فیلتر» انجام بشن ثبت می‌شن — برای حذف‌های قبلی،\n"
                f"    Recent Actions (خود تلگرام) رو چک کن و اگه هست از اونجا دانلود کن\n"
                f"۴️⃣ ارجاع فایل‌ها ممکنه بعد از مدتی منقضی شده باشه — هر چی زودتر بهتر",
                reply_markup=scanner_menu(True),
            )
            return
        samples = "\n".join(
            f"• {_esc(f['name'][:40])} | {f['size'] / (1024 * 1024):.0f}MB"
            for f in found[:5]
        )
        more = f"\n… و {len(found) - 5} فیلم دیگر" if len(found) > 5 else ""
        await query.edit_message_text(
            f"{msg}\n\n{samples}{more}\n\n"
            f"این فیلم‌ها با کپشن اصلیشون دوباره توی کانال پست می‌شن. شروع کنم؟",
            parse_mode="Markdown",
            reply_markup=confirm_keyboard("scan_recover_confirm", "scan_cancel_delete"),
        )
        return

    if data == "scan_recover_confirm":
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        peer = context.user_data.get("recover_peer", "")
        hints = context.user_data.get("recover_hints") or {}
        if not peer:
            await query.answer("اول کانال رو انتخاب کن", show_alert=True)
            return
        status = await query.edit_message_text("♻️ شروع بازیابی... (فایل‌های بزرگ چند دقیقه طول می‌کشه)")
        async def progress(v):
            try:
                await status.edit_text(str(v))
            except Exception:
                pass
        ok, msg, rec, fail = await scanner.recover_deleted(peer, hints, progress_cb=progress)
        await status.edit_text(msg, reply_markup=scanner_menu(True))
        return

    if data == "scan_link_all":
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        channel_id = context.user_data.get("scan_groups_channel", "")
        stored = context.user_data.get("scan_groups") or {}
        all_groups = (stored.get("sure") or []) + (stored.get("suspect") or [])
        if not channel_id or not all_groups:
            await query.answer("اول اسکن کن", show_alert=True)
            return
        lines = []
        for gi, g in enumerate(all_groups, 1):
            lines.append(f"📦 گروه {gi} ({len(g['items'])} نسخه):")
            for it in g["items"]:
                try:
                    entity = await scanner._resolve_entity(channel_id, None)
                    link = scanner.msg_link(entity, int(it["msg_id"]))
                except Exception:
                    link = ""
                size_mb = it["size"] / (1024 * 1024)
                lines.append(f"  • {link} | {size_mb:.0f}MB")
        text_out = "\n".join(lines)
        try:
            await query.message.reply_text(text_out, reply_markup=scanner_menu(True))
        except Exception:
            await query.message.reply_text(text_out, reply_markup=scanner_menu(True))
        try:
            await query.edit_message_text("🔗 لینک همه گروه‌ها فرستاده شد.")
        except Exception:
            pass
        return

    if data.startswith("scan_view_") or data.startswith("scan_link_"):
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        channel_id = context.user_data.get("scan_groups_channel", "")
        stored = context.user_data.get("scan_groups") or {}
        sure_groups = stored.get("sure") or []
        suspect_groups = stored.get("suspect") or []
        if not channel_id or (not sure_groups and not suspect_groups):
            await query.answer("اول اسکن کن", show_alert=True)
            return

        parts = data.split("_")  # ["scan", "view"/"link", "sure"/"suspect", "0"/"all"]
        is_view = parts[1] == "view"
        level = parts[2]
        sel = parts[3]
        pool = sure_groups if level == "sure" else suspect_groups

        if sel == "all":
            target_groups = pool
        else:
            try:
                idx = int(sel)
            except ValueError:
                await query.answer("نامعتبر", show_alert=True)
                return
            if idx < 0 or idx >= len(pool):
                await query.answer("نامعتبر", show_alert=True)
                return
            target_groups = [pool[idx]]

        if is_view:
            # لینک‌ها از طریق خود ربات (مطمئن — فوروارد مستقیم از اکانت اسکنر به چت تو ممکن نیست)
            lines = []
            for gi, g in enumerate(target_groups, 1):
                lines.append(f"📦 گروه {gi} ({len(g['items'])} نسخه):")
                for it in g["items"]:
                    try:
                        entity = await scanner._resolve_entity(channel_id, None)
                        link = scanner.msg_link(entity, int(it["msg_id"]))
                    except Exception:
                        link = ""
                    size_mb = it["size"] / (1024 * 1024)
                    nm = it["filename"] or "بدون اسم"
                    lines.append(f"  • {link} | {nm[:30]} | {size_mb:.0f}MB")
            text_out = "\n".join(lines)
            back_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 بازگشت به نتیجه اسکن", callback_data="scan_back_to_result")],
                [InlineKeyboardButton("🏠 منو", callback_data="back_main")],
            ])
            try:
                await query.message.reply_text(text_out, reply_markup=back_kb)
            except Exception:
                await query.message.reply_text(text_out, reply_markup=back_kb)
            try:
                await query.edit_message_text("🔗 لینک‌ها فرستاده شد — روی هر کدوم بزن تا ویدیو رو ببینی.")
            except Exception:
                pass
        else:
            lines = []
            for gi, g in enumerate(target_groups, 1):
                lines.append(f"📦 گروه {gi}:")
                for it in g["items"]:
                    try:
                        entity = await scanner._client.get_entity(int(channel_id))
                        link = scanner.msg_link(entity, int(it["msg_id"]))
                    except Exception:
                        link = ""
                    size_mb = it["size"] / (1024 * 1024)
                    lines.append(f"  • {link} ({size_mb:.0f}MB)")
            await query.message.reply_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=scanner_menu(True),
            )
            try:
                await query.edit_message_text("📎 لینک‌ها فرستاده شد.")
            except Exception:
                pass
        return

    if data == "scan_confirm_delete":
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        channel_id = context.user_data.get("scan_groups_channel", "")
        stored = context.user_data.get("scan_groups") or {}
        sure_groups = stored.get("sure") or []
        if not channel_id or not sure_groups:
            await query.answer("اول اسکن کن", show_alert=True)
            return
        total_dups = sum(len(g["dups"]) for g in sure_groups)
        await query.edit_message_text(
            f"⚠️ {total_dups} فایل تکراری قطعی حذف بشه?\n\n"
            f"از هر گروه فقط «قدیمی‌ترین نسخه» می‌مونه و بقیه حذف می‌شن.\n"
            f"این کار قابل برگشت نیست!",
            reply_markup=confirm_keyboard("scan_confirm_delete_yes", "scan_cancel_delete"),
        )
        return

    if data == "scan_confirm_delete_yes":
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        channel_id = context.user_data.get("scan_groups_channel", "")
        if not channel_id:
            await query.answer("اول اسکن کن", show_alert=True)
            return
        await do_scan_delete(update, context, channel_id, only_sure=True)
        return

    if data == "scan_confirm_delete_suspect":
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        channel_id = context.user_data.get("scan_groups_channel", "")
        stored = context.user_data.get("scan_groups") or {}
        suspect_groups = stored.get("suspect") or []
        if not channel_id or not suspect_groups:
            await query.answer("اول اسکن کن", show_alert=True)
            return
        total_dups = sum(len(g["dups"]) for g in suspect_groups)
        # ⚠️ هشدار قرمز: اینا قطعی نیستن!
        await query.edit_message_text(
            f"🚨⚠️ {total_dups} فایل «احتمالی» حذف بشه؟!\n\n"
            f"این فایل‌ها فقط «شبیه» هستن (حجم/مدت نزدیک) — ممکنه اصلاً تکراری نباشن!\n"
            f"قبل از حذف حتماً با «👁 ببین» چک کن.\n"
            f"از هر گروه فقط قدیمی‌ترین می‌مونه — بقیه برای همیشه پاک می‌شن و قابل برگشت نیست!",
            reply_markup=confirm_keyboard("scan_confirm_delete_suspect_yes", "scan_cancel_delete"),
        )
        return

    if data == "scan_confirm_delete_suspect_yes":
        if not settings.is_admin(user_id):
            await query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        channel_id = context.user_data.get("scan_groups_channel", "")
        if not channel_id:
            await query.answer("اول اسکن کن", show_alert=True)
            return
        await do_scan_delete(update, context, channel_id, only_sure=False)
        return

    if data == "scan_cancel_delete":
        await query.edit_message_text(
            "🚫 حذف لغو شد.",
            reply_markup=scanner_menu(await scanner.is_logged_in()),
        )
        return

    # ---- GitHub ----
    if data == "menu_github":
        if not is_owner:
            await query.answer("⛔ فقط مدیر اصلی", show_alert=True)
            return
        status = await github_deployer.get_status()
        await query.edit_message_text(
            f"مدیریت دیپلوی گیت‌هاب\n\n{status}",
            reply_markup=github_menu(),
        )
        return

    if data == "set_github":
        if not is_owner:
            await query.answer("⛔ فقط مدیر اصلی", show_alert=True)
            return
        await query.edit_message_text(
            "توکن گیت‌هاب رو بفرست (با دسترسی `repo`):\n"
            "از اینجا بساز: https://github.com/settings/tokens",
            reply_markup=back_only(),
        )
        return WAIT_GITHUB_TOKEN

    if data == "github_status":
        if not is_owner:
            return
        status = await github_deployer.get_status()
        await query.edit_message_text(status, reply_markup=github_menu())
        return

    if data == "deploy_now":
        if not is_owner:
            await query.answer("⛔ فقط مدیر اصلی", show_alert=True)
            return
        await query.edit_message_text("⏳ در حال جمع‌آوری فایل‌ها و دیپلوی به گیت‌هاب...")
        ok, msg = await github_deployer.deploy_current_project(
            commit_message="Deploy from Telegram bot",
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=github_menu())
        return

    if data == "update_from_zip":
        if not is_owner:
            await query.answer("⛔ فقط مدیر اصلی", show_alert=True)
            return
        await query.edit_message_text(
            "📦 آپدیت از فایل ZIP:\n\n"
            "فایل ZIP آپدیت (شامل پوشه `app/`) رو بفرست.\n"
            "ربات فایل‌ها رو به گیت‌هاب push می‌کنه و Railway خودش دیپلوی می‌کنه.",
            reply_markup=back_only(),
        )
        return

    if data == "confirm_update_zip":
        if not is_owner:
            await query.answer("⛔ فقط مدیر اصلی", show_alert=True)
            return
        files = context.user_data.pop("update_zip_files", None)
        if not files:
            await query.edit_message_text(
                "❌ فایل ZIP پیدا نشد؛ دوباره فایل رو بفرست.",
                reply_markup=back_only(),
            )
            return
        await query.edit_message_text("⏳ در حال push به گیت‌هاب...")
        ok, msg = await github_deployer.deploy_files(
            files, commit_message="Update from bot ZIP"
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=github_menu())
        return

    # ---- Settings ----
    if data == "menu_settings":
        await query.edit_message_text("تنظیمات:", reply_markup=settings_menu(is_owner))
        return

    if data == "system_status":
        text = (
            f"🤖 ربات: فعال\n"
            f"👤 مدیر اصلی: `{settings.owner_id}`\n"
            f"👥 ادمین‌ها: {len(settings.admin_ids)}\n"
            f"📡 اسکنر: {'✅ وارد شده' if await scanner.is_logged_in() else '❌ وارد نشده'}\n"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=settings_menu(is_owner))
        return

    if data == "manage_admins":
        if not is_owner:
            await query.answer("⛔ فقط مدیر اصلی", show_alert=True)
            return
        lines = [f"• `{a}`" for a in settings.admin_ids] or ["• (فعلاً ادمین اضافه‌ای نیست)"]
        await query.edit_message_text(
            "مدیریت ادمین‌ها (غیر از مدیر اصلی):\n\n"
            + "\n".join(lines)
            + "\n\nآیدی عددی ادمین جدید رو بفرست تا اضافه بشه:",
            parse_mode="Markdown",
            reply_markup=back_only(),
        )
        return WAIT_ADMIN_ID


# -------------------- Conversation handlers --------------------

@owner_only
async def received_admin_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text.strip()
    if not text.lstrip("-").isdigit():
        await update.effective_message.reply_text(
            "❌ فقط آیدی عددی بفرست.", reply_markup=back_only()
        )
        return WAIT_ADMIN_ID
    aid = int(text)
    if aid == settings.owner_id:
        await update.effective_message.reply_text("این آیدی خود مدیر اصلیه.")
        return ConversationHandler.END
    if aid in settings.admin_ids:
        await update.effective_message.reply_text("این آیدی از قبل ادمینه.")
        return ConversationHandler.END
    settings.admin_ids.append(aid)
    await db.set_setting("admin_ids", settings.admin_ids)
    await update.effective_message.reply_text(
        f"✅ ادمین `{aid}` اضافه شد.",
        parse_mode="Markdown",
        reply_markup=main_menu(True),
    )
    return ConversationHandler.END


@owner_only
async def received_github_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.effective_message.text.strip()
    context.user_data["gh_token"] = token
    await update.effective_message.reply_text(
        "حالا نام ریپو رو بفرست (فرمت: `username/repo`):",
        parse_mode="Markdown",
    )
    return WAIT_GITHUB_REPO


@owner_only
async def received_github_repo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repo = update.effective_message.text.strip()
    token = context.user_data.get("gh_token")
    if not token:
        await update.effective_message.reply_text("❌ توکن پیدا نشد. از اول شروع کن.")
        return ConversationHandler.END

    ok, msg = await github_deployer.save_credentials(token, repo)
    await update.effective_message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=main_menu(True),
    )
    context.user_data.pop("gh_token", None)
    return ConversationHandler.END


# -------------------- Scanner conversation handlers --------------------

@admin_only
async def received_scan_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text.strip()
    if not text.isdigit():
        await update.effective_message.reply_text("❌ api_id فقط عددیه. دوباره بفرست:", reply_markup=back_only())
        return WAIT_SCAN_API_ID
    context.user_data["scan_api_id"] = text
    await update.effective_message.reply_text(
        "حالا api_hash رو بفرست (یه رشته حرف/عددیه):",
        reply_markup=back_only(),
    )
    return WAIT_SCAN_API_HASH


@admin_only
async def received_scan_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_hash = update.effective_message.text.strip()
    api_id = context.user_data.get("scan_api_id")
    if not api_id:
        await update.effective_message.reply_text("❌ api_id پیدا نشد. از اول شروع کن.", reply_markup=back_only())
        return WAIT_SCAN_API_ID
    settings.tg_api_id = int(api_id)
    settings.tg_api_hash = api_hash
    await db.set_setting("tg_api_id", int(api_id))
    await db.set_setting("tg_api_hash", api_hash)
    context.user_data.pop("scan_api_id", None)
    await update.effective_message.reply_text(
        "✅ api_id و api_hash ذخیره شد.\n📱 حالا شماره تلفن اکانت اسکنر رو بفرست:\nمثال: `+989123456789`",
        parse_mode="Markdown",
        reply_markup=back_only(),
    )
    return WAIT_SCAN_PHONE


@admin_only
async def received_scan_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.effective_message.text.strip()
    if not phone.startswith("+"):
        await update.effective_message.reply_text(
            "❌ شماره باید با + شروع شه (فرمت بین‌المللی). دوباره بفرست:",
            reply_markup=back_only(),
        )
        return WAIT_SCAN_PHONE
    context.user_data["scan_phone"] = phone
    ok, msg = await scanner.request_code(phone)
    await update.effective_message.reply_text(msg, reply_markup=back_only())
    if ok:
        return WAIT_SCAN_CODE
    return WAIT_SCAN_PHONE


@admin_only
async def received_scan_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.effective_message.text.strip()
    phone = context.user_data.get("scan_phone", "")
    ok, msg, need_pw = await scanner.submit_code(phone, code)
    if need_pw:
        await update.effective_message.reply_text(msg, reply_markup=back_only())
        return WAIT_SCAN_PASSWORD
    await update.effective_message.reply_text(msg, reply_markup=scanner_menu(ok))
    context.user_data.pop("scan_phone", None)
    return ConversationHandler.END if ok else WAIT_SCAN_CODE


@admin_only
async def received_scan_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pw = update.effective_message.text.strip()
    ok, msg = await scanner.submit_password(pw)
    await update.effective_message.reply_text(msg, reply_markup=scanner_menu(ok))
    return ConversationHandler.END if ok else WAIT_SCAN_PASSWORD


@admin_only
async def received_scan_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    peer = update.effective_message.text.strip()

    # ثبت کانال (اگه resolve بشه)
    try:
        entity = await scanner._resolve_entity(peer, None)
        ch_id = str(entity.id)
        await db.add_channel(
            chat_id=ch_id,
            title=getattr(entity, "title", "") or peer,
            username=getattr(entity, "username", "") or "",
        )
        # از این به بعد با آیدی عددی اسکن کن (مطمئن‌تر)
        peer = ch_id
    except Exception:
        pass

    status = await update.effective_message.reply_text("⏳ در حال اسکن کانال... (ممکنه چند دقیقه طول بکشه)")

    async def progress(value):
        try:
            if isinstance(value, int):
                await status.edit_text(f"⏳ در حال اسکن... {value} پیام بررسی شد")
            else:
                await status.edit_text(str(value))
        except Exception:
            pass

    ok, msg, nfiles = await scanner.scan_channel(peer, progress_cb=progress)
    if not ok:
        await status.edit_text(msg, reply_markup=scanner_menu(await scanner.is_logged_in()))
        return ConversationHandler.END

    # تشخیص تکراری (با resolve مطمئن)
    try:
        entity = await scanner._resolve_entity(peer, None)
        channel_id = str(entity.id)
        found = await scanner.find_duplicates(channel_id)
        try:
            await db.save_scan_result(channel_id, nfiles, found)
        except Exception:
            pass
    except Exception as e:
        await status.edit_text(f"{msg}\n\n❌ خطا در تشخیص تکراری: {str(e)[:100]}", reply_markup=back_only())
        return ConversationHandler.END

    await show_duplicate_report(update, context, status, msg, channel_id, found)
    return ConversationHandler.END


async def show_duplicate_report(update, context, status, msg, channel_id, found):
    """نمایش گزارش تکراری‌ها (قطعی + مشکوک + دیباگ اگه هیچی نبود)"""
    sure_groups = found.get("sure") or []
    suspect_groups = found.get("suspect") or []

    if not sure_groups and not suspect_groups:
        debug = found.get("debug") or ""
        text_out = f"{msg}\n\n🎉 هیچ فایل تکراری پیدا نشد!{_esc(debug)}"
        try:
            await status.edit_text(text_out, reply_markup=scanner_menu(True))
        except Exception:
            try:
                await status.edit_text(text_out, reply_markup=scanner_menu(True))
            except Exception:
                pass
        return

    # ذخیره برای دکمه‌ها
    context.user_data["scan_groups_channel"] = channel_id
    context.user_data["scan_groups"] = {"sure": sure_groups, "suspect": suspect_groups}
    # ---------- گزارش ساده و یکپارچه ----------
    lines = [f"{msg}", ""]

    total_groups = len(sure_groups) + len(suspect_groups)
    total_dups = sum(len(g["dups"]) for g in sure_groups) + sum(len(g["dups"]) for g in suspect_groups)

    if sure_groups:
        lines.append(f"✅ {len(sure_groups)} گروه تکراری قطعی (هم‌حجم + هم‌مدت):")
        lines.append("")
        for i, g in enumerate(sure_groups[:10], 1):
            keep = g["keep"]
            name = _esc(keep["filename"] or "🎬 بدون اسم")
            size_mb = keep["size"] / (1024 * 1024)
            dur = keep["duration"] or 0
            dur_str = f"{int(dur // 60)}:{int(dur % 60):02d}" if dur > 0 else ""
            lines.append(f"{i}. {name} | {size_mb:.0f}MB" + (f" | {dur_str}" if dur_str else "") + f" × {len(g['items'])} نسخه")
        lines.append("")

    if suspect_groups:
        lines.append(f"⚠️ {len(suspect_groups)} گروه احتمالی (حجم/مدت نزدیک — چک کن):")
        lines.append("")
        for i, g in enumerate(suspect_groups[:5], 1):
            keep = g["keep"]
            name = _esc(keep["filename"] or "🎬 بدون اسم")
            size_mb = keep["size"] / (1024 * 1024)
            dur = keep["duration"] or 0
            dur_str = f"{int(dur // 60)}:{int(dur % 60):02d}" if dur > 0 else ""
            lines.append(f"⚠️ {i}. {name} | {size_mb:.0f}MB" + (f" | {dur_str}" if dur_str else "") + f" × {len(g['items'])} نسخه")
        lines.append("")

    if total_groups == 0:
        lines.append("هیچ گروه تکراری‌ای نیست.")
    else:
        lines.append(f"مجموعاً {total_dups} فایل اضافه (تکراری) هست که می‌تونی حذف کنی.")

    # ---------- دکمه‌های ساده ----------
    kb_rows = []
    for i in range(min(total_groups, 8)):
        if i < len(sure_groups):
            kb_rows.append([InlineKeyboardButton(f"👁 ببین گروه {i+1}", callback_data=f"scan_view_sure_{i}")])
        else:
            si = i - len(sure_groups)
            kb_rows.append([InlineKeyboardButton(f"👁 ببین گروه {i+1} (احتمالی)", callback_data=f"scan_view_suspect_{si}")])
    if total_groups > 8:
        kb_rows.append([InlineKeyboardButton("🔗 لینک همه گروه‌ها", callback_data="scan_link_all")])
    if sure_groups:
        kb_rows.append([InlineKeyboardButton("✅ حذف تکراری‌های قطعی", callback_data="scan_confirm_delete")])
    if suspect_groups:
        kb_rows.append([InlineKeyboardButton("🗑 حذف احتمالی‌ها (⚠️ فقط بعد از چک با 👁)", callback_data="scan_confirm_delete_suspect")])
    kb_rows.append([InlineKeyboardButton("❌ انصراف", callback_data="scan_cancel_delete")])
    kb = InlineKeyboardMarkup(kb_rows)

    text_out = "\n".join(lines)
    try:
        await status.edit_text(text_out, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        try:
            await status.edit_text(text_out, reply_markup=kb)
        except Exception:
            pass



async def do_scan_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str, only_sure: bool = True):
    query = update.callback_query
    status = await query.edit_message_text("⏳ در حال حذف تکراری‌ها...")
    found = await scanner.find_duplicates(channel_id)
    groups = (found.get("sure") or []) if only_sure else ((found.get("sure") or []) + (found.get("suspect") or []))
    if not groups:
        await status.edit_text("چیزی برای حذف نیست.", reply_markup=scanner_menu(True))
        return
    ok, msg, deleted = await scanner.delete_duplicates(channel_id, groups)
    await status.edit_text(msg, reply_markup=scanner_menu(True))


# -------------------- فوروارد → اسکن --------------------

async def _start_scan_for_peer(update: Update, context: ContextTypes.DEFAULT_TYPE, peer: str, hints: dict = None):
    """شروع اسکن کانال با پیام پیشرفت + گزارش تکراری‌ها (با قفل ضد اجرای همزمان)"""
    hints = hints or {}
    # قفل: اگه همین الان اسکنی در حال اجراست، شروع نکن
    if context.bot_data.get("scan_running"):
        await update.effective_message.reply_text(
            "⏳ الان یه اسکن دیگه در حال اجراست — کمی صبر کن و دوباره فوروارد کن.",
            reply_markup=back_only(),
        )
        return

    # چک لاگین
    if not await scanner.is_logged_in():
        await update.effective_message.reply_text(
            "⚠️ اول باید توی بخش «📡 اسکن کانال» با اکانت اسکنر لاگین کنی.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 ورود اسکنر", callback_data="scan_login")],
            ]),
        )
        return

    title = hints.get("title") or peer
    status = await update.effective_message.reply_text(
        f"⏳ در حال اسکن کانال «{_esc(title)}»...\n(ممکنه چند دقیقه طول بکشه)",
        parse_mode="Markdown",
    )

    context.bot_data["scan_running"] = True
    try:
        async def progress(value):
            try:
                if isinstance(value, int):
                    await status.edit_text(f"⏳ در حال اسکن... {value} پیام بررسی شد")
                else:
                    await status.edit_text(str(value))
            except Exception:
                pass

        ok, msg, nfiles = await scanner.scan_channel(peer, progress_cb=progress, hints=hints)
        if not ok:
            await status.edit_text(msg, reply_markup=scanner_menu(True))
            return

        # تشخیص تکراری (با resolve مطمئن مثل خود اسکن)
        try:
            entity = await scanner._resolve_entity(peer, hints)
            channel_id = str(entity.id)
            found = await scanner.find_duplicates(channel_id)
            # 📌 ذخیره نتیجه — دیگه لازم نیست دوباره اسکن کنی
            try:
                await db.save_scan_result(channel_id, nfiles, found)
            except Exception:
                pass
        except Exception as e:
            await status.edit_text(f"{msg}\n\n❌ خطا در تشخیص تکراری: {str(e)[:100]}", reply_markup=back_only())
            return

        await show_duplicate_report(update, context, status, msg, channel_id, found)
    finally:
        context.bot_data["scan_running"] = False


# -------------------- فوروارد → اسکن --------------------

@admin_only
async def handle_forward_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فوروارد ویدیو/عکس/صدا بدون کپشن — دکمه اسکن کانال مبدأ"""
    message = update.effective_message
    if not message.forward_origin:
        return

    source = _forward_source_detail(message)
    if not source:
        await message.reply_text(
            "🔍 این فوروارد منبع قابل‌تشخیصی نداره. اگه می‌خوای کانال اسکن بشه،"
            " آیدی عددی یا یوزرنیم کانال رو مستقیم بفرست.",
            reply_markup=back_only(),
        )
        return

    context.user_data["last_forward_peer"] = source
    hints = _forward_hints(message)
    context.user_data["last_forward_hints"] = hints

    # 📌 ثبت خودکار کانال توی لیست (برای اسکن‌های بعدی بدون فوروارد)
    try:
        ch_id = str(hints.get("id") or source.lstrip("@") or source)
        await db.add_channel(
            chat_id=ch_id,
            title=hints.get("title") or source,
            username=hints.get("username") or "",
        )
    except Exception:
        pass

    # 🤖 خودکار: مستقیم اسکن شروع می‌شه (اگه لاگین باشه)
    await _start_scan_for_peer(update, context, source, hints)


@admin_only
async def handle_forward_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فوروارد متن از کانال — دکمه اسکن (فقط فوروارد؛ متن معمولی بی‌صدا رد می‌شه)"""
    message = update.effective_message
    if not message.forward_origin:
        return
    source = _forward_source_detail(message)
    if not source:
        return
    context.user_data["last_forward_peer"] = source
    hints = _forward_hints(message)
    context.user_data["last_forward_hints"] = hints

    # 📌 ثبت خودکار کانال توی لیست
    try:
        ch_id = str(hints.get("id") or source.lstrip("@") or source)
        await db.add_channel(
            chat_id=ch_id,
            title=hints.get("title") or source,
            username=hints.get("username") or "",
        )
    except Exception:
        pass

    # 🤖 خودکار: مستقیم اسکن شروع می‌شه (اگه لاگین باشه)
    await _start_scan_for_peer(update, context, source, hints)


# -------------------- آپدیت ZIP --------------------

async def handle_update_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت ZIP آپدیت از مدیر اصلی و push به گیت‌هاب"""
    message = update.effective_message
    user = update.effective_user
    if user is None or not settings.is_owner(user.id):
        return

    doc = message.document
    if not doc or not (doc.file_name or "").lower().endswith(".zip"):
        return

    status = await message.reply_text("📦 در حال بررسی فایل ZIP...")
    try:
        tg_file = await doc.get_file()
        data = await tg_file.download_as_bytearray()
    except Exception as e:
        await status.edit_text(f"❌ خطا در دانلود فایل: {e}")
        return

    ok, msg, files = parse_update_zip(bytes(data))
    if not ok:
        await status.edit_text(f"❌ {msg}", reply_markup=back_only())
        return

    names = "\n".join(f"• `{n}`" for n in sorted(files)[:25])
    if len(files) > 25:
        names += f"\n• و {len(files) - 25} فایل دیگر..."
    context.user_data["update_zip_files"] = files
    await status.edit_text(
        f"✅ {msg}\n\n{names}\n\n"
        "این فایل‌ها مستقیم به گیت‌هاب push می‌شن و Railway خودش آپدیت می‌کنه.\n"
        "مطمئنی؟",
        parse_mode="Markdown",
        reply_markup=confirm_keyboard("confirm_update_zip", "back_main"),
    )


# -------------------- خطای سراسری --------------------

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """هر خطایی در هر هندلر بیاد → به مدیر اصلی پیام بده"""
    logger.exception("Handler error: %s", context.error)
    try:
        if settings.owner_id:
            await context.bot.send_message(
                chat_id=settings.owner_id,
                text=(
                    f"⚠️ خطا در ربات:\n`{type(context.error).__name__}: {str(context.error)[:300]}`\n\n"
                    f"این پیام خودکاره تا بدونیم چیزی گیر کرده."
                ),
                parse_mode="Markdown",
            )
    except Exception:
        pass


# -------------------- ثبت هندلرها --------------------

# Conversation states
(
    WAIT_ADMIN_ID,
    WAIT_GITHUB_TOKEN,
    WAIT_GITHUB_REPO,
    WAIT_SCAN_API_ID,
    WAIT_SCAN_API_HASH,
    WAIT_SCAN_PHONE,
    WAIT_SCAN_CODE,
    WAIT_SCAN_PASSWORD,
    WAIT_SCAN_CHANNEL,
) = range(9)


def setup_handlers(application: Application):
    application.add_error_handler(global_error_handler)
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("cancel", cancel))

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_router)],
        states={
            WAIT_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_admin_id)],
            WAIT_GITHUB_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_github_token)],
            WAIT_GITHUB_REPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_github_repo)],
            WAIT_SCAN_API_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_scan_api_id)],
            WAIT_SCAN_API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_scan_api_hash)],
            WAIT_SCAN_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_scan_phone)],
            WAIT_SCAN_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_scan_code)],
            WAIT_SCAN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_scan_password)],
            WAIT_SCAN_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_scan_channel)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(callback_router)],
        allow_reentry=True,
    )
    application.add_handler(conv)

    application.add_handler(CallbackQueryHandler(callback_router))

    # فوروارد متن از کانال → پیشنهاد اسکن
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_forward_text)
    )

    # فوروارد مدیا بدون کپشن (ویدیو/عکس/صدا) → پیشنهاد اسکن
    application.add_handler(
        MessageHandler(
            (filters.VIDEO | filters.PHOTO | filters.ANIMATION | filters.AUDIO
             | filters.VOICE | filters.VIDEO_NOTE) & ~filters.CAPTION,
            handle_forward_media,
        )
    )

    # فوروارد با کپشن → پیشنهاد اسکن
    application.add_handler(
        MessageHandler(filters.CAPTION, handle_forward_media)
    )

    # ZIP آپدیت (فقط owner)
    application.add_handler(
        MessageHandler(filters.Document.FileExtension("zip"), handle_update_zip)
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "لغو شد.",
        reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
    )
    return ConversationHandler.END
