from __future__ import annotations
import logging
import time
import json
import requests
from collections import deque
from datetime import date
from google import genai
from google.genai import types
from config import SETTINGS
from database import (
    increment_gemini_count, get_state, set_state,
    is_model_on_cooldown, set_model_cooldown,
    get_recent_replies, add_recent_reply
)

logger = logging.getLogger(__name__)

# ── Multi-Project Client Pool ─────────────────────────
_clients: list[genai.Client] = []

def _init_clients():
    global _clients
    _clients = [genai.Client(api_key=k) for k in SETTINGS.gemini_api_keys if k]
    logger.info(f"Initialized {len(_clients)} Gemini Project Clients.")

_init_clients()

# ── 2026 Model Configurations ─────────────────────────
# Note: RPD limits are per-project. We track them locally to prevent 429s.
MODEL_CONFIGS = [
    {"id": "gemini-3.5-flash", "rpm": 10, "rpd": 1500, "label": "3.5 Flash (Aesthetic Core)"},
    {"id": "gemini-3.1-flash-lite", "rpm": 15, "rpd": 1500, "label": "3.1 Lite (Filters)"},
    {"id": "gemini-2.5-pro", "rpm": 5, "rpd": 50, "label": "2.5 Pro (Deep Logic)"},
]

_model_rpm_calls: dict[str, deque] = {m["id"]: deque() for m in MODEL_CONFIGS}

def _get_model_rpd_today(model_id: str) -> int:
    return int(get_state(f"rpd_{model_id}_{date.today()}") or 0)

def _increment_model_rpd(model_id: str) -> int:
    key = f"rpd_{model_id}_{date.today()}"
    count = int(get_state(key) or 0) + 1
    set_state(key, str(count))
    return count

def _record_call(model_id: str):
    _model_rpm_calls[model_id].append(time.time())
    rpd_count = _increment_model_rpd(model_id)
    total = increment_gemini_count()
    if total % 100 == 0: logger.info(f"Gemini total calls today: {total}")

# ── Core Generation Engine (Multi-Key + Multi-Model Cascade) ──
def _generate(
    prompt: str,
    max_length: int = 200,
    task_type: str = "comment",
    image_url: str | None = None,
    response_schema: dict | None = None
) -> str | None:
    
    # Dynamic Router Pattern
    if task_type in ["spam", "dm_filter"]:
        models_to_try = ["gemini-3.1-flash-lite", "gemini-3.5-flash"]
    elif task_type == "dm":
        models_to_try = ["gemini-3.5-flash", "gemini-2.5-pro"]
    else:
        models_to_try = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]

    # Build Multimodal Contents
    contents = [prompt]
    if image_url:
        try:
            img_data = requests.get(image_url, timeout=5).content
            contents.append(types.Part.from_bytes(data=img_data, mime_type="image/jpeg"))
        except Exception as e:
            logger.warning(f"Failed to load image: {e}")

    # Build Config (Inject JSON Schema if provided)
    config_kwargs = {"max_output_tokens": max_length}
    if response_schema:
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = response_schema
        
    config = types.GenerateContentConfig(**config_kwargs)

    # The Cascade Loop: Tries Client 1 -> Models, then Client 2 -> Models
    for client_idx, client in enumerate(_clients):
        for model_id in models_to_try:
            if is_model_on_cooldown(model_id):
                continue
                
            # Local RPM Check
            calls = _model_rpm_calls[model_id]
            now = time.time()
            while calls and now - calls[0] > 60: calls.popleft()
            model_conf = next(m for m in MODEL_CONFIGS if m["id"] == model_id)
            if len(calls) >= model_conf["rpm"]: continue
            
            # Local RPD Check
            if _get_model_rpd_today(model_id) >= model_conf["rpd"]: continue

            try:
                resp = client.models.generate_content(
                    model=model_id, contents=contents, config=config
                )
                _record_call(model_id)
                set_state("consecutive_429s", "0")
                return (resp.text or "").strip()
                
            except Exception as e:
                error_msg = str(e)
                if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                    logger.warning(f"429 on {model_id} (Project {client_idx+1}). Cascading...")
                    set_model_cooldown(model_id, 5) # Short cooldown, try next
                    continue
                elif "400" in error_msg and "SAFETY" in error_msg:
                    logger.warning(f"Safety block on {model_id}")
                    return None
                else:
                    logger.error(f"Fatal error on {model_id}: {e}")
                    break # Break model loop, try next project client
    return None

# ── Aesthetic & Professional Prompts ──────────────────

