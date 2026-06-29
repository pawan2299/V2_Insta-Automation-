from __future__ import annotations
import logging
import requests
from config import SETTINGS

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v25.0"   # Comments
INSTA_BASE = "https://graph.instagram.com/v25.0"  # DMs
TIMEOUT = 10


def _graph_post(endpoint: str, data: dict, token: str) -> bool:
    """Graph API — comments के लिए।"""
    try:
        resp = requests.post(
            f"{GRAPH_BASE}/{endpoint}",
            params={"access_token": token},
            json=data,
            timeout=TIMEOUT
        )
        if resp.ok:
            logger.info(f"Graph API Success: {resp.status_code}")
            return True

        logger.error("=" * 80)
        logger.error(f"Endpoint : {GRAPH_BASE}/{endpoint}")
        logger.error(f"Status   : {resp.status_code}")
        logger.error(f"Response : {resp.text}")
        logger.error("=" * 80)
        return False

    except requests.RequestException:
        logger.exception("Graph API request failed")
        return False


def _insta_post(endpoint: str, data: dict, token: str) -> bool:
    """Instagram Login API — DMs के लिए।"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    try:
        resp = requests.post(
            f"{INSTA_BASE}/{endpoint}",
            headers=headers,
            json=data,
            timeout=TIMEOUT
        )
        if resp.ok:
            logger.info(f"Instagram API Success: {resp.status_code}")
            return True

        logger.error("=" * 80)
        logger.error(f"Endpoint : {INSTA_BASE}/{endpoint}")
        logger.error(f"Status   : {resp.status_code}")
        logger.error(f"Response : {resp.text}")
        logger.error("=" * 80)
        return False

    except requests.RequestException:
        logger.exception("Instagram API request failed")
        return False


def reply_to_comment(comment_id: str, message: str) -> bool:
    """Comment reply — Graph API + IGAA token।"""
    return _graph_post(
        f"{comment_id}/replies",
        {"message": message},
        SETTINGS.ig_user_token
    )


def send_dm(user_id: str, message: str) -> bool:
    """DM — Instagram Login API + IGAA token。"""
    logger.info("=" * 60)
    logger.info(f"Sending DM via Instagram Login API")
    logger.info(f"Recipient : {user_id}")
    logger.info(f"Message   : {message[:50]}...")
    logger.info("=" * 60)

    headers = {
        "Authorization": f"Bearer {SETTINGS.dm_access_token}",
        "Content-Type": "application/json"
    }
    try:
        resp = requests.post(
            "https://graph.instagram.com/v25.0/me/messages",
            headers=headers,
            json={
                "recipient": {"id": user_id},
                "message": {"text": message},
            },
            timeout=TIMEOUT
        )
        if resp.ok:
            logger.info(f"DM Success: {resp.status_code}")
            return True

        logger.error("=" * 80)
        logger.error(f"DM Failed  : {resp.status_code}")
        logger.error(f"Response   : {resp.text}")
        logger.error("=" * 80)
        return False

    except requests.RequestException:
        logger.exception("DM request failed")
        return False


def get_media_url(media_id: str) -> str | None:
    """Post का image URL लाओ।"""
    try:
        resp = requests.get(
            f"{GRAPH_BASE}/{media_id}",
            params={
                "fields": "media_url,permalink",
                "access_token": SETTINGS.ig_user_token
            },
            timeout=TIMEOUT
        )
        if resp.ok:
            return resp.json().get("media_url")
        logger.error(f"Media URL fetch failed {resp.status_code}: {resp.text}")
        return None
    except Exception as e:
        logger.error(f"Media URL fetch error: {e}")
        return None


def check_token_validity(token_type: str = "ig_user") -> bool:
    token = (
        SETTINGS.ig_user_token
        if token_type == "ig_user"
        else SETTINGS.page_access_token
    )
    try:
        resp = requests.get(
            "https://graph.facebook.com/debug_token",
            params={
                "input_token": token,
                "access_token": token
            },
            timeout=10
        )
        if not resp.ok:
            logger.error(f"{token_type} debug failed: {resp.text[:200]}")
            return False

        data = resp.json().get("data", {})
        is_valid = data.get("is_valid", False)

        if is_valid:
            logger.info(f"✅ {token_type} token valid.")
        else:
            error = data.get("error", {})
            logger.error(
                f"❌ {token_type} invalid: "
                f"{error.get('message', 'Unknown')}"
            )
        return is_valid

    except Exception as e:
        logger.error(f"Token check error: {e}")
        return False
