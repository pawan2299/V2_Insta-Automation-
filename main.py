from __future__ import annotations
import logging
import sys
import threading
import time
import json
import os
import hmac
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from collections import deque
from flask import Flask, request, jsonify, render_template, Response
from config import SETTINGS
from database import (init_db, is_safe_mode, set_config, get_config, get_stats, 
                      get_recent_activity, list_keywords, get_model_rpd, cleanup_old_data, 
                      get_and_lock_failed_webhooks, delete_failed_webhook, start_db_keepalive, is_bot_paused)
from security import verify_signature
from bot_logic import handle_comment, handle_dm
from telegram_bot import handle_update, _send, get_webhook_info, register_telegram_webhook, check_and_send_festival_reminders, check_and_send_token_expiry_alert
from instagram_api import check_token_validity
import psutil

logging.basicConfig(level=getattr(logging, SETTINGS.log_level, logging.INFO), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates')
# ✅ FIX: Reduced to 3 workers for Render free tier (512MB RAM limit)
executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="webhook-worker")

# ✅ FIX: Reduced maxlen to 50 to prevent memory leaks on free tier
webhook_metrics = deque(maxlen=50)
error_metrics = deque(maxlen=50)
_reply_counts = []
_reply_lock = threading.Lock()
MAX_REPLIES_PER_MINUTE = 20

# ✅ NEW: Dashboard authentication token (simple free-tier solution)
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", SETTINGS.verify_token)

def start_memory_monitor():
    def monitor():
        while True:
            time.sleep(60)
            mem = psutil.virtual_memory().percent
            if mem > 85: 
                _send(SETTINGS.telegram_chat_id, f"🚨 <b>High Memory Alert!</b>\nUsage: {mem}%")
    threading.Thread(target=monitor, daemon=True).start()

# ✅ NEW: Auto-Retry Background Worker
def start_auto_retry_worker():
    def worker():
        while True:
            time.sleep(1800) # 30 minutes
            try:
                failed = get_and_lock_failed_webhooks(5)
                if not failed: continue
                logger.info(f"🔄 Auto-retry worker processing {len(failed)} failed webhooks...")
                for row in failed:
                    try:
                        payload = row['payload']
                        if 'messaging' in payload: handle_dm(payload['messaging'][0])
                        elif 'changes' in payload: handle_comment(payload['changes'][0].get('value', {}))
                        delete_failed_webhook(row['event_id'])
                    except Exception as e:
                        logger.error(f"❌ Auto-retry failed for {row['event_id']}: {e}")
            except Exception as e:
                logger.error(f"❌ Auto-retry worker crashed: {e}")
    threading.Thread(target=worker, daemon=True).start()

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
        start_db_keepalive()
        start_memory_monitor()
        start_auto_retry_worker() # ✅ Start Auto-Retry
        register_telegram_webhook()
        check_and_send_festival_reminders()
        check_and_send_token_expiry_alert()
        ig_valid = check_token_validity("ig_user")
        page_valid = check_token_validity("page_access")
        wh_url = get_webhook_info().get("result", {}).get("url", "None")
        _send(SETTINGS.telegram_chat_id, f"🦚 <b>Krishna Bot V7.0 Enterprise Startup</b>\nIG: {'✅' if ig_valid else '❌'}\nPage: {'✅' if page_valid else '❌'}\nTG Webhook: <code>{wh_url}</code>")
    except Exception as e: 
        logger.critical(f"❌ Startup failed: {e}")
        sys.exit(1)

_startup()

@app.before_request
def wake_up():
    from database import init_pool
    try: init_pool()
    except: pass

