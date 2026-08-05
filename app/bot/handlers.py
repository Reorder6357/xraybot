"""
هندلرهای اصلی ربات
"""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
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
    main_menu, channels_menu, subs_menu, tag_menu,
    schedule_menu, settings_menu, github_menu, back_only, confirm_keyboard,
    queue_menu, personal_menu, extract_actions_keyboard, cancel_run_keyboard, fixed_menu,
    scanner_menu,
)
from app.services.github_deploy import github_deployer
from app.services.config_extractor import (
    extract_from_message_text,
    extract_from_document,
    parse_basic_info,
)
from app.services.runner import run_full_test, _run_lock
from app.services.xray_tester import test_batch, select_top, TestResult
from app.services.scheduler import reload_schedule
from app.services.collector import collect_from_subscriptions, collect_all
from app.services.channel_scanner import scanner
from pathlib import Path

logger = logging.getLogger(__name__)


def _esc(s: str) -> str:
    """فرار از کاراکترهای خاص Markdown (برای متن‌های کاربر/کانال که نباید کرش کنه)"""
    if not s:
        return ""
    for ch in ("_", "*", "`", "[", "]"):
        s = s.replace(ch, "\\" + ch)
    return s


NON_TESTABLE_SCHEMES = {"hysteria2", "hy2", "tuic", "ssr", "wireguard", "hysteria", "kcp"}


def _forward_source_detail(message) -> str:
    """نام کانال/کاربر مبدأ فوروارد رو با forward_origin (ساختار جدید PTB v21) برمی‌گردونه.
    (فیلدهای قدیمی forward_date/forward_from/forward_from_chat توی v21 حذف شدن)"""
    origin = message.forward_origin
    if not origin:
        return ""
    try:
        if origin.type == "channel":
            return origin.chat.title or str(origin.chat.id)
        if origin.type == "chat":
            return origin.sender_chat.title or str(origin.sender_chat.id)
        if origin.type == "user":
            return origin.sender_user.full_name or str(origin.sender_user.id)
        if origin.type == "hidden_user":
            return origin.sender_user_name or "کاربر ناشناس"
    except Exception:
        pass
    return ""


def _count_non_testable(text: str) -> int:
    """تعداد لینک‌های پروتکل‌هایی که قابل تست نیستن (فقط برای گزارش به کاربر)"""
    count = 0
    lower = text.lower()
    for scheme in NON_TESTABLE_SCHEMES:
        count += lower.count(scheme + "://")
    return count


