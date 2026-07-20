from __future__ import annotations
import json
import logging
import random
import re
import time
from datetime import datetime, timezone, timedelta
from database import (
    is_already_replied, log_reply, claim_event, claim_c2dm_dm,
    is_bot_paused, is_gemini_enabled, is_safe_mode, get_config, set_config,
    get_keyword_reply, is_active_hours, is_c2dm_enabled, find_c2dm_trigger,
    save_dm_memory, is_in_human_handoff, set_human_handoff,
    save_failed_webhook, get_dm_memory, save_conversation_summary, trim_old_memories,
    enqueue_pending_reply, delete_pending_batch
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

# ============================================================================
# ✅ NEW (Part A): Humanised reply delay ranges.
# Every outbound reply (AI-generated AND deterministic keyword/C2DM replies)
# now goes through the pending_replies queue instead of firing the instant a
# webhook arrives. Story mentions are intentionally NOT included - they're a
# separate known issue, out of scope for this change.
# ============================================================================
COMMENT_DELAY_MIN = 30    # seconds
COMMENT_DELAY_MAX = 120   # seconds (2 min)
DM_DELAY_MIN = 60         # seconds (1 min)
DM_DELAY_MAX = 300        # seconds (5 min)
# Hard cap measured from the FIRST message in a batch - a user who keeps typing
# never pushes their reply past this, even if each new message keeps re-rolling
# a fresh random delay.
BATCH_CEILING_SECONDS = 420  # 7 minutes


def _enqueue(user_id: str, source: str, media_id: str | None, event_id: str,
             text: str, min_delay: float, max_delay: float):
    delay = random.uniform(min_delay, max_delay)
    candidate = datetime.now(timezone.utc) + timedelta(seconds=delay)
    enqueue_pending_reply(user_id, source, media_id, event_id, text, candidate, BATCH_CEILING_SECONDS)

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
    """
    ✅ CHANGED (Part A): no longer decides/sends a reply itself. It only does the
    cheap, immediate checks (pause/hours/dedup) and then enqueues the comment into
    pending_replies. The actual keyword/C2DM/AI decision + reply now happens later
    in _process_comment_batch(), called by the background pending-reply scheduler.
    """
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

        _enqueue(from_id, "comment", media_id, comment_id, text, COMMENT_DELAY_MIN, COMMENT_DELAY_MAX)

    except Exception as e:
        logger.error(f"❌ CRITICAL ERROR in handle_comment (enqueue): {e}")
        save_failed_webhook(comment_data.get("id", "unknown"), comment_data, str(e))


def _process_comment_batch(from_id: str, media_id: str | None, messages: list[dict]):
    """
    Runs in the background pending-reply-scheduler thread, NOT in a webhook thread.
    Combines every message in this batch (usually 1, sometimes several if the same
    person commented repeatedly on the same post within the delay window) into one
    piece of context, then applies the exact same decision logic handle_comment()
    used to apply instantly: C2DM > keyword > emoji > spam-filter > AI intent.
    Replies are always sent against the LATEST comment_id in the batch (that's the
    one Instagram will thread the reply under).
    """
    start_time = time.time()
    combined_text = "\n".join(m["text"] for m in messages)
    latest_comment_id = messages[-1]["event_id"]

    if is_c2dm_enabled():
        trigger = find_c2dm_trigger(combined_text)
        if trigger:
            logger.info(f"✅ C2DM Triggered for keyword: '{trigger['keyword']}'")
            time.sleep(random.uniform(1.0, 2.5))
            reply_to_comment(latest_comment_id, trigger['public_reply'])
            if claim_c2dm_dm(from_id, trigger['keyword']):
                send_dm(from_id, trigger['dm_message'])
            log_reply(latest_comment_id, from_id, trigger['public_reply'], media_id, "c2dm")
            return

    use_ai = is_gemini_enabled() and not is_safe_mode()
    reply = None
    reply_type = "unknown"

    # ✅ Keyword priority (Highest priority, before emoji/spam/AI checks)
    reply = get_keyword_reply(combined_text)
    if reply:
        reply_type = "keyword"
    elif _is_emoji_only(combined_text):
        reply = random.choice(EMOJI_REPLIES)
        reply_type = "emoji"
    else:
        if len(combined_text) > 15 and _looks_suspicious(combined_text) and use_ai:
            if is_spam_or_negative(combined_text):
                logger.info(f"🛡️ [SPAM FILTERED] Comment: {combined_text[:50]}...")
                log_reply(latest_comment_id, from_id, "[Filtered Spam]", media_id)
                return

        if use_ai:
            intent = classify_comment_intent(combined_text)
            reply_type = intent
            if intent == "spam":
                logger.info(f"🛡️ [AI FILTERED] Comment: {combined_text[:50]}...")
                log_reply(latest_comment_id, from_id, "[AI Filtered Spam]", media_id)
                return
            elif intent in ("greeting", "praise"):
                reply = random.choice(AESTHETIC_REPLIES)
            else:
                details = get_media_details(media_id) if media_id else {}
                image_url = details.get("url") if details.get("type") == "IMAGE" else None
                post_caption = details.get("caption", "")
                reply = generate_reply(combined_text, post_caption=post_caption, image_url=image_url)

        if reply is None:
            reply = random.choice(AESTHETIC_REPLIES)
            reply_type = "fallback"

    if reply:
        # Small buffer only - the batch delay itself already provides the
        # realistic "thought about it" timing, so this no longer needs to be
        # the full 2-7s it used to be.
        time.sleep(random.uniform(1.0, 2.5))
        if reply_to_comment(latest_comment_id, reply):
            log_reply(latest_comment_id, from_id, reply, media_id, "comment")
            latency = (time.time() - start_time) * 1000
            logger.info(f"✅ Comment Replied [{reply_type}] | ID: {latest_comment_id[:15]}... | Batch: {len(messages)} msg(s) | Latency: {latency:.0f}ms")
        else:
            logger.error(f"❌ Comment Reply Failed | ID: {latest_comment_id[:15]}...")

def _notify_human_dm(sender_id: str, message_text: str):
    try:
        from telegram_bot import _send
        from config import SETTINGS
        _send(SETTINGS.telegram_chat_id, f"🚨 <b>DM Escalated to Admin!</b>\nFrom: <code>{sender_id}</code>\nMessage: {message_text[:200]}\n<i>(Bot locked for this user for 24h)</i>")
    except Exception as e: logger.error(f"❌ Notify human DM failed: {e}")

def handle_dm(dm_data: dict):
    """
    ✅ CHANGED (Part A): no longer decides/sends a reply itself for normal text DMs.
    Story mentions are UNCHANGED and stay instant - that's a separate, already-known
    issue, intentionally kept out of scope here. Everything else (keyword/AI DM
    replies) now goes into pending_replies and is handled later by
    _process_dm_batch() in the background scheduler thread.
    """
    try:
        # ✅ (kept) sleep-hours (/setsleep) applies to DMs too.
        if is_bot_paused() or not is_active_hours(): return
        if dm_data.get("message", {}).get("is_echo", False): return

        sender_id = str(dm_data.get("sender", {}).get("id", ""))
        message_text = (dm_data.get("message", {}).get("text") or "").strip()
        message_id = dm_data.get("message", {}).get("mid", "")

        if not sender_id or not message_id: return

        from config import SETTINGS
        if sender_id in (SETTINGS.own_account_id, SETTINGS.page_id): return
        if not claim_event(message_id): return

        # Story mentions: intentionally left untouched, still instant.
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

        # Handoff messages are still saved to memory immediately, but never
        # enqueued for an auto-reply - a human is expected to respond.
        if is_in_human_handoff(sender_id):
            save_dm_memory(sender_id, "user", message_text)
            return

        # Memory is saved immediately (not delayed) so conversation_memory stays
        # accurate in real time regardless of how long the reply itself waits.
        save_dm_memory(sender_id, "user", message_text)

        _enqueue(sender_id, "dm", None, message_id, message_text, DM_DELAY_MIN, DM_DELAY_MAX)

    except Exception as e:
        logger.error(f"❌ CRITICAL ERROR in handle_dm (enqueue): {e}")
        save_failed_webhook(dm_data.get("message", {}).get("mid", "unknown"), dm_data, str(e))


def _process_dm_batch(sender_id: str, messages: list[dict]):
    """
    Runs in the background pending-reply-scheduler thread. Combines every DM in
    this batch into one piece of context for a single, better-informed reply -
    instead of replying to each message separately as they arrive.
    """
    start_time = time.time()
    combined_text = "\n".join(m["text"] for m in messages)
    latest_message_id = messages[-1]["event_id"]

    # Defensive re-check: handoff could theoretically have been set in between
    # enqueue time and processing time (e.g. admin manually escalated meanwhile).
    if is_in_human_handoff(sender_id):
        return

    use_ai = is_gemini_enabled() and not is_safe_mode()

    reply = get_keyword_reply(combined_text)
    reply_type = "keyword"

    if reply is None and use_ai:
        should_reply = _gemini_should_reply_dm(combined_text, sender_id)
        if not should_reply:
            logger.info(f"🚨 DM Escalated (needs human): {combined_text[:50]}...")
            ack = generate_escalation_ack(combined_text, sender_id) or random.choice(ESCALATION_ACK_FALLBACKS)
            time.sleep(random.uniform(1.0, 3.0))
            if send_dm(sender_id, ack):
                log_reply(f"ack_{latest_message_id}", sender_id, ack, source="dm_ack")
                save_dm_memory(sender_id, "bot", ack)
                set_human_handoff(sender_id, 24)
                _notify_human_dm(sender_id, combined_text)
            return
        reply = generate_dm_reply(combined_text, sender_id)
        reply_type = "ai"

    if reply is None:
        reply = random.choice(AESTHETIC_REPLIES)
        reply_type = "fallback"

    # Small buffer only - the batch delay itself already provides the
    # realistic "thought about it" timing.
    time.sleep(random.uniform(1.0, 3.0))
    if send_dm(sender_id, reply):
        log_reply(latest_message_id, sender_id, reply, source="dm")
        save_dm_memory(sender_id, "bot", reply)
        latency = (time.time() - start_time) * 1000
        logger.info(f"✅ DM Replied [{reply_type}] | User: {sender_id} | Batch: {len(messages)} msg(s) | Latency: {latency:.0f}ms")
        check_and_summarize_memory(sender_id)
    else:
        logger.error(f"❌ DM Reply Failed | User: {sender_id}")


def process_pending_batch(batch: dict):
    """
    Entry point called by database.start_pending_reply_scheduler() for every due
    batch. Dispatches to the comment or DM processor, then deletes the batch row.
    If this raises, the caller (database.py's scheduler loop) puts the batch back
    to 'pending' so it gets retried on the next poll instead of being lost.
    """
    source = batch["source"]
    user_id = batch["user_id"]
    media_id = batch.get("media_id")
    messages = batch["messages"]
    if isinstance(messages, str):
        messages = json.loads(messages)

    if not messages:
        delete_pending_batch(batch["id"])
        return

    if source == "comment":
        _process_comment_batch(user_id, media_id, messages)
    elif source == "dm":
        _process_dm_batch(user_id, messages)
    else:
        logger.error(f"❌ Unknown pending_replies source '{source}' for batch id={batch['id']}")

    delete_pending_batch(batch["id"])

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




