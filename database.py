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
        if _pool and not force:
            return
        if _pool and force:
            try:
                _pool.closeall()
            except Exception:
                pass
            _pool = None
        try:
            _pool = pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=8,
                dsn=SETTINGS.database_url,
                cursor_factory=RealDictCursor,
                connect_timeout=5,
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
            logger.warning("DB pool is closed or None. Reinitializing...")
            init_pool(force=True)
        conn = _pool.getconn()
        try:
            with conn.cursor() as test_cur:
                test_cur.execute("SELECT 1")
        except Exception:
            logger.warning("Stale connection detected, recreating pool.")
            try:
                _pool.putconn(conn, close=True)
            except Exception:
                pass
            conn = None
            init_pool(force=True)
            conn = _pool.getconn()
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn:
            try:
                _pool.putconn(conn)
            except Exception:
                pass

def init_db():
    init_pool()
    with get_db() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_comments (
            comment_id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_events (
            event_id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """)
        cur.execute("""
        INSERT INTO bot_state (key, value) VALUES
        ('bot_paused', 'false'),
        ('gemini_enabled', 'true'),
        ('safe_mode', 'false'),
        ('consecutive_429s', '0'),
        ('circuit_breaker_until', '0')
        ON CONFLICT DO NOTHING
        """)
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_comments_created
        ON processed_comments(created_at DESC)
        """)
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_created
        ON processed_events(created_at DESC)
        """)
        init_keywords_table()
        init_c2dm_table()
        logger.info("DB initialized.")

# ── Bot State ──────────────────────────────────────────
def get_state(key: str) -> str:
    with get_db() as cur:
        cur.execute("SELECT value FROM bot_state WHERE key = %s", (key,))
        row = cur.fetchone()
        return row["value"] if row else ""

def set_state(key: str, value: str):
    with get_db() as cur:
        cur.execute("""
        INSERT INTO bot_state (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value))

def is_bot_paused() -> bool:
    return get_state("bot_paused") == "true"

def is_gemini_enabled() -> bool:
    return get_state("gemini_enabled") == "true"

def is_safe_mode() -> bool:
    return get_state("safe_mode") == "true"

# ── Event Deduplication ────────────────────────────────
def claim_event(event_id: str) -> bool:
    if not event_id:
        return False
    with get_db() as cur:
        cur.execute("""
        INSERT INTO processed_events (event_id)
        VALUES (%s)
        ON CONFLICT (event_id) DO NOTHING
        """, (event_id,))
        return cur.rowcount == 1

# ── Comment Dedup ──────────────────────────────────────
_comment_cache: set[str] = set()
_cache_lock = threading.Lock()

def is_already_replied(comment_id: str) -> bool:
    with _cache_lock:
        if comment_id in _comment_cache:
            return True
    with get_db() as cur:
        cur.execute(
            "SELECT 1 FROM processed_comments WHERE comment_id = %s",
            (comment_id,)
        )
        found = cur.fetchone() is not None
        if found:
            with _cache_lock:
                _comment_cache.add(comment_id)
        return found

def mark_replied(comment_id: str):
    with _cache_lock:
        _comment_cache.add(comment_id)
    with get_db() as cur:
        cur.execute(
            "INSERT INTO processed_comments (comment_id) VALUES (%s) "
            "ON CONFLICT DO NOTHING",
            (comment_id,)
        )

# ── Stats ──────────────────────────────────────────────
def get_stats() -> dict:
    with get_db() as cur:
        cur.execute("SELECT COUNT(*) as c FROM processed_comments")
        total_replied = cur.fetchone()["c"]
        cur.execute(
            "SELECT COUNT(*) as c FROM processed_comments "
            "WHERE created_at > NOW() - INTERVAL '24 hours'"
        )
        today_replied = cur.fetchone()["c"]
        
        cb_until = get_state("circuit_breaker_until")
        is_cb_active = False
        if cb_until and cb_until != "0":
            try:
                if datetime.now(timezone.utc).timestamp() < float(cb_until):
                    is_cb_active = True
            except ValueError:
                pass
        return {
            "total_comments_replied": total_replied,
            "last_24h_replies": today_replied,
            "welcome_dms_sent": 0,
            "bot_paused": is_bot_paused(),
            "gemini_enabled": is_gemini_enabled(),
            "safe_mode": is_safe_mode(),
            "consecutive_429s": int(get_state("consecutive_429s") or 0),
            "circuit_breaker_active": is_cb_active
        }

