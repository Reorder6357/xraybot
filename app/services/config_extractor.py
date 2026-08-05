"""
استخراج کانفیگ‌های پروکسی از متن، فایل و پیام فوروارد شده.
پشتیبانی از: vless://  vmess://  trojan://  ss://  ssr://  hysteria2://  hy2://  tuic://
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import logging
from typing import Optional
from urllib.parse import urlparse, unquote, parse_qs

logger = logging.getLogger(__name__)

# الگوهای لینک‌های شناخته‌شده
LINK_PATTERNS = [
    # استاندارد با //
    r'(?:vless|vmess|trojan|ss|ssr|hysteria2?|hy2|tuic|wireguard)://[^\s<>"\'\]]+',
    # بعضی جاها بدون // هم می‌نویسند (نادر)
    r'(?:vless|vmess|trojan):[a-zA-Z0-9+/=_-]{20,}@[^\s<>"\']+',
]

# پروتکل‌هایی که موتور تست (xray_tester) واقعاً می‌تونه تست کنه.
# بقیه (ssr/hysteria2/tuic/wireguard/...) قابل تست نیستن و فقط صف رو پر می‌کنن.
TESTABLE_SCHEMES = {"vless", "vmess", "trojan", "ss"}

# کامپایل برای سرعت
COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in LINK_PATTERNS]

# کاراکترهایی که معمولاً ته لینک می‌چسبند و باید پاک بشن
TRAILING_JUNK = set('.,;:!?)）】》>\'"`')


def _clean_link(raw: str) -> str:
    """پاکسازی کاراکترهای اضافی از انتهای لینک"""
    link = raw.strip()
    while link and link[-1] in TRAILING_JUNK:
        link = link[:-1]
    # بعضی وقت‌ها markdown یا html باقی می‌مونه
    link = link.rstrip(']')
    return link


def _is_valid_proxy_link(link: str) -> bool:
    """چک خیلی ساده برای معتبر بودن لینک"""
    if len(link) < 20:
        return False
    try:
        # باید حداقل یک @ یا base64 بلند داشته باشه
        if '://' not in link:
            return False
        scheme = link.split('://', 1)[0].lower()
        if scheme not in {
            'vless', 'vmess', 'trojan', 'ss', 'ssr',
            'hysteria', 'hysteria2', 'hy2', 'tuic', 'wireguard'
        }:
            return False
        return True
    except Exception:
        return False


def extract_links_from_text(text: str) -> list[str]:
    """
    از یک متن خام همه لینک‌های پروکسی رو استخراج می‌کنه.
    تکراری‌ها حذف می‌شن (با حفظ ترتیب).
    """
    if not text:
        return []

    found: list[str] = []
    seen: set[str] = set()

    for pattern in COMPILED_PATTERNS:
        for match in pattern.finditer(text):
            link = _clean_link(match.group(0))
            if not _is_valid_proxy_link(link):
                continue
            # فقط پروتکل‌هایی که موتور تست پشتیبانی می‌کنه (بقیه صف رو پر می‌کنن)
            scheme = link.split('://', 1)[0].lower()
            if scheme not in TESTABLE_SCHEMES:
                continue
            # برای حذف تکراری، نسخه بدون ریمارک رو هش می‌کنیم
            key = normalize_for_dedup(link)
            if key not in seen:
                seen.add(key)
                found.append(link)

    return found


def normalize_for_dedup(link: str) -> str:
    """
    لینک رو برای تشخیص تکراری نرمال می‌کنه.
    ریمارک (#...) و بعضی پارامترهای بی‌اهمیت حذف می‌شن.
    """
    try:
        # ریمارک رو جدا کن
        if '#' in link:
            main_part = link.split('#', 1)[0]
        else:
            main_part = link

        # برای vmess که base64 هست، سعی می‌کنیم decode کنیم و فیلدهای اصلی رو نگه داریم
        if main_part.lower().startswith('vmess://'):
            b64 = main_part[8:]
            # پدینگ
            pad = 4 - len(b64) % 4
            if pad != 4:
                b64 += '=' * pad
            try:
                decoded = base64.b64decode(b64).decode('utf-8', errors='ignore')
                data = json.loads(decoded)
                # فقط فیلدهای هویت‌ساز
                key_fields = {
                    'add': data.get('add') or data.get('host'),
                    'port': data.get('port'),
                    'id': data.get('id'),
                    'aid': data.get('aid'),
                    'net': data.get('net'),
                    'path': data.get('path'),
                    'tls': data.get('tls'),
                    'sni': data.get('sni') or data.get('host'),
                }
                return 'vmess:' + json.dumps(key_fields, sort_keys=True)
            except Exception:
                pass

        # برای بقیه پروتکل‌ها همون قسمت اصلی کافیه
        return main_part.lower().strip()
    except Exception:
        return link.lower().strip()


def config_hash(link: str) -> str:
    """هش یکتا برای ذخیره در دیتابیس"""
    normalized = normalize_for_dedup(link)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:32]


def get_remark(link: str) -> str:
    """استخراج ریمارک فعلی از لینک"""
    if '#' in link:
        return unquote(link.split('#', 1)[1])
    return ""


def set_remark(link: str, new_remark: str) -> str:
    """جایگزینی ریمارک لینک"""
    if '#' in link:
        base = link.split('#', 1)[0]
    else:
        base = link
    # ریمارک رو encode می‌کنیم تا کاراکترهای خاص مشکل نسازن
    from urllib.parse import quote
    return f"{base}#{quote(new_remark, safe='')}"


def parse_basic_info(link: str) -> dict:
    """
    اطلاعات پایه‌ای لینک رو برمی‌گردونه (برای نمایش و لاگ).
    """
    info = {
        "protocol": "",
        "address": "",
        "port": "",
        "remark": get_remark(link),
        "raw": link,
    }
    try:
        lower = link.lower()
        if lower.startswith('vmess://'):
            info["protocol"] = "vmess"
            b64 = link[8:].split('#')[0]
            pad = 4 - len(b64) % 4
            if pad != 4:
                b64 += '=' * pad
            data = json.loads(base64.b64decode(b64).decode('utf-8', errors='ignore'))
            info["address"] = data.get('add') or data.get('host') or ''
            info["port"] = str(data.get('port') or '')
            if not info["remark"]:
                info["remark"] = data.get('ps') or ''
        else:
            # vless / trojan / ss / ...
            parsed = urlparse(link)
            info["protocol"] = parsed.scheme
            info["address"] = parsed.hostname or ''
            info["port"] = str(parsed.port or '')
    except Exception as e:
        logger.debug(f"parse_basic_info failed: {e}")
    return info


# -------------------- استخراج از منابع مختلف --------------------

def extract_from_message_text(text: str) -> list[str]:
    """از متن پیام (معمولی یا فوروارد)"""
    return extract_links_from_text(text or "")


def extract_from_caption(caption: str) -> list[str]:
    """از کپشن فایل یا عکس"""
    return extract_links_from_text(caption or "")


async def extract_from_document(file_bytes: bytes, filename: str = "") -> list[str]:
    """
    از محتوای فایل متنی.
    فقط فایل‌های متنی (.txt, .json, .csv, بدون پسوند و ...) رو قبول می‌کنه.
    """
    # جلوگیری از فایل‌های خیلی بزرگ
    if len(file_bytes) > 5 * 1024 * 1024:  # 5MB
        logger.warning("File too large, skipped")
        return []

    # سعی در تشخیص encoding
    text = None
    for enc in ('utf-8', 'utf-8-sig', 'utf-16', 'latin-1'):
        try:
            text = file_bytes.decode(enc)
            break
        except Exception:
            continue

    if text is None:
        logger.warning(f"Could not decode file: {filename}")
        return []

    return extract_links_from_text(text)


def extract_from_any(
    text: Optional[str] = None,
    caption: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    filename: str = "",
) -> list[str]:
    """
    ترکیب همه منابع و حذف تکراری.
    """
    all_links: list[str] = []
    seen: set[str] = set()

    sources = []
    if text:
        sources.append(extract_from_message_text(text))
    if caption:
        sources.append(extract_from_caption(caption))

    for links in sources:
        for link in links:
            key = normalize_for_dedup(link)
            if key not in seen:
                seen.add(key)
                all_links.append(link)

    # فایل جداگانه (async نیست اینجا، چون caller باید await کنه)
    # این تابع فقط متنی‌ها رو یکجا می‌کنه
    return all_links
