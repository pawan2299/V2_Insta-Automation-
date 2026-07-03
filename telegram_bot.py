from __future__ import annotations
import logging
import requests
import traceback
from datetime import datetime, timezone, timedelta
from config import SETTINGS
import database as db
import gemini_client as ai
from festivals import get_upcoming_festivals

logger = logging.getLogger(__name__)
BASE_URL = f"https://api.telegram.org/bot{SETTINGS.telegram_bot_token}"

# ── UI Helpers (Progressive Disclosure) ───────────────────
def _make_progress_bar(used: int, limit: int, length: int = 10) -> str:
    if limit <= 0: return "░" * length
    pct = min(100, int((used / limit) * 100))
    filled = int(length * pct / 100)
    return "█" * filled + "░" * (length - filled)

def _send(chat_id: str, text: str, reply_markup: dict = None):
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup: payload["reply_markup"] = reply_markup
        resp = requests.post(f"{BASE_URL}/sendMessage", json=payload, timeout=10)
        if not resp.ok: logger.error(f"Telegram send error: {resp.status_code}")
    except Exception as e: logger.error(f"Telegram request failed: {e}")

def _edit_message(chat_id: str, message_id: int, text: str, reply_markup: dict = None):
    try:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if reply_markup: payload["reply_markup"] = reply_markup
        requests.post(f"{BASE_URL}/editMessageText", json=payload, timeout=10)
    except Exception as e: logger.error(f"Telegram edit failed: {e}")

def _answer_callback(callback_query_id: str, text: str = ""):
    try:
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": callback_query_id, "text": text}, timeout=10)
    except: pass

MAIN_MENU_BUTTONS = {
    "inline_keyboard": [
        [{"text": "📊 Status & Quotas", "callback_data": "menu_status"}],
        [{"text": "🎉 Festivals & Ideas", "callback_data": "menu_festivals"}],
        [{"text": "⚙️ System Controls", "callback_data": "menu_controls"}],
        [{"text": "🔑 Keywords & AI", "callback_data": "menu_ai"}]
    ]
}

# ── Core Update Handler ───────────────────────────────────
def handle_update(update: dict):
    try:
        # 1. Handle Button Clicks (Callback Queries)
        if "callback_query" in update:
            handle_callback_query(update["callback_query"])
            return

        # 2. Handle Text Commands (Messages)
        msg = update.get("message")
        if not msg: return
        
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()
        
        if chat_id != SETTINGS.telegram_chat_id: return
        if not text.startswith("/"): return
        
        cmd_parts = text.split()
        cmd = cmd_parts[0].lower().split("@")[0]
        args = cmd_parts[1:]

        if cmd == "/start" or cmd == "/menu":
            _send(chat_id, "🦚 <b>Krishna Verse AI Control Center</b>\n\nSelect a category to manage your digital ashram:", reply_markup=MAIN_MENU_BUTTONS)
            
        elif cmd == "/status":
            send_status_update(chat_id)
            
        elif cmd == "/festivals":
            send_festivals_update(chat_id)

        # ── [PRESERVED] All your existing commands ──────────────
        elif cmd == "/pause":
            db.set_state("bot_paused", "true")
            _send(chat_id, "⏸ <b>Bot operations paused.</b>\nNo replies will be sent.")
        elif cmd == "/resume":
            db.set_state("bot_paused", "false")
            db.set_state("safe_mode", "false")
            db.set_state("consecutive_429s", "0")
            db.set_state("circuit_breaker_until", "0")
            _send(chat_id, "🟢 <b>Bot operations resumed.</b>\nSafe mode and circuit breaker reset.")
        elif cmd == "/panic":
            db.set_state("safe_mode", "true")
            db.set_state("gemini_enabled", "false")
            _send(chat_id, "🚨 <b>PANIC MODE ENABLED!</b>\nGemini disabled. Bot switched to Safe Mode.")
        elif cmd == "/gemini_on":
            db.set_state("gemini_enabled", "true")
            _send(chat_id, "✨ Gemini AI enabled.")
        elif cmd == "/gemini_off":
            db.set_state("gemini_enabled", "false")
            _send(chat_id, "⚪ Gemini AI disabled.")
        elif cmd == "/addkeyword":
            if not args or "|" not in " ".join(args):
                _send(chat_id, "Usage: <code>/addkeyword keyword | reply</code>")
                return
            k, r = " ".join(args).split("|", 1)
            db.add_keyword(k, r)
            _send(chat_id, f"✅ Added keyword: <b>{k.strip()}</b>")
        elif cmd == "/removekeyword":
            if not args: return
            k = " ".join(args)
            _send(chat_id, f"✅ Removed: <b>{k}</b>" if db.remove_keyword(k) else "❌ Not found.")
        elif cmd == "/keywords":
            kw = db.list_keywords()
            if not kw: _send(chat_id, "No keywords set.")
            else: _send(chat_id, "🔑 <b>Active Keywords:</b>\n" + "\n".join([f"• <b>{r['keyword']}</b>: {r['reply'][:30]}..." for r in kw]))
        elif cmd == "/caption":
            if not args: return
            _send(chat_id, "⏳ Generating caption...")
            cap = ai.generate_caption(" ".join(args))
            _send(chat_id, cap or "❌ Failed.")
        elif cmd == "/setsleep":
            if len(args) != 2: return
            try:
                s, e = int(args[0]), int(args[1])
                db.set_state("sleep_start", str(s))
                db.set_state("sleep_end", str(e))
                _send(chat_id, f"✅ Sleep hours set: {s}:00 to {e}:00 IST")
            except ValueError: _send(chat_id, "❌ Invalid hours.")
        elif cmd == "/logs":
            logs = db.get_recent_activity()
            if not logs: _send(chat_id, "No recent activity.")
            else: _send(chat_id, "📋 <b>Recent Activity:</b>\n" + "\n".join([f"• {l['action']} at {l['created_at'].strftime('%H:%M:%S')}" for l in logs]))
        elif cmd == "/ping":
            _send(chat_id, "🏓 <b>Pong</b>\nBot: Running\nDatabase: Connected\nTelegram: OK")
        elif cmd == "/help":
            _send(chat_id, "🦚 <b>Commands:</b>\n/menu - Open Dashboard\n/status - Quotas & Health\n/festivals - Upcoming events\n/pause, /resume, /panic\n/gemini_on, /gemini_off\n/addkeyword, /removekeyword\n/caption, /setsleep, /logs")
        elif cmd == "/c2dm":
            # Added back C2DM command as it was present in previous version
            from database import is_c2dm_enabled, clear_telegram_state
            from telegram_bot import show_c2dm_main_menu
            show_c2dm_main_menu(chat_id)
            
    except Exception:
        logger.error(f"Telegram command failed:\n{traceback.format_exc()}")