async def _ensure_fixed_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دکمه‌های ثابت پایین چت: اولین بار که کاربر با ربات حرف می‌زنه میاد و
    بعدش همیشه پایین چت می‌مونه (تا وقتی عوضش نکنیم). یک بار در هر چت فرستاده می‌شه."""
    try:
        if context.chat_data.get("fixed_shown"):
            return
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⌨️ دکمه‌های سریع:",
            reply_markup=fixed_menu(),
        )
        context.chat_data["fixed_shown"] = True
    except Exception:
        pass


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


# Conversation states
(
    WAIT_CHANNEL,
    WAIT_SUB,
    WAIT_TAG,
    WAIT_SCHEDULE_TIMES,
    WAIT_GITHUB_TOKEN,
    WAIT_GITHUB_REPO,
    WAIT_ADMIN_ID,
    WAIT_REMOVE_CHANNEL,
    WAIT_REMOVE_SUB,
    WAIT_SCAN_API_ID,
    WAIT_SCAN_API_HASH,
    WAIT_SCAN_PHONE,
    WAIT_SCAN_CODE,
    WAIT_SCAN_PASSWORD,
    WAIT_SCAN_CHANNEL,
) = range(15)


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None:
            return  # پیام‌های کانال/سرویس کاربر ندارن
        user_id = user.id
        if not settings.is_owner(user_id):
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
            return  # پیام‌های کانال/سرویس کاربر ندارن
        user_id = user.id
        if not settings.is_admin(user_id):
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
    text = (
        f"سلام {user.first_name} 👋\n\n"
        "ربات مدیریت و تست کانفیگ‌های Xray آماده‌ست.\n"
        "از دکمه‌های زیر استفاده کن:"
    )
    await update.effective_message.reply_text(
        text, reply_markup=main_menu(is_owner)
    )
    await _ensure_fixed_keyboard(update, context)


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

    if data == "menu_channels":
        await query.edit_message_text("مدیریت کانال‌ها:", reply_markup=channels_menu())
        return

    if data == "menu_subs":
        await query.edit_message_text("مدیریت سابسکریپشن‌ها:", reply_markup=subs_menu())
        return

    if data == "menu_tag":
        tag_info = await db.get_channel_tag()
        await query.edit_message_text(
            "تنظیم تگ کانال (مثلاً @Wpnfa):\n"
            "وقتی روشن باشه به انتهای ریمارک اضافه می‌شه.",
            reply_markup=tag_menu(tag_info["enabled"], tag_info["tag"]),
        )
        return

    if data == "menu_schedule":
        sched = await db.get_schedule()
        await query.edit_message_text(
            "زمان‌بندی اجرای خودکار:",
            reply_markup=schedule_menu(sched["enabled"], sched["times"]),
        )
        return

    if data == "menu_settings":
        await query.edit_message_text("تنظیمات:", reply_markup=settings_menu(is_owner))
        return

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

    # ---- Channels ----
    if data == "add_channel":
        await query.edit_message_text(
            "آیدی عددی یا یوزرنیم کانال عمومی رو بفرست:\n"
            "مثال:\n`@mychannel`\nیا\n`-1001234567890`",
            parse_mode="Markdown",
            reply_markup=back_only(),
        )
        return WAIT_CHANNEL

    if data == "list_channels":
        rows = await db.list_channels()
        if not rows:
            text = "هیچ کانالی ثبت نشده."
        else:
            lines = []
            for r in rows:
                uname = f"@{r['username']}" if r["username"] else r["chat_id"]
                lines.append(f"• `{r['title'] or uname}` (`{r['chat_id']}`)")
            text = ("لیست کانال‌ها (برای حذف از دکمه 🗑 استفاده کن):\n\n"
                    + "\n".join(lines))
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=channels_menu())
        return

    if data == "remove_channel":
        await query.edit_message_text(
            "آیدی یا یوزرنیم کانالی که می‌خوای حذف بشه رو بفرست:\n"
            "مثال:\n`-1001234567890`\nیا\n`@mychannel`",
            parse_mode="Markdown",
            reply_markup=back_only(),
        )
        return WAIT_REMOVE_CHANNEL

    if data == "remove_sub":
        await query.edit_message_text(
            "لینک سابی که می‌خوای حذف بشه رو بفرست (یا قسمتی از لینک):",
            reply_markup=back_only(),
        )
        return WAIT_REMOVE_SUB

    if data == "manage_admins":
        lines = [f"• `{a}`" for a in settings.admin_ids] or ["• (فعلاً ادمین اضافه‌ای نیست)"]
        await query.edit_message_text(
            "مدیریت ادمین‌ها (غیر از مدیر اصلی):\n\n"
            + "\n".join(lines)
            + "\n\nآیدی عددی ادمین جدید رو بفرست تا اضافه بشه:",
            parse_mode="Markdown",
            reply_markup=back_only(),
        )
        return WAIT_ADMIN_ID

    # ---- Subs ----
    if data == "add_sub":
        await query.edit_message_text(
            "لینک سابسکریپشن رو بفرست:",
            reply_markup=back_only(),
        )
        return WAIT_SUB

    if data == "list_subs":
        rows = await db.list_subscriptions()
        if not rows:
            text = "هیچ سابی ثبت نشده."
        else:
            lines = [f"• {r['name'] or r['url'][:60]}" for r in rows]
            text = "لیست ساب‌ها:\n\n" + "\n".join(lines)
        await query.edit_message_text(text, reply_markup=subs_menu())
        return

    if data == "refresh_subs":
        await query.edit_message_text("🔄 در حال رفرش ساب‌ها...")
        try:
            new_count = await collect_from_subscriptions()
            pending = await db.count_pending()
            await query.edit_message_text(
                f"✅ رفرش تموم شد\n"
                f"• کانفیگ جدید: {new_count}\n"
                f"• کل صف تست: {pending}",
                reply_markup=subs_menu(),
            )
        except Exception as e:
            await query.edit_message_text(f"❌ خطا: {e}", reply_markup=subs_menu())
        return

    # ---- Tag ----
    if data == "toggle_tag":
        info = await db.get_channel_tag()
        new_state = not info["enabled"]
        await db.set_channel_tag(info["tag"], new_state)
        await query.edit_message_text(
            "تنظیم تگ کانال:",
            reply_markup=tag_menu(new_state, info["tag"]),
        )
        return

    if data == "set_tag":
        await query.edit_message_text(
            "تگ کانال رو بفرست (مثلاً `@Wpnfa`):",
            parse_mode="Markdown",
            reply_markup=back_only(),
        )
        return WAIT_TAG

    # ---- Schedule ----
    if data == "toggle_schedule":
        sched = await db.get_schedule()
        new_state = not sched["enabled"]
        await db.set_schedule(new_state, sched["times"])
        await reload_schedule()
        await query.edit_message_text(
            "زمان‌بندی:",
            reply_markup=schedule_menu(new_state, sched["times"]),
        )
        return

    if data == "set_schedule_times":
        await query.edit_message_text(
            "ساعت‌های اجرا رو به این صورت بفرست (هر خط یک ساعت):\n"
            "`08:00`\n`14:30`\n`22:00`",
            parse_mode="Markdown",
            reply_markup=back_only(),
        )
        return WAIT_SCHEDULE_TIMES

    # ---- GitHub (owner only) ----
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
            "ربات فایل‌ها رو به گیت‌هاب push می‌کنه و Railway خودش دیپلوی می‌کنه.\n"
            "بعد از ارسال، قبل از push ازت تأیید می‌گیره.",
            parse_mode="Markdown",
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

    # ---- Run now ----
    if data == "run_now":
        status = await query.edit_message_text("🔄 رفرش ساب‌ها و آماده‌سازی...")
        try:
            sub_new = await collect_from_subscriptions()
        except Exception:
            sub_new = 0

        pending = await db.count_pending()
        if pending == 0:
            await status.edit_text(
                "📦 صف خالیه. اول چند تا کانفیگ بفرست یا ساب اضافه کن.",
                reply_markup=back_only(),
            )
            return

        await status.edit_text(
            f"⏳ شروع تست...\n"
            f"• کانفیگ جدید از ساب: {sub_new}\n"
            f"• تعداد در صف: {pending}\n"
            f"• همزمانی: {settings.test_concurrency}\n"
            f"این کار ممکنه چند دقیقه طول بکشه."
        )

        cancel_event = asyncio.Event()
        context.bot_data["cancel_event"] = cancel_event

        async def progress(done, total, last_res):
            try:
                mark = "✅" if last_res.success else "❌"
                await status.edit_text(
                    f"⏳ در حال تست...\n"
                    f"• پیشرفت: {done}/{total}\n"
                    f"• آخرین: {mark} {last_res.address or '?'} "
                    f"({int(last_res.latency_ms) if last_res.success else last_res.error[:30]})\n"
                    f"برای توقف، دکمه پایین رو بزن.",
                    reply_markup=cancel_run_keyboard(),
                )
            except Exception:
                pass

        try:
            run = await run_full_test(progress_callback=progress, cancel_event=cancel_event)
        finally:
            context.bot_data.pop("cancel_event", None)

        if run.cancelled and not run.top:
            await status.edit_text(
                f"⛔ تست متوقف شد — نتیجه‌ای نیامد.\n⏱ زمان: {run.duration_sec}s",
                reply_markup=back_only(),
            )
            return

        if run.error and not run.top:
            await status.edit_text(
                f"❌ {run.error}\n⏱ زمان: {run.duration_sec}s",
                reply_markup=back_only(),
            )
            return

        head = "⛔ متوقف شد — نتیجه تا این لحظه:" if run.cancelled else "✅ تست تموم شد"
        summary = (
            f"{head}\n\n• ورودی: {run.total_input}\n• رد شده (تکراری ۲۴س): {run.skipped_recent}\n• تست‌شده: {run.tested}\n• سالم: {run.success}\n• ناموفق: {run.failed}\n• انتخاب‌شده (top): {len(run.top)}\n⏱ زمان: {run.duration_sec}s"
        )
        await status.edit_text(summary)


        if run.output_file and run.output_file.exists():
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=run.output_file.open("rb"),
                filename=run.output_file.name,
                caption=f"📄 {len(run.output_lines)} کانفیگ سالم",
            )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="منوی اصلی:",
            reply_markup=main_menu(is_owner),
        )
        return

    # ---- Queue section ----
    if data == "menu_queue":
        count = await db.count_pending()
        await query.edit_message_text(
            f"📦 مدیریت صف تست\nدر صف: `{count}` کانفیگ",
            parse_mode="Markdown",
            reply_markup=queue_menu(),
        )
        return

    if data == "view_queue":
        rows = await db.get_pending_configs(limit=10)
        count = await db.count_pending()
        if not rows:
            text = "📦 صف خالیه."
        else:
            lines = [f"📦 کل صف: {count} | نمایش ۱۰ تای اول:", ""]
            for r in rows:
                lines.append(f"• `{r['protocol'] or '?'}` → `{r['address'] or '?'}` ({r['source']})")
            text = "\n".join(lines)
        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=queue_menu(),
        )
        return

    if data == "clear_queue":
        await query.edit_message_text(
            "🗑 کل صف تست پاک بشه؟ (کانفیگ‌های سالمِ ارسال‌شده توی تاریخچه می‌مونن)",
            reply_markup=confirm_keyboard("confirm_clear_queue", "menu_queue"),
        )
        return

    if data == "confirm_clear_queue":
        await db.clear_pending()
        await query.edit_message_text(
            "🗑 صف تست پاک شد.",
            reply_markup=queue_menu(),
        )
        return

    # ---- Personal section ----
    if data == "menu_personal":
        count = await db.count_personal()
        await query.edit_message_text(
            f"👤 بخش شخصی\nکانفیگ ذخیره‌شده: `{count}`",
            parse_mode="Markdown",
            reply_markup=personal_menu(),
        )
        return

    if data == "personal_list":
        rows = await db.list_personal(limit=20)
        count = await db.count_personal()
        if not rows:
            text = "👤 شخصی خالیه. بعد از فرستادن کانفیگ، دکمه «ذخیره در شخصی» رو بزن."
        else:
            lines = [f"👤 {count} کانفیگ (۲۰ تای اول):", ""]
            for i, r in enumerate(rows, 1):
                lines.append(f"{i}. `{r['config_line'][:70]}...`")
            text = "\n".join(lines)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=personal_menu())
        return

    if data == "personal_test":
        rows = await db.list_personal()
        if not rows:
            await query.edit_message_text("👤 شخصی خالیه.", reply_markup=personal_menu())
            return
        links = [r["config_line"] for r in rows]
        await query.edit_message_text(f"👤 تست {len(links)} کانفیگ شخصی...")
        await _run_manual_test(update, context, links, title="تست بخش شخصی")
        return

    if data == "personal_clear":
        await query.edit_message_text(
            "🗑 کل بخش شخصی پاک بشه؟",
            reply_markup=confirm_keyboard("confirm_personal_clear", "menu_personal"),
        )
        return

    if data == "confirm_personal_clear":
        await db.clear_personal()
        await query.edit_message_text("🗑 بخش شخصی پاک شد.", reply_markup=personal_menu())
        return

    # ---- Actions on extracted configs ----
    if data == "act_test_now":
        links = context.user_data.pop("last_extracted_links", None)
        if not links:
            await query.answer("اول یه کانفیگ بفرست", show_alert=True)
            return
        await _run_manual_test(update, context, links, title="تست کانفیگ‌های ارسال‌شده")
        return

    if data == "act_save_personal":
        links = (
            context.user_data.pop("last_extracted_links", None)
            or context.user_data.pop("last_success_links", None)
        )
        if not links:
            await query.answer("چیزی برای ذخیره نیست", show_alert=True)
            return
        new_count, dup_count = await db.add_personal_configs(links)
        from app.services.config_extractor import config_hash
        await db.delete_pending_by_hashes([config_hash(l) for l in links])
        await query.edit_message_text(
            f"👤 ذخیره شد در شخصی: {new_count} جدید، {dup_count} تکراری\n(از صف تست هم حذف شد)",
            reply_markup=personal_menu(),
        )
        return

    if data == "act_remove_queue":
        links = context.user_data.pop("last_extracted_links", None)
        if not links:
            await query.answer("چیزی برای حذف نیست", show_alert=True)
            return
        from app.services.config_extractor import config_hash
        await db.delete_pending_by_hashes([config_hash(l) for l in links])
        await query.edit_message_text(
            f"🗑 {len(links)} کانفیگ از صف حذف شد.",
            reply_markup=main_menu(is_owner),
        )
        return

    if data == "act_cancel_run":
        ev = context.bot_data.get("cancel_event")
        if ev:
            ev.set()
            await query.answer("⏹ در حال توقف تست...", show_alert=False)
            try:
                await query.edit_message_text(
                    "⏹ در حال توقف... نتیجه تا این لحظه ارسال می‌شه.",
                    reply_markup=None,
                )
            except Exception:
                pass
        else:
            await query.answer("تستی در حال اجرا نیست")
        return

    # ---- Scanner section ----
    if data == "menu_scanner":
        logged = await scanner.is_logged_in()
        status = "✅ وارد شده‌اید" if logged else "❌ هنوز وارد نشده‌اید"
        await query.edit_message_text(
            f"📡 اسکن کانال برای پیدا کردن فایل‌های تکراری\n\n{status}\n\n"
            f"برای اسکن کامل تاریخچه کانال (اسم فایل، حجم، مدت ویدیو) به یه اکانت تلگرام نیازه.\n"
            f"اول ورود، بعد اسکن.",
            reply_markup=scanner_menu(logged),
        )
        return

    if data == "scan_login":
        if not is_owner:
            await query.answer("⛔ فقط مدیر اصلی", show_alert=True)
            return
        # چک api_id/api_hash
        if not settings.tg_api_id or not settings.tg_api_hash:
            await query.edit_message_text(
                "🔑 اول api_id و api_hash لازمه (از my.telegram.org می‌گیری).\n"
                "api_id رو بفرست:",
                reply_markup=back_only(),
            )
            return WAIT_SCAN_API_ID
        await query.edit_message_text(
            "📱 شماره تلفن اکانت اسکنر رو با فرمت بین‌المللی بفرست:\n"
            "مثال: `+989123456789`",
            parse_mode="Markdown",
            reply_markup=back_only(),
        )
        return WAIT_SCAN_PHONE

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
        await db.clear_scanned_files()
        await query.edit_message_text(
            "🗑 داده‌ی اسکن (فایل‌های ثبت‌شده) پاک شد.",
            reply_markup=scanner_menu(await scanner.is_logged_in()),
        )
        return

    if data == "scan_confirm_delete":
        if not is_owner:
            await query.answer("⛔ فقط مدیر اصلی", show_alert=True)
            return
        channel_id = context.user_data.get("scan_groups_channel", "")
        if not channel_id:
            await query.answer("اول اسکن کن", show_alert=True)
            return
        await do_scan_delete(update, context, channel_id)
        return

    if data == "scan_cancel_delete":
        await query.edit_message_text(
            "🚫 حذف لغو شد.",
            reply_markup=scanner_menu(await scanner.is_logged_in()),
        )
        return

    if data == "last_output":
        out_dir = DATA_DIR / "outputs"
        if not out_dir.exists():
            await query.edit_message_text("هنوز خروجی‌ای تولید نشده.", reply_markup=back_only())
            return
        files = sorted(out_dir.glob("healthy_*.txt"), reverse=True)
        if not files:
            await query.edit_message_text("هنوز خروجی‌ای تولید نشده.", reply_markup=back_only())
            return
        latest = files[0]
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=latest.open("rb"),
            filename=latest.name,
            caption=f"📄 آخرین خروجی: `{latest.name}`",
            parse_mode="Markdown",
        )
        return

    if data == "system_status":
        pending = await db.count_pending()
        text = (
            f"🤖 ربات: فعال\n"
            f"👤 مدیر اصلی: `{settings.owner_id}`\n"
            f"👥 ادمین‌ها: {len(settings.admin_ids)}\n"
            f"📡 کانال‌ها: {len(await db.list_channels())}\n"
            f"🔗 ساب‌ها: {len(await db.list_subscriptions())}\n"
            f"📦 کانفیگ در صف تست: `{pending}`\n"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=settings_menu(is_owner))
        return


# -------------------- Conversation handlers --------------------
async def received_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text.strip()
    chat_id = text
    title = text
    username = ""

    # resolve: سعی می‌کنیم آیدی واقعی کانال رو بگیریم (و ببینیم ربات دسترسی داره یا نه)
    admin_warning = ""
    try:
        chat = await context.bot.get_chat(text)
        if chat.id:
            chat_id = str(chat.id)
            title = chat.title or title
            username = chat.username or ""
        # چک کنیم ربات ادمین/عضو کاناله؟
        bot_member = await context.bot.get_chat_member(chat_id, (await context.bot.get_me()).id)
        status = bot_member.status
        if status not in ("administrator", "creator", "member"):
            admin_warning = (
                f"\n\n⚠️ ربات فعلاً ادمین/عضو این کانال نیست!\n"
                f"برای اینکه پست‌های کانال خودکار جمع بشن، ربات رو توی کانال ادمین کن."
            )
    except Exception:
        # کانال خصوصیه یا دسترسی نیست — ذخیره می‌کنیم ولی هشدار می‌دیم
        admin_warning = (
            f"\n\n⚠️ نتونستم دسترسی ربات به کانال رو چک کنم.\n"
            f"مطمئن شو ربات توی کانال ادمین شده (با حق دیدن پست‌ها)."
        )

    await db.add_channel(chat_id=chat_id, title=title, username=username)
    await update.effective_message.reply_text(
        f"✅ کانال `{_esc(title)}` اضافه شد.\n"
        f"آیدی عددی: `{chat_id}`"
        + admin_warning,
        parse_mode="Markdown",
        reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
    )
    return ConversationHandler.END


async def received_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.effective_message.text.strip()
    await db.add_subscription(url=url)
    await update.effective_message.reply_text(
        f"✅ ساب اضافه شد:\n`{url[:80]}`",
        parse_mode="Markdown",
        reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
    )
    return ConversationHandler.END


async def received_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tag = update.effective_message.text.strip()
    info = await db.get_channel_tag()
    await db.set_channel_tag(tag, info["enabled"])
    await update.effective_message.reply_text(
        f"✅ تگ تنظیم شد: `{tag}`",
        parse_mode="Markdown",
        reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
    )
    return ConversationHandler.END


async def received_schedule_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [l.strip() for l in update.effective_message.text.strip().splitlines() if l.strip()]
    # اعتبارسنجی HH:MM (ساعت ۰-۲۳، دقیقه ۰-۵۹)
    valid = []
    for t in lines:
        if len(t) == 5 and t[2] == ":" and t[:2].isdigit() and t[3:].isdigit():
            h, m = int(t[:2]), int(t[3:])
            if 0 <= h <= 23 and 0 <= m <= 59:
                valid.append(t)
    if not valid:
        await update.effective_message.reply_text("❌ فرمت اشتباه. دوباره تلاش کن.")
        return WAIT_SCHEDULE_TIMES

    sched = await db.get_schedule()
    await db.set_schedule(sched["enabled"], valid)
    await reload_schedule()
    await update.effective_message.reply_text(
        f"✅ ساعت‌ها تنظیم شد:\n" + "\n".join(f"• {t}" for t in valid),
        reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
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


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "لغو شد.",
        reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
    )
    await _ensure_fixed_keyboard(update, context)
    return ConversationHandler.END


# -------------------- تست دستی (تک‌سرور یا گروه کوچک) --------------------

async def _run_manual_test(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    links: list[str],
    title: str = "تست دستی",
):
    """تست سریع چند لینک با دکمه توقف. نتیجه تا لحظه توقف ارسال می‌شه."""
    if _run_lock.locked():
        await update.effective_message.reply_text(
            "⏳ الان یک تست دیگه در حال اجراست؛ چند لحظه صبر کن.",
            reply_markup=back_only(),
        )
        return

    status = await update.effective_message.reply_text(
        f"⏳ {title}: {len(links)} کانفیگ\n"
        f"برای توقف، دکمه پایین رو بزن.",
        reply_markup=cancel_run_keyboard(),
    )

    cancel_event = asyncio.Event()
    context.bot_data["cancel_event"] = cancel_event

    async def progress(done, total, last_res):
        try:
            mark = "✅" if last_res.success else "❌"
            extra = f" ({int(last_res.latency_ms)}ms)" if last_res.success else ""
            await status.edit_text(
                f"⏳ در حال تست...\n• پیشرفت: {done}/{total}\n"
                f"• آخرین: {mark} {last_res.address or '?'}{extra}",
                reply_markup=cancel_run_keyboard(),
            )
        except Exception:
            pass

    try:
        results = await test_batch(
            links,
            concurrency=min(settings.test_concurrency, max(5, len(links))),
            timeout=float(settings.test_timeout),
            progress_callback=progress,
            cancel_event=cancel_event,
        )
    finally:
        context.bot_data.pop("cancel_event", None)

    ok = [r for r in results if r.success]
    cancelled = cancel_event.is_set()

    lines = [f"{'⛔ متوقف شد — نتیجه تا این لحظه:' if cancelled else '✅ تست تموم شد:'}"]
    for r in results[:30]:
        if r.success:
            spd = f" | {int(r.speed_kbps)}KB/s" if r.speed_kbps else ""
            lines.append(f"✅ {r.flag} {r.country_name or r.country_code} | {int(r.latency_ms)}ms{spd} | {r.address}")
        else:
            lines.append(f"❌ {r.address or '?'} — {r.error[:40]}")
    if len(results) > 30:
        lines.append(f"… و {len(results) - 30} مورد دیگر")

    text = "\n".join(lines)
    kb = None
    if ok:
        text += f"\n\n🎯 {len(ok)} مورد از {len(results)} سالم."
        context.user_data["last_success_links"] = [r.link for r in ok]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 ذخیره سالم‌ها در شخصی", callback_data="act_save_personal")],
        ])

    try:
        await status.edit_text(text, reply_markup=kb)
    except Exception:
        await update.effective_message.reply_text(text, reply_markup=kb)

    if ok:
        remark_lines = [r.with_new_remark() for r in ok]
        if len(remark_lines) <= 15:
            await update.effective_message.reply_text(
                "📄 کانفیگ‌های سالم:\n\n" + "\n".join(remark_lines),
                reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
            )
        else:
            import tempfile
            fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="healthy_")
            with open(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(remark_lines) + "\n")
            await update.effective_message.reply_document(
                document=open(tmp, "rb"),
                filename="healthy_manual.txt",
                caption=f"📄 {len(remark_lines)} کانفیگ سالم",
            )
    else:
        await update.effective_message.reply_text(
            "منوی اصلی:", reply_markup=main_menu(settings.is_owner(update.effective_user.id))
        )
    await _ensure_fixed_keyboard(update, context)


# -------------------- استخراج کانفیگ از پیام / فایل / فوروارد --------------------

@admin_only
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    پیام متنی معمولی یا فوروارد شده.
    اگر داخل Conversation بودیم، این هندلر صدا زده نمی‌شه (اولویت با ConversationHandler است).
    """
    message = update.effective_message
    text = message.text or message.caption or ""

    if not text.strip():
        return

    # تشخیص منبع
    source = "forward" if message.forward_origin else "message"
    source_detail = _forward_source_detail(message)

    links = extract_from_message_text(text)

    if not links:
        # اگه لینکی پیدا نشد، هیچی نمی‌گیم تا ربات شلوغ نشه
        # (مگر اینکه کاربر صریحاً چیزی فرستاده باشه که به نظر کانفیگ بیاد)
        if any(x in text.lower() for x in ("vless://", "vmess://", "trojan://", "ss://")):
            await message.reply_text("❌ هیچ کانفیگ معتبری پیدا نشد.")
        return

    new_count, dup_count = await db.add_pending_configs(
        links, source=source, source_detail=source_detail
    )
    total_pending = await db.count_pending()

    # لینک‌ها رو برای دکمه‌های اکشن نگه می‌داریم
    context.user_data["last_extracted_links"] = links

    skipped = _count_non_testable(text)
    lines = [
        f"✅ استخراج انجام شد",
        f"• پیدا شده: {len(links)}",
        f"• جدید: {new_count}",
        f"• تکراری: {dup_count}",
        f"• کل موجود در صف تست: {total_pending}",
    ]
    if skipped:
        lines.append(f"⚠️ {skipped} تا پروتکل غیرقابل‌تست (hysteria2/tuic/...) حذف شد")
    if source == "forward" and source_detail:
        lines.append(f"• منبع: فوروارد از {_esc(source_detail)}")

    # نمایش چند تا نمونه
    samples = links[:3]
    if samples:
        lines.append("\nنمونه:")
        for s in samples:
            info = parse_basic_info(s)
            proto = info.get("protocol") or "?"
            addr = info.get("address") or "?"
            lines.append(f"• `{_esc(proto)}` → `{_esc(addr)}`")

    lines.append("\n🎛 با دکمه‌های زیر انتخاب کن:")

    text_out = "\n".join(lines)
    try:
        await message.reply_text(text_out, parse_mode="Markdown", reply_markup=extract_actions_keyboard())
    except Exception:
        # اگه مارک‌داون خطا داد، بدون parse_mode بفرست که حتماً جواب برسه
        await message.reply_text(text_out, reply_markup=extract_actions_keyboard())