def generate_reply(comment_text: str, post_caption: str = "", image_url: str | None = None) -> str | None:
    if not can_use_gemini(): return None
    
    visual_instr = "\nAnalyze the post image to make your reply specific to the visual content." if image_url else ""
    context = f"\nPost Context: {post_caption[:150]}" if post_caption else ""
    
    prompt = (
        "You are the lead community manager for @krishna.verse.ai, an aesthetic and professional spiritual platform.\n"
        "Your voice is poetic, graceful, and deeply respectful.\n"
        "Respond to the comment with warmth and divine elegance.\n"
        "Constraints:\n"
        "- Maximum 15 words.\n"
        "- Match the user's language (Hindi/English/Hinglish).\n"
        "- Conclude naturally with 'Radhe Radhe 🙏' or 'Hare Krishna 🌸'.\n"
        "- Never mention you are an AI or a bot."
        f"{visual_instr}{context}\n"
        f"Comment: {comment_text}"
    )
    result = _generate(prompt, max_length=100, task_type="comment", image_url=image_url)
    return result.replace('"', "").replace("'", "") if result else None

def generate_welcome_dm(username: str) -> str | None:
    if not can_use_gemini(): return None
    prompt = (
        f"You are the aesthetic community manager for @krishna.verse.ai.\n"
        f"Draft a graceful, professional, and deeply spiritual welcome message for our new follower, '{username}'.\n"
        "Constraints:\n"
        "- Maximum 40 words.\n"
        "- Express genuine gratitude for them joining our digital ashram.\n"
        "- Conclude naturally with 'Radhe Radhe 🙏' or 'Hare Krishna 🦚'.\n"
        "- Never mention automation."
    )
    return _generate(prompt, max_length=200, task_type="dm")

def generate_dm_reply(message_text: str) -> str | None:
    if not can_use_gemini(): return None
    prompt = (
        "You are the professional community manager for @krishna.verse.ai.\n"
        "A devotee has sent a direct message. Reply with warmth, grace, and spiritual professionalism.\n"
        "Constraints:\n"
        "- Under 50 words.\n"
        "- Match their language.\n"
        "- Conclude with 'Radhe Radhe 🙏' or 'Hare Krishna ✨'."
        f"\nMessage: {message_text}"
    )
    return _generate(prompt, max_length=300, task_type="dm")

# ── JSON Structured Filters (No more parsing errors) ──

def is_spam_or_negative(text: str) -> bool:
    if not can_use_gemini(): return False
    prompt = f"Classify this Instagram comment for a spiritual page:\nSPAM = promotional, bot-like\nNEGATIVE = hate, abuse\nSAFE = genuine, devotional\nComment: {text}"
    
    schema = {
        "type": "OBJECT",
        "properties": {"status": {"type": "STRING", "enum": ["SPAM", "NEGATIVE", "SAFE"]}},
        "required": ["status"]
    }
    
    result = _generate(prompt, max_length=50, task_type="spam", response_schema=schema)
    if not result: return False
    try:
        data = json.loads(result)
        return data.get("status") in ("SPAM", "NEGATIVE")
    except json.JSONDecodeError:
        return "SPAM" in result.upper() or "NEGATIVE" in result.upper()

def _gemini_should_reply_dm(text: str) -> bool:
    if not can_use_gemini(): return False
    prompt = (
        "Classify this incoming DM for @krishna.verse.ai:\n"
        "BOT_REPLY = standard greetings, devotion, emojis, short appreciation.\n"
        "HUMAN_REPLY = complex questions, business, collaborations, complaints."
        f"\nDM: {text}"
    )
    
    schema = {
        "type": "OBJECT",
        "properties": {"action": {"type": "STRING", "enum": ["BOT_REPLY", "HUMAN_REPLY"]}},
        "required": ["action"]
    }
    
    result = _generate(prompt, max_length=50, task_type="dm_filter", response_schema=schema)
    if not result: return False
    try:
        data = json.loads(result)
        return data.get("action") == "BOT_REPLY"
    except json.JSONDecodeError:
        return "BOT_REPLY" in result.upper()

def can_use_gemini() -> bool:
    # Circuit breaker logic remains the same...
    cb_until = get_state("circuit_breaker_until")
    if cb_until and cb_until != "0":
        try:
            if time.time() < float(cb_until): return False
            else:
                set_state("circuit_breaker_until", "0")
                set_state("consecutive_429s", "0")
        except ValueError: pass
    return True

def get_model_status() -> str:
    lines = ["\n📊 <b>Model Usage Today</b>"]
    for model in MODEL_CONFIGS:
        mid = model["id"]
        used = _get_model_rpd_today(mid)
        limit = model["rpd"] * len(_clients) # Total pool limit
        pct = int(used / limit * 100) if limit > 0 else 0
        icon = "🟢" if pct < 50 else ("🟡" if pct < 80 else "🔴")
        lines.append(f"{icon} {model['label']}: {used}/{limit}")
    return "\n".join(lines)
