from __future__ import annotations
import logging
import random
import re
from database import (
    is_already_replied, log_reply, claim_event, claim_welcome_dm,
    is_bot_paused, is_gemini_enabled, is_safe_mode, get_config, set_config,
    get_keyword_reply, is_active_hours, is_c2dm_enabled, find_c2dm_trigger,
    save_dm_memory
)
from gemini_client import (
    generate_reply, can_use_gemini, generate_dm_reply, generate_welcome_dm,
    is_spam_or_negative, _gemini_should_reply_dm,
)
from instagram_api import reply_to_comment, send_dm, get_media_details

logger = logging.getLogger(__name__)

AESTHETIC_REPLIES = [
    "Thank you so much for your kind words 🌸✨ Please follow us @krishna.verse.ai 🙏🏻❣️ Radhe Radhe! 🪷",
    "Radhe Radhe! 🙏🏻🙏🏻🙏🏻 Thank you for your beautiful comment 🌺 Please follow @krishna.verse.ai ✨🧡",
    "Jai Shri Krishna! 🦚✨ Your sweet words made our day 🌼 Please follow @krishna.verse.ai 🙏🏻❣️",
]
EMOJI_REPLIES = ["🙏🏻🙏🏻🙏🏻 Thank you! Please follow @krishna.verse.ai 🌸✨ Radhe Radhe! ❣️", "Radhe Radhe! 🪷✨ Please follow @krishna.verse.ai 🙏🏻🧡"]
WELCOME_DM = "🌸 Radhe Radhe! Thank you so much for following @krishna.verse.ai! 🙏\nMay Lord Krishna's love always surround you. ✨\nJai Shri Krishna! 🦚"

# ✅ Auto-Acknowledgment Message for Admin Escalation
ESCALATION_ACK_DM = (
    "Radhe Radhe 🙏\n\n"
    "Thank you for reaching out! I have forwarded your message to the admin team. "
    "They will review it personally and get back to you as soon as possible. ✨\n\n"
    "Jai Shri Krishna! 🦚"
)

GREETING_WORDS = {"hi", "hello", "hey", "namaste", "radhe", "jai", "hare", "hari", "bol"}
PRAISE_WORDS = {"beautiful", "amazing", "lovely", "nice", "good", "great", "wow", "awesome", "love", "cute", "best", "divine", "blessed"}
SPAM_SIGNALS = {"follow", "check", "link", "bio", "giveaway", "free", "click", "promo", "dm me", "collab"}

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
    if is_bot_paused() or not is_active_hours(): return
    comment_id = comment_data.get("id", "")
    text = comment_data.get("text", "").strip()
    from_id = comment_data.get("from", {}).get("id", "")
    media_id = comment_data.get("media_id", "")
    
    if not comment_id or not text or not from_id: return
    from config import SETTINGS
    if from_id == SETTINGS.own_account_id: return
    if not claim_event(comment_id): return
    if is_already_replied(comment_id): return

    if is_c2dm_enabled():
        trigger = find_c2dm_trigger(text)
        if trigger:
            reply_to_comment(comment_id, trigger['public_reply'])
            send_dm(from_id, trigger['dm_message'])
            log_reply(comment_id, from_id, trigger['public_reply'], media_id, "c2dm")
            return

    use_ai = is_gemini_enabled() and not is_safe_mode()
    if len(text) > 15 and _looks_suspicious(text) and use_ai:
        if is_spam_or_negative(text):
            log_reply(comment_id, from_id, "[Filtered Spam]", media_id)
            return

    reply = get_keyword_reply(text)
    reply_type = "keyword"
    
    if reply is None:
        comment_type = _classify(text)
        reply_type = comment_type
        if comment_type in ("emoji", "short", "greeting", "praise"):
            reply = random.choice(EMOJI_REPLIES if comment_type == "emoji" else AESTHETIC_REPLIES)
        elif comment_type == "ai" and use_ai:
            details = get_media_details(media_id) if media_id else {}
            image_url = details.get("url") if details.get("type") == "IMAGE" else None
            post_caption = details.get("caption", "")
            reply = generate_reply(text, post_caption=post_caption, image_url=image_url)
            
        if reply is None:
            reply = random.choice(AESTHETIC_REPLIES)
            reply_type = "fallback"

    if reply_to_comment(comment_id, reply):
        log_reply(comment_id, from_id, reply, media_id, "comment")
        logger.info(f"Replied [{reply_type}] to {comment_id}")

def handle_new_follower(user_id: str, username: str = ""):
    if is_bot_paused() or not user_id: return
    from config import SETTINGS
    if user_id == SETTINGS.own_account_id: return
    if not claim_welcome_dm(user_id): return
    
    use_ai = is_gemini_enabled() and not is_safe_mode()
    dm_text = (generate_welcome_dm(username) if username and use_ai else None) or WELCOME_DM
    if send_dm(user_id, dm_text):
        log_reply(f"welcome_{user_id}", user_id, dm_text, source="dm")
        save_dm_memory(user_id, "bot", dm_text)

def _notify_human_dm(sender_id: str, message_text: str):
    try:
        from telegram_bot import _send
        from config import SETTINGS
        _send(SETTINGS.telegram_chat_id, f"📩 <b>DM Escalated to Admin!</b>\n\nFrom: <code>{sender_id}</code>\nMessage: {message_text[:200]}")
    except Exception as e: logger.error(f"Notify human DM failed: {e}")

def handle_dm(dm_data: dict):
    if is_bot_paused(): return
    if dm_data.get("message", {}).get("is_echo", False): return
    
    sender_id = dm_data.get("sender", {}).get("id", "")
    message_text = dm_data.get("message", {}).get("text", "")
    message_id = dm_data.get("message", {}).get("mid", "")
    
    if not sender_id or not message_text or not message_id: return
    from config import SETTINGS
    if sender_id in (SETTINGS.own_account_id, SETTINGS.page_id): return
    if not claim_event(message_id): return
    
    save_dm_memory(sender_id, "user", message_text)
    
    use_ai = is_gemini_enabled() and not is_safe_mode()
    reply = get_keyword_reply(message_text)
    
    if reply is None and use_ai:
        should_reply = _gemini_should_reply_dm(message_text, sender_id)
        
        if not should_reply:
            logger.info(f"DM Escalated (needs human): {message_text[:50]}")
            if send_dm(sender_id, ESCALATION_ACK_DM):
                log_reply(f"ack_{message_id}", sender_id, ESCALATION_ACK_DM, source="dm_ack")
                save_dm_memory(sender_id, "bot", ESCALATION_ACK_DM)
            _notify_human_dm(sender_id, message_text)
            return
            
        reply = generate_dm_reply(message_text, sender_id)
        
    if reply is None: reply = random.choice(AESTHETIC_REPLIES)
    
    if send_dm(sender_id, reply):
        log_reply(message_id, sender_id, reply, source="dm")
        save_dm_memory(sender_id, "bot", reply)
