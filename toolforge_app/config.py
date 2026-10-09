import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    database_url: str = "sqlite:///var/toolforge.sqlite3"
    secret_key: str = ""
    public_url: str = "http://127.0.0.1:5000"
    timezone: str = "Europe/Moscow"
    admin_username: str = ""
    start_month: str = "2019-01"
    quiet_minutes: int = 15
    poll_seconds: int = 300
    edit_interval: int = 10
    month_end_time: str = "23:30"
    log_retention_days: int = 90
    wiki_write: bool = False
    bot_username: str = ""
    bot_login: str = ""
    bot_password: str = ""
    user_agent: str = "ruwiki-works/1.0 (https://github.com/sc113/ruwiki-works)"
    oauth_key: str = ""
    oauth_secret: str = ""
    api_url: str = "https://ru.wikipedia.org/w/api.php"
    oauth_url: str = "https://meta.wikimedia.org/w/index.php"

    @property
    def zone(self):
        return ZoneInfo(self.timezone)

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        values = {}
        for name, field in cls.__dataclass_fields__.items():
            raw = os.environ.get("TOOLFORGE_" + name.upper())
            if raw is not None:
                values[name] = raw.lower() in {"true", "1", "yes"} if field.type is bool else int(raw) if field.type is int else raw
        result = cls(**values)
        if result.quiet_minutes < 1 or result.poll_seconds < 1:
            raise ValueError("Intervals must be positive")
        if not 1 <= result.log_retention_days <= 3650:
            raise ValueError("LOG_RETENTION_DAYS must be between 1 and 3650")
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", result.start_month):
            raise ValueError("START_MONTH must be YYYY-MM")
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", result.month_end_time):
            raise ValueError("MONTH_END_TIME must be HH:MM")
        if not 1 <= result.edit_interval <= 60:
            raise ValueError("EDIT_INTERVAL must be between 1 and 60 seconds")
        if result.public_url.startswith("https://") and result.database_url.startswith("sqlite"):
            raise ValueError("Use ToolsDB for a deployed Toolforge service")
        # Production must share a stable signing key across web instances.
        if result.public_url.startswith("https://") and not result.secret_key:
            raise ValueError("Set TOOLFORGE_SECRET_KEY for production")
        result.secret_key = result.secret_key or secrets.token_urlsafe(48)
        result.zone
        return result
