from __future__ import annotations
import logging
import sys
import threading
import time
import json
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from flask import Flask, request, jsonify, render_template, Response
from config import SETTINGS
from database import (init_db, is_safe_mode, set_config, get_config, get_stats, 
                      get_recent_activity, list_keywords, get_model_rpd, cleanup_old_data, 
                      get_failed_webhooks, delete_failed_webhook, start_db_keepalive, is_bot_paused)
from security import verify_signature
from bot_logic import handle_comment, handle_dm, handle_new_follower
from telegram_bot import handle_update, _send, get_webhook_info, register_telegram_webhook, check_and_send_festival_reminders, check_and_send_token_expiry_alert
from instagram_api import check_token_validity
import psutil

logging.basicConfig(level=getattr(logging, SETTINGS.log_level, logging.INFO), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates')

# ✅ UPDATED: ThreadPool reduced to 5
executor = ThreadPoolExecutor(max_workers=5)

# ✅ NEW: In-Memory Metrics (Ring Buffer)
webhook_metrics = deque(maxlen=100)
error_metrics = deque(maxlen=100)
_reply_counts = []
_reply_lock = threading.Lock()
MAX_REPLIES_PER_MINUTE = 20

# ✅ NEW: Memory Usage Monitor
def start_memory_monitor():
    def monitor():
        while True:
            time.sleep(60)
            mem = psutil.virtual_memory().percent
            if mem > 85: # Render limit is 512MB, 85% is safe threshold
                _send(SETTINGS.telegram_chat_id, f"🚨 <b>High Memory Alert!</b>\nUsage: {mem}%")
    threading.Thread(target=monitor, daemon=True).start()

def _check_rate_limit() -> bool:
    now = time.time()
    with _reply_lock:
        while _reply_counts and now - _reply_counts[0] > 60: _reply_counts.pop(0)
        if len(_reply_counts) >= MAX_REPLIES_PER_MINUTE:
            if not is_safe_mode():
                set_config("safe_mode", "true")
                _send(SETTINGS.telegram_chat_id, "🚨 <b>Global Rate Limit Triggered!</b>\nSafe Mode enabled.")
            return False
        _reply_counts.append(now)
        return True

def _startup():
    try:
        init_db()
        start_db_keepalive() # ✅ NEW: Start DB Ping
        start_memory_monitor() # ✅ NEW: Start Memory Monitor
        register_telegram_webhook()
        ig_valid = check_token_validity("ig_user")
        page_valid = check_token_validity("page_access")
        wh_url = get_webhook_info().get("result", {}).get("url", "None")
        _send(SETTINGS.telegram_chat_id, f"🦚 <b>Krishna Bot V4.0 Enterprise Startup</b>\nIG: {'✅' if ig_valid else '❌'}\nPage: {'✅' if page_valid else '❌'}\nTG Webhook: <code>{wh_url}</code>")
    except Exception as e: logger.critical(f"Startup failed: {e}"); sys.exit(1)

_startup()

@app.before_request
def wake_up():
    from database import init_pool
    try: init_pool()
    except: pass

@app.get("/")
def health():
    return render_template("dashboard.html") # ✅ Serve Premium Dashboard

# ✅ NEW: Dashboard API Endpoints
@app.get("/api/stats")
def api_stats():
    stats = get_stats()
    return jsonify({
        "total_replies": stats['total_comments_replied'],
        "replies_24h": stats['last_24h_replies'],
        "status_text": "⏸ Paused" if stats['bot_paused'] else ("🛡️ Safe Mode" if stats['safe_mode'] else "🟢 Live"),
        "status_class": "text-gray-400" if stats['bot_paused'] else ("text-yellow-400" if stats['safe_mode'] else "text-green-400"),
        "bot_paused": stats['bot_paused'],
        "safe_mode": stats['safe_mode']
    })

@app.post("/api/toggle-bot")
def api_toggle_bot():
    if is_bot_paused():
        set_config("bot_paused", "false"); set_config("safe_mode", "false")
    else:
        set_config("bot_paused", "true")
    return jsonify({"status": "ok"})

@app.post("/api/panic")
def api_panic():
    set_config("safe_mode", "true"); set_config("gemini_enabled", "false")
    return jsonify({"status": "ok"})

# ✅ NEW: Server-Sent Events (SSE) for Live Dashboard Metrics
@app.get("/stream")
def stream():
    def event_stream():
        while True:
            time.sleep(2)
            avg_latency = sum(webhook_metrics) / len(webhook_metrics) * 1000 if webhook_metrics else 0
            error_rate = (sum(error_metrics) / len(error_metrics) * 100) if error_metrics else 0
            
            from gemini_client import MODEL_CONFIGS, _clients
            models = []
            total_pool = len(_clients) if _clients else 1
            for m in MODEL_CONFIGS:
                used = get_model_rpd(m["id"])
                limit = m["rpd"] * total_pool
                models.append({"label": m["label"], "used": used, "limit": limit})
                
            yield f"data: {json.dumps({'latency': avg_latency, 'error_rate': error_rate, 'models': models})}\n\n"
    return Response(event_stream(), mimetype='text/event-stream')

@app.get("/webhook")
def verify_webhook():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == SETTINGS.verify_token:
        return request.args.get("hub.challenge", ""), 200
    return "Forbidden", 403

@app.post("/webhook")
def webhook():
    if not verify_signature(request.data, request.headers.get("X-Hub-Signature-256", "")): return "Forbidden", 403
    data = request.get_json(silent=True) or {}
    
    start_time = time.time()
    event_count = 0
    is_error = False
    
    def process():
        nonlocal is_error, event_count  # <--- बस यहाँ event_count add कर दिया
        try:
            check_and_send_festival_reminders()
            check_and_send_token_expiry_alert()
            if not _check_rate_limit(): return
            
            for entry in data.get("entry", []):
                event_count += len(entry.get("messaging", []))
                event_count += len(entry.get("changes", []))
                for msg in entry.get("messaging", []): handle_dm(msg)
                for change in entry.get("changes", []):
                    if change.get("field") == "comments": handle_comment(change.get("value", {}))
                    elif change.get("field") == "follows": handle_new_follower(change.get("value", {}).get("id", ""))
        except Exception as e:
            is_error = True
            logger.error(f"Webhook processing error: {e}")
        finally:
            # ✅ NEW: Record Metrics
            webhook_metrics.append(time.time() - start_time)
            error_metrics.append(1 if is_error else 0)
            processing_time = time.time() - start_time
            queue_depth = executor._work_queue.qsize() if hasattr(executor, '_work_queue') else -1
            logger.info(f"✅ Webhook processed: {event_count} events in {processing_time:.2f}s | Queue Depth: {queue_depth}")

    executor.submit(process)
    return "OK", 200

# ✅ NEW: Retry Failed Webhooks Endpoint
@app.post("/retry-failed-webhooks")
def retry_failed():
    failed = get_failed_webhooks(10)
    if not failed: return jsonify({"message": "No failed webhooks found."})
    
    success_count = 0
    for row in failed:
        try:
            payload = row['payload']
            if 'messaging' in payload: handle_dm(payload['messaging'][0])
            elif 'changes' in payload: handle_comment(payload['changes'][0].get('value', {}))
            delete_failed_webhook(row['event_id'])
            success_count += 1
        except Exception as e:
            logger.error(f"Retry failed for {row['event_id']}: {e}")
            
    return jsonify({"message": f"Retried {success_count} webhooks."})

@app.post("/telegram-webhook")
def telegram_webhook():
    executor.submit(handle_update, request.get_json(silent=True) or {})
    return "OK", 200

@app.get("/weekly-report")
def weekly_report_trigger():
    try:
        from gemini_client import generate_weekly_insight
        stats = get_stats()
        insight = generate_weekly_insight(stats)
        if insight:
            _send(SETTINGS.telegram_chat_id, f"📈 <b>Weekly Report</b>\nReplies: {stats['total_comments_replied']}\nDMs: {stats['welcome_dms_sent']}\n💡 {insight}")
        cleanup_old_data()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__": app.run(host="0.0.0.0", port=SETTINGS.port, debug=False)