# ── UI Component Functions ────────────────────────────────
def send_status_update(chat_id: str, msg_id: int = None):
    from gemini_client import MODEL_CONFIGS, _get_model_rpd_today, _clients
    stats = db.get_stats()
    gemini_count = db.get_gemini_count_today()
    
    state_str = "🟢 RUNNING"
    if stats['bot_paused']: state_str = "⏸ PAUSED"
    elif stats['safe_mode']: state_str = "🛡️ SAFE MODE"

    text = f"📊 <b>Bot Status:</b> {state_str}\n"
    text += f"🤖 <b>Gemini:</b> {'🟢 ON' if stats['gemini_enabled'] else '⚪ OFF'}\n"
    text += f"🚨 <b>Circuit Breaker:</b> {'ACTIVE' if stats['circuit_breaker_active'] else '🟢 OK'}\n"
    text += f"📈 <b>Total Calls Today:</b> {gemini_count}\n\n"
    
    text += "<b>🔋 Gemini Quotas (Today)</b>\n"
    total_pool = len(_clients) if _clients else 1
    for m in MODEL_CONFIGS:
        used = _get_model_rpd_today(m["id"])
        limit = m["rpd"] * total_pool
        bar = _make_progress_bar(used, limit)
        text += f"<code>{bar}</code> {m['label']}\n"
        text += f"     {used} / {limit} requests\n\n"
        
    text += f"💌 <b>Replies (24h):</b> {stats['last_24h_replies']}"
    
    back_btn = {"inline_keyboard": [[{"text": "🔙 Back to Menu", "callback_data": "menu_main"}]]}
    
    if msg_id:
        _edit_message(chat_id, msg_id, text, reply_markup=back_btn)
    else:
        _send(chat_id, text, reply_markup=back_btn)

def send_festivals_update(chat_id: str, msg_id: int = None):
    upcoming = get_upcoming_festivals(days_ahead=30)
    text = "🎉 <b>Upcoming Festivals (Next 30 Days)</b>\n\n"
    
    if not upcoming:
        text += "<i>No major festivals in the next 30 days.</i>\n"
    else:
        for fest in upcoming:
            text += f"🌸 <b>{fest['name']}</b>\n"
            text += f"   📅 {fest['date_obj'].strftime('%d %b')} (<i>In {fest['days_until']} days</i>)\n"
            text += f"   💡 <i>Ideas: {', '.join(fest['ideas'][:2])}</i>\n\n"
            
    text += "<i>Tip: Use <code>/caption Janmashtami</code> to generate posts!</i>"
    
    back_btn = {"inline_keyboard": [[{"text": "🔙 Back to Menu", "callback_data": "menu_main"}]]}
    
    if msg_id:
        _edit_message(chat_id, msg_id, text, reply_markup=back_btn)
    else:
        _send(chat_id, text, reply_markup=back_btn)

