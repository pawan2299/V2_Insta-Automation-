from __future__ import annotations
import logging
import time
import json
import re
import requests
import threading
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
_rpm_lock = threading.Lock()

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
    text = re.sub(r'^
http://googleusercontent.com/immersive_entry_chip/0

---

### 4. `database.py` (Complete Original Code)
इसमें आपकी इच्छा के अनुसार **कस्टम C2DM सबस्ट्रिंग मैचिंग (`in`) को सुरक्षित रखा गया है**।

```python
from __future__ import annotations
import logging
import threading
import json
import hashlib
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta, date
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from config import SETTINGS

logger = logging.getLogger(__name__)

_pool: pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

def init_pool(force: bool = False):
    global _pool
    with _pool_lock:
        if _pool and not force: return
        if _pool and force:
            try: _pool.closeall()
            except Exception: pass
            _pool = None
        try:
            _pool = pool.ThreadedConnectionPool(
                minconn=1, maxconn=6, dsn=SETTINGS.database_url,
                cursor_factory=RealDictCursor, connect_timeout=20,
            )
            logger.info("DB pool initialized (maxconn=6).")
        except Exception as e:
            logger.error(f"Failed to initialize DB pool: {e}")
            _pool = None
            raise

@contextmanager
def get_db():
    global _pool
    conn = None
    try:
        if _pool is None or getattr(_pool, 'closed', False):
            logger.warning("DB pool closed. Reinitializing...")
            init_pool(force=True)
        
        conn = _pool.getconn()
        try:
            with conn.cursor() as test_cur: test_cur.execute("SELECT 1")
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            logger.warning("Stale connection detected, recreating pool.")
            try: _pool.putconn(conn, close=True)
            except Exception: pass
            conn = None
            init_pool(force=True)
            conn = _pool.getconn()

        with conn.cursor() as cur: yield cur
        conn.commit()
    except Exception:
        if conn:
            try: conn.rollback()
            except Exception: pass
        raise
    finally:
        if conn:
            try: _pool.putconn(conn)
            except Exception: pass

def init_db():
    init_pool()
    with get_db() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS system_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS processed_events (event_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS reply_logs (id SERIAL PRIMARY KEY, event_id TEXT UNIQUE, user_id TEXT, reply_text TEXT, media_id TEXT, source TEXT DEFAULT 'comment', created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS conversation_memory (id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, role TEXT NOT NULL, message_text TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS gemini_quotas (model_id TEXT NOT NULL, usage_date DATE NOT NULL, call_count INTEGER DEFAULT 0, PRIMARY KEY (model_id, usage_date))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS custom_keywords (keyword TEXT PRIMARY KEY, reply TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS comment_to_dm (id SERIAL PRIMARY KEY, keyword TEXT UNIQUE NOT NULL, public_reply TEXT NOT NULL, dm_message TEXT NOT NULL, is_active BOOLEAN DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS dm_cooldowns (user_id TEXT PRIMARY KEY, sent_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS human_handoff_cooldowns (user_id TEXT PRIMARY KEY, expires_at TIMESTAMPTZ NOT NULL)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS failed_webhooks (event_id TEXT PRIMARY KEY, payload JSONB NOT NULL, error_msg TEXT, retry_count INTEGER DEFAULT 0, created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS conversation_summaries (user_id TEXT PRIMARY KEY, summary TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS c2dm_cooldowns (user_id TEXT NOT NULL, keyword TEXT NOT NULL, sent_at TIMESTAMPTZ DEFAULT NOW(), PRIMARY KEY (user_id, keyword))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS reply_feedback (id SERIAL PRIMARY KEY, reply_log_id INTEGER UNIQUE REFERENCES reply_logs(id) ON DELETE CASCADE, feedback TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW())""")
        
        cur.execute("""INSERT INTO system_config (key, value) VALUES ('bot_paused', 'false'), ('gemini_enabled', 'true'), ('safe_mode', 'false'), ('consecutive_429s', '0'), ('circuit_breaker_until', '0'), ('c2dm_enabled', 'true') ON CONFLICT DO NOTHING""")
        
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reply_logs_created ON reply_logs(created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reply_logs_media ON reply_logs(media_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_conv_mem_user ON conversation_memory(user_id, created_at DESC)")
        logger.info("🚀 Enterprise DB initialized.")

def claim_event(event_id: str) -> bool:
    if not event_id: return False
    lock_id = int(hashlib.md5(event_id.encode()).hexdigest(), 16) % (2**31 - 1)
    with get_db() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s) as locked", (lock_id,))
        row = cur.fetchone()
        if not row or not row['locked']: return False
        cur.execute("INSERT INTO processed_events (event_id) VALUES (%s) ON CONFLICT DO NOTHING", (event_id,))
        return cur.rowcount == 1

def save_failed_webhook(event_id: str, payload: dict, error_msg: str):
    with get_db() as cur:
        cur.execute("""INSERT INTO failed_webhooks (event_id, payload, error_msg) VALUES (%s, %s, %s) ON CONFLICT (event_id) DO UPDATE SET error_msg = EXCLUDED.error_msg, retry_count = failed_webhooks.retry_count + 1""", (event_id, json.dumps(payload), error_msg))

def get_failed_webhooks(limit=10):
    with get_db() as cur:
        cur.execute("SELECT * FROM failed_webhooks ORDER BY created_at ASC LIMIT %s", (limit,))
        return cur.fetchall()

def get_and_lock_failed_webhooks(limit=5):
    with get_db() as cur:
        cur.execute("""
        UPDATE failed_webhooks SET retry_count = retry_count + 1
        WHERE event_id IN (
            SELECT event_id FROM failed_webhooks WHERE retry_count < 5
            ORDER BY created_at ASC LIMIT %s FOR UPDATE SKIP LOCKED
        ) RETURNING *
        """, (limit,))
        return cur.fetchall()

def delete_failed_webhook(event_id: str):
    with get_db() as cur:
        cur.execute("DELETE FROM failed_webhooks WHERE event_id = %s", (event_id,))

def save_conversation_summary(user_id: str, summary: str):
    with get_db() as cur:
        cur.execute("""INSERT INTO conversation_summaries (user_id, summary) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET summary = EXCLUDED.summary, updated_at = NOW()""", (user_id, summary))

def get_conversation_summary(user_id: str) -> str:
    with get_db() as cur:
        cur.execute("SELECT summary FROM conversation_summaries WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row['summary'] if row else ""

def trim_old_memories(user_id: str, keep_last: int = 3):
    with get_db() as cur:
        cur.execute("""DELETE FROM conversation_memory WHERE user_id = %s AND id NOT IN (SELECT id FROM conversation_memory WHERE user_id = %s ORDER BY created_at DESC LIMIT %s)""", (user_id, user_id, keep_last))

def get_config(key: str) -> str:
    with get_db() as cur:
        cur.execute("SELECT value FROM system_config WHERE key = %s", (key,))
        row = cur.fetchone()
        return row["value"] if row else ""

def set_config(key: str, value: str):
    with get_db() as cur:
        cur.execute("""INSERT INTO system_config (key, value, updated_at) VALUES (%s, %s, NOW()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""", (key, value))

def is_bot_paused() -> bool: return get_config("bot_paused") == "true"
def is_gemini_enabled() -> bool: return get_config("gemini_enabled") == "true"
def is_safe_mode() -> bool: return get_config("safe_mode") == "true"

def is_circuit_breaker_active() -> bool:
    cb_until = get_config("circuit_breaker_until")
    if cb_until and cb_until != "0":
        try:
            if datetime.now(timezone.utc).timestamp() < float(cb_until): return True
        except ValueError: pass
    return False

def is_already_replied(event_id: str) -> bool:
    with get_db() as cur:
        cur.execute("SELECT 1 FROM reply_logs WHERE event_id = %s", (event_id,))
        return cur.fetchone() is not None

def log_reply(event_id: str, user_id: str, reply_text: str, media_id: str = "", source: str = "comment"):
    with get_db() as cur:
        cur.execute("""INSERT INTO reply_logs (event_id, user_id, reply_text, media_id, source) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING""", (event_id, user_id, reply_text, media_id, source))

def get_recent_replies(limit: int = 5) -> list[str]:
    with get_db() as cur:
        cur.execute("SELECT reply_text FROM reply_logs ORDER BY created_at DESC LIMIT %s", (limit,))
        return [row["reply_text"] for row in cur.fetchall()]

def save_dm_memory(user_id: str, role: str, text: str):
    with get_db() as cur:
        cur.execute("INSERT INTO conversation_memory (user_id, role, message_text) VALUES (%s, %s, %s)", (user_id, role, text))

def get_dm_memory(user_id: str, limit: int = 5) -> list[dict]:
    with get_db() as cur:
        cur.execute("SELECT role, message_text FROM conversation_memory WHERE user_id = %s ORDER BY created_at DESC LIMIT %s", (user_id, limit))
        return cur.fetchall()[::-1]

def claim_welcome_dm(user_id: str) -> bool:
    with get_db() as cur:
        cur.execute("INSERT INTO dm_cooldowns (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
        return cur.rowcount == 1

def claim_c2dm_dm(user_id: str, keyword: str, cooldown_hours: int = 24) -> bool:
    with get_db() as cur:
        cur.execute("""
        INSERT INTO c2dm_cooldowns (user_id, keyword, sent_at) VALUES (%s, %s, NOW())
        ON CONFLICT (user_id, keyword) DO UPDATE
        SET sent_at = EXCLUDED.sent_at
        WHERE c2dm_cooldowns.sent_at < NOW() - make_interval(hours => %s)
        """, (user_id, keyword, cooldown_hours))
        return cur.rowcount == 1

def set_human_handoff(user_id: str, hours: int = 24):
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    with get_db() as cur:
        cur.execute("""INSERT INTO human_handoff_cooldowns (user_id, expires_at) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET expires_at = EXCLUDED.expires_at""", (user_id, expires))

def is_in_human_handoff(user_id: str) -> bool:
    with get_db() as cur:
        cur.execute("SELECT 1 FROM human_handoff_cooldowns WHERE user_id = %s AND expires_at > NOW()", (user_id,))
        return cur.fetchone() is not None

def increment_gemini_count(model_id: str) -> int:
    today = date.today()
    with get_db() as cur:
        cur.execute("""INSERT INTO gemini_quotas (model_id, usage_date, call_count) VALUES (%s, %s, 1) ON CONFLICT (model_id, usage_date) DO UPDATE SET call_count = gemini_quotas.call_count + 1""", (model_id, today))
        cur.execute("SELECT call_count FROM gemini_quotas WHERE model_id = %s AND usage_date = %s", (model_id, today))
        return cur.fetchone()["call_count"]

def get_model_rpd(model_id: str) -> int:
    with get_db() as cur:
        cur.execute("SELECT call_count FROM gemini_quotas WHERE model_id = %s AND usage_date = %s", (model_id, date.today()))
        row = cur.fetchone()
        return row["call_count"] if row else 0

def get_total_gemini_today() -> int:
    with get_db() as cur:
        cur.execute("SELECT SUM(call_count) as total FROM gemini_quotas WHERE usage_date = %s", (date.today(),))
        row = cur.fetchone()
        return int(row["total"]) if row["total"] else 0

def get_stats() -> dict:
    with get_db() as cur:
        cur.execute("SELECT COUNT(*) as c FROM reply_logs WHERE source = 'comment'")
        total_replied = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM reply_logs WHERE source = 'comment' AND created_at > NOW() - INTERVAL '24 hours'")
        today_replied = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM reply_logs WHERE source IN ('dm', 'dm_ack', 'story_mention')")
        total_dms = cur.fetchone()["c"]
    return {
        "total_comments_replied": total_replied, "last_24h_replies": today_replied,
        "welcome_dms_sent": total_dms, "bot_paused": is_bot_paused(),
        "gemini_enabled": is_gemini_enabled(), "safe_mode": is_safe_mode(),
        "consecutive_429s": int(get_config("consecutive_429s") or 0),
        "circuit_breaker_active": is_circuit_breaker_active()
    }

def get_recent_activity(limit: int = 10) -> list:
    with get_db() as cur:
        cur.execute("""
        (SELECT source AS action, created_at FROM reply_logs ORDER BY created_at DESC LIMIT 50)
        UNION ALL
        (SELECT 'Welcome DM' AS action, sent_at AS created_at FROM dm_cooldowns ORDER BY sent_at DESC LIMIT 10)
        ORDER BY created_at DESC LIMIT %s
        """, (limit,))
        return cur.fetchall()

def cleanup_old_data():
    with get_db() as cur:
        cur.execute("DELETE FROM processed_events WHERE created_at < NOW() - INTERVAL '7 days'")
        cur.execute("DELETE FROM reply_logs WHERE created_at < NOW() - INTERVAL '30 days'")
        cur.execute("DELETE FROM conversation_memory WHERE created_at < NOW() - INTERVAL '14 days'")
        cur.execute("DELETE FROM human_handoff_cooldowns WHERE expires_at < NOW()")
        cur.execute("DELETE FROM gemini_quotas WHERE usage_date < NOW() - INTERVAL '30 days'")
        cur.execute("DELETE FROM failed_webhooks WHERE created_at < NOW() - INTERVAL '30 days'")
        cur.execute("DELETE FROM conversation_summaries WHERE updated_at < NOW() - INTERVAL '90 days'")
        cur.execute("DELETE FROM c2dm_cooldowns WHERE sent_at < NOW() - INTERVAL '30 days'")
        cur.execute("DELETE FROM reply_feedback WHERE created_at < NOW() - INTERVAL '90 days'")

def is_active_hours() -> bool:
    ist_hour = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).hour
    start = int(get_config("sleep_start") or 1)
    end = int(get_config("sleep_end") or 6)
    if start <= end: return not (start <= ist_hour < end)
    else: return not (ist_hour >= start or ist_hour < end)

def add_keyword(keyword: str, reply: str):
    with get_db() as cur:
        cur.execute("INSERT INTO custom_keywords (keyword, reply) VALUES (%s, %s) ON CONFLICT (keyword) DO UPDATE SET reply = EXCLUDED.reply", (keyword.lower().strip(), reply.strip()))

def remove_keyword(keyword: str) -> bool:
    with get_db() as cur:
        cur.execute("DELETE FROM custom_keywords WHERE keyword = %s", (keyword.lower().strip(),))
        return cur.rowcount > 0

def list_keywords() -> list[dict]:
    with get_db() as cur:
        cur.execute("SELECT keyword, reply FROM custom_keywords ORDER BY created_at DESC")
        return cur.fetchall()

def get_keyword_reply(text: str) -> str | None:
    lower_text = text.lower()
    with get_db() as cur:
        cur.execute("SELECT keyword, reply FROM custom_keywords")
        for row in cur.fetchall():
            if row["keyword"] in lower_text: return row["reply"]
    return None

def is_c2dm_enabled() -> bool: return get_config("c2dm_enabled") == "true"
def toggle_c2dm(): set_config("c2dm_enabled", "false" if is_c2dm_enabled() else "true")

def add_c2dm_trigger(keyword: str, public_reply: str, dm_message: str):
    with get_db() as cur:
        cur.execute("""INSERT INTO comment_to_dm (keyword, public_reply, dm_message) VALUES (%s, %s, %s) ON CONFLICT (keyword) DO UPDATE SET public_reply = EXCLUDED.public_reply, dm_message = EXCLUDED.dm_message, is_active = TRUE""", (keyword.lower().strip(), public_reply, dm_message))

def get_c2dm_triggers() -> list[dict]:
    with get_db() as cur:
        cur.execute("SELECT * FROM comment_to_dm ORDER BY created_at DESC")
        return cur.fetchall()

def delete_c2dm_trigger(trigger_id: int):
    with get_db() as cur: cur.execute("DELETE FROM comment_to_dm WHERE id = %s", (trigger_id,))

def find_c2dm_trigger(text: str) -> dict | None:
    lower_text = text.lower()
    with get_db() as cur:
        cur.execute("SELECT * FROM comment_to_dm WHERE is_active = TRUE")
        for row in cur.fetchall():
            if row["keyword"] in lower_text: return row
    return None

def set_telegram_state(chat_id: str, state_data: dict): set_config(f"tg_state_{chat_id}", json.dumps(state_data))

def get_telegram_state(chat_id: str) -> dict | None:
    raw = get_config(f"tg_state_{chat_id}")
    if raw:
        try: return json.loads(raw)
        except Exception: pass
    return None

def clear_telegram_state(chat_id: str): set_config(f"tg_state_{chat_id}", "")

def get_recent_ai_replies(limit: int = 5) -> list:
    with get_db() as cur:
        cur.execute("""
        SELECT id, event_id, user_id, reply_text, media_id, created_at
        FROM reply_logs
        WHERE source = 'comment'
        AND reply_text NOT LIKE 'Thank you%%'
        AND reply_text NOT LIKE 'Radhe Radhe%%'
        AND reply_text NOT LIKE '🙏%%'
        AND reply_text NOT LIKE '[Filtered Spam]'
        AND reply_text NOT LIKE 'Glad you liked it%%'
        ORDER BY created_at DESC LIMIT %s
        """, (limit,))
        return cur.fetchall()

def save_reply_feedback(reply_log_id: int, feedback: str):
    with get_db() as cur:
        cur.execute("""
        INSERT INTO reply_feedback (reply_log_id, feedback)
        VALUES (%s, %s)
        ON CONFLICT (reply_log_id) DO UPDATE SET feedback = EXCLUDED.feedback
        """, (reply_log_id, feedback))

def update_reply_text(reply_log_id: int, new_text: str):
    with get_db() as cur:
        cur.execute("UPDATE reply_logs SET reply_text = %s WHERE id = %s", (new_text, reply_log_id))

def get_top_posts(limit: int = 5) -> list:
    with get_db() as cur:
        cur.execute("""
        SELECT media_id, COUNT(*) as reply_count
        FROM reply_logs
        WHERE media_id IS NOT NULL AND media_id != '' AND source = 'comment'
        GROUP BY media_id ORDER BY reply_count DESC LIMIT %s
        """, (limit,))
        return cur.fetchall()
