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

def _make_progress_bar(used: int, limit: int, length: int = 10) -> str:
    if limit <= 0: return "░" * length
    pct = min(100, int((used / limit) * 100))
    filled = int(length * pct / 100)
    return "█" * filled + "░" * (length - filled)

def _send(chat_id: str, text: str, reply_markup: dict = None):
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup: payload["reply_markup"] = reply_markup
        requests.post(f"{BASE_URL}/sendMessage", json=payload, timeout=10)
    except Exception as e: logger.error(f"Telegram request failed: {e}")

def _edit_message(chat_id: str, message_id: int, text: str, reply_markup: dict = None):
    try:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if reply_markup: payload["reply_markup"] = reply_markup
        requests.post(f"{BASE_URL}/editMessageText", json=payload, timeout=10)
    except Exception: pass

def _answer_callback(callback_query_id: str, text: str = ""):
    try: requests.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": callback_query_id, "text": text}, timeout=10)
    except: pass

MAIN_MENU_BUTTONS = {"inline_keyboard": [
    [{"text": "📊 Status & Quotas", "callback_data": "menu_status"}],
    [{"text": "🎉 Festivals & Ideas", "callback_data": "menu_festivals"}],
    [{"text": "⚙️ System Controls", "callback_data": "menu_controls"}],
    [{"text": "🔑 Keywords & AI", "callback_data": "menu_ai"}]
]}

def handle_update(update: dict):
    try:
        if "callback_query" in update:
            handle_callback_query(update["callback_query"])
            return
        msg = update.get("message")
        if not msg: return
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if chat_id != SETTINGS.telegram_chat_id: return
        
        # 🌟 FIX 5: State Machine Collision Prevention
        state = db.get_telegram_state(chat_id)
        if state and state.get("action") == "c2dm_setup":
            if text.lower() == "/cancel":
                db.clear_telegram_state(chat_id)
                _send(chat_id, "❌ Setup cancelled.")
                show_c2dm_main_menu(chat_id)
                return
            elif not text.startswith("/"):
                handle_c2dm_text_input(chat_id, text, state)
                return
            else:
                # User typed a command (e.g. /status) during setup. Clear state and proceed to command.
                db.clear_telegram_state(chat_id)
                _send(chat_id, "⚠️ <i>Setup cancelled due to new command.</i>")

        if not text.startswith("/"): return
        
        cmd_parts = text.split()
        cmd = cmd_parts[0].lower().split("@")[0]
        args = cmd_parts[1:]

        if cmd == "/start" or cmd == "/menu":
            _send(chat_id, "🦚 <b>Krishna Verse AI Control Center</b>\nSelect a category:", reply_markup=MAIN_MENU_BUTTONS)
        elif cmd == "/status": send_status_update(chat_id)
        elif cmd == "/festivals": send_festivals_update(chat_id)
        elif cmd == "/pause":
            db.set_config("bot_paused", "true")
            _send(chat_id, "⏸ <b>Bot operations paused.</b>")
        elif cmd == "/resume":
            db.set_config("bot_paused", "false"); db.set_config("safe_mode", "false")
            db.set_config("consecutive_429s", "0"); db.set_config("circuit_breaker_until", "0")
            _send(chat_id, "🟢 <b>Bot operations resumed.</b>")
        elif cmd == "/panic":
            db.set_config("safe_mode", "true"); db.set_config("gemini_enabled", "false")
            _send(chat_id, "🚨 <b>PANIC MODE ENABLED!</b>")
        elif cmd == "/gemini_on": db.set_config("gemini_enabled", "true"); _send(chat_id, "✨ Gemini AI enabled.")
        elif cmd == "/gemini_off": db.set_config("gemini_enabled", "false"); _send(chat_id, "⚪ Gemini AI disabled.")
        elif cmd == "/addkeyword":
            if not args or "|" not in " ".join(args): _send(chat_id, "Usage: <code>/addkeyword keyword | reply</code>"); return
            k, r = " ".join(args).split("|", 1)
            db.add_keyword(k, r); _send(chat_id, f"✅ Added keyword: <b>{k.strip()}</b>")
        elif cmd == "/removekeyword":
            k = " ".join(args); _send(chat_id, f"✅ Removed: <b>{k}</b>" if db.remove_keyword(k) else "❌ Not found.")
        elif cmd == "/keywords":
            kw = db.list_keywords()
            _send(chat_id, "🔑 <b>Active Keywords:</b>\n" + "\n".join([f"• <b>{r['keyword']}</b>: {r['reply'][:30]}..." for r in kw]) if kw else "No keywords set.")
        elif cmd == "/caption":
            _send(chat_id, "⏳ Generating..."); cap = ai.generate_caption(" ".join(args)); _send(chat_id, cap or "❌ Failed.")
        elif cmd == "/setsleep":
            s, e = int(args[0]), int(args[1]); db.set_config("sleep_start", str(s)); db.set_config("sleep_end", str(e))
            _send(chat_id, f"✅ Sleep hours set: {s}:00 to {e}:00 IST")
        elif cmd == "/logs":
            logs = db.get_recent_activity()
            _send(chat_id, "📋 <b>Recent Activity:</b>\n" + "\n".join([f"• {l['action']} at {l['created_at'].strftime('%H:%M:%S')}" for l in logs]) if logs else "No activity.")
        elif cmd == "/ping": _send(chat_id, "🏓 <b>Pong</b>\nBot: Running\nDatabase: Connected\nTelegram: OK")
        
        # 🌟 ADDED: /help command for better UX
        elif cmd == "/help":
            _send(chat_id, (
                "🦚 <b>Krishna Verse AI Help</b>\n\n"
                "Use /menu for the interactive dashboard.\n\n"
                "<b>Quick Commands:</b>\n"
                "/status — Live stats & Quotas\n"
                "/pause — Stop bot\n"
                "/resume — Start bot\n"
                "/panic — Emergency stop\n"
                "/caption topic — Generate caption\n"
                "/festivals — Upcoming festivals\n"
                "/c2dm — Comment-to-DM setup\n"
                "/logs — Recent activity"
            ))
            
        elif cmd == "/c2dm": show_c2dm_main_menu(chat_id)
            
    except Exception: logger.error(f"Telegram command failed:\n{traceback.format_exc()}")

