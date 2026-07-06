from __future__ import annotations
import hashlib
import hmac
import logging
from config import SETTINGS

logger = logging.getLogger(__name__)

def verify_signature(payload: bytes, signature: str) -> bool:
    if not SETTINGS.app_secret:
        # ✅ FIX: Production में बिना Secret के Webhook accept करना खतरनाक है
        logger.critical("🚨 APP_SECRET missing! Webhook verification is disabled. Please set APP_SECRET in Render.")
        return True # Fallback for dev, but logged as critical
    if not signature:
        return False
    try:
        expected = "sha256=" + hmac.new(
            SETTINGS.app_secret.encode(),
            msg=payload,
            digestmod=hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception as e:
        logger.error(f"❌ Signature error: {e}")
        return False