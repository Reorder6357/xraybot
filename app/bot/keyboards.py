from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu(is_owner: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("📡 اسکن کانال (تکراری‌یاب)", callback_data="menu_scanner"),
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


def scanner_menu(login_status: bool = False) -> InlineKeyboardMarkup:
    btn1 = "🔑 ورود با شماره (لاگین)" if not login_status else "✅ وارد شده‌اید"
    rows = [
        [InlineKeyboardButton("📋 کانال‌های ثبت‌شده", callback_data="scan_channels_list")],
        [InlineKeyboardButton(btn1, callback_data="scan_login")],
    ]
    if login_status:
        rows.append([InlineKeyboardButton("🚪 خروج از حساب اسکنر", callback_data="scan_logout")])
    rows.append([InlineKeyboardButton("🗑 پاک کردن داده اسکن", callback_data="scan_clear_data")])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


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
