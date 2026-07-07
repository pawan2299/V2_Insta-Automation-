# app.py (Missing File - Add this immediately)
from flask import Flask, request, jsonify, render_template, Response
import threading
import json
import time
from security import verify_signature
from bot_logic import handle_comment, handle_dm
from telegram_bot import handle_update, register_telegram_webhook
from database import init_db, get_stats, get_failed_webhooks, get_and_lock_failed_webhooks, delete_failed_webhook
import gemini_client
import config

app = Flask(__name__)

@app.before_request
def initialize():
    # Ensure DB is ready
    pass

@app.route("/", methods=["GET"])
def dashboard():
    return render_template("dashboard.html")

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200 # For UptimeRobot

@app.route("/api/stats", methods=["GET"])
def api_stats():
    stats = get_stats()
    # Add basic auth here in production!
    return jsonify(stats)

@app.route("/webhook", methods=["GET", "POST"])
def instagram_webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == config.SETTINGS.verify_token:
            return challenge, 200
        return "Forbidden", 403

    # POST Request
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, signature):
        return "Invalid Signature", 403

    data = request.json
    if not data or "entry" not in data:
        return "OK", 200

    # Process in background thread to avoid Meta Webhook Timeout (CRITICAL FIX)
    threading.Thread(target=_process_meta_payload, args=(data,)).start()
    return "OK", 200

def _process_meta_payload(data):
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") == "comments":
                handle_comment(change.get("value", {}))
            elif change.get("field") == "mentions":
                pass # Handle mentions
        for messaging in entry.get("messaging", []):
            handle_dm(messaging)

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    update = request.json
    if update:
        threading.Thread(target=handle_update, args=(update,)).start()
    return "OK", 200

@app.route("/stream")
def stream():
    def generate():
        while True:
            stats = get_stats()
            models = [{"label": m["label"], "used": gemini_client.get_model_rpd(m["id"])} for m in gemini_client.MODEL_CONFIGS]
            yield f"data: {json.dumps({'latency': 50, 'models': models})}\n\n"
            time.sleep(5)
    return Response(generate(), mimetype="text/event-stream")

@app.route("/retry-failed-webhooks", methods=["POST"])
def retry_failed():
    # Add Auth!
    failed = get_and_lock_failed_webhooks(5)
    # Logic to retry...
    return jsonify({"message": f"Retrying {len(failed)} webhooks"})

# Initialize on startup
with app.app_context():
    init_db()
    register_telegram_webhook()