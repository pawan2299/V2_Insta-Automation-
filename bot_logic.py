from __future__ import annotations
import logging
import random
import re # 🌟 ADD THIS

from database import (
    is_already_replied, mark_replied,
    claim_event,
    is_bot_paused, is_gemini_enabled, is_safe_mode,
    get_keyword_reply, is_active_hours,
    is_c2dm_enabled, find_c2dm_trigger # 🌟 ADD THESE
)
from gemini_client import (
    generate_reply, can_use_gemini,
    generate_dm_reply,
    is_spam_or_negative,
    _gemini_should_reply_dm,
)
from instagram_api import reply_to_comment, send_dm, get_media_details

logger = logging.getLogger(__name__)

# 🌸 10 Unified Aesthetic Thank You Messages
AESTHETIC_REPLIES = [
    "Thank you so much for your kind words 🌸✨ Please follow us @krishna.verse.ai 🙏🏻❣️ Radhe Radhe! 🪷",
    "Radhe Radhe! 🙏🏻🙏🏻🙏🏻 Thank you for your beautiful comment 🌺 Please follow @krishna.verse.ai ✨🧡",
    "Jai Shri Krishna! 🦚✨ Your sweet words made our day 🌼 Please follow @krishna.verse.ai 🙏🏻❣️",
    "Thank you so much! 🌸🙏🏻 Stay connected with us @krishna.verse.ai 🌻✨ Radhe Radhe! ☘️",
    "We are so grateful for your love 🪷🧡 Please don't forget to follow @krishna.verse.ai 🙏🏻🌺",
    "Radhe Radhe! 🙏🏻✨ Thank you for your lovely comment 🌸 Follow @krishna.verse.ai ❣️🌼",
    "Hare Krishna! 🌺✨ Thank you for your sweet words 🙏🏻 Please follow @krishna.verse.ai 🌻❣️",
    "Thank you so much! 🌸🙏🏻🙏🏻 Please follow @krishna.verse.ai 🪷✨ Radhe Radhe! 🧡",
    "Thank you for your comment ☘️🌸 Please follow @krishna.verse.ai to join our family 🙏🏻✨",
    "Jai Shri Krishna! 🦚🌺 Thank you for your lovely message 🙏🏻 Follow @krishna.verse.ai ❣️🌼"
]

# 😍 6 Simple Emoji-Only Replies
EMOJI_REPLIES = [
    "🙏🏻🙏🏻🙏🏻 Thank you! Please follow @krishna.verse.ai 🌸✨ Radhe Radhe! ❣️",
    "Radhe Radhe! 🪷✨ Please follow @krishna.verse.ai 🙏🏻🧡",
    "😍😍😍 Thank you so much! 🌸 Follow @krishna.verse.ai 🙏🏻🌺",
    "Jai Shri Krishna! 🦚✨ Please follow @krishna.verse.ai 🌻❣️🙏🏻",
    "Thank you! 🌼✨ Please follow @krishna.verse.ai 🙏🏻☘️ Radhe Radhe!",
    "Hare Krishna! 🌺🧡 Follow @krishna.verse.ai 🙏🏻🪷✨"
]

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
    lower = text.lower()
    if any(signal in lower for signal in SPAM_SIGNALS): return True
    if len(set(text.replace(" ", ""))) < 3: return True
    return False

def _is_emoji_only(text: str) -> bool:
    clean = re.sub(r'[\s!.,?@#\-_]', '', text)
    if not clean: return True 
    if re.search(r'[a-zA-Z0-9]', clean): return False
    return True

def _classify(text: str) -> str:
    clean = text.lower().strip()
    words = set(clean.split())
    if _is_emoji_only(text): return "emoji"
    if len(clean) <= 5: return "short"
    if words & GREETING_WORDS and len(clean) < 25: return "greeting"
    if words & PRAISE_WORDS and len(clean) < 40: return "praise"
    if len(clean) > 30 or "?" in text: return "ai"
    return "short"

def handle_comment(comment_data: dict):
    if is_bot_paused(): return
    if not is_active_hours(): return

    comment_id = comment_data.get("id", "")
    text = comment_data.get("text", "").strip()
    from_id = comment_data.get("from", {}).get("id", "")
    
    if not comment_id or not text or not from_id: return
    from config import SETTINGS
    if from_id == SETTINGS.own_account_id: return
    if not claim_event(comment_id): return
    if is_already_replied(comment_id): return

    # 🌟 NEW: Comment-to-DM Interceptor (Highest Priority)
    if is_c2dm_enabled():
        trigger = find_c2dm_trigger(text)
        if trigger:
            reply_to_comment(comment_id, trigger['public_reply'])
            send_dm(from_id, trigger['dm_message'])
            mark_replied(comment_id)
            logger.info(f"🌸 C2DM Triggered [{trigger['keyword']}] for {comment_id}")
            return

    use_ai = is_gemini_enabled() and not is_safe_mode()
    
    if len(text) > 15 and _looks_suspicious(text) and use_ai:
        if is_spam_or_negative(text):
            mark_replied(comment_id)
            return

    reply = get_keyword_reply(text)
    reply_type = "keyword"
    
    if reply is None:
        comment_type = _classify(text)
        reply_type = comment_type
        
        # 🌟 SIMPLIFIED LOGIC: Short/Greeting/Praise/Emoji -> Unified Aesthetic Pool
        if comment_type in ("emoji", "short", "greeting", "praise"):
            if comment_type == "emoji":
                reply = random.choice(EMOJI_REPLIES)
            else:
                reply = random.choice(AESTHETIC_REPLIES)
                
        # 🤖 AI LOGIC: Long/Complex/Questions -> AI (AI will auto-translate)
        elif comment_type == "ai" and use_ai:
            media_id = comment_data.get("media_id")
            # ✅ Fetch caption and filter out Videos/Reels
            details = get_media_details(media_id) if media_id else {}
            
            # Only pass image_url if it's actually an IMAGE (Gemini crashes on MP4s)
            image_url = details.get("url") if details.get("type") == "IMAGE" else None
            post_caption = details.get("caption", "")
            
            reply = generate_reply(text, post_caption=post_caption, image_url=image_url)
            
        # Ultimate Fallback
        if reply is None:
            reply = random.choice(AESTHETIC_REPLIES)
            reply_type = "fallback"

    success = reply_to_comment(comment_id, reply)
    if success:
        mark_replied(comment_id)
        logger.info(f"Replied [{reply_type}] to {comment_id} | SafeMode: {is_safe_mode()}")


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

    use_ai = is_gemini_enabled() and not is_safe_mode()

    # ── Keyword check पहले ────────────────────────────
    reply = get_keyword_reply(message_text)

    if reply is None and use_ai:
        # ── Gemini से पूछो — reply करूँ या नहीं? ────────
        should_reply = _gemini_should_reply_dm(message_text)

        if not should_reply:
            # तुम्हें Telegram पर बताओ
            logger.info(f"DM skipped (needs human): {message_text[:50]}")
            _notify_human_dm(sender_id, message_text)
            return

        reply = generate_dm_reply(message_text)

    if reply is None:
        # Simple greeting → hardcoded
        reply = random.choice(AESTHETIC_REPLIES)

    success = send_dm(sender_id, reply)
    if success:
        logger.info(f"DM replied to {sender_id}")