async def handle_update_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت ZIP آپدیت از مدیر اصلی و push به گیت‌هاب"""
    message = update.effective_message
    user = update.effective_user
    if user is None or not settings.is_owner(user.id):
        return  # فقط مدیر اصلی؛ بقیه با هندلرهای بعدی پردازش می‌شن

    doc = message.document
    if not doc or not (doc.file_name or "").lower().endswith(".zip"):
        return  # هندلرهای بعدی ادامه بدن

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


@admin_only
async def handle_media_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فوروارد/ارسال عکس، ویدیو و... که کانفیگ توی کپشنشونه.
    (بیشتر کانال‌ها کانفیگ رو به‌صورت عکس با کپشن می‌فرستن — این هندلر اون‌ها رو می‌گیره)"""
    message = update.effective_message
    caption = message.caption or ""
    if not caption.strip():
        return

    # اگه فایل/مستند هم هست، بذار هندلر فایل پردازشش کنه (فقط کپشن‌های رسانه)
    if message.document is not None:
        return

    source = "forward" if message.forward_origin else "message"
    source_detail = _forward_source_detail(message)

    links = extract_from_message_text(caption)
    if not links:
        return

    new_count, dup_count = await db.add_pending_configs(
        links, source=source, source_detail=source_detail
    )
    total_pending = await db.count_pending()
    context.user_data["last_extracted_links"] = links

    lines = [
        f"✅ استخراج از کپشن انجام شد",
        f"• پیدا شده: {len(links)}",
        f"• جدید: {new_count}",
        f"• تکراری: {dup_count}",
        f"• کل موجود در صف تست: {total_pending}",
    ]
    if source == "forward" and source_detail:
        lines.append(f"• منبع: فوروارد از {_esc(source_detail)}")

    # نمایش چند تا نمونه
    samples = links[:3]
    if samples:
        lines.append("\nنمونه:")
        for s in samples:
            info = parse_basic_info(s)
            proto = info.get("protocol") or "?"
            addr = info.get("address") or "?"
            lines.append(f"• `{_esc(proto)}` → `{_esc(addr)}`")

    lines.append("\n🎛 با دکمه‌های زیر انتخاب کن:")

    text_out = "\n".join(lines)
    try:
        await message.reply_text(text_out, parse_mode="Markdown", reply_markup=extract_actions_keyboard())
    except Exception:
        await message.reply_text(text_out, reply_markup=extract_actions_keyboard())
    await _ensure_fixed_keyboard(update, context)


