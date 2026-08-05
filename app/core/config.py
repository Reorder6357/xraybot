import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    """مسیر داده: اول env DATA_DIR، بعد /app/data (داخل داکر/ریل‌وی)،
    و اگر قابل نوشتن نبود (تست لوکال) مسیر محلی data/"""
    env = os.environ.get("DATA_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    p = Path("/app/data")
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:
        local = Path.cwd() / "data"
        local.mkdir(parents=True, exist_ok=True)
        return local


DATA_DIR = _default_data_dir()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Bot
    bot_token: Optional[str] = None
    owner_id: Optional[int] = None          # فقط مدیر اصلی
    admin_ids: list[int] = []               # ادمین‌های اضافی (فقط استفاده، نه ادیت حساس)

    # GitHub deploy
    github_token: Optional[str] = None
    github_repo: Optional[str] = None       # username/repo

    # Runtime
    timezone: str = "Asia/Tehran"
    max_configs_per_run: int = 3000
    test_concurrency: int = 25              # تعداد تست همزمان
    test_timeout: int = 8                   # ثانیه
    keep_top_n: int = 20
    history_ttl_hours: int = 24
    speed_test_enabled: bool = True         # تست سرعت دانلود (رد سرورهای خیلی کند)
    min_speed_kbps: int = 50                # حداقل سرعت قبول (کیلوبیت بر ثانیه)

    # Paths
    db_path: Path = DATA_DIR / "bot.db"
    geoip_path: Path = DATA_DIR / "GeoLite2-Country.mmdb"

    def is_owner(self, user_id: int) -> bool:
        return self.owner_id is not None and user_id == self.owner_id

    def is_admin(self, user_id: int) -> bool:
        if self.is_owner(user_id):
            return True
        return user_id in self.admin_ids

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.owner_id)


settings = Settings()
