"""
نقطه ورود اصلی:
- FastAPI برای صفحه setup اولیه + healthcheck
- ربات تلگرام
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment
from pydantic import BaseModel
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
    tg_id = await db.get_setting("tg_api_id")
    tg_hash = await db.get_setting("tg_api_hash")
    tg_phone = await db.get_setting("scanner_phone")

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
    if tg_id:
        settings.tg_api_id = int(tg_id)
    if tg_hash:
        settings.tg_api_hash = tg_hash
    if tg_phone:
        settings.scanner_phone = tg_phone


async def get_or_create_setup_key() -> str:
    """کلید محافظت از صفحه راه‌اندازی.
    اولویت: env SETUP_KEY > کلید ذخیره‌شده در دیتابیس > ساخت کلید جدید (یک بار نمایش داده می‌شه)."""
    env_key = os.environ.get("SETUP_KEY", "").strip()
    if env_key:
        return env_key
    stored = await db.get_setting("setup_key")
    if stored:
        return stored
    key = secrets.token_urlsafe(16)
    await db.set_setting("setup_key", key)
    return key


async def verify_setup_key(provided: str) -> bool:
    expected = await get_or_create_setup_key()
    return secrets.compare_digest(provided.strip(), expected)


async def validate_bot_token(token: str) -> tuple[bool, str]:
    """اعتبارسنجی توکن با getMe قبل از ذخیره"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            if r.status_code == 200 and r.json().get("ok"):
                bot = r.json().get("result", {})
                return True, f"🤖 @{bot.get('username', '')} — {bot.get('first_name', '')}"
            return False, "توکن نامعتبر است (پاسخ تلگرام: ناموفق)."
    except Exception as e:
        return False, f"خطا در ارتباط با تلگرام: {e}"


async def start_bot():
    global bot_app
    if not settings.bot_token:
        logger.warning("Bot token not set yet. Waiting for setup via web.")
        return

    try:
        bot_app = (
            Application.builder()
            .token(settings.bot_token)
            .concurrent_updates(True)
            .build()
        )
        setup_handlers(bot_app)
        await bot_app.initialize()
        await bot_app.start()
        # drop_pending_updates=False: اگه پیام موقع ری‌استارت/آپدیت بیاد گم نشه
        # (مثلاً کانفیگی که موقع دیپلوی Railway فرستاده می‌شه)
        await bot_app.updater.start_polling(drop_pending_updates=False)
        logger.info("Telegram bot started.")
    except Exception as e:
        logger.error(f"Failed to start Telegram bot: {e}")
        bot_app = None
        # صفحه وب باید همچنان کار کند حتی اگر توکن اشتباه باشد