# ── Custom Keywords ────────────────────────────────────
def init_keywords_table():
    with get_db() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS custom_keywords (
            keyword TEXT PRIMARY KEY,
            reply   TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """)

def add_keyword(keyword: str, reply: str):
    with get_db() as cur:
        cur.execute("""
        INSERT INTO custom_keywords (keyword, reply)
        VALUES (%s, %s)
        ON CONFLICT (keyword) DO UPDATE SET reply = EXCLUDED.reply
        """, (keyword.lower().strip(), reply.strip()))

def remove_keyword(keyword: str) -> bool:
    with get_db() as cur:
        cur.execute(
            "DELETE FROM custom_keywords WHERE keyword = %s",
            (keyword.lower().strip(),)
        )
        return cur.rowcount > 0

def list_keywords() -> list[dict]:
    with get_db() as cur:
        cur.execute(
            "SELECT keyword, reply FROM custom_keywords "
            "ORDER BY created_at DESC"
        )
        return cur.fetchall()

def get_keyword_reply(text: str) -> str | None:
    lower_text = text.lower()
    with get_db() as cur:
        cur.execute("SELECT keyword, reply FROM custom_keywords")
        for row in cur.fetchall():
            if row["keyword"] in lower_text:
                return row["reply"]
    return None

# ── Gemini Call Tracking ───────────────────────────────
def increment_gemini_count() -> int:
    today = str(date.today())
    with get_db() as cur:
        cur.execute("""
        INSERT INTO bot_state (key, value)
        VALUES (%s, '1')
        ON CONFLICT (key) DO UPDATE
        SET value = (CAST(bot_state.value AS INTEGER) + 1)::TEXT
        """, (f"gemini_calls_{today}",))
        cur.execute(
            "SELECT value FROM bot_state WHERE key = %s",
            (f"gemini_calls_{today}",)
        )
        row = cur.fetchone()
        return int(row["value"]) if row else 0

def get_gemini_count_today() -> int:
    today = str(date.today())
    with get_db() as cur:
        cur.execute(
            "SELECT value FROM bot_state WHERE key = %s",
            (f"gemini_calls_{today}",)
        )
        row = cur.fetchone()
        return int(row["value"]) if row else 0

def set_model_cooldown(model_id: str, duration_mins: int = 10):
    until = (datetime.now(timezone.utc) + timedelta(minutes=duration_mins)).timestamp()
    set_state(f"cooldown_{model_id}", str(until))

def is_model_on_cooldown(model_id: str) -> bool:
    until = get_state(f"cooldown_{model_id}")
    if not until or until == "0":
        return False
    try:
        return datetime.now(timezone.utc).timestamp() < float(until)
    except ValueError:
        return False

def get_recent_replies(limit: int = 10) -> list[str]:
    with get_db() as cur:
        cur.execute("""
        SELECT value FROM bot_state
        WHERE key LIKE 'last_reply_%%'
        ORDER BY key DESC LIMIT %s
        """, (limit,))
        return [row["value"] for row in cur.fetchall()]

def add_recent_reply(text: str):
    ts = datetime.now(timezone.utc).timestamp()
    set_state(f"last_reply_{ts}", text)

# ── Active Hours ───────────────────────────────────────
def is_active_hours() -> bool:
    ist_hour = (
        datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    ).hour
    with get_db() as cur:
        cur.execute("""
        SELECT key, value FROM bot_state
        WHERE key IN ('sleep_start', 'sleep_end')
        """)
        rows = {row["key"]: int(row["value"]) for row in cur.fetchall()}
        sleep_start = rows.get("sleep_start", 1)
        sleep_end = rows.get("sleep_end", 6)
        if sleep_start <= sleep_end:
            return not (sleep_start <= ist_hour < sleep_end)
        else:
            return not (ist_hour >= sleep_start or ist_hour < sleep_end)

# ── Activity Log ───────────────────────────────────────
def get_recent_activity(limit: int = 10) -> list:
    with get_db() as cur:
        cur.execute("""
        SELECT
            'Comment replied' AS action,
            created_at
        FROM processed_comments
        UNION ALL
        SELECT
            'Event processed' AS action,
            created_at
        FROM processed_events
        ORDER BY created_at DESC
        LIMIT %s
        """, (limit,))
        return cur.fetchall()

# ── Comment-to-DM Automation ────────────────────────────
def init_c2dm_table():
    with get_db() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS comment_to_dm (
            id SERIAL PRIMARY KEY,
            keyword TEXT UNIQUE NOT NULL,
            public_reply TEXT NOT NULL,
            dm_message TEXT NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """)
        cur.execute("""
        INSERT INTO bot_state (key, value) VALUES ('c2dm_enabled', 'true')
        ON CONFLICT DO NOTHING
        """)

def is_c2dm_enabled() -> bool:
    return get_state("c2dm_enabled") == "true"

def toggle_c2dm():
    current = is_c2dm_enabled()
    set_state("c2dm_enabled", "false" if current else "true")

def add_c2dm_trigger(keyword: str, public_reply: str, dm_message: str):
    with get_db() as cur:
        cur.execute("""
        INSERT INTO comment_to_dm (keyword, public_reply, dm_message)
        VALUES (%s, %s, %s)
        ON CONFLICT (keyword) DO UPDATE 
        SET public_reply = EXCLUDED.public_reply, dm_message = EXCLUDED.dm_message, is_active = TRUE
        """, (keyword.lower().strip(), public_reply, dm_message))

def get_c2dm_triggers() -> list[dict]:
    with get_db() as cur:
        cur.execute("SELECT * FROM comment_to_dm ORDER BY created_at DESC")
        return cur.fetchall()

def delete_c2dm_trigger(trigger_id: int):
    with get_db() as cur:
        cur.execute("DELETE FROM comment_to_dm WHERE id = %s", (trigger_id,))

def find_c2dm_trigger(text: str) -> dict | None:
    lower_text = text.lower()
    with get_db() as cur:
        cur.execute("SELECT * FROM comment_to_dm WHERE is_active = TRUE")
        for row in cur.fetchall():
            if row["keyword"] in lower_text:
                return row
    return None

# ── Telegram UI State Machine ───────────────────────────
def set_telegram_state(chat_id: str, state_data: dict):
    set_state(f"tg_state_{chat_id}", json.dumps(state_data))

def get_telegram_state(chat_id: str) -> dict | None:
    raw = get_state(f"tg_state_{chat_id}")
    if raw:
        try: 
            return json.loads(raw)
        except Exception: 
            pass
    return None

def clear_telegram_state(chat_id: str):
    set_state(f"tg_state_{chat_id}", "")
