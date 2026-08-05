"""
هندلرهای اصلی ربات
"""

from __future__ import annotations

import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from app.core.config import settings
from app.core.database import db
from app.bot.keyboards import (
    main_menu, channels_menu, subs_menu, tag_menu,
    schedule_menu, settings_menu, github_menu, back_only, confirm_keyboard
)
from app.services.github_deploy import github_deployer
from app.services.config_extractor import (
    extract_from_message_text,
    extract_from_document,
    parse_basic_info,
)
from app.services.runner import run_full_test
from app.services.scheduler import reload_schedule
from app.services.collector import collect_from_subscriptions, collect_all
from pathlib import Path

logger = logging.getLogger(__name__)

# Conversation states
(
    WAIT_CHANNEL,
    WAIT_SUB,
    WAIT_TAG,
    WAIT_SCHEDULE_TIMES,
    WAIT_GITHUB_TOKEN,
    WAIT_GITHUB_REPO,
    WAIT_ADMIN_ID,
) = range(7)


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
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
        user_id = update.effective_user.id
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
    await update.effective_message.reply_text(text, reply_markup=main_menu(is_owner))


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
                lines.append(f"• {r['title'] or uname} (`{r['chat_id']}`)")
            text = "لیست کانال‌ها:\n\n" + "\n".join(lines)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=channels_menu())
        return

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

        async def progress(done, total, last_res):
            try:
                mark = "✅" if last_res.success else "❌"
                await status.edit_text(
                    f"⏳ در حال تست...\n"
                    f"• پیشرفت: {done}/{total}\n"
                    f"• آخرین: {mark} {last_res.address or '?'} "
                    f"({int(last_res.latency_ms) if last_res.success else last_res.error[:30]})"
                )
            except Exception:
                pass

        run = await run_full_test(progress_callback=progress)

        if run.error and not run.top:
            await status.edit_text(
                f"❌ {run.error}\n"
                f"⏱ زمان: {run.duration_sec}s",
                reply_markup=back_only(),
            )
            return

        summary = (
            f"✅ تست تموم شد\n\n"
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

    if data == "last_output":
        out_dir = Path("/app/data/outputs")
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
    # ساده: فعلاً فقط ذخیره می‌کنیم
    await db.add_channel(chat_id=text, title=text)
    await update.effective_message.reply_text(
        f"✅ کانال `{text}` اضافه شد.",
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
    # اعتبارسنجی ساده HH:MM
    valid = []
    for t in lines:
        if len(t) == 5 and t[2] == ":" and t[:2].isdigit() and t[3:].isdigit():
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
    return ConversationHandler.END


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
    source = "forward" if message.forward_origin or message.forward_date else "message"
    source_detail = ""
    if message.forward_from_chat:
        source_detail = message.forward_from_chat.title or str(message.forward_from_chat.id)
    elif message.forward_from:
        source_detail = message.forward_from.full_name or str(message.forward_from.id)

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

    lines = [
        f"✅ استخراج انجام شد",
        f"• پیدا شده: {len(links)}",
        f"• جدید: {new_count}",
        f"• تکراری: {dup_count}",
        f"• کل موجود در صف تست: {total_pending}",
    ]
    if source == "forward" and source_detail:
        lines.append(f"• منبع: فوروارد از {source_detail}")

    # نمایش چند تا نمونه
    samples = links[:3]
    if samples:
        lines.append("\nنمونه:")
        for s in samples:
            info = parse_basic_info(s)
            proto = info.get("protocol") or "?"
            addr = info.get("address") or "?"
            lines.append(f"• `{proto}` → `{addr}`")

    await message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
    )


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

    new_count, dup_count = await db.add_pending_configs(
        links,
        source="file",
        source_detail=doc.file_name or "document",
    )
    total_pending = await db.count_pending()

    text = (
        f"✅ فایل پردازش شد\n"
        f"• نام فایل: `{doc.file_name}`\n"
        f"• پیدا شده: {len(links)}\n"
        f"• جدید: {new_count}\n"
        f"• تکراری: {dup_count}\n"
        f"• کل موجود در صف تست: {total_pending}"
    )
    await status_msg.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_menu(settings.is_owner(update.effective_user.id)),
    )


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


def setup_handlers(application: Application):
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("pending", cmd_pending))
    application.add_handler(CommandHandler("clear_pending", cmd_clear_pending))

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
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(callback_router)],
        allow_reentry=True,
    )
    application.add_handler(conv)

    # callbackهای معمولی
    application.add_handler(CallbackQueryHandler(callback_router))

    # استخراج کانفیگ از پیام متنی و فوروارد
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )

    # استخراج از فایل
    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )

    # پست‌های کانال (اگر ربات ادمین باشد)
    application.add_handler(
        MessageHandler(filters.ChatType.CHANNEL, handle_channel_post)
    )
