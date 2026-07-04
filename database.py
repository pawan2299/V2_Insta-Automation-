# ... (बाकी code वैसा ही रहने दें) ...

def get_recent_activity(limit: int = 10) -> list:
    with get_db() as cur:
        # 🌟 OPTIMIZED: Subqueries with LIMIT prevent full table scans & RAM crashes
        cur.execute("""
            SELECT action, created_at FROM (
                SELECT source AS action, created_at FROM reply_logs ORDER BY created_at DESC LIMIT 50
            ) r
            UNION ALL
            SELECT 'Welcome DM' AS action, sent_at AS created_at FROM dm_cooldowns ORDER BY sent_at DESC LIMIT 10
            ORDER BY created_at DESC LIMIT %s
        """, (limit,))
        return cur.fetchall()

# 🌟 NEW: Auto Database Cleanup (Prevents Neon Free Tier overflow)
def cleanup_old_data():
    with get_db() as cur:
        cur.execute("DELETE FROM processed_events WHERE created_at < NOW() - INTERVAL '7 days'")
        cur.execute("DELETE FROM reply_logs WHERE created_at < NOW() - INTERVAL '30 days'")
        cur.execute("DELETE FROM conversation_memory WHERE created_at < NOW() - INTERVAL '14 days'")
        cur.execute("DELETE FROM human_handoff_cooldowns WHERE expires_at < NOW()")