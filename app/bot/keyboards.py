from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton


def fixed_menu() -> ReplyKeyboardMarkup:
    """دکمه‌های ثابت پایین چت (همیشه جلوی چشم کاربر)"""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🏠 منو"), KeyboardButton("⚡ اجرای تست"), KeyboardButton("⛔ لغو")],
            [KeyboardButton("📦 صف تست"), KeyboardButton("👤 شخصی"), KeyboardButton("🚀 دیپلوی")],
        ],
        resize_keyboard=True,
        input_field_placeholder="کانفیگ بفرست یا از دکمه‌ها استفاده کن...",
    )


def main_menu(is_owner: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("📡 کانال‌ها", callback_data="menu_channels"),
            InlineKeyboardButton("🔗 سابسکریپشن", callback_data="menu_subs"),
        ],
        [
            InlineKeyboardButton("📦 صف تست", callback_data="menu_queue"),
            InlineKeyboardButton("👤 شخصی", callback_data="menu_personal"),
        ],
        [
            InlineKeyboardButton("📡 اسکن کانال (تکراری‌یاب)", callback_data="menu_scanner"),
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


def queue_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 مشاهده صف", callback_data="view_queue")],
        [InlineKeyboardButton("🗑 پاک کردن کل صف", callback_data="clear_queue")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")],
    ])


def personal_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 لیست کانفیگ‌ها", callback_data="personal_list")],
        [InlineKeyboardButton("⚡ تست همه", callback_data="personal_test")],
        [InlineKeyboardButton("🗑 پاک کردن همه", callback_data="personal_clear")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")],
    ])


def extract_actions_keyboard(scan: bool = False) -> InlineKeyboardMarkup:
    """دکمه‌های زیر پیام استخراج کانفیگ"""
    rows = [
        [InlineKeyboardButton("⚡ تست همین الان", callback_data="act_test_now")],
        [
            InlineKeyboardButton("👤 ذخیره در شخصی", callback_data="act_save_personal"),
            InlineKeyboardButton("🗑 حذف از صف", callback_data="act_remove_queue"),
        ],
    ]
    if scan:
        rows.insert(0, [InlineKeyboardButton("📡 اسکن این کانال", callback_data="act_scan_forward")])
    return InlineKeyboardMarkup(rows)


def cancel_run_keyboard() -> InlineKeyboardMarkup:
    """دکمه توقف در حین تست"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛔ توقف تست", callback_data="act_cancel_run")],
    ])


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


def scanner_menu(login_status: bool = False) -> InlineKeyboardMarkup:
    btn1 = "🔑 ورود با شماره (لاگین)" if not login_status else "✅ وارد شده‌اید"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(btn1, callback_data="scan_login")],
        [InlineKeyboardButton("📡 اسکن کانال", callback_data="scan_channel")],
        [InlineKeyboardButton("🗑 پاک کردن داده اسکن", callback_data="scan_clear_data")],
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
