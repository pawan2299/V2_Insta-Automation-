from __future__ import annotations
import logging
import time
from collections import deque
from datetime import date
from google import genai
from config import SETTINGS
from database import increment_gemini_count, get_state, set_state

logger = logging.getLogger(__name__)

_client: genai.Client | None = None

# ── Model Configuration — Real Data से ────────────────
# Priority: quality पहले, high-quota बाद में, fallback आखिर में
MODEL_CONFIGS = [
    {
        "id": "gemini-2.5-flash",
        "rpm": 15,
        "rpd": 50,
        "label": "2.5 Flash (Best Quality)"
    },
    {
        "id": "gemini-2.5-flash-lite",
        "rpm": 15,
        "rpd": 1500,
        "label": "2.5 Flash Lite ⭐"
    },
    {
        "id": "gemini-2.0-flash",
        "rpm": 15,
        "rpd": 1500,
        "label": "2.0 Flash ⭐"
    },
    {
        "id": "gemini-3-flash",
        "rpm": 15,
        "rpd": 1500,
        "label": "3 Flash"
    },
    {
        "id": "gemini-3.5-flash",
        "rpm": 15,
        "rpd": 1500,
        "label": "3.5 Flash"
    },
    {
        "id": "gemma-4-27b",
        "rpm": 15,
        "rpd": 1500,
        "label": "Gemma 4 27B (Fallback)"
    },
]

# Per-model RPM tracking
_model_rpm_calls: dict[str, deque] = {
    m["id"]: deque() for m in MODEL_CONFIGS
}


def _get_client() -> genai.Client | None:
    global _client
    if _client is None:
        try:
            _client = genai.Client(api_key=SETTINGS.gemini_api_key)
            logger.info("Gemini client initialized.")
        except Exception as e:
            logger.error(f"Gemini init failed: {e}")
    return _client


def _get_model_rpd_today(model_id: str) -> int:
    key = f"rpd_{model_id}_{date.today()}"
    return int(get_state(key) or 0)


def _increment_model_rpd(model_id: str) -> int:
    key = f"rpd_{model_id}_{date.today()}"
    count = int(get_state(key) or 0) + 1
    set_state(key, str(count))
    return count


def _get_best_model() -> str | None:
    """Priority order में best available model चुनो।"""
    now = time.time()

    for model in MODEL_CONFIGS:
        mid = model["id"]

        # RPM check
        calls = _model_rpm_calls[mid]
        while calls and now - calls[0] > 60:
            calls.popleft()
        if len(calls) >= model["rpm"]:
            logger.debug(f"{mid}: RPM limit ({len(calls)}/{model['rpm']})")
            continue

        # RPD check
        today_count = _get_model_rpd_today(mid)
        if today_count >= model["rpd"]:
            logger.debug(f"{mid}: RPD limit ({today_count}/{model['rpd']})")
            continue

        logger.debug(f"Model selected: {mid} | Today: {today_count}/{model['rpd']}")
        return mid

    return None  # सब exhaust


def _record_call(model_id: str):
    """Call record करो।"""
    _model_rpm_calls[model_id].append(time.time())
    rpd_count = _increment_model_rpd(model_id)
    total = increment_gemini_count()
    _track_call(total)
    logger.info(f"✅ {model_id} | RPD: {rpd_count} | Total today: {total}")


def _track_call(count: int):
    if count % 100 == 0:  # हर 100 calls पर log
        logger.info(f"Gemini total calls today: {count}")
    if count >= 7000:  # 7550 का 90%
        try:
            from telegram_bot import _send
            _send(
                SETTINGS.telegram_chat_id,
                f"⚠️ <b>Gemini Limit Warning</b>\n\n"
                f"आज {count} total calls हो गई हैं!\n"
                f"सभी models की limit खत्म होने वाली है।"
            )
        except Exception:
            pass


def _handle_gemini_error(e: Exception, model_id: str = ""):
    error_msg = str(e)
    if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
        count = int(get_state("consecutive_429s") or 0) + 1
        set_state("consecutive_429s", str(count))
        logger.warning(f"429 on {model_id}. Count: {count}")

        if count >= 5:  # 5 consecutive 429s → circuit break
            cooldown = time.time() + (30 * 60)
            set_state("circuit_breaker_until", str(cooldown))
            logger.critical("Circuit Breaker TRIPPED for 30 mins.")
            try:
                from telegram_bot import _send
                _send(
                    SETTINGS.telegram_chat_id,
                    "🚨 <b>Circuit Breaker!</b>\n\n"
                    "बहुत ज़्यादा 429 errors। AI 30 min बंद।"
                )
            except Exception:
                pass
    else:
        set_state("consecutive_429s", "0")


