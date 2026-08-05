"""
اجرای کامل یک دوره تست:
1. گرفتن کانفیگ‌های pending (+ ساب‌ها در آینده)
2. حذف تکراری با تاریخچه ۲۴ ساعته
3. تست با Xray
4. انتخاب top N
5. تغییر ریمارک
6. ذخیره در تاریخچه
7. ساخت فایل خروجی
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from app.core.config import settings, DATA_DIR
from app.core.database import db
from app.services.config_extractor import config_hash
from app.services.xray_tester import test_batch, select_top, TestResult

logger = logging.getLogger(__name__)

# قفل اجرای همزمان: جلوگیری از دو تست موازی (دکمه دستی + زمان‌بندی + دوبار کلیک)
_run_lock = asyncio.Lock()


class RunResult:
    def __init__(self):
        self.total_input = 0
        self.skipped_recent = 0
        self.tested = 0
        self.success = 0
        self.failed = 0
        self.top: list[TestResult] = []
        self.output_file: Optional[Path] = None
        self.output_lines: list[str] = []
        self.duration_sec = 0.0
        self.error: str = ""


async def run_full_test(
    max_configs: int | None = None,
    concurrency: int | None = None,
    keep_top: int | None = None,
    progress_callback: Optional[Callable] = None,
) -> RunResult:
    """
    یک دوره کامل تست را اجرا می‌کند.
    """
    result = RunResult()
    t0 = time.perf_counter()

    max_configs = max_configs or settings.max_configs_per_run
    concurrency = concurrency or settings.test_concurrency
    keep_top = keep_top or settings.keep_top_n

    if _run_lock.locked():
        result.error = "یک اجرای تست در حال انجام است (کمی صبر کن)"
        return result

    async with _run_lock:
        return await _run_locked(
            result, max_configs, concurrency, keep_top, progress_callback, t0
        )


async def _run_locked(
    result: RunResult,
    max_configs: int,
    concurrency: int,
    keep_top: int,
    progress_callback: Optional[Callable],
    t0: float,
) -> RunResult:
    try:
        # ۱. گرفتن کانفیگ‌های در صف (قدیمی‌ترین‌ها اول)
        rows = await db.get_pending_configs(limit=max_configs)
        links = [r["config_line"] for r in rows]
        result.total_input = len(links)

        if not links:
            result.error = "هیچ کانفیگی در صف نیست"
            return result

        # ۲. فیلتر کردن اونایی که اخیراً (۲۴ ساعت) فرستاده شدن
        to_test = []
        for link in links:
            h = config_hash(link)
            if await db.is_recently_sent(h, ttl_hours=settings.history_ttl_hours):
                result.skipped_recent += 1
            else:
                to_test.append(link)

        if not to_test:
            result.error = "همه کانفیگ‌ها اخیراً تست و ارسال شدن (۲۴ ساعت)"
            return result

        # ۳. تست
        async def _progress(done, total, last_res):
            if progress_callback:
                await progress_callback(done, total, last_res)

        test_results = await test_batch(
            to_test,
            concurrency=concurrency,
            timeout=float(settings.test_timeout),
            progress_callback=_progress,
        )

        result.tested = len(test_results)
        result.success = sum(1 for r in test_results if r.success)
        result.failed = result.tested - result.success

        # ۴. انتخاب برترین‌ها
        top = select_top(test_results, top_n=keep_top)
        result.top = top

        if not top:
            result.error = "هیچ کانفیگ سالمی پیدا نشد"
            result.duration_sec = time.perf_counter() - t0
            return result

        # ۵. تگ کانال
        tag_info = await db.get_channel_tag()
        channel_tag = tag_info.get("tag") or ""
        tag_enabled = bool(tag_info.get("enabled"))

        # ۶. ساخت خطوط خروجی + ذخیره تاریخچه
        output_lines = []
        for r in top:
            new_link = r.with_new_remark(channel_tag, tag_enabled)
            output_lines.append(new_link)
            await db.add_healthy(
                config_hash=config_hash(r.link),
                config_line=new_link,
                remark=r.make_remark(channel_tag, tag_enabled),
                country=r.country_code or "",
                latency=r.latency_ms,
            )

        result.output_lines = output_lines

        # ۷. نوشتن فایل
        out_dir = DATA_DIR / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"healthy_{ts}.txt"
        out_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
        result.output_file = out_path

    except Exception as e:
        logger.exception("run_full_test failed")
        result.error = str(e)[:200]
    finally:
        # ۸. پاکسازی صف: کانفیگ‌های این دوره (موفق + ناموفق) از صف حذف می‌شن.
        #     اینطوری صف همیشه «تست‌نشده»هاست و هر بار همه‌چیز دوباره تست نمی‌شه؛
        #     کانفیگ‌های سالم تا ۲۴ ساعت توی history هستن و ساب‌ها هم هر دوره دوباره جمع می‌شن.
        if links:
            try:
                await db.delete_pending_by_hashes([config_hash(l) for l in links])
            except Exception as e:
                logger.warning(f"Failed to clean pending queue: {e}")

        # ۹. پاکسازی تاریخچه قدیمی
        try:
            await db.cleanup_old_history(ttl_hours=settings.history_ttl_hours)
        except Exception:
            pass

        # ۱۰. آپدیت last_run
        try:
            await db.update_last_run()
        except Exception:
            pass

    result.duration_sec = round(time.perf_counter() - t0, 1)
    return result
