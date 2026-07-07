from flask import Flask, request, jsonify, render_template
import threading
import json
import time
import logging
import os
from functools import wraps
from datetime import datetime, timezone, timedelta

from config import SETTINGS
from security import verify_signature
from bot_logic import handle_comment, handle_dm
from telegram_bot import handle_update, register_telegram_webhook
from database import (
    init_db, get_stats, get_and_lock_failed_webhooks, 
    is_bot_paused, set_config, get_model_rpd, get_recent_activity,
    get_top_posts
)
import gemini_client

logging.basicConfig(level=getattr(logging, SETTINGS.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- 🔒 Dashboard Security ---
def require_dashboard_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        required_password = os.environ.get("DASHBOARD_PASSWORD", "admin123")
        
        if not auth or auth.password != required_password:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

@app.before_request
def initialize_app():
    if not getattr(app, '_db_initialized', False):
        try:
            init_db()
            register_telegram_webhook()
            app._db_initialized = True
            logger.info("🚀 Database and Telegram Webhook initialized.")
        except Exception as e:
            logger.error(f"❌ Initialization failed: {e}")

@app.route("/", methods=["GET"])
@require_dashboard_auth
def dashboard():
    return render_template("dashboard.html")

@app.route("/health", methods=["GET"])
def health_check():
    return "OK", 200

@app.route("/webhook", methods=["GET", "POST"])
def instagram_webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == SETTINGS.verify_token:
            logger.info("✅ Meta Webhook verified.")
            return challenge, 200
        return "Forbidden", 403

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, signature):
        return "Invalid Signature", 403

    data = request.get_json(silent=True)
    if not data or "entry" not in data:
        return "OK", 200

    threading.Thread(target=_process_meta_payload, args=(data,), daemon=True).start()
    return "OK", 200

def _process_meta_payload(data):
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                field = change.get("field")
                value = change.get("value", {})
                if field == "comments": handle_comment(value)
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

# --- 📊 Enhanced Dashboard API ---
@app.route("/api/stats", methods=["GET"])
@require_dashboard_auth
def api_stats():
    try:
        stats = get_stats()
        
        # 🎯 Gemini Model Usage - Real Data
        total_pool = len(gemini_client._clients) if gemini_client._clients else 1
        models_usage = []
        total_calls_today = 0
        for m in gemini_client.MODEL_CONFIGS:
            used = get_model_rpd(m["id"])
            limit = m["rpd"] * total_pool
            percentage = int((used / limit) * 100) if limit > 0 else 0
            models_usage.append({
                "id": m["id"],
                "label": m["label"],
                "used": used,
                "limit": limit,
                "percentage": percentage,
                "rpm": m["rpm"] * total_pool,
                "rpd": m["rpd"] * total_pool,
            })
            total_calls_today += used
        
        # 📜 Recent Activity Feed
        recent_activity = []
        try:
            activities = get_recent_activity(8)
            for act in activities:
                recent_activity.append({
                    "action": act["action"],
                    "time": act["created_at"].strftime("%H:%M:%S"),
                    "timestamp": act["created_at"].isoformat()
                })
        except Exception as e:
            logger.warning(f"Recent activity fetch failed: {e}")
        
        # 🔥 Top Performing Posts
        top_posts = []
        try:
            posts = get_top_posts(5)
            for post in posts:
                top_posts.append({
                    "media_id": post["media_id"],
                    "reply_count": post["reply_count"]
                })
        except Exception as e:
            logger.warning(f"Top posts fetch failed: {e}")
        
        # System Status Logic
        bot_paused = stats.get("bot_paused", False)
        safe_mode = stats.get("safe_mode", False)
        circuit_breaker = stats.get("circuit_breaker_active", False)
        
        if bot_paused:
            status_text = "Paused"
            status_class = "status-paused"
            status_icon = "⏸"
        elif circuit_breaker:
            status_text = "Circuit Breaker"
            status_class = "status-warning"
            status_icon = "🚨"
        elif safe_mode:
            status_text = "Safe Mode"
            status_class = "status-warning"
            status_icon = "🛡️"
        else:
            status_text = "Live"
            status_class = "status-live"
            status_icon = "●"
        
        return jsonify({
            "success": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stats": {
                "total_replies": stats.get("total_comments_replied", 0),
                "replies_24h": stats.get("last_24h_replies", 0),
                "total_dms": stats.get("welcome_dms_sent", 0),
                "bot_paused": bot_paused,
                "safe_mode": safe_mode,
                "gemini_enabled": stats.get("gemini_enabled", True),
                "circuit_breaker": circuit_breaker,
                "consecutive_429s": stats.get("consecutive_429s", 0),
                "total_gemini_today": total_calls_today,
            },
            "status": {
                "text": status_text,
                "class": status_class,
                "icon": status_icon,
            },
            "models": models_usage,
            "recent_activity": recent_activity,
            "top_posts": top_posts,
        })
    except Exception as e:
        logger.error(f"Stats API error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

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

@app.route("/api/resume", methods=["POST"])
@require_dashboard_auth
def resume_bot():
    set_config("bot_paused", "false")
    set_config("safe_mode", "false")
    set_config("gemini_enabled", "true")
    set_config("consecutive_429s", "0")
    set_config("circuit_breaker_until", "0")
    return jsonify({"success": True})

@app.route("/api/retry-failed", methods=["POST"])
@require_dashboard_auth
def retry_failed():
    failed = get_and_lock_failed_webhooks(limit=5)
    if not failed:
        return jsonify({"success": True, "message": "No failed webhooks to retry.", "count": 0})
    
    threading.Thread(target=_retry_webhooks, args=(failed,), daemon=True).start()
    return jsonify({"success": True, "message": f"Retrying {len(failed)} webhooks in background.", "count": len(failed)})

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=SETTINGS.port, debug=False)
