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
    admin_ids: list[int] = []               # ادمین‌های اضافی

    # GitHub deploy
    github_token: Optional[str] = None
    github_repo: Optional[str] = None       # username/repo

    # Telegram MTProto (اسکن کانال با Telethon)
    tg_api_id: Optional[int] = None         # از my.telegram.org
    tg_api_hash: Optional[str] = None
    scanner_phone: Optional[str] = None     # شماره اکانت اسکنر

    # Paths
    db_path: Path = DATA_DIR / "bot.db"

    def is_owner(self, user_id: int) -> bool:
        return self.owner_id is not None and user_id == self.owner_id

    def is_admin(self, user_id: int) -> bool:
        if self.is_owner(user_id):
            return True
        return user_id in self.admin_ids

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.owner_id)


settings = Settings()
