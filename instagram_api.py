from __future__ import annotations
import logging
import requests
from config import SETTINGS

logger = logging.getLogger(__name__)

BASE = "https://graph.facebook.com/v25.0"
TIMEOUT = 10


def _post(endpoint: str, data: dict, token: str) -> bool:
    try:
        resp = requests.post(
            f"{BASE}/{endpoint}",
            params={"access_token": token},
            json=data,
            timeout=TIMEOUT
        )

        if resp.ok:
            logger.info(f"Instagram API Success: {resp.status_code}")
            return True

        logger.error("=" * 80)
        logger.error(f"Endpoint : {BASE}/{endpoint}")
        logger.error(f"Payload  : {data}")
        logger.error(f"Status   : {resp.status_code}")
        logger.error(f"Response : {resp.text}")
        logger.error("=" * 80)

        return False

    except requests.RequestException:
        logger.exception("Instagram request failed")
        return False


def reply_to_comment(comment_id: str, message: str) -> bool:
    # Comments require User Access Token
    return _post(f"{comment_id}/replies", {"message": message}, SETTINGS.ig_user_token)


def send_dm(user_id: str, message: str) -> bool:

    logger.info("=" * 60)
    logger.info(f"Sending Instagram DM")
    logger.info(f"Recipient : {user_id}")
    logger.info(f"Message   : {message}")
    logger.info("=" * 60)

    return _post(
        f"{SETTINGS.page_id}/messages",
        {
            "recipient": {
                "id": user_id
            },
            "message": {
                "text": message
            },
            "messaging_type": "RESPONSE"
        },
        SETTINGS.page_access_token
    )


def get_media_url(media_id: str) -> str | None:
    """Fetch the image/video URL for a specific post."""
    try:
        resp = requests.get(
            f"{BASE}/{media_id}",
            params={
                "fields": "media_url,permalink",
                "access_token": SETTINGS.ig_user_token
            },
            timeout=TIMEOUT
        )
        if resp.ok:
            return resp.json().get("media_url")
        
        logger.error("Failed to fetch media_url %s: %s", resp.status_code, resp.text)
        return None
    except Exception as e:
        logger.error(f"Failed to fetch media_url: {e}")
        return None


def check_token_validity(token_type: str = "ig_user") -> bool:
    """
    Meta debug endpoint से token verify करो।
    """
    token = SETTINGS.ig_user_token if token_type == "ig_user" else SETTINGS.page_access_token
    try:
        # Note: Ideally debug_token should use an APP ACCESS TOKEN as the 'access_token' param
        # but using the token itself often works for basic validation.
        resp = requests.get(
            "https://graph.facebook.com/debug_token",
            params={
                "input_token": token,
                "access_token": token
            },
            timeout=10
        )
        if not resp.ok:
            logger.error(f"{token_type} Token debug failed {resp.status_code}: {resp.text}")
            return False

        data = resp.json().get("data", {})
        is_valid = data.get("is_valid", False)
        
        if is_valid:
            logger.info(f"✅ {token_type} Token is valid.")
            return True
        else:
            error = data.get("error", {})
            logger.error(f"❌ {token_type} Token invalid: {error.get('message', 'Unknown error')}")
            return False

    except Exception as e:
        logger.error(f"{token_type} Token check error: {e}")
        return False
