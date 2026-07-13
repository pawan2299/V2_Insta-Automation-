from __future__ import annotations
import logging
import time
import json
import re
import requests
from collections import deque
from urllib.parse import urlparse
import ipaddress
import socket
from google import genai
from google.genai import types
from config import SETTINGS
from database import (
    get_config, set_config, get_model_rpd, increment_gemini_count,
    get_recent_replies, get_dm_memory, get_conversation_summary
)

logger = logging.getLogger(__name__)

_clients: list[genai.Client] = []

def _init_clients():
    global _clients
    _clients = [genai.Client(api_key=k) for k in SETTINGS.gemini_api_keys if k]

_init_clients()

MODEL_CONFIGS = [
    {"id": "gemini-3.5-flash", "rpm": 10, "rpd": 1500, "label": "3.5 Flash (Core)"},
    {"id": "gemini-3.1-flash-lite", "rpm": 15, "rpd": 1500, "label": "3.1 Lite (Filters)"},
    {"id": "gemini-2.5-pro", "rpm": 5, "rpd": 50, "label": "2.5 Pro (Deep)"},
    {"id": "gemini-2.5-flash", "rpm": 10, "rpd": 1500, "label": "2.5 Flash (Fallback)"},
]

model_usage_counter = {m["id"]: 0 for m in MODEL_CONFIGS}
_model_rpm_calls: dict[str, deque] = {m["id"]: deque() for m in MODEL_CONFIGS}

