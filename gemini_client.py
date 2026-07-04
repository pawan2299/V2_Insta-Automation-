from __future__ import annotations
import logging
import time
import json
import requests
from collections import deque
from google import genai
from google.genai import types
from config import SETTINGS
from database import get_config, set_config, get_model_rpd, increment_gemini_count, get_recent_replies, save_dm_memory, get_dm_memory

logger = logging.getLogger(__name__)
_clients: list[genai.Client] = []

def _init_clients():
    global _clients
    _clients = [genai.Client(api_key=k) for k in SETTINGS.gemini_api_keys if k]
_init_clients()

MODEL_CONFIGS = [
    {"id": "gemini-3.5-flash", "rpm": 10, "rpd": 1500, "label": "3.5 Flash (Core)"},
    {"id": "gemini-3.1-flash-lite", "rpm": 15, "rpd": 1500, "label": "3.1 Lite (Filters/Intent)"},
    {"id": "gemini-2.5-pro", "rpm": 5, "rpd": 50, "label": "2.5 Pro (Deep)"},
    {"id": "gemini-2.5-flash", "rpm": 10, "rpd": 1500, "label": "2.5 Flash (Fallback)"},
]
_model_rpm_calls: dict[str, deque] = {m["id"]: deque() for m in MODEL_CONFIGS}

def _record_call(model_id: str):
    _model_rpm_calls[model_id].append(time.time())
    increment_gemini_count(model_id)

def _generate(prompt: str, max_length: int = 200, task_type: str = "comment", image_url: str | None = None, response_schema: dict | None = None) -> str | None:
    # 🌟 PHASE 2: Added "intent" to lightweight router
    if task_type in ["spam", "dm_filter", "intent"]: models_to_try = ["gemini-3.1-flash-lite", "gemini-2.5-flash"]
    elif task_type == "dm": models_to_try = ["gemini-3.5-flash", "gemini-2.5-pro", "gemini-2.5-flash"]
    else: models_to_try = ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash"]

    contents = [prompt]
    if image_url:
        try:
            img_data = requests.get(image_url, timeout=5).content
            contents.append(types.Part.from_bytes(data=img_data, mime_type="image/jpeg"))
        except Exception: pass

    config_kwargs = {"max_output_tokens": max_length}
    if response_schema:
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = response_schema
    config = types.GenerateContentConfig(**config_kwargs)

    for client in _clients:
        for model_id in models_to_try:
            until = get_config(f"cooldown_{model_id}")
            if until and until != "0" and time.time() < float(until): continue
            calls = _model_rpm_calls[model_id]
            now = time.time()
            while calls and now - calls[0] > 60: calls.popleft()
            model_conf = next((m for m in MODEL_CONFIGS if m["id"] == model_id), None)
            if not model_conf or len(calls) >= model_conf["rpm"]: continue
            if get_model_rpd(model_id) >= model_conf["rpd"]: continue

            try:
                resp = client.models.generate_content(model=model_id, contents=contents, config=config)
                _record_call(model_id)
                set_config("consecutive_429s", "0")
                return (resp.text or "").strip()
            except Exception as e:
                error_msg = str(e)
                if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                    set_config(f"cooldown_{model_id}", str(time.time() + 300))
                    continue
                elif "400" in error_msg and "SAFETY" in error_msg: return None
                else: break 
    return None