def send_status_update(chat_id: str, msg_id: int = None):
    from gemini_client import MODEL_CONFIGS, _clients
    stats = db.get_stats()
    gemini_count = db.get_total_gemini_today()
    state_str = "🟢 RUNNING"
    if stats['bot_paused']: state_str = "⏸ PAUSED"
    elif stats['safe_mode']: state_str = "🛡️ SAFE MODE"

    text = f"📊 <b>Bot Status:</b> {state_str}\n🤖 <b>Gemini:</b> {'🟢 ON' if stats['gemini_enabled'] else '⚪ OFF'}\n"
    text += f"🚨 <b>Circuit Breaker:</b> {'ACTIVE' if stats['circuit_breaker_active'] else '🟢 OK'}\n📈 <b>Total Calls Today:</b> {gemini_count}\n\n"
    text += "<b>🔋 Gemini Quotas (Today)</b>\n"
    total_pool = len(_clients) if _clients else 1
    for m in MODEL_CONFIGS:
        used = db.get_model_rpd(m["id"])
        limit = m["rpd"] * total_pool
        bar = _make_progress_bar(used, limit)
        text += f"<code>{bar}</code> {m['label']}\n     {used} / {limit} requests\n\n"
    text += f"💌 <b>Replies (24h):</b> {stats['last_24h_replies']}"
    
    back_btn = {"inline_keyboard": [[{"text": "🔙 Back to Menu", "callback_data": "menu_main"}]]}
    if msg_id: _edit_message(chat_id, msg_id, text, reply_markup=back_btn)
    else: _send(chat_id, text, reply_markup=back_btn)