@admin_only
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فایل متنی حاوی کانفیگ"""
    message = update.effective_message
    doc = message.document

    if not doc:
        return

    # فقط فایل‌های نسبتاً کوچک و متنی
    file_name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()

    allowed_ext = (".txt", ".json", ".csv", ".list", ".conf", ".yml", ".yaml")
    is_textish = (
        any(file_name.endswith(ext) for ext in allowed_ext)
        or mime.startswith("text/")
        or mime in ("application/json", "application/octet-stream")
        or not file_name  # بعضی وقت‌ها بدون پسوند می‌فرستن
    )

    if not is_textish:
        await message.reply_text(
            "❌ فقط فایل متنی (.txt و مشابه) پشتیبانی می‌شه.",
            reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
        )
        return

    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await message.reply_text("❌ حجم فایل بیشتر از ۵ مگابایته.")
        return

    status_msg = await message.reply_text("⏳ در حال خواندن فایل...")

    try:
        tg_file = await doc.get_file()
        file_bytes = await tg_file.download_as_bytearray()
        links = await extract_from_document(bytes(file_bytes), filename=doc.file_name or "")
    except Exception as e:
        logger.exception("Failed to process document")
        await status_msg.edit_text(f"❌ خطا در خواندن فایل: {e}")
        return

    if not links:
        await status_msg.edit_text("❌ داخل فایل هیچ کانفیگ معتبری پیدا نشد.")
        return

    # کپشن فایل هم اگه لینک داشته باشه اضافه می‌کنیم
    cap_links = extract_from_message_text(message.caption or "")
    all_links = links + [l for l in cap_links if l not in links]

    new_count, dup_count = await db.add_pending_configs(
        all_links,
        source="file",
        source_detail=doc.file_name or "document",
    )
    total_pending = await db.count_pending()

    # لینک‌ها رو برای دکمه‌های اکشن نگه می‌داریم
    context.user_data["last_extracted_links"] = all_links

    text = (
        f"✅ فایل پردازش شد\n"
        f"• نام فایل: `{_esc(doc.file_name or '')}`\n"
        f"• پیدا شده: {len(all_links)}\n"
        f"• جدید: {new_count}\n"
        f"• تکراری: {dup_count}\n"
        f"• کل موجود در صف تست: {total_pending}\n\n"
        f"🎛 با دکمه‌های زیر انتخاب کن:"
    )
    try:
        await status_msg.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=extract_actions_keyboard(),
        )
    except Exception:
        await status_msg.edit_text(text, reply_markup=extract_actions_keyboard())
    await _ensure_fixed_keyboard(update, context)


async def received_remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text.strip()
    rows = await db.list_channels(only_active=False)
    found = None
    for r in rows:
        uname = r["username"] or ""
        if str(r["chat_id"]) == text or uname == text.lstrip("@") or f"@{uname}" == text:
            found = r
            break
    if not found:
        await update.effective_message.reply_text(
            "❌ کانالی با این مشخصات پیدا نشد.",
            reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
        )
        return ConversationHandler.END
    await db.remove_channel(str(found["chat_id"]))
    await update.effective_message.reply_text(
        f"✅ کانال `{found['title'] or found['chat_id']}` حذف شد.",
        parse_mode="Markdown",
        reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
    )
    return ConversationHandler.END


async def received_remove_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text.strip()
    rows = await db.list_subscriptions(only_active=False)
    found = None
    for r in rows:
        if r["url"] == text or text in r["url"]:
            found = r
            break
    if not found:
        await update.effective_message.reply_text(
            "❌ سابی با این مشخصات پیدا نشد.",
            reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
        )
        return ConversationHandler.END
    await db.remove_subscription(found["url"])
    await update.effective_message.reply_text(
        "✅ ساب حذف شد.",
        reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
    )
    return ConversationHandler.END


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


@admin_only
async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اجرای تست کل صف (معادل دکمه اجرای دستی)"""
    msg = update.effective_message
    status = await msg.reply_text("🔄 آماده‌سازی...", reply_markup=fixed_menu())
    try:
        sub_new = await collect_from_subscriptions()
    except Exception:
        sub_new = 0

    pending = await db.count_pending()
    if pending == 0:
        await status.edit_text(
            "📦 صف خالیه. اول چند تا کانفیگ بفرست یا ساب اضافه کن.",
            reply_markup=back_only(),
        )
        return

    await status.edit_text(
        f"⏳ شروع تست...\n"
        f"• کانفیگ جدید از ساب: {sub_new}\n"
        f"• تعداد در صف: {pending}\n"
        f"این کار ممکنه چند دقیقه طول بکشه."
    )

    cancel_event = asyncio.Event()
    context.bot_data["cancel_event"] = cancel_event

    async def progress(done, total, last_res):
        try:
            mark = "✅" if last_res.success else "❌"
            await status.edit_text(
                f"⏳ در حال تست...\n• پیشرفت: {done}/{total}\n"
                f"• آخرین: {mark} {last_res.address or '?'} "
                f"({int(last_res.latency_ms) if last_res.success else last_res.error[:30]})\n"
                f"برای توقف، دکمه پایین رو بزن.",
                reply_markup=cancel_run_keyboard(),
            )
        except Exception:
            pass

    try:
        run = await run_full_test(progress_callback=progress, cancel_event=cancel_event)
    finally:
        context.bot_data.pop("cancel_event", None)

    if run.cancelled and not run.top:
        await status.edit_text(
            f"⛔ تست متوقف شد — نتیجه‌ای نیامد.\n⏱ زمان: {run.duration_sec}s",
            reply_markup=back_only(),
        )
        return
    if run.error and not run.top:
        await status.edit_text(f"❌ {run.error}\n⏱ زمان: {run.duration_sec}s", reply_markup=back_only())
        return

    head = "⛔ متوقف شد — نتیجه تا این لحظه:" if run.cancelled else "✅ تست تموم شد"
    summary = (
        f"{head}\n\n"
        f"• ورودی: {run.total_input}\n"
        f"• رد شده (تکراری ۲۴س): {run.skipped_recent}\n"
        f"• تست‌شده: {run.tested}\n"
        f"• سالم: {run.success}\n"
        f"• ناموفق: {run.failed}\n"
        f"• انتخاب‌شده (top): {len(run.top)}\n"
        f"⏱ زمان: {run.duration_sec}s"
    )
    await status.edit_text(summary)

    if run.output_file and run.output_file.exists():
        await context.bot.send_document(
            chat_id=msg.chat_id,
            document=run.output_file.open("rb"),
            filename=run.output_file.name,
            caption=f"📄 {len(run.output_lines)} کانفیگ سالم",
        )
    await context.bot.send_message(
        chat_id=msg.chat_id,
        text="منوی اصلی:",
        reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
    )
    await _ensure_fixed_keyboard(update, context)