def generate_reply(comment_text: str, post_caption: str = "", image_url: str | None = None) -> str | None:
    if not can_use_gemini(): return None
    visual_instr = "\nAnalyze the post image to make your reply specific to the visual content." if image_url else ""
    context = f"\nPost Context: {post_caption[:150]}" if post_caption else ""
    recent = get_recent_replies(5)
    history_context = "\nRecent replies (DO NOT REPEAT THESE): " + " | ".join(recent) if recent else ""
    
    prompt = (
        "You are @krishna.verse.ai — devotional Krishna Instagram page. "
        "Reply to this comment with warmth and spiritual love. "
        "Keep it short, precise, natural and conversational. "
        "End with 'Radhe Radhe 🙏' or 'Jai Shri Krishna ✨'. "
        "Never say you're an AI. "
        "CRITICAL: You MUST reply in the EXACT SAME LANGUAGE as the user's comment. "
        "Do NOT use markdown formatting like **bold** or *italics*. Instagram does not support it.\n"
        "Output ONLY the exact reply text. No prefixes, no quotes.\n"
        f"{history_context}{visual_instr}{context}\n"
        f"Comment: {comment_text}"
    )
    
    result = _generate(prompt, max_length=150, task_type="comment", image_url=image_url)
    if not result: return None
    text = result.strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")): text = text[1:-1]
    text = text.replace("**", "").replace("*", "").replace('"', '').split('\n')[0].strip()
    return text

def generate_dm_reply(message_text: str, user_id: str) -> str | None:
    if not can_use_gemini(): return None
    history = get_dm_memory(user_id, 5)
    history_str = "\n".join([f"{m['role']}: {m['message_text']}" for m in history]) if history else ""
    if history_str: history_str = f"\nPrevious Conversation:\n{history_str}\n"

    prompt = (
        "You manage Instagram DMs for @krishna.verse.ai (ultra-realistic AI videos of Little Krishna, Radha, Vrindavan).\n"
        "Your purpose: build a positive devotional community, answer naturally, make it feel warm, respectful, and human.\n"
        "NEVER sound like a chatbot. NEVER mention AI, prompts, or automation.\n\n"
        "PERSONALITY: Warm, humble, respectful, friendly, calm, positive, patient. Devotional but not preachy.\n"
        "LANGUAGE: Automatically reply in the EXACT language used by the follower (Hindi, English, Hinglish).\n"
        "TONE: Natural, short, precise, context-aware. Like a real person from the KrishnaVerse AI team.\n"
        "EMOJIS: Use sparingly (max 1-2). Examples: 🙏 ✨ 💙 🌸 😊\n\n"
        "RULES:\n"
        "- If they praise Krishna/Videos: Respond warmly with gratitude. Never be arrogant.\n"
        "- If they ask 'Is this AI?': Answer honestly but gracefully (e.g., 'Haan, ye AI ki madad se banaye jaate hain, par har bhavna soch-samajh kar taiyar ki jaati hai 🙏').\n"
        "- If they send only emojis: Reply with matching warm emotion.\n"
        "- If they are sad: Be empathetic. No medical/spiritual guarantees.\n"
        "- If they insult: Remain calm, never insult back.\n"
        "- DO NOT pretend to be Lord Krishna or claim divine powers.\n"
        "- DO NOT write long paragraphs. Keep it short and precise.\n\n"
        f"{history_str}"
        f"Follower's new message: {message_text}\n"
        "Your reply:"
    )
    return _generate(prompt, max_length=300, task_type="dm")

def generate_welcome_dm(username: str) -> str | None:
    if not can_use_gemini(): return None
    prompt = (
        f"Draft a warm, personal welcome DM for '{username}' who followed @krishna.verse.ai.\n"
        "Make it feel human, NOT an automated broadcast. Max 20 words. No hashtags.\n"
        "Conclude naturally with 'Radhe Radhe 🙏' or 'Jai Shri Krishna ✨'."
    )
    return _generate(prompt, max_length=100, task_type="dm")

# 🌟 PHASE 3: Story Mention Auto-Reply Generator
def generate_story_thank_you() -> str | None:
    if not can_use_gemini(): return None
    prompt = (
        "A follower just mentioned @krishna.verse.ai in their Instagram Story.\n"
        "Draft a very short, warm, and aesthetic thank you DM.\n"
        "Max 25 words. No hashtags. Express genuine gratitude for sharing our content.\n"
        "Conclude with 'Radhe Radhe 🙏' or 'Jai Shri Krishna ✨'."
    )
    return _generate(prompt, max_length=100, task_type="dm")

