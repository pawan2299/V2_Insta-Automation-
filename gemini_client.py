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
MODEL_CONFIGS = [
    {"id": "gemini-3.5-flash", "rpm": 10, "rpd": 1500, "label": "3.5 Flash (Aesthetic Core)"},
    {"id": "gemini-3.1-flash-lite", "rpm": 15, "rpd": 1500, "label": "3.1 Lite (Filters)"},
    {"id": "gemini-2.5-pro", "rpm": 5, "rpd": 50, "label": "2.5 Pro (Deep Logic)"},
    {"id": "gemini-2.5-flash", "rpm": 10, "rpd": 1500, "label": "2.5 Flash (Fallback)"},
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
        models_to_try = ["gemini-3.1-flash-lite", "gemini-2.5-flash"]
    elif task_type == "dm":
        models_to_try = ["gemini-3.5-flash", "gemini-2.5-pro", "gemini-2.5-flash"]
    else:
        models_to_try = ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash"]

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
            model_conf = next((m for m in MODEL_CONFIGS if m["id"] == model_id), None)
            if not model_conf or len(calls) >= model_conf["rpm"]: continue
            
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
                    set_model_cooldown(model_id, 5) 
                    continue
                elif "400" in error_msg and "SAFETY" in error_msg:
                    logger.warning(f"Safety block on {model_id}")
                    return None
                else:
                    logger.error(f"Fatal error on {model_id}: {e}")
                    break 
    return None

# ── Public Functions (Preserved & Updated for New Engine) ──────────────────

def generate_reply(comment_text: str, post_caption: str = "", image_url: str | None = None) -> str | None:
    if not can_use_gemini(): return None
    
    visual_instr = "\nAnalyze the post image to make your reply specific to the visual content." if image_url else ""
    context = f"\nPost Context: {post_caption[:150]}" if post_caption else ""
    
    recent = get_recent_replies(5)
    history_context = "\nRecent replies (DO NOT REPEAT THESE): " + " | ".join(recent) if recent else ""
    
    prompt = (
        "You are @krishna.verse.ai — devotional Krishna Instagram page. "
        "Reply to this comment with warmth and spiritual love. "
        "SHORT (max 12 words), natural. "
        "End with 'Radhe Radhe 🙏' or 'Jai Shri Krishna ✨'. "
        "Never say you're an AI. Match comment language."
        f"{history_context}{visual_instr}{context}\n"
        f"Comment: {comment_text}"
    )
    result = _generate(prompt, max_length=100, task_type="comment", image_url=image_url)
    if result:
        add_recent_reply(result)
    return result.replace('"', "").replace("'", "") if result else None



def generate_dm_reply(message_text: str) -> str | None:
    if not can_use_gemini(): return None
    prompt = (
        "You are @krishna.verse.ai — devotional Krishna page. "
        "Someone sent a DM. Reply warmly and spiritually. "
        "Under 50 words. Natural tone. "
        "End with Radhe Radhe 🙏 or Jai Shri Krishna ✨. "
        "Match language (Hindi or English)."
        f"\nMessage: {message_text}"
    )
    return _generate(prompt, max_length=300, task_type="dm")

def generate_caption(topic: str) -> str | None:
    if not can_use_gemini(): return None
    prompt = (
        "Write Instagram caption for @krishna.verse.ai — Krishna devotional page.\n"
        f"Topic: {topic}\n"
        "3-4 lines, spiritual tone, 5-8 hashtags at end. "
        "Hindi/English mix okay. "
        "End with Radhe Radhe 🙏 or Jai Shri Krishna ✨"
    )
    return _generate(prompt, max_length=1000)

def generate_weekly_insight(stats: dict) -> str | None:
    prompt = (
        "Social media analyst for @krishna.verse.ai.\n"
        f"This week: {stats.get('total_comments_replied', 0)} replies, "
        f"{stats.get('welcome_dms_sent', 0)} welcome DMs.\n"
        "Give 3 practical growth suggestions. Under 100 words."
    )
    return _generate(prompt, max_length=500)

# ── JSON Structured Filters (No more parsing errors) ──
def is_spam_or_negative(text: str) -> bool:
    if not can_use_gemini(): return False
    prompt = (
        "Classify this Instagram comment:\n"
        "SPAM = promotional, irrelevant, bot-like\n"
        "NEGATIVE = hate, abuse, offensive\n"
        "SAFE = genuine, devotional, curious\n"
        f"Comment: {text}"
    )
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
        "You are a filter for @krishna.verse.ai Instagram DM inbox.\n"
        "Classify this DM:\n"
        "BOT_REPLY = greetings, appreciation, devotional expressions, short emotional messages, emojis only\n"
        "HUMAN_REPLY = questions, requests, business inquiries, collab, price, complaints, long messages\n"
        f"DM: {text}"
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
    total_pool = len(_clients) if _clients else 1
    for model in MODEL_CONFIGS:
        mid = model["id"]
        used = _get_model_rpd_today(mid)
        limit = model["rpd"] * total_pool 
        pct = int(used / limit * 100) if limit > 0 else 0
        icon = "🟢" if pct < 50 else ("🟡" if pct < 80 else "🔴")
        lines.append(f"{icon} {model['label']}: {used}/{limit}")
    return "\n".join(lines)
