"""
زمان‌بندی اجرای خودکار تست‌ها با APScheduler.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.database import db
from app.services.runner import run_full_test

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.timezone))
_bot_app = None  # بعداً از main ست می‌شه


def set_bot_app(app):
    global _bot_app
    _bot_app = app


async def _notify_owner(text: str, document_path: Optional[str] = None):
    """ارسال نتیجه به مدیر اصلی"""
    if not _bot_app or not settings.owner_id:
        return
    try:
        await _bot_app.bot.send_message(chat_id=settings.owner_id, text=text)
        if document_path:
            from pathlib import Path
            p = Path(document_path)
            if p.exists():
                await _bot_app.bot.send_document(
                    chat_id=settings.owner_id,
                    document=p.open("rb"),
                    filename=p.name,
                    caption="📄 خروجی زمان‌بندی‌شده",
                )
    except Exception as e:
        logger.error(f"Failed to notify owner: {e}")


async def scheduled_job():
    """جاب اصلی که در ساعت‌های مشخص اجرا می‌شه"""
    logger.info("Scheduled job started")
    try:
        # اول ساب‌ها و کانال‌ها رو رفرش کن (اگه پیاده‌سازی شده باشن)
        try:
            from app.services.collector import collect_from_subscriptions, collect_from_channels
            sub_new = await collect_from_subscriptions()
            ch_new = await collect_from_channels()
            logger.info(f"Collected: subs={sub_new}, channels={ch_new}")
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"Collector error: {e}")

        run = await run_full_test()

        if run.error and not run.top:
            await _notify_owner(f"⏰ اجرای زمان‌بندی‌شده\n❌ {run.error}")
            return

        summary = (
            f"⏰ اجرای زمان‌بندی‌شده تموم شد\n\n"
            f"• ورودی: {run.total_input}\n"
            f"• رد شده (تکراری): {run.skipped_recent}\n"
            f"• تست‌شده: {run.tested}\n"
            f"• سالم: {run.success}\n"
            f"• top: {len(run.top)}\n"
            f"⏱ {run.duration_sec}s"
        )
        doc = str(run.output_file) if run.output_file else None
        await _notify_owner(summary, document_path=doc)
    except Exception as e:
        logger.exception("Scheduled job failed")
        await _notify_owner(f"⏰ خطا در اجرای زمان‌بندی‌شده:\n{e}")


async def reload_schedule():
    """بارگذاری مجدد زمان‌بندی از دیتابیس"""
    sched = await db.get_schedule()
    if not sched["enabled"] or not sched["times"]:
        scheduler.remove_all_jobs()
        logger.info("Schedule disabled or empty")
        return

    # اول همه ساعت‌ها رو اعتبارسنجی کن؛ اگه یکی غلط بود، برنامه قبلی رو پاک نکن
    parsed_times = []
    for t in sched["times"]:
        try:
            hour, minute = t.split(":")
            hour, minute = int(hour), int(minute)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError(f"out of range: {t}")
            parsed_times.append((t, hour, minute))
        except Exception as e:
            logger.error(f"Invalid schedule time {t}: {e}")
            return  # برنامه قبلی دست‌نخورده می‌مونه

    scheduler.remove_all_jobs()

    for t, hour, minute in parsed_times:
        try:
            job_id = f"run_{hour:02d}_{minute:02d}"
            scheduler.add_job(
                scheduled_job,
                CronTrigger(hour=hour, minute=minute, timezone=ZoneInfo(settings.timezone)),
                id=job_id,
                replace_existing=True,
                misfire_grace_time=300,
            )
            logger.info(f"Scheduled job at {t}")
        except Exception as e:
            logger.error(f"Failed to schedule {t}: {e}")


def start_scheduler():
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
