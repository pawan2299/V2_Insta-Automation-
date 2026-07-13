from __future__ import annotations
import logging
import random
import re
import time
from database import (
    is_already_replied, log_reply, claim_event, claim_c2dm_dm,
    is_bot_paused, is_gemini_enabled, is_safe_mode, get_config, set_config,
    get_keyword_reply, is_active_hours, is_c2dm_enabled, find_c2dm_trigger,
    save_dm_memory, is_in_human_handoff, set_human_handoff,
    save_failed_webhook, get_dm_memory, save_conversation_summary, trim_old_memories
)
from gemini_client import (
    generate_reply, can_use_gemini, generate_dm_reply,
    is_spam_or_negative, _gemini_should_reply_dm, classify_comment_intent,
    generate_story_thank_you, summarize_conversation, generate_escalation_ack
)
from instagram_api import reply_to_comment, send_dm, get_media_details

logger = logging.getLogger(__name__)

# ✅ FIX: Pool ko 6 se badhakar 22 kiya - zyada variety, zyada human, kam repeat
# Sirf kabhi-kabhar devotional (Radhe Radhe / Hare Krishna) - har jagah force nahi.
AESTHETIC_REPLIES = [
    "Thank you so much! 🌸✨", "Glad you liked it! 💛", "So sweet of you! ✨",
    "Thank you! 🥺💛", "Haha shukriya! 😄", "Itna support hi humein aage badhata hai 🙏",
    "Yeh sunke acha laga! 😊", "Thanks a lot! ❤️", "Glad it made your day! 🌟",
    "Aww thank you! 🥹", "Sach mein khushi hui yeh padhke 💛", "Appreciate it! 😄",
    "Radhe Radhe 🌸", "Hare Krishna ✨", "Nice to hear that! 😊",
    "Thanks yaar! 🙌", "Bahut acha laga sunke 💫", "Sahi bola! 😄",
    "Thank you so much for this! 🌸", "Glad you're enjoying it! ✨",
    "Yeh comment dekh ke mood ban gaya 😄", "Shukriya itna pyaar dene ke liye 💛",
]
EMOJI_REPLIES = [
    "🙏🏻✨", "Thank you! 🥺💛", "😄❤️", "🙈❤️", "😊🌸", "🔥🔥",
    "Radhe Radhe 🌸", "Hare Krishna ✨", "😄👍", "💛💛",
]
# ✅ FIX: AI now generates a language-matched escalation ack (see generate_escalation_ack).
# These are only used if that AI call itself fails - a small pool spanning English and Hindi/Hinglish
# so the fallback doesn't jarringly switch language mid-conversation.
ESCALATION_ACK_FALLBACKS = [
    "Hi there! 👋 I've passed your message to the admin. They'll get back to you soon! ✨",
    "Aapka message admin tak pahucha diya hai, jaldi hi reply milega! 🙏",
    "Noted! Isko team tak forward kar diya hai, thodi der mein baat hogi 😊",
]

# ✅ FIX: Story-mention thank-you ke liye bhi ab ek chhota diverse pool -
# pehle yeh hamesha ek hi hardcoded "Radhe Radhe" line thi jab AI fail hota tha.
STORY_THANK_YOU_FALLBACKS = [
    "🌸 Thanks so much for sharing on your story! ✨",
    "Aww thank you for the shoutout! 💛",
    "Shukriya itna support karne ke liye! 😊🙏",
    "Thanks for sharing! Means a lot 🌸",
]

def _is_emoji_only(text: str) -> bool:
    clean = re.sub(r'[\s!.,?@#\-_]', '', text)
    if not clean: return True
    if re.search(r'[a-zA-Z0-9\u0900-\u097F]', clean): return False
    return True

SPAM_SIGNALS = {"follow", "check", "link", "bio", "giveaway", "free", "click", "promo", "dm me", "collab"}

