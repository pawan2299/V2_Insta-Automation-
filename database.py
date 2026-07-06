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
            # ✅ UPDATED: maxconn reduced from 8 to 4 to prevent Render OOM/Connection limits
            _pool = pool.ThreadedConnectionPool(
                minconn=1, maxconn=4, dsn=SETTINGS.database_url,
                cursor_factory=RealDictCursor, connect_timeout=5,
            )
            logger.info("DB pool initialized (maxconn=4).")
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
        # ✅ Health Check: Verify connection is alive before yielding
        try:
            with conn.cursor() as test_cur: test_cur.execute("SELECT 1")
        except Exception:
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
        # Existing tables...
        cur.execute("""CREATE TABLE IF NOT EXISTS system_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS processed_events (event_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS reply_logs (id SERIAL PRIMARY KEY, event_id TEXT UNIQUE, user_id TEXT, reply_text TEXT, media_id TEXT, source TEXT DEFAULT 'comment', created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS conversation_memory (id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, role TEXT NOT NULL, message_text TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS gemini_quotas (model_id TEXT NOT NULL, usage_date DATE NOT NULL, call_count INTEGER DEFAULT 0, PRIMARY KEY (model_id, usage_date))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS custom_keywords (keyword TEXT PRIMARY KEY, reply TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS comment_to_dm (id SERIAL PRIMARY KEY, keyword TEXT UNIQUE NOT NULL, public_reply TEXT NOT NULL, dm_message TEXT NOT NULL, is_active BOOLEAN DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS dm_cooldowns (user_id TEXT PRIMARY KEY, sent_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS human_handoff_cooldowns (user_id TEXT PRIMARY KEY, expires_at TIMESTAMPTZ NOT NULL)""")
        
        # ✅ NEW: Dead Letter Queue & Infinite Memory Tables
        cur.execute("""CREATE TABLE IF NOT EXISTS failed_webhooks (
            event_id TEXT PRIMARY KEY, payload JSONB NOT NULL, error_msg TEXT, 
            retry_count INTEGER DEFAULT 0, created_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS conversation_summaries (
            user_id TEXT PRIMARY KEY, summary TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        
        cur.execute("""INSERT INTO system_config (key, value) VALUES ('bot_paused', 'false'), ('gemini_enabled', 'true'), ('safe_mode', 'false'), ('consecutive_429s', '0'), ('circuit_breaker_until', '0'), ('c2dm_enabled', 'true') ON CONFLICT DO NOTHING""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reply_logs_created ON reply_logs(created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_conv_mem_user ON conversation_memory(user_id, created_at DESC)")
        logger.info("🚀 Enterprise DB initialized with DLQ & Summaries.")

# ✅ NEW: Advisory Locks for Zero-Duplicate Webhooks
def claim_event(event_id: str) -> bool:
    if not event_id: return False
    lock_id = int(hashlib.md5(event_id.encode()).hexdigest(), 16) % (2**31 - 1)
    with get_db() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (lock_id,))
        if not cur.fetchone()[0]:
            return False # Another worker is already processing this
        cur.execute("INSERT INTO processed_events (event_id) VALUES (%s) ON CONFLICT DO NOTHING", (event_id,))
        return cur.rowcount == 1

# ✅ NEW: Dead Letter Queue (DLQ) Functions
def save_failed_webhook(event_id: str, payload: dict, error_msg: str):
    with get_db() as cur:
        cur.execute("""
            INSERT INTO failed_webhooks (event_id, payload, error_msg) 
            VALUES (%s, %s, %s) ON CONFLICT (event_id) DO UPDATE 
            SET error_msg = EXCLUDED.error_msg, retry_count = failed_webhooks.retry_count + 1
        """, (event_id, json.dumps(payload), error_msg))

def get_failed_webhooks(limit=10):
    with get_db() as cur:
        cur.execute("SELECT * FROM failed_webhooks ORDER BY created_at ASC LIMIT %s", (limit,))
        return cur.fetchall()

def delete_failed_webhook(event_id: str):
    with get_db() as cur:
        cur.execute("DELETE FROM failed_webhooks WHERE event_id = %s", (event_id,))

# ✅ NEW: Infinite Memory (Summarization) Functions
def save_conversation_summary(user_id: str, summary: str):
    with get_db() as cur:
        cur.execute("""
            INSERT INTO conversation_summaries (user_id, summary) 
            VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE 
            SET summary = EXCLUDED.summary, updated_at = NOW()
        """, (user_id, summary))

def get_conversation_summary(user_id: str) -> str:
    with get_db() as cur:
        cur.execute("SELECT summary FROM conversation_summaries WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row['summary'] if row else ""

def trim_old_memories(user_id: str, keep_last: int = 3):
    with get_db() as cur:
        cur.execute("""
            DELETE FROM conversation_memory 
            WHERE user_id = %s AND id NOT IN (
                SELECT id FROM conversation_memory WHERE user_id = %s ORDER BY created_at DESC LIMIT %s
            )
        """, (user_id, user_id, keep_last))

# ✅ NEW: DB Keep-Alive Ping (Prevents Render Free Tier Disconnects)
def start_db_keepalive():
    def ping():
        while True:
            time.sleep(240) # 4 minutes
            try:
                with get_db() as cur: cur.execute("SELECT 1")
            except Exception as e: logger.error(f"DB keepalive failed: {e}")
    threading.Thread(target=ping, daemon=True).start()

# --- [बाकी के सभी पुराने Functions (get_config, set_config, log_reply, आदि) बिल्कुल वैसा ही रहेगा जैसा पहले था] ---
# (Space बचाने के लिए यहाँ skip किया है, लेकिन आप अपने पुराने code के बाकी functions को जस का तस रखेंगे)