def send_festivals_update(chat_id: str, msg_id: int = None):
    upcoming = get_upcoming_festivals(days_ahead=30)
    text = "🎉 <b>Upcoming Festivals (Next 30 Days)</b>\n"
    if not upcoming: text += "<i>No major festivals.</i>\n"
    else:
        for fest in upcoming:
            text += f"🌸 <b>{fest['name']}</b>\n   📅 {fest['date_obj'].strftime('%d %b')} (<i>In {fest['days_until']} days</i>)\n   💡 <i>Ideas: {', '.join(fest['ideas'][:2])}</i>\n\n"
    back_btn = {"inline_keyboard": [[{"text": "🔙 Back to Menu", "callback_data": "menu_main"}]]}
    if msg_id: _edit_message(chat_id, msg_id, text, reply_markup=back_btn)
    else: _send(chat_id, text, reply_markup=back_btn)

def handle_callback_query(query: dict):
    chat_id = str(query["message"]["chat"]["id"]); msg_id = query["message"]["message_id"]
    data = query["data"]; query_id = query["id"]
    if chat_id != SETTINGS.telegram_chat_id: _answer_callback(query_id, "Unauthorized"); return
    _answer_callback(query_id, "Loading...")
    if data == "menu_main": _edit_message(chat_id, msg_id, "🦚 <b>Krishna Verse AI</b>", reply_markup=MAIN_MENU_BUTTONS)
    elif data == "menu_status": send_status_update(chat_id, msg_id)
    elif data == "menu_festivals": send_festivals_update(chat_id, msg_id)
    elif data == "menu_controls":
        controls = {"inline_keyboard": [[{"text": "⏸ Pause", "callback_data": "ctrl_pause"}, {"text": "▶️ Resume", "callback_data": "ctrl_resume"}], [{"text": "🚨 Panic", "callback_data": "ctrl_panic"}], [{"text": "🔙 Back", "callback_data": "menu_main"}]]}
        _edit_message(chat_id, msg_id, "⚙️ <b>System Controls</b>", reply_markup=controls)
    elif data == "menu_ai":
        ai_btns = {"inline_keyboard": [[{"text": "✨ AI On", "callback_data": "ctrl_ai_on"}, {"text": "⚪ AI Off", "callback_data": "ctrl_ai_off"}], [{"text": "🔙 Back", "callback_data": "menu_main"}]]}
        _edit_message(chat_id, msg_id, "🤖 <b>AI & Keywords</b>", reply_markup=ai_btns)
    elif data == "ctrl_pause": db.set_config("bot_paused", "true"); send_status_update(chat_id, msg_id)
    elif data == "ctrl_resume": db.set_config("bot_paused", "false"); db.set_config("safe_mode", "false"); send_status_update(chat_id, msg_id)
    elif data == "ctrl_panic": db.set_config("safe_mode", "true"); db.set_config("gemini_enabled", "false"); send_status_update(chat_id, msg_id)
    elif data == "ctrl_ai_on": db.set_config("gemini_enabled", "true")
    elif data == "ctrl_ai_off": db.set_config("gemini_enabled", "false")
    elif data == "c2dm_menu": show_c2dm_main_menu(chat_id, msg_id)
    elif data == "c2dm_add":
        db.set_telegram_state(chat_id, {"action": "c2dm_setup", "step": 1})
        _send(chat_id, "➕ <b>Step 1/3:</b>\nSend the <b>Trigger Keyword</b>.\n\n<i>Type /cancel to abort.</i>")
    elif data == "c2dm_list": show_c2dm_list(chat_id, msg_id)
    elif data == "c2dm_toggle": db.toggle_c2dm(); show_c2dm_main_menu(chat_id, msg_id)
    elif data.startswith("c2dm_del_"): db.delete_c2dm_trigger(int(data.split("_")[2])); show_c2dm_list(chat_id, msg_id)