# ── Button Click Handler ──────────────────────────────────
def handle_callback_query(query: dict):
    chat_id = str(query["message"]["chat"]["id"])
    msg_id = query["message"]["message_id"]
    data = query["data"]
    query_id = query["id"]

    if chat_id != SETTINGS.telegram_chat_id:
        _answer_callback(query_id, "Unauthorized")
        return

    _answer_callback(query_id, "Loading...")

    if data == "menu_main" or data == "menu_start":
        _edit_message(chat_id, msg_id, "🦚 <b>Krishna Verse AI Control Center</b>\n\nSelect a category:", reply_markup=MAIN_MENU_BUTTONS)
    elif data == "menu_status":
        send_status_update(chat_id, msg_id)
    elif data == "menu_festivals":
        send_festivals_update(chat_id, msg_id)
    elif data == "menu_controls":
        controls = {"inline_keyboard": [
            [{"text": "⏸ Pause Bot", "callback_data": "ctrl_pause"}, {"text": "▶️ Resume Bot", "callback_data": "ctrl_resume"}],
            [{"text": "🚨 Panic Mode", "callback_data": "ctrl_panic"}],
            [{"text": "🔙 Back", "callback_data": "menu_main"}]
        ]}
        _edit_message(chat_id, msg_id, "⚙️ <b>System Controls</b>", reply_markup=controls)
    elif data == "menu_ai":
        ai_btns = {"inline_keyboard": [
            [{"text": "✨ AI On", "callback_data": "ctrl_ai_on"}, {"text": "⚪ AI Off", "callback_data": "ctrl_ai_off"}],
            [{"text": "🔙 Back", "callback_data": "menu_main"}]
        ]}
        _edit_message(chat_id, msg_id, "🤖 <b>AI & Keywords</b>\n<i>Use /addkeyword in chat to manage triggers.</i>", reply_markup=ai_btns)
        
    # Control Actions
    elif data == "ctrl_pause":
        db.set_state("bot_paused", "true")
        _answer_callback(query_id, "Bot Paused!")
        send_status_update(chat_id, msg_id)
    elif data == "ctrl_resume":
        db.set_state("bot_paused", "false")
        db.set_state("safe_mode", "false")
        _answer_callback(query_id, "Bot Resumed!")
        send_status_update(chat_id, msg_id)
    elif data == "ctrl_panic":
        db.set_state("safe_mode", "true")
        db.set_state("gemini_enabled", "false")
        _answer_callback(query_id, "Panic Mode ON!")
        send_status_update(chat_id, msg_id)
    elif data == "ctrl_ai_on":
        db.set_state("gemini_enabled", "true")
        _answer_callback(query_id, "AI Enabled!")
    elif data == "ctrl_ai_off":
        db.set_state("gemini_enabled", "false")
        _answer_callback(query_id, "AI Disabled!")
    elif data == "c2dm_menu": show_c2dm_main_menu(chat_id, msg_id)
    elif data == "c2dm_add":
        db.set_telegram_state(chat_id, {"action": "c2dm_setup", "step": 1})
        _send(chat_id, "➕ <b>Step 1/3:</b>\n\nSend the <b>Trigger Keyword</b> (e.g., 'radhe', '🦚').\n\n<i>Type /cancel to abort.</i>")
    elif data == "c2dm_list": show_c2dm_list(chat_id, msg_id)
    elif data == "c2dm_toggle":
        db.toggle_c2dm()
        status = "🟢 ENABLED" if db.is_c2dm_enabled() else "🔴 DISABLED"
        _answer_callback(query_id, f"Feature is now {status}")
        show_c2dm_main_menu(chat_id, msg_id)
    elif data.startswith("c2dm_del_"):
        trigger_id = int(data.split("_")[2])
        db.delete_c2dm_trigger(trigger_id)
        _answer_callback(query_id, "Deleted!")
        show_c2dm_list(chat_id, msg_id)

# ── Automated Daily Festival Reminder Check ───────────────
def check_and_send_festival_reminders():
    """Runs silently in the background. Sends alerts for festivals <= 4 days away."""
    try:
        ist_today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
        last_check = db.get_state("last_festival_check")
        
        if last_check == str(ist_today):
            return # Already checked today
            
        db.set_state("last_festival_check", str(ist_today))
        
        upcoming = get_upcoming_festivals(days_ahead=7)
        for fest in upcoming:
            if 0 <= fest["days_until"] <= 4:
                reminder_key = f"fest_reminder_{fest['name']}_{fest['date']}"
                if db.get_state(reminder_key) != "sent":
                    msg = f"🎉 <b>Festival Alert: {fest['name']}</b>\n"
                    msg += f"📅 In <b>{fest['days_until']} days</b> ({fest['date_obj'].strftime('%d %b')})\n\n"
                    msg += "<b>💡 Content Ideas:</b>\n"
                    for idea in fest["ideas"]:
                        msg += f"• {idea}\n"
                    msg += f"\n<i>Type /caption {fest['name']} to generate a post!</i>"
                    _send(SETTINGS.telegram_chat_id, msg)
                    db.set_state(reminder_key, "sent")
    except Exception as e:
        logger.error(f"Festival check failed: {e}")

