from __future__ import annotations
import logging
import random
from database import (
    is_already_replied, mark_replied,
    claim_welcome_dm, claim_event,
    is_bot_paused, is_gemini_enabled, is_safe_mode,
    get_keyword_reply, is_active_hours,
    mark_human_handled, is_human_handled,  # ← add karo
)
from gemini_client import (
    generate_reply, can_use_gemini,
    generate_dm_reply, generate_welcome_dm,
    is_spam_or_negative,
    _gemini_should_reply_dm,
)
from instagram_api import reply_to_comment, send_dm, get_media_url

logger = logging.getLogger(__name__)

SHORT_REPLIES = [
    "Radhe Radhe! 🙏",
    "Jai Shri Krishna! 🌸",
    "Hari Bol! ✨",
    "🙏💛",
    "Jai Radhe! 🌺",
    "Shri Krishna ki jai! ✨",
]

GREETING_REPLIES = [
    "Radhe Radhe! 🙏 Jai Shri Krishna!",
    "Jai Shri Krishna! 🌸 Hare Krishna!",
    "Hari Bol! 🙏 Welcome, devotee!",
]

PRAISE_REPLIES = [
    "Thank you so much! 🙏 Radhe Radhe!",
    "Your love means everything! Jai Shri Krishna! ✨",
    "Hare Krishna! 🌸 So grateful for you!",
    "Krishna's blessings to you! 💛🙏",
]

WELCOME_DM = (
    "🌸 Radhe Radhe! Thank you so much for following @krishna.verse.ai! 🙏\n\n"
    "May Lord Krishna's love and blessings always surround you. "
    "Stay tuned for beautiful devotional content every day! ✨\n\n"
    "Jai Shri Krishna! 🦚"
)

GREETING_WORDS = {
    "hi", "hello", "hey", "namaste", "radhe", "jai", "hare", "hari", "bol"
}
PRAISE_WORDS = {
    "beautiful", "amazing", "lovely", "nice", "good", "great", "wow",
    "awesome", "love", "cute", "best", "divine", "blessed", "wonderful",
    "superb", "heart"
}

SPAM_SIGNALS = {
    "follow", "check", "link", "bio", "giveaway",
    "free", "click", "promo", "dm me", "collab"
}


def _looks_suspicious(text: str) -> bool:
    """Quick local check — Gemini call से पहले।"""
    lower = text.lower()
    if any(signal in lower for signal in SPAM_SIGNALS):
        return True
    if len(set(text.replace(" ", ""))) < 3:
        return True
    return False


def _classify(text: str) -> str:
    clean = text.lower().strip()
    words = set(clean.split())
    if len(clean) <= 5:
        return "short"
    if words & GREETING_WORDS and len(clean) < 25:
        return "greeting"
    if words & PRAISE_WORDS and len(clean) < 40:
        return "praise"
    if len(clean) > 30:
        return "ai"
    return "short"


def handle_comment(comment_data: dict):
    if is_bot_paused():
        return
    if not is_active_hours():
        logger.debug("Silent hours — skipping comment.")
        return

    comment_id = comment_data.get("id", "")
    text = comment_data.get("text", "").strip()
    from_id = comment_data.get("from", {}).get("id", "")

    if not comment_id or not text or not from_id:
        return

    from config import SETTINGS
    if from_id == SETTINGS.own_account_id:
        return

    # Event Claim for Deduplication
    if not claim_event(comment_id):
        logger.debug(f"Comment {comment_id} already being processed or finished.")
        return

    if is_already_replied(comment_id):
        logger.debug(f"Already replied to {comment_id}, skipping.")
        return

    # Safe Mode Check
    use_ai = is_gemini_enabled() and not is_safe_mode()

    if len(text) > 15 and _looks_suspicious(text) and use_ai:
        if is_spam_or_negative(text):
            logger.info(f"Spam/negative comment ignored: {comment_id}")
            mark_replied(comment_id)
            return

    reply = get_keyword_reply(text)
    reply_type = "keyword"

    if reply is None:
        comment_type = _classify(text)
        reply_type = comment_type

        if comment_type == "ai" and use_ai:
            # Fetch visual context if available
            media_id = comment_data.get("media_id")
            image_url = get_media_url(media_id) if media_id else None
            
            # Note: We can also pass post_caption here if we had it in comment_data
            reply = generate_reply(text, image_url=image_url)

        if reply is None:
            if comment_type == "greeting":
                reply = random.choice(GREETING_REPLIES)
            elif comment_type == "praise":
                reply = random.choice(PRAISE_REPLIES)
            else:
                reply = random.choice(SHORT_REPLIES)

    success = reply_to_comment(comment_id, reply)
    if success:
        mark_replied(comment_id)
        logger.info(f"Replied [{reply_type}] to {comment_id} | SafeMode: {is_safe_mode()}")


