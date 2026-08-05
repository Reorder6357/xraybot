from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton


def main_menu(is_owner: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("📡 کانال‌ها", callback_data="menu_channels"),
            InlineKeyboardButton("🔗 سابسکریپشن", callback_data="menu_subs"),
        ],
        [
            InlineKeyboardButton("🏷️ تگ کانال", callback_data="menu_tag"),
            InlineKeyboardButton("⏰ زمان‌بندی", callback_data="menu_schedule"),
        ],
        [
            InlineKeyboardButton("▶️ اجرای دستی", callback_data="run_now"),
            InlineKeyboardButton("📄 آخرین خروجی", callback_data="last_output"),
        ],
        [
            InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"),
        ],
    ]
    if is_owner:
        buttons.append([
            InlineKeyboardButton("🚀 دیپلوی گیت‌هاب", callback_data="menu_github"),
        ])
    return InlineKeyboardMarkup(buttons)


def channels_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ اضافه کردن کانال", callback_data="add_channel")],
        [InlineKeyboardButton("📋 لیست کانال‌ها", callback_data="list_channels")],
        [InlineKeyboardButton("🗑 حذف کانال", callback_data="remove_channel")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")],
    ])


def subs_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ اضافه کردن ساب", callback_data="add_sub")],
        [InlineKeyboardButton("📋 لیست ساب‌ها", callback_data="list_subs")],
        [InlineKeyboardButton("🔄 رفرش همه ساب‌ها", callback_data="refresh_subs")],
        [InlineKeyboardButton("🗑 حذف ساب", callback_data="remove_sub")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")],
    ])


def tag_menu(enabled: bool, tag: str) -> InlineKeyboardMarkup:
    status = "🟢 روشن" if enabled else "🔴 خاموش"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"وضعیت: {status}", callback_data="toggle_tag")],
        [InlineKeyboardButton(f"تگ فعلی: {tag or '—'}", callback_data="set_tag")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")],
    ])


def schedule_menu(enabled: bool, times: list[str]) -> InlineKeyboardMarkup:
    status = "🟢 فعال" if enabled else "🔴 غیرفعال"
    times_str = ", ".join(times) if times else "تنظیم نشده"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"وضعیت: {status}", callback_data="toggle_schedule")],
        [InlineKeyboardButton(f"ساعت‌ها: {times_str}", callback_data="set_schedule_times")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")],
    ])


def settings_menu(is_owner: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📊 وضعیت سیستم", callback_data="system_status")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")],
    ]
    if is_owner:
        buttons.insert(0, [InlineKeyboardButton("👤 مدیریت ادمین‌ها", callback_data="manage_admins")])
    return InlineKeyboardMarkup(buttons)


def github_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 تنظیم توکن و ریپو", callback_data="set_github")],
        [InlineKeyboardButton("📦 آپدیت از فایل ZIP", callback_data="update_from_zip")],
        [InlineKeyboardButton("📤 دیپلوی فایل‌های فعلی", callback_data="deploy_now")],
        [InlineKeyboardButton("📋 وضعیت گیت‌هاب", callback_data="github_status")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")],
    ])


def confirm_keyboard(yes_data: str, no_data: str = "back_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ بله", callback_data=yes_data),
            InlineKeyboardButton("❌ خیر", callback_data=no_data),
        ]
    ])


def back_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")]
    ])