def is_safe_url(url: str) -> bool:
    """Blocks private IPs (localhost, AWS/Render metadata, etc.) to prevent SSRF."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'): return False
        hostname = parsed.hostname
        if not hostname: return False
        ip = ipaddress.ip_address(socket.gethostbyname(hostname))
        return ip.is_global
    except Exception:
        return False

def _record_call(model_id: str):
    _model_rpm_calls[model_id].append(time.time())
    increment_gemini_count(model_id)

def _record_failure_and_maybe_trip_breaker():
    count = int(get_config("consecutive_429s") or 0) + 1
    set_config("consecutive_429s", str(count))
    if count >= 8:
        until = time.time() + 900
        set_config("circuit_breaker_until", str(until))
        logger.critical(f"🚨 Circuit Breaker Tripped! {count} consecutive failures. AI disabled for 15 mins.")

def _clean_json_string(text: str) -> str:
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text.strip())
    return text.strip()

# ✅ FIX: Reply beech mein kat jaane wali bug ka safety-net.
# Root cause thi ki thinking-models apna internal reasoning bhi max_output_tokens
# budget mein se hi kaatte the, isliye asli reply adhoora reh jaata tha.
# thinking_budget=0 (neeche _generate mein) usse pehle hi rok deta hai,
# yeh function sirf ek extra safety-layer hai agar kabhi phir bhi cut ho jaaye.
def _trim_incomplete_sentence(text: str) -> str:
    """Agar reply beech mein kat gaya ho, toh aakhri adhoore vaakya ko hata dega."""
    if not text:
        return text
    enders = ['😊', '🙏', '🦚', '🌺', '❣️', '💛', '😍', '❤️', '🔥']
    if text[-1] in enders:
        return text
    positions = [text.rfind(e) for e in enders if text.rfind(e) != -1]
    if not positions:
        return text  # kuch bhi na mile toh jaisa hai waisa hi rehne do
    last_pos = max(positions)
    if last_pos > 10:  # bahut chhota trim na ho jaaye
        return text[:last_pos + 1].strip()
    return text

def _generate(prompt: str, max_length: int = 200, task_type: str = "comment", image_url: str | None = None, response_schema: dict | None = None) -> str | None:
    if task_type in ["spam", "dm_filter", "intent", "summary"]:
        models_to_try = ["gemini-3.1-flash-lite", "gemini-2.5-flash"]
    elif task_type == "dm":
        models_to_try = ["gemini-3.5-flash", "gemini-2.5-pro", "gemini-2.5-flash"]
    else:
        models_to_try = ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash"]

    contents = [prompt]

    if image_url:
        # 🛡️ SSRF Protection: Block internal/private IPs
        if not is_safe_url(image_url):
            logger.warning(f"🛡️ SSRF Blocked: Unsafe image URL {image_url}")
        else:
            try:
                head_resp = requests.head(image_url, timeout=3, allow_redirects=True)
                content_length = int(head_resp.headers.get('Content-Length', 0))
                if 0 < content_length < 2 * 1024 * 1024:
                    img_data = requests.get(image_url, timeout=5).content
                    contents.append(types.Part.from_bytes(data=img_data, mime_type="image/jpeg"))
                    del img_data
            except Exception as e:
                logger.warning(f"⚠️ Image download skipped for {image_url}: {e}")

    # ✅ FIX: max_output_tokens mein safety-buffer + thinking_budget=0
    # (yehi asli fix hai "reply beech mein kat jaane" wali bug ka)
    config_kwargs = {
        "max_output_tokens": max(max_length, 300),
        "temperature": 0.9,
        "top_p": 0.95,
        "thinking_config": types.ThinkingConfig(thinking_budget=0),
    }
    if response_schema:
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = response_schema
        config_kwargs["temperature"] = 0.1

    config = types.GenerateContentConfig(**config_kwargs)

    for client_idx, client in enumerate(_clients):
        for model_id in models_to_try:
            until = get_config(f"cooldown_{model_id}_{client_idx}")
            if until and until != "0" and time.time() < float(until):
                continue

            calls = _model_rpm_calls[model_id]
            now = time.time()
            while calls and now - calls[0] > 60:
                calls.popleft()

            model_conf = next((m for m in MODEL_CONFIGS if m["id"] == model_id), None)
            if not model_conf or len(calls) >= model_conf["rpm"]:
                continue
            if get_model_rpd(model_id) >= model_conf["rpd"]:
                continue

            try:
                start_time = time.time()
                resp = client.models.generate_content(model=model_id, contents=contents, config=config)
                latency_ms = (time.time() - start_time) * 1000
                _record_call(model_id)
                set_config("consecutive_429s", "0")
                logger.info(f"✅ Gemini API Success: {model_id} (Key {client_idx}) | Latency: {latency_ms:.0f}ms | Task: {task_type}")
                return (resp.text or "").strip()
            except Exception as e:
                error_msg = str(e)
                logger.error(f"❌ Gemini API Failed: {model_id} (Key {client_idx}) | Error: {error_msg}")
                if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                    set_config(f"cooldown_{model_id}_{client_idx}", str(time.time() + 300))
                    _record_failure_and_maybe_trip_breaker()
                    continue
                elif "400" in error_msg and "SAFETY" in error_msg:
                    return None
                else:
                    _record_failure_and_maybe_trip_breaker()
                    break
    return None

def generate_reply(comment_text: str, post_caption: str = "", image_url: str | None = None) -> str | None:
    if not can_use_gemini(): return None
    visual_instr = "\nAnalyze the post image to make your reply specific to the visual content." if image_url else ""
    context = f"\nPost Context: {post_caption[:150]}" if post_caption else ""
    recent = get_recent_replies(5)
    history_context = "\nRecent replies (DO NOT REPEAT THESE): " + " | ".join(recent) if recent else ""

    # ✅ FIX: Naya human-team-member persona + tone-matching + devotional-restraint rule
    prompt = (
        "You are a real team member replying to comments on @krishna.verse.ai's Instagram page. "
        "Reply exactly like a genuine human page-admin would - NOT a spiritual bot, NOT a customer-service bot.\n"
        "RULES:\n"
        "- React to the SPECIFIC thing the person said - never a generic templated reply.\n"
        "- Vary your length naturally - sometimes 2-3 words, sometimes a full sentence, like real texting.\n"
        "- Match the commenter's TONE: if they're casual/funny, be casual back. If they're polite or "
        "professional (business, collab, formal Hindi/English), reply politely and professionally too - no jokes, no slang.\n"
        "- Devotional words (Radhe Radhe, Hare Krishna etc.) ONLY if the comment itself is genuinely devotional/spiritual "
        "in tone. Never force them into casual comments.\n"
        "- NEVER ask people to follow.\n"
        "- Match the user's language and script exactly - whatever language they wrote in (Hindi, English, "
        "Hinglish, or any other language like Tamil, Bengali, Marathi, Spanish, etc.). Reply naturally in "
        "that same language, don't default to Hindi or English.\n"
        "- If someone genuinely and directly asks whether you are AI, a bot, or an app, answer honestly and "
        "briefly - a light, casual admission is fine, but NEVER deny it or dodge the question.\n"
        "- Do NOT use markdown formatting like **bold**.\n"
        "Output ONLY the exact reply text. No prefixes, no quotes.\n"
        f"{history_context}{visual_instr}{context}\n"
        f"Comment: {comment_text}"
    )
    result = _generate(prompt, max_length=150, task_type="comment", image_url=image_url)
    if not result: return None

    text = result.strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1]
    text = text.replace("**", "").replace("*", "").replace('"', '').replace('\n', ' ').replace('\r', '').strip()
    while "  " in text: text = text.replace("  ", " ")
    text = _trim_incomplete_sentence(text)  # ✅ FIX: safety-net agar phir bhi cut ho
    return text

def generate_dm_reply(message_text: str, user_id: str) -> str | None:
    if not can_use_gemini(): return None
    summary = get_conversation_summary(user_id)
    history = get_dm_memory(user_id, 3)
    history_str = "\n".join([f"{m['role']}: {m['message_text']}" for m in history]) if history else ""
    summary_context = f"\n[Long-term Context Summary]: {summary}\n" if summary else ""

    # ✅ FIX: DM mein bhi wahi human-team-member persona + tone-matching + devotional-restraint
    prompt = (
        "You are a real team member replying to Instagram DMs for @krishna.verse.ai. "
        "Reply exactly like a genuine human would - NOT a spiritual bot, NOT a customer-service script.\n"
        "RULES:\n"
        "- React to the SPECIFIC thing the person said - never generic or templated.\n"
        "- Match the sender's TONE: if they're casual/friendly, be casual back. If they're polite or "
        "professional (business, collab, serious question), reply politely and professionally - no jokes, no slang.\n"
        "- Devotional words (Radhe Radhe, Hare Krishna etc.) ONLY if the person's message is genuinely "
        "devotional/spiritual in tone. Never force them.\n"
        "- Don't volunteer that you're AI unprompted. But if someone genuinely and directly asks whether "
        "you are AI, a bot, or an app, answer honestly and briefly - a light, casual admission is fine, "
        "but NEVER deny it or dodge the question. Denying it when sincerely asked is dishonest and not allowed.\n"
        "- Don't make up or confidently answer personal questions about the page owner's life, other jobs, "
        "or activities unrelated to this page - keep it brief and vague rather than inventing details.\n"
        "- Match the user's language and script exactly - whatever language they wrote in (Hindi, English, "
        "Hinglish, or any other language). Reply naturally in that same language.\n"
        "- Vary length naturally like real texting - don't always write the same length.\n"
        f"{summary_context}"
        f"Recent Messages:\n{history_str}\n"
        f"Follower's new message: {message_text}\nYour reply:"
    )
    result = _generate(prompt, max_length=300, task_type="dm")
    if not result: return None
    return _trim_incomplete_sentence(result.strip())  # ✅ FIX: safety-net agar phir bhi cut ho

def generate_escalation_ack(message_text: str, user_id: str) -> str | None:
    """Short 'passed to admin' message, matched to the language the user is writing in."""
    if not can_use_gemini(): return None
    history = get_dm_memory(user_id, 3)
    history_str = "\n".join([f"{m['role']}: {m['message_text']}" for m in history]) if history else ""
    prompt = (
        "Write a very short (max 15 words), warm message telling the person their message has been "
        "passed to the admin/team and they'll get a reply soon. "
        "Match the language/script the person is writing in (Hindi, Hinglish, or English).\n"
        f"Recent messages:\n{history_str}\nTheir message: {message_text}\nYour message:"
    )
    result = _generate(prompt, max_length=80, task_type="dm")
    if not result: return None
    return _trim_incomplete_sentence(result.strip())

def generate_welcome_dm(username: str) -> str | None:
    if not can_use_gemini(): return None
    prompt = (f"Draft a very short, cute welcome DM for '{username}' who followed @krishna.verse.ai. "
              "Max 15 words. No heavy blessings. Just a warm 'Glad to have you here! 🌸' vibe.")
    return _generate(prompt, max_length=100, task_type="dm")

def generate_story_thank_you() -> str | None:
    if not can_use_gemini(): return None
    prompt = ("A follower just mentioned @krishna.verse.ai in their Story. "
              "Draft a very short, cute thank you DM. Max 15 words. No heavy blessings.")
    return _generate(prompt, max_length=100, task_type="dm")

def generate_caption(topic: str) -> str | None:
    if not can_use_gemini(): return None
    prompt = f"Write Instagram caption for @krishna.verse.ai.\nTopic: {topic}\n3-4 lines, aesthetic tone, 5-8 hashtags at end."
    return _generate(prompt, max_length=1000)

def classify_comment_intent(text: str) -> str:
    if not can_use_gemini(): return "general"
    prompt = f"Classify intent:\n- EMOJI\n- GREETING\n- PRAISE\n- QUESTION\n- SPAM\n- GENERAL\nComment: {text}"
    schema = {"type": "OBJECT", "properties": {"intent": {"type": "STRING", "enum": ["EMOJI", "GREETING", "PRAISE", "QUESTION", "SPAM", "GENERAL"]}}, "required": ["intent"]}
    result = _generate(prompt, max_length=20, task_type="intent", response_schema=schema)
    if not result: return "general"
    try: return json.loads(_clean_json_string(result)).get("intent", "GENERAL").lower()
    except: return "general"

def is_spam_or_negative(text: str) -> bool:
    if not can_use_gemini(): return False
    prompt = f"Classify:\nSPAM=bot/promo\nNEGATIVE=hate\nSAFE=genuine\nComment: {text}"
    schema = {"type": "OBJECT", "properties": {"status": {"type": "STRING", "enum": ["SPAM", "NEGATIVE", "SAFE"]}}, "required": ["status"]}
    result = _generate(prompt, max_length=50, task_type="spam", response_schema=schema)
    if not result: return False
    try: return json.loads(_clean_json_string(result)).get("status") in ("SPAM", "NEGATIVE")
    except: return "SPAM" in result.upper() or "NEGATIVE" in result.upper()

def _gemini_should_reply_dm(text: str, user_id: str) -> bool:
    if not can_use_gemini(): return False
    history = get_dm_memory(user_id, 5)
    history_str = "\n".join([f"{m['role']}: {m['message_text']}" for m in history]) if history else ""
    if history_str: history_str = f"\nContext:\n{history_str}\n"
    prompt = (
        "Classify DM:\n"
        "BOT_REPLY = greetings, praise, emojis, simple questions clearly about the page's content.\n"
        "HUMAN_REPLY = business, collabs, payments, complaints, OR ambiguous/personal questions about the "
        "page owner (their job, other work, personal life) that the AI can't reliably answer, OR the "
        "conversation shows the user re-asking the same thing in different words / seems confused or "
        "frustrated after 2+ exchanges on the same topic.\n"
        f"{history_str}New Message: {text}\n"
    )
    schema = {"type": "OBJECT", "properties": {"action": {"type": "STRING", "enum": ["BOT_REPLY", "HUMAN_REPLY"]}}, "required": ["action"]}
    result = _generate(prompt, max_length=50, task_type="dm_filter", response_schema=schema)
    if not result: return False
    try: return json.loads(_clean_json_string(result)).get("action") == "BOT_REPLY"
    except: return "BOT_REPLY" in result.upper()

def can_use_gemini() -> bool:
    cb_until = get_config("circuit_breaker_until")
    if cb_until and cb_until != "0":
        try:
            if time.time() < float(cb_until): return False
            else:
                set_config("circuit_breaker_until", "0")
                set_config("consecutive_429s", "0")
        except ValueError: pass
    return True

def get_model_status() -> str:
    lines = ["\n📊 <b>Model Usage Today</b>"]
    total_pool = len(_clients) if _clients else 1
    for model in MODEL_CONFIGS:
        mid = model["id"]
        used = get_model_rpd(mid)
        limit = model["rpd"] * total_pool
        pct = int(used / limit * 100) if limit > 0 else 0
        icon = "🟢" if pct < 50 else ("🟡" if pct < 80 else "🔴")
        lines.append(f"{icon} {model['label']}: {used}/{limit}")
    return "\n".join(lines)

def generate_weekly_insight(stats: dict) -> str | None:
    if not can_use_gemini(): return None
    prompt = f"Social media analyst for @krishna.verse.ai.\nThis week: {stats.get('total_comments_replied', 0)} replies, {stats.get('welcome_dms_sent', 0)} DMs.\nGive 3 practical growth suggestions. Under 100 words."
    return _generate(prompt, max_length=500, task_type="dm")

def summarize_conversation(user_id: str, messages: list[dict]) -> str | None:
    if not can_use_gemini(): return None
    history_str = "\n".join([f"{m['role']}: {m['message_text']}" for m in messages])
    prompt = (
        "Summarize the following Instagram DM conversation in 2-3 sentences. "
        "Capture the core intent, user's mood, and any specific questions asked. "
        "Do not use greetings or filler words.\n"
        f"Conversation:\n{history_str}\nSummary:"
    )
    return _generate(prompt, max_length=200, task_type="summary")