def handle_c2dm_text_input(chat_id: str, text: str, state: dict):
    step = state.get("step", 1)
    if step == 1:
        state["keyword"] = text.strip(); state["step"] = 2
        db.set_telegram_state(chat_id, state)
        _send(chat_id, f"✅ Trigger: <b>{text.strip()}</b>\n➡️ <b>Step 2/3:</b>\nSend the <b>PUBLIC Reply</b>.")
    elif step == 2:
        state["public_reply"] = text.strip(); state["step"] = 3
        db.set_telegram_state(chat_id, state)
        _send(chat_id, "✅ Public reply saved.\n➡️ <b>Step 3/3:</b>\nSend the <b>PRIVATE DM Message</b>.")
    elif step == 3:
        state["dm_message"] = text.strip()
        db.add_c2dm_trigger(state["keyword"], state["public_reply"], state["dm_message"])
        db.clear_telegram_state(chat_id)
        _send(chat_id, f"🎉 <b>Success!</b>\nTrigger <b>{state['keyword']}</b> is live.")
        show_c2dm_main_menu(chat_id)

def check_and_send_festival_reminders():
    try:
        ist_today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
        if db.get_config("last_festival_check") == str(ist_today): return
        db.set_config("last_festival_check", str(ist_today))
        for fest in get_upcoming_festivals(7):
            if 0 <= fest["days_until"] <= 4:
                key = f"fest_reminder_{fest['name']}_{fest['date']}"
                if db.get_config(key) != "sent":
                    _send(SETTINGS.telegram_chat_id, f"🎉 <b>Festival Alert: {fest['name']}</b>\n📅 In <b>{fest['days_until']} days</b>")
                    db.set_config(key, "sent")
    except Exception as e: logger.error(f"Festival check failed: {e}")

def check_and_send_token_expiry_alert():
    try:
        ist_today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
        if db.get_config("last_token_expiry_check") == str(ist_today): return
        db.set_config("last_token_expiry_check", str(ist_today))
        from instagram_api import get_token_expiry_days
        days_left = get_token_expiry_days("ig_user")
        if days_left is not None and days_left != -1 and days_left <= 7:
            _send(SETTINGS.telegram_chat_id, f"🚨 <b>Meta API Token Expiring in {days_left} days!</b>")
    except Exception as e: logger.error(f"Token check failed: {e}")

def register_telegram_webhook():
    try: requests.post(f"{BASE_URL}/setWebhook", json={"url": f"{SETTINGS.public_base_url}/telegram-webhook"}, timeout=10)
    except: pass

def get_webhook_info() -> dict:
    try: return requests.get(f"{BASE_URL}/getWebhookInfo", timeout=10).json()
    except: return {}

def show_c2dm_main_menu(chat_id: str, msg_id: int = None):
    db.clear_telegram_state(chat_id)
    status = "🟢 Active" if db.is_c2dm_enabled() else "🔴 Paused"
    btns = {"inline_keyboard": [[{"text": "➕ Add", "callback_data": "c2dm_add"}], [{"text": "📋 List", "callback_data": "c2dm_list"}], [{"text": f"⚙️ Toggle ({status})", "callback_data": "c2dm_toggle"}], [{"text": "🔙 Back", "callback_data": "menu_main"}]]}
    if msg_id: _edit_message(chat_id, msg_id, f"🌸 <b>C2DM Setup</b>\nStatus: {status}", reply_markup=btns)
    else: _send(chat_id, f"🌸 <b>C2DM Setup</b>\nStatus: {status}", reply_markup=btns)

def show_c2dm_list(chat_id: str, msg_id: int = None):
    triggers = db.get_c2dm_triggers()
    btns = [[{"text": f"🗑 {t['keyword']}", "callback_data": f"c2dm_del_{t['id']}"}] for t in triggers]
    btns.append([{"text": "🔙 Back", "callback_data": "c2dm_menu"}])
    text = "📋 <b>Triggers:</b>\n" + "\n".join([f"• {t['keyword']}" for t in triggers]) if triggers else "No triggers."
    if msg_id: _edit_message(chat_id, msg_id, text, reply_markup={"inline_keyboard": btns})
    else: _send(chat_id, text, reply_markup={"inline_keyboard": btns})