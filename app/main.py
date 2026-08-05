"""
نقطه ورود اصلی:
- FastAPI برای صفحه setup اولیه + healthcheck
- ربات تلگرام
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from telegram.ext import Application

from app.core.config import settings, DATA_DIR
from app.core.database import db
from app.bot.handlers import setup_handlers
from app.services.github_deploy import github_deployer
from app.services.scheduler import (
    set_bot_app, start_scheduler, stop_scheduler, reload_schedule
)
logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("main")

# Global bot application
bot_app: Application | None = None


async def load_runtime_settings():
    """بارگذاری تنظیمات ذخیره‌شده در دیتابیس"""
    token = await db.get_setting("bot_token")
    owner = await db.get_setting("owner_id")
    admins = await db.get_setting("admin_ids") or []
    gh_token = await db.get_setting("github_token")
    gh_repo = await db.get_setting("github_repo")

    if token:
        settings.bot_token = token
    if owner:
        settings.owner_id = int(owner)
    if admins:
        settings.admin_ids = [int(a) for a in admins]
    if gh_token:
        settings.github_token = gh_token
    if gh_repo:
        settings.github_repo = gh_repo


async def start_bot():
    global bot_app
    if not settings.bot_token:
        logger.warning("Bot token not set yet. Waiting for setup via web.")
        return

    bot_app = (
        Application.builder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .build()
    )
    setup_handlers(bot_app)
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot started.")


async def stop_bot():
    global bot_app
    if bot_app:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        bot_app = None
        logger.info("Telegram bot stopped.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await db.connect()
    await load_runtime_settings()
    await start_bot()
    set_bot_app(bot_app)
    start_scheduler()
    await reload_schedule()
    yield
    # Shutdown
    stop_scheduler()
    await stop_bot()
    await db.close()


app = FastAPI(title="Xray Config Bot", lifespan=lifespan)


# -------------------- Health --------------------
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "bot_configured": settings.is_configured(),
        "owner_id": settings.owner_id,
    }


# -------------------- Setup Web UI --------------------
SETUP_HTML = """
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>راه‌اندازی ربات</title>
  <style>
    body { font-family: Tahoma, sans-serif; background: #0f172a; color: #e2e8f0; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
    .card { background: #1e293b; padding: 2rem; border-radius: 1rem; width: 100%; max-width: 420px; box-shadow: 0 10px 40px rgba(0,0,0,.4); }
    h1 { margin-top: 0; font-size: 1.4rem; text-align: center; }
    label { display: block; margin: 1rem 0 .3rem; font-size: .9rem; color: #94a3b8; }
    input { width: 100%; padding: .7rem; border-radius: .5rem; border: 1px solid #334155; background: #0f172a; color: #fff; box-sizing: border-box; }
    button { width: 100%; margin-top: 1.5rem; padding: .8rem; border: none; border-radius: .5rem; background: #3b82f6; color: white; font-size: 1rem; cursor: pointer; }
    button:hover { background: #2563eb; }
    .msg { margin-top: 1rem; padding: .8rem; border-radius: .5rem; text-align: center; }
    .ok { background: #065f46; }
    .err { background: #7f1d1d; }
    .info { font-size: .8rem; color: #64748b; margin-top: 1rem; text-align: center; }
    .badge { display: inline-block; padding: .2rem .6rem; border-radius: .4rem; font-size: .75rem; margin-bottom: 1rem; }
    .badge-on { background: #065f46; }
    .badge-off { background: #7f1d1d; }
  </style>
</head>
<body>
  <div class="card">
    <h1>🚀 تنظیمات ربات Xray</h1>
    {% if configured %}
      <div style="text-align:center"><span class="badge badge-on">فعال</span></div>
    {% else %}
      <div style="text-align:center"><span class="badge badge-off">پیکربندی نشده</span></div>
    {% endif %}

    <form method="post" action="/setup">
      <label>توکن ربات تلگرام</label>
      <input type="text" name="bot_token" placeholder="123456:ABC-DEF..." value="{{ bot_token or '' }}" required>
      <label>آیدی عددی مدیر اصلی</label>
      <input type="number" name="owner_id" placeholder="123456789" value="{{ owner_id or '' }}" required>
      <button type="submit">{{ 'ذخیره و اعمال تغییرات' if configured else 'فعال‌سازی ربات' }}</button>
    </form>

    <div class="info">
      {% if configured %}
        می‌تونی توکن یا آیدی مدیر رو عوض کنی و ذخیره کنی. ربات با تنظیمات جدید ری‌استارت می‌شه.
      {% else %}
        بعد از فعال‌سازی، ربات شروع به کار می‌کند.
      {% endif %}
    </div>

    {% if message %}
      <div class="msg {{ 'ok' if success else 'err' }}">{{ message }}</div>
    {% endif %}
  </div>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def setup_page(request: Request):
    from jinja2 import Template
    configured = settings.is_configured()
    # توکن کامل رو نشون نمی‌دیم؛ فقط اگه خالی باشه placeholder
    masked = ""
    if settings.bot_token:
        t = settings.bot_token
        masked = t[:8] + "..." + t[-4:] if len(t) > 15 else t
    html = Template(SETUP_HTML).render(
        configured=configured,
        bot_token="",  # خالی می‌ذاریم تا کاربر توکن جدید رو کامل وارد کنه
        owner_id=settings.owner_id or "",
        message=None,
        success=False,
    )
    return HTMLResponse(html)


@app.post("/setup", response_class=HTMLResponse)
async def do_setup(
    bot_token: str = Form(...),
    owner_id: int = Form(...),
):
    from jinja2 import Template

    token = bot_token.strip()
    if not token or ":" not in token:
        html = Template(SETUP_HTML).render(
            configured=settings.is_configured(),
            bot_token="",
            owner_id=owner_id,
            message="❌ فرمت توکن نامعتبر است.",
            success=False,
        )
        return HTMLResponse(html)

    was_configured = settings.is_configured()

    # ذخیره (هم برای اولین بار، هم برای ادیت)
    await db.set_setting("bot_token", token)
    await db.set_setting("owner_id", owner_id)
    if not was_configured:
        await db.set_setting("admin_ids", [])
        settings.admin_ids = []

    settings.bot_token = token
    settings.owner_id = owner_id

    # ری‌استارت ربات با توکن جدید
    try:
        await stop_bot()
        await start_bot()
        msg = (
            "✅ تنظیمات ذخیره شد و ربات با توکن جدید ری‌استارت شد.\n"
            "حالا توی تلگرام /start بزن."
        )
        ok = True
    except Exception as e:
        msg = f"⚠️ ذخیره شد ولی در راه‌اندازی ربات خطا رخ داد: {e}"
        ok = False

    html = Template(SETUP_HTML).render(
        configured=True,
        bot_token="",
        owner_id=owner_id,
        message=msg,
        success=ok,
    )
    return HTMLResponse(html)


# برای Railway که گاهی از root استفاده می‌کنه
@app.get("/healthz")
async def healthz():
    return {"ok": True}