def _looks_suspicious(text: str) -> bool:
    lower = text.lower()
    if any(signal in lower for signal in SPAM_SIGNALS): return True
    if len(set(text.replace(" ", ""))) < 3: return True
    return False

def handle_comment(comment_data: dict):
    start_time = time.time()
    try:
        if is_bot_paused() or not is_active_hours(): return
        
        comment_id = comment_data.get("id", "")
        text = comment_data.get("text", "").strip()
        from_id = str(comment_data.get("from", {}).get("id", ""))
        media_id = comment_data.get("media_id", "")
        
        if not comment_id or not text or not from_id: return
        
        from config import SETTINGS
        if from_id == SETTINGS.own_account_id: return
        if not claim_event(comment_id): return
        if is_already_replied(comment_id): return
        
        if is_c2dm_enabled():
            trigger = find_c2dm_trigger(text)
            if trigger:
                logger.info(f"✅ C2DM Triggered for keyword: '{trigger['keyword']}'")
                reply_to_comment(comment_id, trigger['public_reply'])
                if claim_c2dm_dm(from_id, trigger['keyword']):
                    send_dm(from_id, trigger['dm_message'])
                log_reply(comment_id, from_id, trigger['public_reply'], media_id, "c2dm")
                return
                
        use_ai = is_gemini_enabled() and not is_safe_mode()
        reply = None
        reply_type = "unknown"
        
        # ✅ FIX: Keyword priority (Highest priority, before emoji/spam/AI checks)
        reply = get_keyword_reply(text)
        if reply:
            reply_type = "keyword"
        elif _is_emoji_only(text):
            reply = random.choice(EMOJI_REPLIES)
            reply_type = "emoji"
        else:
            if len(text) > 15 and _looks_suspicious(text) and use_ai:
                if is_spam_or_negative(text):
                    logger.info(f"🛡️ [SPAM FILTERED] Comment: {text[:50]}...")
                    log_reply(comment_id, from_id, "[Filtered Spam]", media_id)
                    return
                    
            if use_ai:
                intent = classify_comment_intent(text)
                reply_type = intent
                if intent == "spam":
                    logger.info(f"🛡️ [AI FILTERED] Comment: {text[:50]}...")
                    log_reply(comment_id, from_id, "[AI Filtered Spam]", media_id)
                    return
                elif intent in ("greeting", "praise"):
                    reply = random.choice(AESTHETIC_REPLIES)
                else:
                    details = get_media_details(media_id) if media_id else {}
                    image_url = details.get("url") if details.get("type") == "IMAGE" else None
                    post_caption = details.get("caption", "")
                    reply = generate_reply(text, post_caption=post_caption, image_url=image_url)
                    
            if reply is None:
                reply = random.choice(AESTHETIC_REPLIES)
                reply_type = "fallback"
                
        if reply:
            # 🧠 Anti-Ban: Simulate human reading & typing time (2 to 7 seconds)
            time.sleep(random.uniform(2.0, 7.0))
            if reply_to_comment(comment_id, reply):
                log_reply(comment_id, from_id, reply, media_id, "comment")
                latency = (time.time() - start_time) * 1000
                logger.info(f"✅ Comment Replied [{reply_type}] | ID: {comment_id[:15]}... | Latency: {latency:.0f}ms")
            else:
                logger.error(f"❌ Comment Reply Failed | ID: {comment_id[:15]}...")
                
    except Exception as e:
        logger.error(f"❌ CRITICAL ERROR in handle_comment: {e}")
        save_failed_webhook(comment_data.get("id", "unknown"), comment_data, str(e))

def _notify_human_dm(sender_id: str, message_text: str):
    try:
        from telegram_bot import _send
        from config import SETTINGS
        _send(SETTINGS.telegram_chat_id, f"🚨 <b>DM Escalated to Admin!</b>\nFrom: <code>{sender_id}</code>\nMessage: {message_text[:200]}\n<i>(Bot locked for this user for 24h)</i>")
    except Exception as e: logger.error(f"❌ Notify human DM failed: {e}")