def handle_new_follower(user_id: str, username: str = ""):
    if is_bot_paused() or not user_id:
        return
    from config import SETTINGS
    if user_id == SETTINGS.own_account_id:
        return

    if not claim_welcome_dm(user_id):
        logger.debug(f"Welcome DM already claimed for {user_id}")
        return

    # Safe Mode Check
    use_ai = is_gemini_enabled() and not is_safe_mode()

    if username and use_ai:
        dm_text = generate_welcome_dm(username) or WELCOME_DM
    else:
        dm_text = WELCOME_DM

    send_dm(user_id, dm_text)
    logger.info(f"Welcome DM sent to {username or user_id}")


def _notify_human_dm(sender_id: str, message_text: str):
    """
    Human attention चाहिए वाले DMs का
    Telegram पर notification。
    """
    try:
        from telegram_bot import _send
        from config import SETTINGS
        _send(
            SETTINGS.telegram_chat_id,
            f"📩 <b>DM needs your reply!</b>\n\n"
            f"From: <code>{sender_id}</code>\n"
            f"Message: {message_text[:200]}"
        )
    except Exception as e:
        logger.error(f"Notify human DM failed: {e}")


def handle_dm(dm_data: dict):
    if is_bot_paused():
        return

    is_echo = dm_data.get("message", {}).get("is_echo", False)
    if is_echo:
        # ── अगर हमने reply किया → user को human_handled mark करो
        sender_id = dm_data.get("recipient", {}).get("id", "")
        recipient_id = dm_data.get("sender", {}).get("id", "")
        from config import SETTINGS
        if recipient_id and recipient_id not in (
            SETTINGS.own_account_id, SETTINGS.page_id
        ):
            mark_human_handled(recipient_id)
            logger.info(f"Human handled marked: {recipient_id}")
        return

    sender_id = dm_data.get("sender", {}).get("id", "")
    message_text = dm_data.get("message", {}).get("text", "")
    message_id = dm_data.get("message", {}).get("mid", "")

    if not sender_id or not message_text or not message_id:
        return

    from config import SETTINGS
    if sender_id in (SETTINGS.own_account_id, SETTINGS.page_id):
        return

    if not claim_event(message_id):
        return

    recipient_id = dm_data.get("recipient", {}).get("id", "")
    if recipient_id == sender_id:
        return

    # ── Human handled check ───────────────────────────
    if is_human_handled(sender_id):
        logger.info(f"Skipping DM — human handling: {sender_id}")
        return

    use_ai = is_gemini_enabled() and not is_safe_mode()

    reply = get_keyword_reply(message_text)

    if reply is None and use_ai:
        should_reply = _gemini_should_reply_dm(message_text)
        if not should_reply:
            logger.info(f"DM skipped (needs human): {message_text[:50]}")
            _notify_human_dm(sender_id, message_text)
            return
        reply = generate_dm_reply(message_text)

    if reply is None:
        reply = random.choice(GREETING_REPLIES)

    success = send_dm(sender_id, reply)
    if success:
        logger.info(f"DM replied to {sender_id}")