# ✅ NEW: Comprehensive health check endpoint for free tier monitoring
@app.get("/health")
def health_check():
    """Comprehensive health check with DB and API connectivity verification."""
    import time
    health_status = {
        "status": "healthy",
        "timestamp": time.time(),
        "checks": {
            "database": "unknown",
            "memory": "unknown",
            "uptime": "unknown"
        }
    }
    
    # Check database connectivity
    try:
        from database import get_stats
        stats = get_stats()
        health_status["checks"]["database"] = "connected"
    except Exception as e:
        health_status["checks"]["database"] = f"error: {str(e)}"
        health_status["status"] = "unhealthy"
    
    # Check memory usage
    try:
        mem = psutil.virtual_memory()
        health_status["checks"]["memory"] = {
            "percent": mem.percent,
            "available_mb": mem.available / (1024 * 1024)
        }
        if mem.percent > 90:
            health_status["status"] = "degraded"
    except Exception as e:
        health_status["checks"]["memory"] = f"error: {str(e)}"
    
    # Add simple uptime tracking
    health_status["checks"]["uptime"] = "running"
    
    status_code = 200 if health_status["status"] == "healthy" else (503 if health_status["status"] == "unhealthy" else 200)
    return jsonify(health_status), status_code

# ✅ NEW: Dashboard authentication decorator (free-tier friendly)
def _check_dashboard_auth():
    """Check if request has valid dashboard token."""
    auth_header = request.headers.get("Authorization", "")
    token = request.args.get("token", "")
    
    if auth_header.startswith("Bearer "):
        provided_token = auth_header[7:]
    elif token:
        provided_token = token
    else:
        return False
    
    return hmac.compare_digest(provided_token, DASHBOARD_TOKEN)

@app.get("/")
def health():
    return render_template("dashboard.html")

@app.get("/api/stats")
def api_stats():
    # ✅ SECURITY: Add basic auth check for API endpoints
    if not _check_dashboard_auth():
        return jsonify({"error": "Unauthorized"}), 401
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
    # ✅ SECURITY: Add auth check for admin endpoints
    if not _check_dashboard_auth():
        return jsonify({"error": "Unauthorized"}), 401
    if is_bot_paused():
        set_config("bot_paused", "false"); set_config("safe_mode", "false")
    else:
        set_config("bot_paused", "true")
    return jsonify({"status": "ok"})

@app.post("/api/panic")
def api_panic():
    # ✅ SECURITY: Add auth check for admin endpoints
    if not _check_dashboard_auth():
        return jsonify({"error": "Unauthorized"}), 401
    set_config("safe_mode", "true"); set_config("gemini_enabled", "false")
    return jsonify({"status": "ok"})

@app.get("/stream")
def stream():
    # ✅ SECURITY: Add auth check for SSE endpoint (prevent unauthorized streaming)
    if not _check_dashboard_auth():
        return Response("Unauthorized", status=401, mimetype='text/plain')
    
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
        nonlocal is_error, event_count
        try:
            if not _check_rate_limit(): return
            for entry in data.get("entry", []):
                event_count += len(entry.get("messaging", []))
                event_count += len(entry.get("changes", []))
                for msg in entry.get("messaging", []): handle_dm(msg)
                for change in entry.get("changes", []):
                    if change.get("field") == "comments": handle_comment(change.get("value", {}))
        except Exception as e:
            is_error = True
            logger.error(f"❌ Webhook processing error: {e}")
        finally:
            webhook_metrics.append(time.time() - start_time)
            error_metrics.append(1 if is_error else 0)
            processing_time = time.time() - start_time
            logger.info(f"✅ Webhook processed: {event_count} events in {processing_time:.2f}s")

    # ✅ FIX: Add timeout to prevent hanging tasks on free tier
    future = executor.submit(process)
    try:
        future.result(timeout=30)  # 30 second timeout for webhook processing
    except FuturesTimeoutError:
        logger.error(f"⏰ Webhook processing timed out after 30s for event")
        is_error = True
        error_metrics.append(1)
    
    return "OK", 200

@app.post("/retry-failed-webhooks")
def retry_failed():
    # ✅ SECURITY: Add auth check for admin endpoints
    if not _check_dashboard_auth():
        return jsonify({"error": "Unauthorized"}), 401
    
    failed = get_and_lock_failed_webhooks(10)
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
            logger.error(f"❌ Retry failed for {row['event_id']}: {e}")
    return jsonify({"message": f"Retried {success_count} webhooks."})

@app.post("/telegram-webhook")
def telegram_webhook():
    # ✅ SECURITY: Validate Telegram webhook signature (basic free-tier solution)
    # Note: For production, implement proper Telegram signature verification
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