def handle_dm(dm_data: dict):
    start_time = time.time()
    try:
        # ✅ FIX: sleep-hours (/setsleep) was only checked for comments, never for DMs -
        # this is why DMs kept replying through the night even with silent hours set.
        if is_bot_paused() or not is_active_hours(): return
        if dm_data.get("message", {}).get("is_echo", False): return
        
        sender_id = str(dm_data.get("sender", {}).get("id", ""))
        message_text = (dm_data.get("message", {}).get("text") or "").strip()
        message_id = dm_data.get("message", {}).get("mid", "")
        
        if not sender_id or not message_id: return
        
        from config import SETTINGS
        if sender_id in (SETTINGS.own_account_id, SETTINGS.page_id): return
        if not claim_event(message_id): return
        
        attachments = dm_data.get("message", {}).get("attachments", [])
        for att in attachments:
            if att.get("type") == "story_mention":
                logger.info(f"✅ Story Mention detected from {sender_id}")
                story_reply = generate_story_thank_you() or random.choice(STORY_THANK_YOU_FALLBACKS)
                if send_dm(sender_id, story_reply):
                    log_reply(message_id, sender_id, story_reply, source="story_mention")
                    save_dm_memory(sender_id, "bot", story_reply)
                return
                
        if not message_text: return
        if is_in_human_handoff(sender_id): return
        
        save_dm_memory(sender_id, "user", message_text)
        use_ai = is_gemini_enabled() and not is_safe_mode()
        
        reply = get_keyword_reply(message_text)
        reply_type = "keyword"
        
        if reply is None and use_ai:
            should_reply = _gemini_should_reply_dm(message_text, sender_id)
            if not should_reply:
                logger.info(f"🚨 DM Escalated (needs human): {message_text[:50]}...")
                ack = generate_escalation_ack(message_text, sender_id) or random.choice(ESCALATION_ACK_FALLBACKS)
                if send_dm(sender_id, ack):
                    log_reply(f"ack_{message_id}", sender_id, ack, source="dm_ack")
                    save_dm_memory(sender_id, "bot", ack)
                    set_human_handoff(sender_id, 24)
                    _notify_human_dm(sender_id, message_text)
                return
            reply = generate_dm_reply(message_text, sender_id)
            reply_type = "ai"
            
        if reply is None:
            reply = random.choice(AESTHETIC_REPLIES)
            reply_type = "fallback"
            
        # 🧠 Anti-Ban: DMs take slightly longer to type (4 to 10 seconds)
        time.sleep(random.uniform(4.0, 10.0))
        if send_dm(sender_id, reply):
            log_reply(message_id, sender_id, reply, source="dm")
            save_dm_memory(sender_id, "bot", reply)
            latency = (time.time() - start_time) * 1000
            logger.info(f"✅ DM Replied [{reply_type}] | User: {sender_id} | Latency: {latency:.0f}ms")
            check_and_summarize_memory(sender_id)
        else:
            logger.error(f"❌ DM Reply Failed | User: {sender_id}")
            
    except Exception as e:
        logger.error(f"❌ CRITICAL ERROR in handle_dm: {e}")
        save_failed_webhook(dm_data.get("message", {}).get("mid", "unknown"), dm_data, str(e))

def check_and_summarize_memory(user_id: str):
    try:
        messages = get_dm_memory(user_id, 15)
        if len(messages) > 10:
            to_summarize = messages[:-3]
            summary = summarize_conversation(user_id, to_summarize)
            if summary:
                save_conversation_summary(user_id, summary)
                trim_old_memories(user_id, 3)
                logger.info(f"🧠 ✅ Memory Summarized for {user_id}")
    except Exception as e:
        logger.error(f"❌ Summarization failed for {user_id}: {e}")


