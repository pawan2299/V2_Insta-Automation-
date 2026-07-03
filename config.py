from __future__ import annotations
import os
import sys
import logging
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

def _get(key: str, default: str = "") -> str:
    return (os.getenv(key, default) or default).strip()

def _require(key: str) -> str:
    val = _get(key)
    if not val:
        logger.critical(f"Missing required env variable: {key}")
        sys.exit(1)
    return val

@dataclass(frozen=True)
class Settings:
    verify_token: str
    access_token: str  
    ig_user_token: str 
    page_access_token: str 
    app_secret: str
    page_id: str
    own_account_id: str
    gemini_api_keys: list[str]  # ✅ Now supports multiple keys
    database_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    dm_access_token: str
    public_base_url: str = "production"
    environment: str = "production"
    log_level: str = "INFO"
    port: int = 5000

def _load() -> Settings:
    db_url = _require("DATABASE_URL")
    if "sslmode" not in db_url:
        db_url += ("&" if "?" in db_url else "?") + "sslmode=require"
    
    if "keepalives" not in db_url:
        db_url += "&keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=6"

    main_token = _require("ACCESS_TOKEN")
    ig_token = _get("IG_USER_ACCESS_TOKEN", main_token)
    page_token = _get("PAGE_ACCESS_TOKEN", main_token)

    # ── Load Multiple Gemini Keys for Project-Level Quota Pooling ─────────
    gemini_keys = []
    for i in range(1, 11):  # Support up to 10 keys
        key_env = "GEMINI_API_KEY" if i == 1 else f"GEMINI_API_KEY_{i}"
        key = _get(key_env)
        if key:
            gemini_keys.append(key)

    if not gemini_keys:
        logger.critical("Missing at least one GEMINI_API_KEY")
        sys.exit(1)
        
    logger.info(f"Loaded {len(gemini_keys)} Gemini API key(s)")

    return Settings(
        verify_token=_require("VERIFY_TOKEN"),
        access_token=main_token,
        ig_user_token=ig_token,
        page_access_token=page_token,
        app_secret=_require("APP_SECRET"),
        page_id=_require("PAGE_ID"),
        own_account_id=_get("OWN_ACCOUNT_ID"),
        gemini_api_keys=gemini_keys,  
        database_url=db_url,
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_require("TELEGRAM_CHAT_ID"),
        dm_access_token=_get("DM_ACCESS_TOKEN", main_token),
        public_base_url=_get("PUBLIC_BASE_URL", "https://krishnav2.onrender.com"),
        environment=_get("APP_ENV", "production"),
        log_level=_get("LOG_LEVEL", "INFO"),
        port=int(_get("PORT", "5000")),
    )

SETTINGS = _load()
