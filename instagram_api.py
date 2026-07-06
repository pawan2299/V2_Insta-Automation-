from __future__ import annotations
import logging
import requests
import time
from config import SETTINGS

logger = logging.getLogger(__name__)
GRAPH_BASE = "https://graph.facebook.com/v25.0"
TIMEOUT = 10

def _graph_post(endpoint: str, data: dict, token: str, method: str = "POST") -> bool:
    try:
        start_time = time.time()
        resp = requests.request(method, f"{GRAPH_BASE}/{endpoint}", params={"access_token": token}, json=data, timeout=TIMEOUT)
        latency_ms = (time.time() - start_time) * 1000
        if resp.ok:
            logger.info(f"✅ Instagram API Success: {endpoint} | Latency: {latency_ms:.0f}ms")
            return True
        logger.error(f"❌ Instagram API Failed: {endpoint} | Status: {resp.status_code} | Response: {resp.text}")
        return False
    except requests.RequestException: return False

def reply_to_comment(comment_id: str, message: str) -> bool:
    return _graph_post(f"{comment_id}/replies", {"message": message}, SETTINGS.ig_user_token, method="POST")

def send_dm(user_id: str, message: str) -> bool:
    message = message.replace("@", "")
    headers = {"Authorization": f"Bearer {SETTINGS.dm_access_token}", "Content-Type": "application/json"}
    try:
        start_time = time.time()
        resp = requests.post("https://graph.instagram.com/v25.0/me/messages", headers=headers, json={"recipient": {"id": user_id}, "message": {"text": message}}, timeout=TIMEOUT)
        latency_ms = (time.time() - start_time) * 1000
        if resp.ok:
            logger.info(f"✅ Instagram DM Success: {user_id} | Latency: {latency_ms:.0f}ms")
            return True
        logger.error(f"❌ Instagram DM Failed: {user_id} | Status: {resp.status_code} | Response: {resp.text}")
        return False
    except requests.RequestException: return False

def get_media_details(media_id: str) -> dict:
    try:
        start_time = time.time()
        resp = requests.get(f"{GRAPH_BASE}/{media_id}", params={"fields": "media_url,permalink,caption,media_type", "access_token": SETTINGS.ig_user_token}, timeout=TIMEOUT)
        latency_ms = (time.time() - start_time) * 1000
        if resp.ok:
            data = resp.json()
            return {"url": data.get("media_url"), "caption": data.get("caption", ""), "type": data.get("media_type", "")}
        logger.error(f"❌ Failed to fetch media details for {media_id} | Status: {resp.status_code}")
        return {}
    except Exception: return {}

def check_token_validity(token_type: str = "ig_user") -> bool:
    token = SETTINGS.ig_user_token if token_type == "ig_user" else SETTINGS.page_access_token
    try:
        resp = requests.get("https://graph.facebook.com/debug_token", params={"input_token": token, "access_token": token}, timeout=10)
        if not resp.ok: return False
        return resp.json().get("data", {}).get("is_valid", False)
    except Exception: return False

def get_token_expiry_days(token_type: str = "ig_user") -> int | None:
    token = SETTINGS.ig_user_token if token_type == "ig_user" else SETTINGS.page_access_token
    try:
        resp = requests.get("https://graph.facebook.com/debug_token", params={"input_token": token, "access_token": token}, timeout=10)
        if not resp.ok: return None
        expires_at = resp.json().get("data", {}).get("expires_at", 0)
        if expires_at == 0: return -1
        return (expires_at - int(time.time())) // (24 * 3600)
    except Exception: return None