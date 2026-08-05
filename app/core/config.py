from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_DIR = Path("/app/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)


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
