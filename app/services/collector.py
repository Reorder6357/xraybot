"""
جمع‌آوری کانفیگ از:
- لینک‌های سابسکریپشن
- کانال‌های عمومی (از طریق getChat / در آینده با listener)
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Optional

import httpx

from app.core.database import db
from app.services.config_extractor import extract_links_from_text

logger = logging.getLogger(__name__)


async def fetch_subscription(url: str, timeout: float = 20.0) -> list[str]:
    """
    محتوای یک سابسکریپشن را می‌گیرد و لینک‌ها را استخراج می‌کند.
    پشتیبانی از:
    - متن خام (چند خط vless:// ...)
    - base64 (رایج در ساب‌های ایرانی/چینی)
    """
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; XrayBot/1.0)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            raw = resp.content

        # سعی در decode
        text = None
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                continue
        if text is None:
            return []

        # اگه base64 باشه
        stripped = "".join(text.split())
        if len(stripped) > 50 and not any(
            x in text.lower() for x in ("vless://", "vmess://", "trojan://", "ss://")
        ):
            try:
                pad = 4 - len(stripped) % 4
                if pad != 4:
                    stripped += "=" * pad
                decoded = base64.b64decode(stripped).decode("utf-8", errors="ignore")
                if any(x in decoded.lower() for x in ("vless://", "vmess://", "trojan://", "ss://")):
                    text = decoded
            except Exception:
                pass

        return extract_links_from_text(text)
    except Exception as e:
        logger.warning(f"Failed to fetch subscription {url[:60]}: {e}")
        return []


async def collect_from_subscriptions() -> int:
    """
    همه ساب‌های فعال را می‌گیرد و کانفیگ‌های جدید را به pending اضافه می‌کند.
    برمی‌گرداند: تعداد کانفیگ جدید
    """
    rows = await db.list_subscriptions(only_active=True)
    total_new = 0

    for row in rows:
        url = row["url"]
        links = await fetch_subscription(url)
        if not links:
            continue

        new_count, _ = await db.add_pending_configs(
            links,
            source="subscription",
            source_detail=url[:80],
        )
        total_new += new_count

        # آپدیت last_fetch
        try:
            await db._conn.execute(
                "UPDATE subscriptions SET last_fetch = ? WHERE url = ?",
                (time.time(), url),
            )
            await db._conn.commit()
        except Exception:
            pass

    return total_new


async def collect_from_channels() -> int:
    """
    برای کانال‌های عمومی:
    در نسخه فعلی فقط لیست کانال‌ها را نگه می‌داریم.
    مانیتورینگ زنده کانال نیاز به این دارد که ربات عضو کانال باشد
    و از طریق handler پیام‌های کانال را بگیرد (در handlers اضافه می‌شود).

    این تابع فعلاً 0 برمی‌گرداند تا ساختار scheduler کامل باشد.
    """
    # در آینده می‌توان با Telegram Client (Telethon/Pyrogram) تاریخچه کانال را خواند.
    # با Bot API معمولی فقط پیام‌هایی که به ربات فوروارد می‌شوند یا ربات ادمین است قابل دسترسی‌اند.
    return 0


async def collect_all() -> dict:
    """جمع‌آوری از همه منابع"""
    sub_new = await collect_from_subscriptions()
    ch_new = await collect_from_channels()
    pending = await db.count_pending()
    return {
        "subscription_new": sub_new,
        "channel_new": ch_new,
        "pending_total": pending,
    }
