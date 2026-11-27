import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    database_url: str = os.getenv("DATABASE_URL", "")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


settings = Settings()

if not settings.bot_token:
    raise RuntimeError("Не задан BOT_TOKEN")
if not settings.database_url:
    raise RuntimeError("Не задан DATABASE_URL")
