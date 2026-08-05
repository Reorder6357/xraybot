"""
موتور تست کانفیگ با هسته Xray.
- تبدیل لینک به outbound
- تست اتصال + اندازه‌گیری latency
- تشخیص کشور از روی IP واقعی خروجی
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import socket
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote

import httpx

from app.core.config import settings
from app.services.config_extractor import (
    config_hash,
    get_remark,
    set_remark,
    parse_basic_info,
)

logger = logging.getLogger(__name__)

XRAY_BIN = os.environ.get("XRAY_LOCATION", "/usr/local/bin/xray")

# پرچم کشورها (رایج‌ترین‌ها)
COUNTRY_FLAGS = {
    "US": "🇺🇸", "DE": "🇩🇪", "NL": "🇳🇱", "GB": "🇬🇧", "FR": "🇫🇷",
    "CA": "🇨🇦", "JP": "🇯🇵", "SG": "🇸🇬", "HK": "🇭🇰", "TW": "🇹🇼",
    "KR": "🇰🇷", "AU": "🇦🇺", "IN": "🇮🇳", "TR": "🇹🇷", "FI": "🇫🇮",
    "SE": "🇸🇪", "NO": "🇳🇴", "PL": "🇵🇱", "IT": "🇮🇹", "ES": "🇪🇸",
    "CH": "🇨🇭", "AT": "🇦🇹", "BE": "🇧🇪", "IE": "🇮🇪", "PT": "🇵🇹",
    "BR": "🇧🇷", "MX": "🇲🇽", "AR": "🇦🇷", "RU": "🇷🇺", "UA": "🇺🇦",
    "IR": "🇮🇷", "AE": "🇦🇪", "SA": "🇸🇦", "IL": "🇮🇱", "EG": "🇪🇬",
    "ZA": "🇿🇦", "NG": "🇳🇬", "ID": "🇮🇩", "MY": "🇲🇾", "TH": "🇹🇭",
    "VN": "🇻🇳", "PH": "🇵🇭", "CN": "🇨🇳", "MO": "🇲🇴", "NZ": "🇳🇿",
    "CZ": "🇨🇿", "RO": "🇷🇴", "HU": "🇭🇺", "BG": "🇧🇬", "GR": "🇬🇷",
    "DK": "🇩🇰", "LT": "🇱🇹", "LV": "🇱🇻", "EE": "🇪🇪", "SK": "🇸🇰",
    "SI": "🇸🇮", "HR": "🇭🇷", "RS": "🇷🇸", "BA": "🇧🇦", "MK": "🇲🇰",
    "AL": "🇦🇱", "MD": "🇲🇩", "BY": "🇧🇾", "KZ": "🇰🇿", "UZ": "🇺🇿",
    "GE": "🇬🇪", "AM": "🇦🇲", "AZ": "🇦🇿", "IQ": "🇮🇶", "PK": "🇵🇰",
    "BD": "🇧🇩", "LK": "🇱🇰", "NP": "🇳🇵", "MM": "🇲🇲", "KH": "🇰🇭",
    "LA": "🇱🇦", "MN": "🇲🇳", "CL": "🇨🇱", "CO": "🇨🇴", "PE": "🇵🇪",
    "VE": "🇻🇪", "EC": "🇪🇨", "UY": "🇺🇾", "PY": "🇵🇾", "BO": "🇧🇴",
    "CR": "🇨🇷", "PA": "🇵🇦", "GT": "🇬🇹", "HN": "🇭🇳", "SV": "🇸🇻",
    "NI": "🇳🇮", "DO": "🇩🇴", "CU": "🇨🇺", "JM": "🇯🇲", "TT": "🇹🇹",
    "IS": "🇮🇸", "LU": "🇱🇺", "MT": "🇲🇹", "CY": "🇨🇾", "LI": "🇱🇮",
    "MC": "🇲🇨", "AD": "🇦🇩", "SM": "🇸🇲", "VA": "🇻🇦", "FO": "🇫🇴",
    "GL": "🇬🇱", "AW": "🇦🇼", "CW": "🇨🇼", "BQ": "🇧🇶", "SX": "🇸🇽",
    "UNKNOWN": "🏳️",
}

COUNTRY_NAMES = {
    "US": "United States", "DE": "Germany", "NL": "Netherlands", "GB": "United Kingdom",
    "FR": "France", "CA": "Canada", "JP": "Japan", "SG": "Singapore", "HK": "Hong Kong",
    "TW": "Taiwan", "KR": "South Korea", "AU": "Australia", "IN": "India", "TR": "Turkey",
    "FI": "Finland", "SE": "Sweden", "NO": "Norway", "PL": "Poland", "IT": "Italy",
    "ES": "Spain", "CH": "Switzerland", "AT": "Austria", "BE": "Belgium", "IE": "Ireland",
    "PT": "Portugal", "BR": "Brazil", "MX": "Mexico", "AR": "Argentina", "RU": "Russia",
    "UA": "Ukraine", "IR": "Iran", "AE": "UAE", "SA": "Saudi Arabia", "IL": "Israel",
    "EG": "Egypt", "ZA": "South Africa", "ID": "Indonesia", "MY": "Malaysia", "TH": "Thailand",
    "VN": "Vietnam", "PH": "Philippines", "CN": "China", "NZ": "New Zealand",
}


@dataclass
class TestResult:
    link: str
    success: bool
    latency_ms: float = 0.0
    country_code: str = ""
    country_name: str = ""
    exit_ip: str = ""
    error: str = ""
    protocol: str = ""
    address: str = ""

    @property
    def flag(self) -> str:
        return COUNTRY_FLAGS.get(self.country_code.upper(), "🏳️")

    def make_remark(self, channel_tag: str = "", tag_enabled: bool = False) -> str:
        base = f"{self.flag} {self.country_name or self.country_code or 'Unknown'}"
        if self.latency_ms > 0:
            base += f" | {int(self.latency_ms)}ms"
        if tag_enabled and channel_tag:
            tag = channel_tag if channel_tag.startswith("@") else f"@{channel_tag}"
            base += f" | {tag}"
        return base

    def with_new_remark(self, channel_tag: str = "", tag_enabled: bool = False) -> str:
        return set_remark(self.link, self.make_remark(channel_tag, tag_enabled))


# -------------------- تبدیل لینک به Outbound Xray --------------------

def _b64decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def link_to_outbound(link: str) -> Optional[dict]:
    """
    لینک share را به یک outbound object برای Xray تبدیل می‌کند.
    در صورت عدم پشتیبانی None برمی‌گرداند.
    """
    try:
        lower = link.lower()
        if lower.startswith("vless://"):
            return _parse_vless(link)
        if lower.startswith("vmess://"):
            return _parse_vmess(link)
        if lower.startswith("trojan://"):
            return _parse_trojan(link)
        if lower.startswith("ss://"):
            return _parse_ss(link)
        # بقیه پروتکل‌ها فعلاً پشتیبانی نمی‌شن در تست
        return None
    except Exception as e:
        logger.debug(f"link_to_outbound failed: {e}")
        return None


def _parse_vless(link: str) -> Optional[dict]:
    # vless://uuid@host:port?params#remark
    parsed = urlparse(link)
    uuid = parsed.username
    host = parsed.hostname
    port = parsed.port or 443
    if not uuid or not host:
        return None

    qs = parse_qs(parsed.query)
    def q(key, default=""):
        return qs.get(key, [default])[0]

    network = q("type", "tcp")
    security = q("security", "none")
    flow = q("flow", "")
    encryption = q("encryption", "none")

    stream: dict = {"network": network, "security": security}

    if security == "tls":
        stream["tlsSettings"] = {
            "serverName": q("sni") or q("host") or host,
            "allowInsecure": q("allowInsecure", "0") in ("1", "true", "True"),
            "fingerprint": q("fp") or "",
        }
        if q("alpn"):
            stream["tlsSettings"]["alpn"] = q("alpn").split(",")
    elif security == "reality":
        stream["realitySettings"] = {
            "serverName": q("sni") or host,
            "fingerprint": q("fp") or "chrome",
            "publicKey": q("pbk") or "",
            "shortId": q("sid") or "",
            "spiderX": q("spx") or "",
        }

    if network == "ws":
        stream["wsSettings"] = {
            "path": q("path") or "/",
            "headers": {"Host": q("host") or q("sni") or host},
        }
    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": q("serviceName") or q("servicename") or "",
            "multiMode": q("mode", "") == "multi",
        }
    elif network == "httpupgrade":
        stream["httpupgradeSettings"] = {
            "path": q("path") or "/",
            "host": q("host") or host,
        }
    elif network == "splithttp" or network == "xhttp":
        stream["xhttpSettings"] = {
            "path": q("path") or "/",
            "host": q("host") or host,
            "mode": q("mode") or "auto",
        }

    outbound = {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": host,
                "port": int(port),
                "users": [{
                    "id": uuid,
                    "encryption": encryption,
                    "flow": flow,
                }],
            }],
        },
        "streamSettings": stream,
    }
    return outbound


def _parse_vmess(link: str) -> Optional[dict]:
    raw = link[8:].split("#")[0]
    try:
        data = json.loads(_b64decode(raw).decode("utf-8"))
    except Exception:
        return None

    host = data.get("add") or data.get("host") or ""
    port = int(data.get("port") or 443)
    uuid = data.get("id") or ""
    if not host or not uuid:
        return None

    network = data.get("net") or "tcp"
    tls = data.get("tls") or ""
    security = "tls" if tls in ("tls", "reality") else "none"

    stream: dict = {"network": network, "security": security}
    if security == "tls":
        stream["tlsSettings"] = {
            "serverName": data.get("sni") or data.get("host") or host,
            "allowInsecure": True,
            "fingerprint": data.get("fp") or "",
        }

    if network == "ws":
        stream["wsSettings"] = {
            "path": data.get("path") or "/",
            "headers": {"Host": data.get("host") or host},
        }
    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": data.get("path") or data.get("serviceName") or "",
        }

    return {
        "tag": "proxy",
        "protocol": "vmess",
        "settings": {
            "vnext": [{
                "address": host,
                "port": port,
                "users": [{
                    "id": uuid,
                    "alterId": int(data.get("aid") or 0),
                    "security": data.get("scy") or "auto",
                }],
            }],
        },
        "streamSettings": stream,
    }


def _parse_trojan(link: str) -> Optional[dict]:
    parsed = urlparse(link)
    password = parsed.username
    host = parsed.hostname
    port = parsed.port or 443
    if not password or not host:
        return None

    qs = parse_qs(parsed.query)
    def q(key, default=""):
        return qs.get(key, [default])[0]

    network = q("type", "tcp")
    security = q("security", "tls")
    stream: dict = {"network": network, "security": security}

    if security in ("tls", "reality"):
        stream["tlsSettings"] = {
            "serverName": q("sni") or host,
            "allowInsecure": q("allowInsecure", "0") in ("1", "true"),
            "fingerprint": q("fp") or "",
        }

    if network == "ws":
        stream["wsSettings"] = {
            "path": q("path") or "/",
            "headers": {"Host": q("host") or host},
        }

    return {
        "tag": "proxy",
        "protocol": "trojan",
        "settings": {
            "servers": [{
                "address": host,
                "port": int(port),
                "password": password,
            }],
        },
        "streamSettings": stream,
    }


def _parse_ss(link: str) -> Optional[dict]:
    # ss://base64(method:password)@host:port  یا  ss://method:password@host:port
    try:
        rest = link[5:]
        if "#" in rest:
            rest = rest.split("#", 1)[0]

        if "@" in rest:
            userinfo, hostport = rest.rsplit("@", 1)
            # userinfo ممکنه base64 باشه
            try:
                if ":" not in userinfo:
                    userinfo = _b64decode(userinfo).decode("utf-8")
            except Exception:
                pass
            method, _, password = userinfo.partition(":")
            host, _, port_s = hostport.partition(":")
            port = int(port_s or 8388)
        else:
            # همه base64
            decoded = _b64decode(rest).decode("utf-8")
            userinfo, _, hostport = decoded.partition("@")
            method, _, password = userinfo.partition(":")
            host, _, port_s = hostport.partition(":")
            port = int(port_s or 8388)

        if not method or not password or not host:
            return None

        return {
            "tag": "proxy",
            "protocol": "shadowsocks",
            "settings": {
                "servers": [{
                    "address": host,
                    "port": port,
                    "method": method,
                    "password": password,
                }],
            },
        }
    except Exception:
        return None


def build_xray_config(outbound: dict, local_port: int) -> dict:
    """کانفیگ موقت Xray با inbound ساکس محلی"""
    return {
        "log": {"loglevel": "error"},
        "inbounds": [{
            "tag": "socks",
            "port": local_port,
            "listen": "127.0.0.1",
            "protocol": "socks",
            "settings": {"udp": False},
        }],
        "outbounds": [
            outbound,
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [],
        },
    }


# -------------------- تست یک کانفیگ --------------------

async def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_one_config(
    link: str,
    timeout: float = 8.0,
) -> TestResult:
    """
    یک کانفیگ را تست می‌کند.
    خروجی: TestResult
    """
    info = parse_basic_info(link)
    result = TestResult(
        link=link,
        success=False,
        protocol=info.get("protocol") or "",
        address=info.get("address") or "",
    )

    outbound = link_to_outbound(link)
    if outbound is None:
        result.error = "unsupported_protocol"
        return result

    port = await _find_free_port()
    config = build_xray_config(outbound, port)

    proc: Optional[asyncio.subprocess.Process] = None
    conf_path: Optional[str] = None

    try:
        # نوشتن کانفیگ موقت
        fd, conf_path = tempfile.mkstemp(suffix=".json", prefix="xray_")
        with os.fdopen(fd, "w") as f:
            json.dump(config, f)

        # اجرای Xray
        proc = await asyncio.create_subprocess_exec(
            XRAY_BIN, "run", "-c", conf_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # کمی صبر برای بالا آمدن
        await asyncio.sleep(0.35)

        if proc.returncode is not None:
            result.error = "xray_start_failed"
            return result

        # تست از طریق SOCKS
        proxy_url = f"socks5://127.0.0.1:{port}"
        start = time.perf_counter()

        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            # دو مرحله: اول یک endpoint سبک برای latency، بعد IP
            try:
                r = await client.get("http://cp.cloudflare.com/")
                latency = (time.perf_counter() - start) * 1000
                result.latency_ms = round(latency, 1)
                result.success = True
            except Exception as e1:
                # fallback
                try:
                    start = time.perf_counter()
                    r = await client.get("http://1.1.1.1/cdn-cgi/trace")
                    latency = (time.perf_counter() - start) * 1000
                    result.latency_ms = round(latency, 1)
                    result.success = True
                except Exception as e2:
                    result.error = f"connect_failed: {type(e2).__name__}"
                    return result

            # گرفتن IP خروجی
            try:
                ip_resp = await client.get("http://ip-api.com/json/?fields=status,country,countryCode,query")
                if ip_resp.status_code == 200:
                    data = ip_resp.json()
                    if data.get("status") == "success":
                        result.exit_ip = data.get("query") or ""
                        result.country_code = (data.get("countryCode") or "").upper()
                        result.country_name = data.get("country") or COUNTRY_NAMES.get(result.country_code, result.country_code)
            except Exception:
                # اگه IP نگرفتیم باز هم موفقیت‌آمیز حساب می‌شه
                pass

        return result

    except Exception as e:
        result.error = str(e)[:120]
        return result
    finally:
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        if conf_path and os.path.exists(conf_path):
            try:
                os.unlink(conf_path)
            except Exception:
                pass


# -------------------- تست دسته‌ای --------------------

async def test_batch(
    links: list[str],
    concurrency: int = 20,
    timeout: float = 8.0,
    progress_callback=None,
) -> list[TestResult]:
    """
    تست موازی لیست کانفیگ‌ها با محدودیت concurrency.
    """
    sem = asyncio.Semaphore(concurrency)
    results: list[TestResult] = []
    done = 0
    total = len(links)

    async def worker(link: str):
        nonlocal done
        async with sem:
            res = await test_one_config(link, timeout=timeout)
            results.append(res)
            done += 1
            if progress_callback and (done % 10 == 0 or done == total):
                try:
                    await progress_callback(done, total, res)
                except Exception:
                    pass

    tasks = [asyncio.create_task(worker(link)) for link in links]
    await asyncio.gather(*tasks)
    return results


def select_top(results: list[TestResult], top_n: int = 20) -> list[TestResult]:
    """فقط موفق‌ها رو بر اساس latency مرتب می‌کنه و top_n برمی‌گردونه"""
    ok = [r for r in results if r.success]
    ok.sort(key=lambda r: r.latency_ms)
    return ok[:top_n]