def generate_caption(topic: str) -> str | None:
    if not can_use_gemini(): return None
    prompt = f"Write Instagram caption for @krishna.verse.ai.\nTopic: {topic}\n3-4 lines, spiritual tone, 5-8 hashtags at end. End with Radhe Radhe 🙏"
    return _generate(prompt, max_length=1000)

# 🌟 PHASE 2: AI Intent Router (Replaces hardcoded words)
def classify_comment_intent(text: str) -> str:
    if not can_use_gemini(): return "general"
    prompt = (
        "Classify the intent of this Instagram comment for a spiritual Krishna page.\n"
        "Categories:\n"
        "- EMOJI: Only emojis or symbols.\n"
        "- GREETING: Simple hi, hello, radhe radhe, namaste, jai shri krishna.\n"
        "- PRAISE: Compliments, 'beautiful', 'amazing', 'love this', 'cute'.\n"
        "- QUESTION: Asking something (contains '?' or 'where', 'how', 'who', 'why').\n"
        "- SPAM: Promo, links, 'dm me', 'collab', 'check bio'.\n"
        "- GENERAL: Deep thoughts, long sentences, or anything else.\n"
        f"Comment: {text}"
    )
    schema = {
        "type": "OBJECT",
        "properties": {"intent": {"type": "STRING", "enum": ["EMOJI", "GREETING", "PRAISE", "QUESTION", "SPAM", "GENERAL"]}},
        "required": ["intent"]
    }
    result = _generate(prompt, max_length=20, task_type="intent", response_schema=schema)
    if not result: return "general"
    try: return json.loads(result).get("intent", "GENERAL").lower()
    except: return "general"

def is_spam_or_negative(text: str) -> bool:
    if not can_use_gemini(): return False
    prompt = f"Classify comment:\nSPAM=bot/promo\nNEGATIVE=hate\nSAFE=genuine\nComment: {text}"
    schema = {"type": "OBJECT", "properties": {"status": {"type": "STRING", "enum": ["SPAM", "NEGATIVE", "SAFE"]}}, "required": ["status"]}
    result = _generate(prompt, max_length=50, task_type="spam", response_schema=schema)
    if not result: return False
    try: return json.loads(result).get("status") in ("SPAM", "NEGATIVE")
    except: return "SPAM" in result.upper() or "NEGATIVE" in result.upper()

def _gemini_should_reply_dm(text: str, user_id: str) -> bool:
    if not can_use_gemini(): return False
    history = get_dm_memory(user_id, 5)
    history_str = "\n".join([f"{m['role']}: {m['message_text']}" for m in history]) if history else ""
    if history_str: history_str = f"\nPrevious Conversation Context:\n{history_str}\n"

    prompt = (
        "You are an intelligent routing filter for @krishna.verse.ai Instagram DM inbox.\n"
        "Your job is to decide if the AI should reply, or if the message requires the human Admin's attention.\n\n"
        "BOT_REPLY (AI handles these):\n"
        "- Standard greetings, devotion, praise for Krishna or videos.\n"
        "- Short emotional messages, emojis only.\n"
        "- Simple questions about Krishna, Vrindavan, or Radha.\n\n"
        "HUMAN_REPLY (Escalate to Admin immediately):\n"
        "- Personal opinions, business decisions, collaborations, sponsorships.\n"
        "- Payment issues, pricing inquiries, purchase requests.\n"
        "- Complaints, sensitive matters, or personal problems.\n"
        "- Requests for internal prompts, workflows, or 'how-to' technical details.\n"
        "- Follow-up questions that require human context from previous chats.\n\n"
        "CRITICAL RULE: WHEN IN DOUBT, PREFER ESCALATING TO THE ADMIN (HUMAN_REPLY) rather than guessing.\n\n"
        f"{history_str}"
        f"New Message: {text}\n"
    )
    schema = {"type": "OBJECT", "properties": {"action": {"type": "STRING", "enum": ["BOT_REPLY", "HUMAN_REPLY"]}}, "required": ["action"]}
    result = _generate(prompt, max_length=50, task_type="dm_filter", response_schema=schema)
    if not result: return False
    try: return json.loads(result).get("action") == "BOT_REPLY"
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
