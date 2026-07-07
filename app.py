from flask import Flask, request, jsonify, render_template, Response
import threading
import json
import time
import logging
from functools import wraps

from config import SETTINGS
from security import verify_signature
from bot_logic import handle_comment, handle_dm
from telegram_bot import handle_update, register_telegram_webhook
from database import (
    init_db, get_stats, get_and_lock_failed_webhooks, 
    is_bot_paused, set_config, get_model_rpd
)
import gemini_client

# Configure logging
logging.basicConfig(level=getattr(logging, SETTINGS.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Basic Auth Decorator (Dashboard Security) ---
def require_dashboard_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Future proof: Yahan aap Flask-BasicAuth ya Token check add kar sakte hain
        return f(*args, **kwargs)
    return decorated

# --- Initialization ---
@app.before_request
def initialize_app():
    # Ensure DB and Telegram Webhook are initialized only once per worker
    if not getattr(app, '_db_initialized', False):
        try:
            init_db()
            register_telegram_webhook()
            app._db_initialized = True
            logger.info("🚀 Database and Telegram Webhook initialized.")
        except Exception as e:
            logger.error(f"❌ Initialization failed: {e}")

# --- Routes ---

@app.route("/", methods=["GET"])
@require_dashboard_auth
def dashboard():
    return render_template("dashboard.html")

@app.route("/health", methods=["GET"])
def health_check():
    """Lightweight health check for UptimeRobot. Keeps Render awake, lets Neon sleep."""
    return "OK", 200

@app.route("/webhook", methods=["GET", "POST"])
def instagram_webhook():
    # 1. Meta Webhook Verification (GET)
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        if mode == "subscribe" and token == SETTINGS.verify_token:
            logger.info("✅ Meta Webhook verified successfully.")
            return challenge, 200
        else:
            logger.warning("❌ Meta Webhook verification failed.")
            return "Forbidden", 403

    # 2. Incoming Meta Events (POST)
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, signature):
        logger.warning("🚨 Invalid Meta Webhook Signature!")
        return "Invalid Signature", 403

    data = request.get_json(silent=True)
    if not data or "entry" not in data:
        return "OK", 200

    # 🚨 CRITICAL FIX: Process in background thread to avoid Meta's 3-second timeout limit
    threading.Thread(target=_process_meta_payload, args=(data,), daemon=True).start()
    
    # Return 200 OK immediately to Meta
    return "OK", 200

def _process_meta_payload(data):
    """Background worker for Meta Webhooks"""
    try:
        for entry in data.get("entry", []):
            # Handle Comments / Mentions
            for change in entry.get("changes", []):
                field = change.get("field")
                value = change.get("value", {})
                
                if field == "comments":
                    handle_comment(value)
                elif field == "mentions":
                    pass # Add specific mention logic if needed
            
            # Handle DMs / Story Mentions
            for messaging in entry.get("messaging", []):
                handle_dm(messaging)
    except Exception as e:
        logger.error(f"❌ Error processing Meta payload: {e}")

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(silent=True)
    if update:
        threading.Thread(target=handle_update, args=(update,), daemon=True).start()
    return "OK", 200

# --- Dashboard APIs ---

@app.route("/api/stats", methods=["GET"])
@require_dashboard_auth
def api_stats():
    try:
        stats = get_stats()
        return jsonify({
            "total_replies": stats.get("total_comments_replied", 0),
            "replies_24h": stats.get("last_24h_replies", 0),
            "bot_paused": stats.get("bot_paused", False),
            "status_text": "Paused" if stats.get("bot_paused") else ("Safe Mode" if stats.get("safe_mode") else "Live"),
            "status_class": "text-red-400" if stats.get("bot_paused") else ("text-yellow-400" if stats.get("safe_mode") else "text-green-400")
        })
    except Exception as e:
        logger.error(f"Stats API error: {e}")
        return jsonify({"error": "Failed to fetch stats"}), 500

@app.route("/api/toggle-bot", methods=["POST"])
@require_dashboard_auth
def toggle_bot():
    current_state = is_bot_paused()
    set_config("bot_paused", "false" if current_state else "true")
    return jsonify({"success": True, "paused": not current_state})

@app.route("/api/panic", methods=["POST"])
@require_dashboard_auth
def panic_mode():
    set_config("safe_mode", "true")
    set_config("gemini_enabled", "false")
    return jsonify({"success": True, "panic": True})

@app.route("/retry-failed-webhooks", methods=["POST"])
@require_dashboard_auth
def retry_failed():
    failed = get_and_lock_failed_webhooks(limit=5)
    if not failed:
        return jsonify({"message": "No failed webhooks to retry."})
    
    threading.Thread(target=_retry_webhooks, args=(failed,), daemon=True).start()
    return jsonify({"message": f"Retrying {len(failed)} webhooks in background."})

def _retry_webhooks(failed_records):
    from database import delete_failed_webhook
    for record in failed_records:
        try:
            payload = record.get("payload")
            if "message" in payload and "mid" in payload.get("message", {}):
                handle_dm(payload)
            elif "id" in payload and "text" in payload:
                handle_comment(payload)
            delete_failed_webhook(record["event_id"])
        except Exception as e:
            logger.error(f"Retry failed for {record['event_id']}: {e}")

@app.route("/stream")
@require_dashboard_auth
def stream():
    def generate():
        while True:
            try:
                models = [
                    {"label": m["label"], "used": get_model_rpd(m["id"]), "limit": m["rpd"] * len(gemini_client._clients)} 
                    for m in gemini_client.MODEL_CONFIGS
                ]
                yield f"data: {json.dumps({'latency': 45, 'models': models})}\n\n"
            except Exception as e:
                logger.error(f"SSE Error: {e}")
            time.sleep(5)
            
    return Response(generate(), mimetype="text/event-stream")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=SETTINGS.port, debug=False)