@admin_only
async def cmd_personal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """منوی بخش شخصی"""
    count = await db.count_personal()
    await update.effective_message.reply_text(
        f"👤 بخش شخصی\n\nکانفیگ ذخیره‌شده: `{count}`",
        parse_mode="Markdown",
        reply_markup=personal_menu(),
    )


# -------------------- Scanner conversation handlers --------------------

@owner_only
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


@owner_only
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


@owner_only
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


@owner_only
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


@owner_only
async def received_scan_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pw = update.effective_message.text.strip()
    ok, msg = await scanner.submit_password(pw)
    await update.effective_message.reply_text(msg, reply_markup=scanner_menu(ok))
    return ConversationHandler.END if ok else WAIT_SCAN_PASSWORD


@owner_only
async def received_scan_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    peer = update.effective_message.text.strip()
    status = await update.effective_message.reply_text("⏳ در حال اسکن کانال... (ممکنه چند دقیقه طول بکشه)")

    async def progress(count):
        try:
            await status.edit_text(f"⏳ در حال اسکن... {count} پیام بررسی شد")
        except Exception:
            pass

    ok, msg, nfiles = await scanner.scan_channel(peer, progress_cb=progress)
    if not ok:
        await status.edit_text(msg, reply_markup=scanner_menu(await scanner.is_logged_in()))
        return ConversationHandler.END

    # تشخیص تکراری
    try:
        entity = await scanner._client.get_entity(peer)
        channel_id = str(entity.id)
        groups = await scanner.find_duplicates(channel_id)
    except Exception as e:
        await status.edit_text(f"{msg}\n\n❌ خطا در تشخیص تکراری: {str(e)[:100]}", reply_markup=back_only())
        return ConversationHandler.END

    if not groups:
        await status.edit_text(
            f"{msg}\n\n🎉 هیچ فایل تکراری پیدا نشد!",
            reply_markup=scanner_menu(True),
        )
        return ConversationHandler.END

    total_dups = sum(len(g["dups"]) for g in groups)
    lines = [f"{msg}", "", f"📦 {len(groups)} گروه تکراری پیدا شد (مجموعاً {total_dups} نسخه اضافه):", ""]
    for i, g in enumerate(groups[:10], 1):
        it = g["items"][0]
        name = it["filename"] or "بدون اسم"
        size_mb = it["size"] / (1024 * 1024)
        dur = it["duration"] or 0
        dur_str = f"{int(dur // 60)}:{int(dur % 60):02d}" if dur > 0 else ""
        extra = f" ({size_mb:.0f}MB" + (f" — {dur_str})" if dur_str else ")")
        lines.append(f"{i}. 🎬 `{name}`{extra} × {len(g['items'])} نسخه")
    if len(groups) > 10:
        lines.append(f"… و {len(groups) - 10} گروه دیگر")

    lines.append("")
    lines.append("از هر گروه فقط قدیمی‌ترین نسخه می‌مونه. حذف انجام بدم؟")

    context.user_data["scan_groups_channel"] = channel_id
    await status.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=confirm_keyboard("scan_confirm_delete", "scan_cancel_delete"),
    )
    return ConversationHandler.END