async def stop_bot():
    global bot_app
    if bot_app:
        try:
            if bot_app.updater and bot_app.updater.running:
                await bot_app.updater.stop()
            if bot_app.running:
                await bot_app.stop()
            await bot_app.shutdown()
        except Exception as e:
            logger.error(f"Error while stopping bot: {e}")
        finally:
            bot_app = None
            logger.info("Telegram bot stopped.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — خطا در ربات نباید کل سرویس را بخواباند
    try:
        await db.connect()
        await load_runtime_settings()
    except Exception as e:
        logger.error(f"DB/startup settings error: {e}")

    try:
        await start_bot()
        set_bot_app(bot_app)
    except Exception as e:
        logger.error(f"Bot start error (web UI still available): {e}")

    try:
        start_scheduler()
        await reload_schedule()
    except Exception as e:
        logger.error(f"Scheduler error: {e}")

    yield

    # Shutdown
    try:
        stop_scheduler()
    except Exception:
        pass
    try:
        await stop_bot()
    except Exception:
        pass
    try:
        from app.services.channel_scanner import scanner
        await scanner.disconnect()  # ذخیره سشن قبل از خاموشی
    except Exception:
        pass
    try:
        await db.close()
    except Exception:
        pass


app = FastAPI(title="Xray Config Bot", lifespan=lifespan)

# CORS: تا صفحه HTML تست بتونه از مرورگر به API دسترسی داشته باشه
# (دسترسی با توکن محافظت می‌شه؛ CORS فقط اجازه‌ی فراخوانی می‌ده)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
      {% if configured %}
      <label>کلید راه‌اندازی (برای تغییرات)</label>
      <input type="text" name="setup_key" placeholder="کلید راه‌اندازی" required autocomplete="off">
      {% endif %}
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

    {% if fresh_key %}
      <div class="msg ok">🔑 کلید راه‌اندازی جدید ساخته شد (فقط همین یک بار نمایش داده می‌شود — جایی امن ذخیره کن):<br><code>{{ fresh_key }}</code></div>
    {% endif %}

    {% if message %}
      <div class="msg {{ 'ok' if success else 'err' }}">{{ message }}</div>
    {% endif %}
  </div>
</body>
</html>
"""


_JINJA = Environment(autoescape=True)


def _render_setup(**kwargs) -> str:
    return _JINJA.from_string(SETUP_HTML).render(**kwargs)


class TestRequest(BaseModel):
    links: list[str] = []
    concurrency: int = 20
    timeout: float = 8.0


@app.post("/api/test")
async def api_test(req: TestRequest, request: Request):
    """
    API تست کانفیگ با Xray (برای صفحه HTML تست).
    دسترسی فقط با توکن (X-API-Token = کلید راه‌اندازی).
    """
    # 🔐 احراز هویت با کلید راه‌اندازی
    expected = await get_or_create_setup_key()
    provided = request.headers.get("x-api-token", "")
    if not secrets.compare_digest(provided.strip(), expected):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if not req.links:
        return JSONResponse({"error": "empty_links"}, status_code=400)

    links = [l for l in req.links if isinstance(l, str) and l.strip()][:200]
    if not links:
        return JSONResponse({"error": "no_valid_links"}, status_code=400)

    from app.services.xray_tester import test_batch

    results = await test_batch(
        links,
        concurrency=max(1, min(int(req.concurrency or 20), 25)),
        timeout=max(2.0, min(float(req.timeout or 8.0), 15.0)),
    )

    return {
        "total": len(results),
        "success": sum(1 for r in results if r.success),
        "results": [
            {
                "link": r.link,
                "success": r.success,
                "latency_ms": r.latency_ms,
                "country_code": r.country_code,
                "country_name": r.country_name,
                "exit_ip": r.exit_ip,
                "error": r.error,
                "speed_kbps": r.speed_kbps,
            }
            for r in results
        ],
    }


@app.get("/", response_class=HTMLResponse)
async def setup_page(request: Request):
    configured = settings.is_configured()
    fresh_key = None
    if configured:
        # اگه کلیدی ذخیره نشده (مثلاً از نسخه قبلی آپگرید شده)،
        # کلید جدید بساز و فقط همین یک بار نشون بده
        stored = await db.get_setting("setup_key")
        if not stored and not os.environ.get("SETUP_KEY", "").strip():
            fresh_key = await get_or_create_setup_key()
    html = _render_setup(
        configured=configured,
        bot_token="",  # خالی می‌ذاریم تا کاربر توکن جدید رو کامل وارد کنه
        owner_id=settings.owner_id or "",
        message=None,
        success=False,
        fresh_key=fresh_key,
    )
    return HTMLResponse(html)


@app.post("/setup", response_class=HTMLResponse)
async def do_setup(
    bot_token: str = Form(...),
    owner_id: int = Form(...),
    setup_key: str = Form(""),
):
    configured = settings.is_configured()

    # 🔐 امنیت: وقتی ربات پیکربندی شده، تغییر تنظیمات نیاز به کلید داره
    if configured and not await verify_setup_key(setup_key):
        html = _render_setup(
            configured=True,
            bot_token="",
            owner_id=owner_id,
            message="❌ کلید راه‌اندازی اشتباه است.",
            success=False,
        )
        return HTMLResponse(html)

    token = bot_token.strip()
    if not token or ":" not in token:
        html = _render_setup(
            configured=configured,
            bot_token="",
            owner_id=owner_id,
            message="❌ فرمت توکن نامعتبر است.",
            success=False,
        )
        return HTMLResponse(html)

    # اعتبارسنجی توکن با تلگرام قبل از ذخیره
    token_ok, token_info = await validate_bot_token(token)
    if not token_ok:
        html = _render_setup(
            configured=configured,
            bot_token="",
            owner_id=owner_id,
            message=f"❌ {token_info}",
            success=False,
        )
        return HTMLResponse(html)

    was_configured = configured

    # ذخیره (هم برای اولین بار، هم برای ادیت)
    await db.set_setting("bot_token", token)
    await db.set_setting("owner_id", owner_id)
    if not was_configured:
        await db.set_setting("admin_ids", [])
        settings.admin_ids = []
        # ساخت کلید راه‌اندازی برای تغییرات بعدی
        await get_or_create_setup_key()

    settings.bot_token = token
    settings.owner_id = owner_id

    # ری‌استارت ربات با توکن جدید
    try:
        await stop_bot()
        await start_bot()
        set_bot_app(bot_app)  # مهم: بدون این، زمان‌بندی به نمونه قدیمی اشاره می‌کنه
        if not was_configured:
            setup_key = await get_or_create_setup_key()
            msg = (
                f"✅ ربات {token_info} فعال شد.\n"
                f"حالا توی تلگرام /start بزن.\n\n"
                f"🔑 کلید راه‌اندازی (برای تغییرات بعدی):\n`{setup_key}`\n"
                f"این کلید رو جایی امن نگه دار."
            )
        else:
            msg = f"✅ تنظیمات ذخیره شد و ربات با توکن جدید ری‌استارت شد. ({token_info})"
        ok = True
    except Exception as e:
        msg = f"⚠️ ذخیره شد ولی در راه‌اندازی ربات خطا رخ داد: {e}"
        ok = False

    html = _render_setup(
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
