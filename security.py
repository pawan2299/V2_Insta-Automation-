from __future__ import annotations
import hashlib
import hmac
import logging
from config import SETTINGS

logger = logging.getLogger(__name__)

def verify_signature(payload: bytes, signature: str) -> bool:
    if not SETTINGS.app_secret:
        logger.warning("APP_SECRET missing — skipping verification")
        return True
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
        logger.error(f"Signature error: {e}")
        return False