async def do_scan_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str):
    query = update.callback_query
    status = await query.edit_message_text("⏳ در حال حذف تکراری‌ها...")
    groups = await scanner.find_duplicates(channel_id)
    if not groups:
        await status.edit_text("چیزی برای حذف نیست.", reply_markup=scanner_menu(True))
        return
    ok, msg, deleted = await scanner.delete_duplicates(channel_id, groups)
    await status.edit_text(msg, reply_markup=scanner_menu(True))


@admin_only
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش تعداد کانفیگ‌های در صف"""
    count = await db.count_pending()
    await update.effective_message.reply_text(
        f"📦 تعداد کانفیگ در صف تست: *{count}*",
        parse_mode="Markdown",
        reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
    )


@admin_only
async def cmd_clear_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پاک کردن صف (فقط owner)"""
    if not settings.is_owner(update.effective_user.id):
        await update.effective_message.reply_text("⛔ فقط مدیر اصلی.")
        return
    await db.clear_pending()
    await update.effective_message.reply_text(
        "🗑 صف کانفیگ‌ها پاک شد.",
        reply_markup=main_menu(True),
    )


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    اگر ربات ادمین یک کانال باشد، پست‌های کانال را می‌گیرد
    و در صورت وجود کانفیگ، به صف اضافه می‌کند.
    """
    message = update.channel_post or update.edited_channel_post or update.effective_message
    if not message:
        return

    chat = message.chat
    if not chat:
        return
    chat_id = str(chat.id)

    # فقط کانال‌هایی که در لیست ما هستن
    channels = await db.list_channels(only_active=True)
    known = {str(c["chat_id"]) for c in channels}
    # همچنین یوزرنیم
    if chat.username:
        known.add(f"@{chat.username}")
        known.add(chat.username)

    if chat_id not in known and (not chat.username or f"@{chat.username}" not in known):
        return

    text = message.text or message.caption or ""
    if not text:
        return

    links = extract_from_message_text(text)
    if not links:
        return

    new_count, _ = await db.add_pending_configs(
        links,
        source="channel",
        source_detail=chat.title or chat_id,
    )
    if new_count and settings.owner_id:
        try:
            await context.bot.send_message(
                chat_id=settings.owner_id,
                text=f"📡 از کانال «{chat.title}»: {new_count} کانفیگ جدید اضافه شد.",
            )
        except Exception:
            pass


FIXED_BUTTON_CMDS = {
    "🏠 منو": "start",
    "⚡ اجرای تست": "run",
    "⛔ لغو": "cancel",
    "📦 صف تست": "pending",
    "👤 شخصی": "personal",
    "🚀 دیپلوی": "menu_github",
}


async def handle_fixed_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دکمه‌های ثابت پایین چت → تبدیل به دستور"""
    text = (update.effective_message.text or "").strip()
    cmd = FIXED_BUTTON_CMDS.get(text)
    if not cmd:
        return
    if cmd == "start":
        await cmd_start(update, context)
    elif cmd == "run":
        await cmd_run(update, context)
    elif cmd == "cancel":
        await cancel(update, context)
    elif cmd == "pending":
        await cmd_pending(update, context)
    elif cmd == "personal":
        await cmd_personal(update, context)
    elif cmd == "menu_github":
        if not settings.is_owner(update.effective_user.id):
            await update.effective_message.reply_text("⛔ فقط مدیر اصلی")
            return
        status = await github_deployer.get_status()
        await update.effective_message.reply_text(
            f"مدیریت دیپلوی گیت‌هاب\n\n{status}",
            reply_markup=github_menu(),
        )


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """هر خطایی در هر هندلر بیاد → به مدیر اصلی پیام بده تا بی‌سکوت نباشه"""
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