def can_use_gemini() -> bool:
    # Circuit breaker check
    cb_until = get_state("circuit_breaker_until")
    if cb_until and cb_until != "0":
        try:
            if time.time() < float(cb_until):
                return False
            else:
                # Reset after cooldown
                set_state("circuit_breaker_until", "0")
                set_state("consecutive_429s", "0")
                logger.info("Circuit breaker reset.")
        except ValueError:
            pass

    return _get_best_model() is not None


def _generate(prompt: str, max_length: int = 200) -> str | None:
    """
    Core function — auto model selection।
    सब public functions यही use करते हैं।
    """
    model_id = _get_best_model()
    if not model_id:
        logger.warning("No Gemini model available.")
        return None

    client = _get_client()
    if not client:
        return None

    try:
        resp = client.models.generate_content(
            model=model_id,
            contents=prompt,
        )
        set_state("consecutive_429s", "0")
        _record_call(model_id)

        text = (resp.text or "").strip()
        return text[:max_length] if text else None

    except Exception as e:
        logger.error(f"Gemini error ({model_id}): {e}")
        _handle_gemini_error(e, model_id)
        return None


# ── Public Functions ───────────────────────────────────

def generate_reply(comment_text: str, post_caption: str = "") -> str | None:
    if not can_use_gemini():
        return None

    context = f"\nPost Caption: {post_caption[:150]}" if post_caption else ""
    prompt = (
        "You are @krishna.verse.ai — devotional Krishna Instagram page. "
        "Reply to this comment with warmth and spiritual love. "
        "SHORT (max 12 words), natural. "
        "End with 'Radhe Radhe 🙏' or 'Jai Shri Krishna ✨'. "
        "Never say you're an AI. Match comment language."
        f"{context}"
        f"\nComment: {comment_text}"
    )
    result = _generate(prompt, max_length=200)
    if result:
        result = result.replace('"', "").replace("'", "")
    return result


def generate_dm_reply(message_text: str) -> str | None:
    if not can_use_gemini():
        return None

    prompt = (
        "You are @krishna.verse.ai — devotional Krishna page. "
        "Someone sent a DM. Reply warmly and spiritually. "
        "Under 50 words. Natural tone. "
        "End with Radhe Radhe 🙏 or Jai Shri Krishna ✨. "
        "Match language (Hindi or English)."
        f"\nMessage: {message_text}"
    )
    return _generate(prompt, max_length=300)


def generate_welcome_dm(username: str) -> str | None:
    if not can_use_gemini():
        return None

    prompt = (
        f"Write a warm welcome DM (max 40 words) for '{username}' "
        f"who followed @krishna.verse.ai — devotional Krishna page. "
        "Personal, spiritual, 2-3 emojis. "
        "End with Radhe Radhe 🙏. Don't mention AI."
    )
    return _generate(prompt, max_length=400)


def is_spam_or_negative(text: str) -> bool:
    if not can_use_gemini():
        return False

    prompt = (
        "Classify this Instagram comment:\n"
        "SPAM = promotional, irrelevant, bot-like\n"
        "NEGATIVE = hate, abuse, offensive\n"
        "SAFE = genuine, devotional, curious\n"
        "Reply with ONE word only: SPAM, NEGATIVE, or SAFE\n"
        f"Comment: {text}"
    )
    result = _generate(prompt, max_length=10)
    return (result or "").strip().upper() in ("SPAM", "NEGATIVE")


def generate_caption(topic: str) -> str | None:
    if not can_use_gemini():
        return None

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


def get_model_status() -> str:
    """Telegram /status के लिए model usage।"""
    lines = ["\n📊 <b>Model Usage Today</b>"]
    for model in MODEL_CONFIGS:
        mid = model["id"]
        used = _get_model_rpd_today(mid)
        limit = model["rpd"]
        pct = int(used / limit * 100) if limit > 0 else 0
        if pct < 50:
            icon = "🟢"
        elif pct < 80:
            icon = "🟡"
        else:
            icon = "🔴"
        lines.append(f"{icon} {model['label']}: {used}/{limit}")
    return "\n".join(lines)
