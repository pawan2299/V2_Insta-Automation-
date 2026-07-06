from __future__ import annotations
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, render_template_string
from config import SETTINGS
from database import init_db, is_safe_mode, set_config, get_config, get_stats, get_recent_activity, list_keywords, get_model_rpd, cleanup_old_data
from security import verify_signature
from bot_logic import handle_comment, handle_dm, handle_new_follower
from telegram_bot import handle_update, _send, get_webhook_info, register_telegram_webhook, check_and_send_festival_reminders, check_and_send_token_expiry_alert
from instagram_api import check_token_validity

logging.basicConfig(level=getattr(logging, SETTINGS.log_level, logging.INFO), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
_init_done = False
_init_lock = threading.Lock()

# [R&D FIX]: Reduced to 5 workers to prevent OOM (Out of Memory) crashes on Render Free Tier (512MB RAM)
executor = ThreadPoolExecutor(max_workers=5)

_reply_counts = []
_reply_lock = threading.Lock()
MAX_REPLIES_PER_MINUTE = 20

def _check_rate_limit() -> bool:
    now = time.time()
    with _reply_lock:
        while _reply_counts and now - _reply_counts[0] > 60: 
            _reply_counts.pop(0)
        if len(_reply_counts) >= MAX_REPLIES_PER_MINUTE:
            if not is_safe_mode():
                set_config("safe_mode", "true")
                _send(SETTINGS.telegram_chat_id, "🚨 <b>Global Rate Limit Triggered!</b>\nSafe Mode enabled to protect API quotas.")
            return False
        _reply_counts.append(now)
        return True

def _startup():
    global _init_done
    with _init_lock:
        if _init_done: return
        try:
            init_db()
            register_telegram_webhook()
            ig_valid = check_token_validity("ig_user")
            page_valid = check_token_validity("page_access")
            wh_url = get_webhook_info().get("result", {}).get("url", "None")
            _send(SETTINGS.telegram_chat_id, f"🦚 <b>Krishna Bot V3.2 Startup</b>\nIG: {'✅' if ig_valid else '❌'}\nPage: {'✅' if page_valid else '❌'}\nTG Webhook: <code>{wh_url}</code>")
            _init_done = True
        except Exception as e: 
            logger.critical(f"Startup failed: {e}")
            sys.exit(1)

_startup()

@app.before_request
def wake_up():
    from database import init_pool
    try: init_pool()
    except: pass

# [R&D FIX]: Zero-Cost Ping Endpoint for UptimeRobot (Prevents Neon DB from waking up unnecessarily)
@app.get("/ping")
def ping():
    return "OK", 200

@app.get("/")
def health():
    from gemini_client import MODEL_CONFIGS, _clients
    try:
        stats = get_stats()
        activity = get_recent_activity(15)
        keywords = list_keywords()
        
        status_class = "status-paused" if stats.get('bot_paused') else ("status-safe" if stats.get('safe_mode') else "status-live")
        status_text = "⏸ System Paused" if stats.get('bot_paused') else ("🛡️ Safe Mode" if stats.get('safe_mode') else "🟢 Live")
        
        model_html = ""
        total_pool = len(_clients) if _clients else 1
        for m in MODEL_CONFIGS:
            used = get_model_rpd(m["id"])
            limit = m["rpd"] * total_pool
            pct = int((used / limit) * 100) if limit > 0 else 0
            model_html += f'<div style="margin-bottom:1rem"><div style="display:flex;justify-content:space-between"><span>{m["label"]}</span><span>{used}/{limit}</span></div><div class="progress-bar"><div class="progress-fill" style="width:{pct}%"></div></div></div>'
            
        activity_html = "".join([f"<tr><td>{r['action']}</td><td>{r['created_at'].strftime('%H:%M:%S')}</td></tr>" for r in activity]) or "<tr><td colspan='2'>No activity</td></tr>"
        keywords_html = "".join([f"<tr><td><b>{k['keyword']}</b></td><td>{k['reply'][:30]}...</td></tr>" for k in keywords[:10]]) or "<tr><td colspan='2'>No keywords</td></tr>"
        
        HTML_TEMPLATE = """<!DOCTYPE html><html><head><title>Krishna Verse AI</title><link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@600&family=Inter:wght@300;400;600&display=swap" rel="stylesheet"><style>:root{--bg:#0a0a0c;--card:#141419;--border:#2a2a35;--gold:#d4af37;--saffron:#ff9933;--text:#e0e0e0;--muted:#888899;--success:#4ade80;--danger:#f87171;--warning:#fbbf24}body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;padding:2rem}.container{max-width:1000px;margin:0 auto}h1{font-family:'Cinzel',serif;color:var(--gold);text-align:center}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1.5rem}.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:1.5rem}.stat-value{font-size:2rem;font-weight:600;color:var(--saffron)}.progress-bar{height:8px;background:#222;border-radius:4px;overflow:hidden}.progress-fill{height:100%;background:linear-gradient(90deg,var(--saffron),var(--gold))}table{width:100%;border-collapse:collapse}th,td{padding:0.5rem;text-align:left;border-bottom:1px solid var(--border)}th{color:var(--gold)}</style></head><body><div class="container"><h1>🦚 Krishna Verse AI</h1><div style="text-align:center;margin:1rem 0"><span style="padding:0.5rem 1.5rem;border-radius:50px;background:rgba(74,222,128,0.1);color:var(--success);border:1px solid var(--success)">{{status_text}}</span></div><div class="grid"><div class="card"><h3>🌸 Stats</h3><div>Total Replies</div><div class="stat-value">{{total_replies}}</div><div>Last 24h</div><div class="stat-value" style="font-size:1.5rem">{{replies_24h}}</div></div><div class="card"><h3>🤖 Health</h3><div>Gemini: {{gemini_status}}</div><div>Circuit Breaker: {{cb_status}}</div></div></div><div class="card" style="margin:1.5rem 0"><h3>✨ Quotas</h3>{{model_html|safe}}</div><div class="grid"><div class="card"><h3>⚡ Activity</h3><table><thead><tr><th>Action</th><th>Time</th></tr></thead><tbody>{{activity_html|safe}}</tbody></table></div><div class="card"><h3>🔑 Keywords</h3><table><thead><tr><th>Trigger</th><th>Reply</th></tr></thead><tbody>{{keywords_html|safe}}</tbody></table></div></div></div></body></html>"""
        return render_template_string(HTML_TEMPLATE, status_text=status_text, total_replies=stats['total_comments_replied'], replies_24h=stats['last_24h_replies'], gemini_status="Active" if stats['gemini_enabled'] else "Off", cb_status="Tripped" if stats['circuit_breaker_active'] else "Stable", model_html=model_html, activity_html=activity_html, keywords_html=keywords_html)
    except Exception as e: 
        return f"Error: {e}", 500

@app.get("/webhook")
def verify_webhook():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == SETTINGS.verify_token:
        return request.args.get("hub.challenge", ""), 200
    return "Forbidden", 403

@app.post("/webhook")
def webhook():
    if not verify_signature(request.data, request.headers.get("X-Hub-Signature-256", "")): 
        return "Forbidden", 403
    
    data = request.get_json(silent=True) or {}
    
    # [R&D FIX]: Process webhooks in background threads to prevent Meta Webhook Timeouts (502)
    executor.submit(process_webhook_data, data)
    return "OK", 200

def process_webhook_data(data: dict):
    try:
        check_and_send_festival_reminders()
        check_and_send_token_expiry_alert()
        
        if not _check_rate_limit(): 
            return
            
        for entry in data.get("entry", []):
            for msg in entry.get("messaging", []): 
                handle_dm(msg)
            for change in entry.get("changes", []):
                if change.get("field") == "comments": 
                    handle_comment(change.get("value", {}))
                elif change.get("field") == "follows": 
                    handle_new_follower(change.get("value", {}).get("id", ""))
    except Exception as e:
        logger.error(f"Webhook processing failed: {e}")

@app.post("/telegram-webhook")
def telegram_webhook():
    # [R&D FIX]: Telegram updates also processed in background
    executor.submit(handle_update, request.get_json(silent=True) or {})
    return "OK", 200

@app.get("/weekly-report")
def weekly_report_trigger():
    try:
        from gemini_client import generate_weekly_insight
        stats = get_stats()
        insight = generate_weekly_insight(stats)
        if insight:
            _send(SETTINGS.telegram_chat_id, f"📊 <b>Weekly Report</b>\nReplies: {stats['total_comments_replied']}\nDMs: {stats['welcome_dms_sent']}\n💡 {insight}")
        cleanup_old_data()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__": 
    app.run(host="0.0.0.0", port=SETTINGS.port, debug=False)