def setup_handlers(application: Application):
    application.add_error_handler(global_error_handler)
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("pending", cmd_pending))
    application.add_handler(CommandHandler("clear_pending", cmd_clear_pending))
    application.add_handler(CommandHandler("run", cmd_run))
    application.add_handler(CommandHandler("personal", cmd_personal))

    # Conversation برای دریافت ورودی‌های منو (اولویت بالاتر)
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_router)],
        states={
            WAIT_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_channel)],
            WAIT_SUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_sub)],
            WAIT_TAG: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_tag)],
            WAIT_SCHEDULE_TIMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_schedule_times)],
            WAIT_GITHUB_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_github_token)],
            WAIT_GITHUB_REPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_github_repo)],
            WAIT_REMOVE_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_remove_channel)],
            WAIT_REMOVE_SUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_remove_sub)],
            WAIT_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_admin_id)],
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

    # callbackهای معمولی
    application.add_handler(CallbackQueryHandler(callback_router))

    # دکمه‌های ثابت پایین چت
    application.add_handler(
        MessageHandler(
            filters.Text([b for b in FIXED_BUTTON_CMDS]), handle_fixed_button
        )
    )

    # فوروارد/ارسال عکس، ویدیو و... با کپشن حاوی کانفیگ
    application.add_handler(
        MessageHandler(filters.CAPTION & ~filters.ChatType.CHANNEL, handle_media_caption)
    )

    # ⚠️ پست‌های کانال باید اولین MessageHandler باشن!
    # (اگه بعد از فیلتر TEXT ثبت بشن، پست کانال اول به handle_text_message می‌خوره
    #  و دکوریتور admin_only روی effective_user=None کرش می‌کنه — مانیتور کانال کار نمی‌کنه)
    application.add_handler(
        MessageHandler(filters.ChatType.CHANNEL, handle_channel_post)
    )

    # استخراج کانفیگ از پیام متنی و فوروارد
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )

    # 📦 آپدیت از ZIP (فقط owner) — باید قبل از هندلر فایل معمولی باشه
    application.add_handler(
        MessageHandler(filters.Document.FileExtension("zip"), handle_update_zip)
    )

    # استخراج از فایل
    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )
