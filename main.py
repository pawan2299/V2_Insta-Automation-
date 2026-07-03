from __future__ import annotations
import logging
import sys
import threading
import time
from flask import Flask, request, jsonify, render_template_string
from config import SETTINGS
from database import init_db, is_safe_mode, set_state, get_state
from security import verify_signature
from bot_logic import handle_comment, handle_dm
from telegram_bot import handle_update, _send, get_webhook_info, register_telegram_webhook
from instagram_api import check_token_validity

logging.basicConfig(
    level=getattr(logging, SETTINGS.log_level, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

_init_done = False
_init_lock = threading.Lock()

# Global Rate Limiting
_reply_counts = []
_reply_lock = threading.Lock()
MAX_REPLIES_PER_MINUTE = 20


def _check_rate_limit() -> bool:
    """Global safety limit to prevent loops from exploding."""
    now = time.time()
    with _reply_lock:
        # Cleanup old entries
        while _reply_counts and now - _reply_counts[0] > 60:
            _reply_counts.pop(0)
        
        if len(_reply_counts) >= MAX_REPLIES_PER_MINUTE:
            if not is_safe_mode():
                logger.critical(f"Global rate limit hit ({len(_reply_counts)}/min). Enabling Safe Mode.")
                set_state("safe_mode", "true")
                _send(
                    SETTINGS.telegram_chat_id,
                    "🚨 <b>Global Rate Limit Triggered!</b>\n\n"
                    f"Bot sent {len(_reply_counts)} replies in 60s. Safe Mode enabled automatically to prevent loops."
                )
            return False
        
        _reply_counts.append(now)
        return True


def _startup():
    global _init_done
    with _init_lock:
        if _init_done:
            return

    try:
        logger.info("Starting Krishna Bot initialization...")
        init_db()
        
        # Diagnostics
        logger.info(f"Telegram Bot Token loaded: {'✅' if SETTINGS.telegram_bot_token else '❌'}")
        logger.info(f"Telegram Chat ID loaded: {'✅' if SETTINGS.telegram_chat_id else '❌'}")
        
        # Register Webhook
        register_telegram_webhook()
        
        ig_valid = check_token_validity("ig_user")
        page_valid = check_token_validity("page_access")
        
        status_msg = "🦚 <b>Krishna Bot Startup</b>\n\n"
        status_msg += f"IG User Token: {'✅ Valid' if ig_valid else '❌ Invalid'}\n"
        status_msg += f"Page Token: {'✅ Valid' if page_valid else '❌ Invalid'}\n"
        
        wh_info = get_webhook_info()
        wh_url = "None"
        if wh_info.get("ok"):
            wh_url = wh_info.get("result", {}).get("url", "None")
            status_msg += f"Telegram Webhook: <code>{wh_url}</code>"
        
        logger.info(f"Webhook URL: {wh_url}")
        
        _send(SETTINGS.telegram_chat_id, status_msg)
        
        if not ig_valid and not page_valid:
            logger.critical("Both tokens are invalid. Bot will not function.")
        
        with _init_lock:
            _init_done = True
        logger.info("🦚 Krishna Bot ready!")
    except Exception as e:
        logger.critical(f"Startup failed: {e}")
        sys.exit(1)


_startup()


@app.before_request
def wake_up():
    from database import init_pool
    try:
        init_pool()
    except Exception:
        pass


@app.get("/")
def health():
    from database import get_stats, get_recent_activity, list_keywords
    from gemini_client import MODEL_CONFIGS, _get_model_rpd_today, _clients
    import datetime
    
    try:
        stats = get_stats()
        activity = get_recent_activity(15)
        keywords = list_keywords()
        
        # Status logic
        if stats.get('bot_paused'):
            status_class = "status-paused"
            status_text = "⏸ System Paused"
        elif stats.get('safe_mode'):
            status_class = "status-safe"
            status_text = "🛡 Safe Mode Active"
        else:
            status_class = "status-live"
            status_text = "🟢 Live & Engaging"

        gemini_status = "Active" if stats.get('gemini_enabled') else "Disabled"
        gemini_color = "var(--success)" if stats.get('gemini_enabled') else "var(--danger)"

        cb_status = "Tripped" if stats.get('circuit_breaker_active') else "Stable"
        cb_color = "var(--danger)" if stats.get('circuit_breaker_active') else "var(--success)"

        sleep_start = get_state("sleep_start") or "1"
        sleep_end = get_state("sleep_end") or "6"
        sleep_hours = f"{sleep_start}:00 - {sleep_end}:00"
        
        ist_time = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)).strftime('%Y-%m-%d %H:%M:%S IST')
        gita_quote = "Whenever dharma declines and adharma prevails, I manifest Myself. — Bhagavad Gita 4.7"

        # Models HTML
        model_html = ""
        total_pool = len(_clients) if _clients else 1
        for m in MODEL_CONFIGS:
            used = _get_model_rpd_today(m["id"])
            limit = m["rpd"] * total_pool
            pct = int((used / limit) * 100) if limit > 0 else 0
            model_html += f"""
            <div style="margin-bottom: 1rem;">
                <div style="display: flex; justify-content: space-between; font-size: 0.9rem;">
                    <span>{m['label']}</span>
                    <span>{used} / {limit}</span>
                </div>
                <div class="progress-bar"><div class="progress-fill" style="width: {pct}%"></div></div>
            </div>
            """

        # Activity HTML
        activity_html = ""
        for row in activity:
            action = row.get('action', 'Unknown')
            time_obj = row.get('created_at')
            time_str = time_obj.strftime('%H:%M:%S') if time_obj else 'N/A'
            activity_html += f"<tr><td>{action}</td><td>{time_str}</td></tr>"
        if not activity:
            activity_html = "<tr><td colspan='2' style='text-align:center; color:var(--muted);'>No recent activity</td></tr>"

        # Keywords HTML
        keywords_html = ""
        for kw in keywords[:10]:
            keywords_html += f"<tr><td><b>{kw['keyword']}</b></td><td>{kw['reply'][:40]}...</td></tr>"
        if not keywords:
            keywords_html = "<tr><td colspan='2' style='text-align:center; color:var(--muted);'>No custom keywords</td></tr>"
            
    except Exception as e:
        return f"Error loading dashboard: {str(e)}", 500

    HTML_TEMPLATE = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Krishna Verse AI | Dashboard</title>
        <link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@600&family=Lora:ital,wght@0,400;0,600;1,400&family=Inter:wght@300;400;600&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg: #0a0a0c;
                --card: #141419;
                --border: #2a2a35;
                --gold: #d4af37;
                --saffron: #ff9933;
                --lotus: #f4e4dc;
                --text: #e0e0e0;
                --muted: #888899;
                --success: #4ade80;
                --danger: #f87171;
                --warning: #fbbf24;
            }
            body {
                background-color: var(--bg);
                color: var(--text);
                font-family: 'Inter', sans-serif;
                margin: 0;
                padding: 2rem;
                background-image: radial-gradient(circle at 50% 0%, #1a1a24 0%, #0a0a0c 70%);
                min-height: 100vh;
            }
            .container { max-width: 1200px; margin: 0 auto; }
            header {
                text-align: center;
                margin-bottom: 3rem;
                border-bottom: 1px solid var(--border);
                padding-bottom: 2rem;
            }
            h1 {
                font-family: 'Cinzel', serif;
                color: var(--gold);
                font-size: 2.5rem;
                margin: 0;
                letter-spacing: 2px;
                text-shadow: 0 0 20px rgba(212, 175, 55, 0.3);
            }
            .subtitle {
                font-family: 'Lora', serif;
                color: var(--muted);
                font-style: italic;
                margin-top: 0.5rem;
            }
            .status-badge {
                display: inline-block;
                padding: 0.5rem 1.5rem;
                border-radius: 50px;
                font-weight: 600;
                font-size: 0.9rem;
                margin-top: 1rem;
                text-transform: uppercase;
                letter-spacing: 1px;
            }
            .status-live { background: rgba(74, 222, 128, 0.1); color: var(--success); border: 1px solid var(--success); }
            .status-paused { background: rgba(251, 191, 36, 0.1); color: var(--warning); border: 1px solid var(--warning); }
            .status-safe { background: rgba(248, 113, 113, 0.1); color: var(--danger); border: 1px solid var(--danger); }

            .grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 1.5rem;
                margin-bottom: 2rem;
            }
            .card {
                background: var(--card);
                border: 1px solid var(--border);
                border-radius: 16px;
                padding: 1.5rem;
                box-shadow: 0 4px 20px rgba(0,0,0,0.2);
                transition: transform 0.2s;
            }
            .card:hover { transform: translateY(-2px); border-color: var(--gold); }
            .card h3 {
                font-family: 'Cinzel', serif;
                color: var(--saffron);
                margin-top: 0;
                border-bottom: 1px solid var(--border);
                padding-bottom: 0.5rem;
                font-size: 1.1rem;
            }
            .stat-value {
                font-size: 2rem;
                font-weight: 600;
                color: var(--lotus);
                margin: 0.5rem 0;
            }
            .stat-label {
                color: var(--muted);
                font-size: 0.85rem;
                text-transform: uppercase;
            }
            
            .progress-bar {
                height: 8px;
                background: #222;
                border-radius: 4px;
                overflow: hidden;
                margin-top: 0.5rem;
            }
            .progress-fill {
                height: 100%;
                background: linear-gradient(90deg, var(--saffron), var(--gold));
                transition: width 0.5s ease;
            }

            table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
            th, td { padding: 0.75rem; text-align: left; border-bottom: 1px solid var(--border); }
            th { color: var(--gold); font-weight: 400; }
            
            .lotus-divider { text-align: center; color: var(--muted); margin: 3rem 0 1rem 0; font-size: 1.5rem; }
            .footer-text { text-align: center; color: var(--muted); font-size: 0.8rem; font-family: 'Lora', serif; }
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <h1>🦚 Krishna Verse AI</h1>
                <div class="subtitle">Digital Ashram Automation Dashboard</div>
                <div class="status-badge {{ status_class }}">{{ status_text }}</div>
                <div style="margin-top: 1.5rem; color: var(--muted); font-size: 0.9rem; font-family: 'Lora', serif; font-style: italic; max-width: 600px; margin-left: auto; margin-right: auto;">
                    "{{ gita_quote }}"
                </div>
                <div style="margin-top: 0.5rem; color: var(--muted); font-size: 0.75rem;">
                    Server Time: {{ ist_time }}
                </div>
            </header>

            <div class="grid">
                <div class="card">
                    <h3>🌸 Engagement Stats</h3>
                    <div class="stat-label">Total Comments Replied</div>
                    <div class="stat-value">{{ total_replies }}</div>
                    <div class="stat-label">Last 24 Hours</div>
                    <div class="stat-value" style="font-size: 1.5rem; color: var(--saffron);">{{ replies_24h }}</div>
                </div>

                <div class="card">
                    <h3>💌 Community Growth</h3>
                    <div class="stat-label">Welcome DMs Sent</div>
                    <div class="stat-value">{{ total_dms }}</div>
                    <div class="stat-label">Environment</div>
                    <div style="color: var(--muted); margin-top: 0.5rem; text-transform: uppercase;">{{ env }}</div>
                </div>

                <div class="card">
                    <h3>🤖 System Health</h3>
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem;">
                        <span>Gemini AI</span>
                        <span style="color: {{ gemini_color }}">{{ gemini_status }}</span>
                    </div>
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem;">
                        <span>Circuit Breaker</span>
                        <span style="color: {{ cb_color }}">{{ cb_status }}</span>
                    </div>
                    <div style="display: flex; justify-content: space-between;">
                        <span>Sleep Hours (IST)</span>
                        <span>{{ sleep_hours }}</span>
                    </div>
                </div>
            </div>

            <div class="card" style="margin-bottom: 2rem;">
                <h3>✨ Gemini Model Quotas (Today)</h3>
                {{ model_html | safe }}
            </div>

            <div class="grid">
                <div class="card">
                    <h3>📜 Recent Activity</h3>
                    <table>
                        <thead><tr><th>Action</th><th>Time (UTC)</th></tr></thead>
                        <tbody>
                            {{ activity_html | safe }}
                        </tbody>
                    </table>
                </div>
                <div class="card">
                    <h3>🔑 Active Keywords</h3>
                    <table>
                        <thead><tr><th>Trigger</th><th>Response</th></tr></thead>
                        <tbody>
                            {{ keywords_html | safe }}
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="lotus-divider">🌸 ॐ नमः शान्ति 🌸</div>
            <div class="footer-text">
                Designed for @krishna.verse.ai | Serving with Love & Devotion
            </div>
        </div>
    </body>
    </html>
    """

    return render_template_string(HTML_TEMPLATE,
        status_class=status_class, status_text=status_text,
        total_replies=stats.get('total_comments_replied', 0),
        replies_24h=stats.get('last_24h_replies', 0),
        total_dms=stats.get('welcome_dms_sent', 0),
        env=SETTINGS.environment,
        gemini_status=gemini_status, gemini_color=gemini_color,
        cb_status=cb_status, cb_color=cb_color,
        sleep_hours=sleep_hours,
        model_html=model_html,
        activity_html=activity_html,
        keywords_html=keywords_html,
        ist_time=ist_time,
        gita_quote=gita_quote
    )


@app.get("/webhook")
def verify_webhook():
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == SETTINGS.verify_token):
        logger.info("Webhook verified by Meta.")
        return request.args.get("hub.challenge", ""), 200
    return "Forbidden", 403


@app.post("/webhook")
def webhook():
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, sig):
        logger.warning("Invalid webhook signature!")
        return "Forbidden", 403

    data = request.get_json(silent=True) or {}

    def process():
        # 🌟 Daily background tasks (Runs once a day safely)
        from telegram_bot import check_and_send_festival_reminders, check_and_send_token_expiry_alert
        check_and_send_festival_reminders()
        check_and_send_token_expiry_alert()  # ✅ Added Token Alert

        # Global Rate Limit Check
        if not _check_rate_limit() and not is_safe_mode():
            return

        for entry in data.get("entry", []):
            # Instagram DMs come through entry.messaging
            for msg_event in entry.get("messaging", []):
                handle_dm(msg_event)

            for change in entry.get("changes", []):
                field = change.get("field")
                value = change.get("value", {})
                if field == "comments":
                    handle_comment(value)
                elif field == "messages":
                    handle_dm(value)

    threading.Thread(target=process, daemon=True).start()
    return "OK", 200


@app.post("/telegram-webhook")
def telegram_webhook():
    try:
        update = request.get_json(silent=True) or {}
        threading.Thread(
            target=handle_update, args=(update,), daemon=True
        ).start()
    except Exception as e:
        logger.error(f"Error in telegram_webhook endpoint: {e}")
    return "OK", 200


@app.get("/weekly-report")
def weekly_report_trigger():
    try:
        from database import get_stats
        from gemini_client import generate_weekly_insight
        stats = get_stats()
        insight = generate_weekly_insight(stats)
        if insight:
            _send(
                SETTINGS.telegram_chat_id,
                f"📊 <b>Weekly Krishna Bot Report</b>\n\n"
                f"Total Replies: {stats['total_comments_replied']}\n"
                f"Welcome DMs: {stats['welcome_dms_sent']}\n\n"
                f"🤖 <b>Gemini Insights:</b>\n{insight}"
            )
        return jsonify({"status": "report sent"}), 200
    except Exception as e:
        logger.error(f"Weekly report error: {e}")
        return jsonify({"error": str(e)}), 500


@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled error: {e}", exc_info=True)
    try:
        _send(
            SETTINGS.telegram_chat_id,
            f"🔴 <b>Bot Error!</b>\n\n<code>{str(e)[:300]}</code>"
        )
    except Exception:
        pass
    return jsonify({"error": "Internal server error"}), 500


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=SETTINGS.port, debug=False)
