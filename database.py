from __future__ import annotations
import logging
import threading
import json
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
                minconn=1, maxconn=8, dsn=SETTINGS.database_url,
                cursor_factory=RealDictCursor, connect_timeout=5,
            )
            logger.info("DB pool initialized.")
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
        except Exception:
            logger.warning("Stale connection, recreating pool.")
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
        cur.execute("""INSERT INTO system_config (key, value) VALUES ('bot_paused', 'false'), ('gemini_enabled', 'true'), ('safe_mode', 'false'), ('consecutive_429s', '0'), ('circuit_breaker_until', '0'), ('c2dm_enabled', 'true') ON CONFLICT DO NOTHING""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reply_logs_created ON reply_logs(created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_conv_mem_user ON conversation_memory(user_id, created_at DESC)")
        logger.info("🚀 Enterprise DB initialized.")

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

def claim_event(event_id: str) -> bool:
    if not event_id: return False
    with get_db() as cur:
        cur.execute("INSERT INTO processed_events (event_id) VALUES (%s) ON CONFLICT DO NOTHING", (event_id,))
        return cur.rowcount == 1

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
        # [R&D FIX]: Fixed UNION ALL syntax error by wrapping subqueries properly for PostgreSQL
        cur.execute("""
            SELECT action, created_at FROM (
                SELECT source AS action, created_at FROM reply_logs ORDER BY created_at DESC LIMIT 50
            ) r
            UNION ALL
            SELECT action, created_at FROM (
                SELECT 'Welcome DM' AS action, sent_at AS created_at FROM dm_cooldowns ORDER BY sent_at DESC LIMIT 10
            ) d
            ORDER BY created_at DESC LIMIT %s
        """, (limit,))
        return cur.fetchall()

def cleanup_old_data():
    with get_db() as cur