def register_telegram_webhook():
    webhook_url = f"{SETTINGS.public_base_url}/telegram-webhook"
    try:
        resp = requests.post(f"{BASE_URL}/setWebhook", json={"url": webhook_url}, timeout=10)
        if resp.ok and resp.json().get("ok"): logger.info("✅ Telegram webhook registered")
    except Exception as e: logger.error(f"❌ Telegram webhook error: {e}")

def get_webhook_info() -> dict:
    try:
        resp = requests.get(f"{BASE_URL}/getWebhookInfo", timeout=10)
        return resp.json() if resp.ok else {}
    except Exception: return {}

# ── C2DM UI FUNCTIONS ─────────────────────────────────────
def show_c2dm_main_menu(chat_id: str, msg_id: int = None):
    db.clear_telegram_state(chat_id)
    status = "🟢 Active" if db.is_c2dm_enabled() else "🔴 Paused"
    text = (
        "🌸 <b>Comment-to-DM Automation</b> 🦚\n\n"
        "Turn comments into private blessings.\n\n"
        f"<b>System Status:</b> {status}"
    )
    buttons = [
        [{"text": "➕ Add New Trigger", "callback_data": "c2dm_add"}],
        [{"text": "📋 View / Delete Triggers", "callback_data": "c2dm_list"}],
        [{"text": f"⚙️ Toggle Feature ({status})", "callback_data": "c2dm_toggle"}],
        [{"text": "🔙 Back to Menu", "callback_data": "menu_main"}]
    ]
    if msg_id: _edit_message(chat_id, msg_id, text, reply_markup={"inline_keyboard": buttons})
    else: _send(chat_id, text, reply_markup={"inline_keyboard": buttons})

def show_c2dm_list(chat_id: str, msg_id: int):
    triggers = db.get_c2dm_triggers()
    if not triggers:
        text = "📋 <b>Active Triggers</b>\n\n<i>No triggers set up yet.</i>"
        buttons = [[{"text": "🔙 Back", "callback_data": "c2dm_menu"}]]
        _edit_message(chat_id, msg_id, text, reply_markup={"inline_keyboard": buttons})
        return

    text = "📋 <b>Active Triggers</b>\n\n<i>Click a trigger to delete it.</i>\n"
    buttons = []
    for t in triggers:
        kw = t['keyword']
        pub = t['public_reply'][:20]
        text += f"\n🔑 <b>{kw}</b>\n  ↳ Public: <i>{pub}...</i>\n"
        buttons.append([{"text": f"❌ Delete '{kw}'", "callback_data": f"c2dm_del_{t['id']}"}])

    buttons.append([{"text": "🔙 Back", "callback_data": "c2dm_menu"}])
    _edit_message(chat_id, msg_id, text, reply_markup={"inline_keyboard": buttons})

def handle_c2dm_text_input(chat_id: str, text: str, state: dict):
    if text.lower() == "/cancel":
        db.clear_telegram_state(chat_id)
        _send(chat_id, "❌ Setup cancelled.")
        show_c2dm_main_menu(chat_id)
        return

    step = state.get("step", 1)
    if step == 1:
        state["keyword"] = text.strip()
        state["step"] = 2
        db.set_telegram_state(chat_id, state)
        _send(chat_id, f"✅ Trigger set to: <b>{text.strip()}</b>\n\n➡️ <b>Step 2/3:</b>\nSend the <b>PUBLIC Reply</b> (what everyone sees under the comment).")
    elif step == 2:
        state["public_reply"] = text.strip()
        state["step"] = 3
        db.set_telegram_state(chat_id, state)
        _send(chat_id, "✅ Public reply saved.\n\n➡️ <b>Step 3/3:</b>\nSend the <b>PRIVATE DM Message</b> (what goes to their inbox).\n\n<i>Tip: You can use emojis and line breaks!</i>")
    elif step == 3:
        state["dm_message"] = text.strip()
        db.add_c2dm_trigger(state["keyword"], state["public_reply"], state["dm_message"])
        db.clear_telegram_state(chat_id)
        _send(chat_id, f"🎉 <b>Success!</b>\n\nTrigger <b>{state['keyword']}</b> is now live.\nWhen someone comments it, they will get your DM!")
        show_c2dm_main_menu(chat_id)
