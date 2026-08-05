"""
جمع‌آوری کانفیگ از:
- لینک‌های سابسکریپشن
- کانال‌های عمومی (از طریق getChat / در آینده با listener)
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import socket
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.core.database import db
from app.services.config_extractor import extract_links_from_text

logger = logging.getLogger(__name__)

MAX_SUB_SIZE = 10 * 1024 * 1024  # 10MB


def _is_private_host(url: str) -> bool:
    """
    جلوگیری از SSRF: اگر هاست ساب به IP خصوصی/لوکال/متادیتا اشاره کند رد می‌شود.
    (مثلاً http://169.254.169.254/ یا http://192.168.1.1/)
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return True
        host = parsed.hostname or ""
        if not host:
            return True
        # آی‌پی مستقیم
        try:
            addr = ipaddress.ip_address(host)
            return addr.is_private or addr.is_loopback or addr.is_link_local \
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified
        except ValueError:
            pass
        # رزولوشن DNS (بلوکینگ — در to_thread صدا زده می‌شه)
        for info in socket.getaddrinfo(host, None):
            try:
                addr = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if addr.is_private or addr.is_loopback or addr.is_link_local \
                    or addr.is_multicast or addr.is_reserved:
                return True
        return False
    except Exception:
        return True  # رد شدن امن‌تر از پذیرفتنه


async def fetch_subscription(url: str, timeout: float = 20.0) -> list[str]:
    """
    محتوای یک سابسکریپشن را می‌گیرد و لینک‌ها را استخراج می‌کند.
    پشتیبانی از:
    - متن خام (چند خط vless:// ...)
    - base64 (رایج در ساب‌های ایرانی/چینی)
    """
    try:
        # گارد SSRF
        if await asyncio.to_thread(_is_private_host, url):
            logger.warning(f"Subscription blocked (private host): {url[:60]}")
            return []

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; XrayBot/1.0)"},
        ) as client:
            # محدودیت حجم
            length = None
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                cl = resp.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > MAX_SUB_SIZE:
                    logger.warning(f"Subscription too large: {url[:60]} ({cl} bytes)")
                    return []
                chunks = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_SUB_SIZE:
                        logger.warning(f"Subscription exceeded size cap: {url[:60]}")
                        return []
                    chunks.append(chunk)
                raw = b"".join(chunks)

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
