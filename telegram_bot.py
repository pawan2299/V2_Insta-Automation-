from __future__ import annotations
import logging
import requests
import traceback
from config import SETTINGS
import database as db

logger = logging.getLogger(__name__)

BASE_URL = f"https://api.telegram.org/bot{SETTINGS.telegram_bot_token}"


def register_telegram_webhook():
    """Register the bot's webhook with Telegram."""
    if not SETTINGS.telegram_bot_token:
        logger.warning("No Telegram bot token found, skipping webhook registration.")
        return

    webhook_url = f"{SETTINGS.public_base_url}/telegram-webhook"
    try:
        resp = requests.post(
            f"{BASE_URL}/setWebhook",
            json={"url": webhook_url},
            timeout=10
        )
        if resp.ok:
            logger.info(f"Telegram webhook registered: {webhook_url}")
        else:
            logger.error(f"Telegram webhook failed: {resp.text}")
    except Exception as e:
        logger.error(f"Telegram webhook error: {e}")


def get_webhook_info() -> dict:
    try:
        resp = requests.get(f"{BASE_URL}/getWebhookInfo", timeout=10)
        return resp.json()
    except Exception as e:
        logger.error(f"Failed to get webhook info: {e}")
        return {"ok": False}


def _send(chat_id: str, text: str):
    try:
        requests.post(
            f"{BASE_URL}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML"
            },
            timeout=10
        )
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")

# 🌟 ADD THESE 3 HELPER FUNCTIONS BELOW _send()
def _send_with_buttons(chat_id: str, text: str, buttons: list[list[dict]]):
    try:
        payload = {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons}
        }
        requests.post(f"{BASE_URL}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Telegram button send failed: {e}")

def _edit_message(chat_id: str, message_id: int, text: str, buttons: list[list[dict]]):
    try:
        payload = {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "reply_markup": {"inline_keyboard": buttons}
        }
        requests.post(f"{BASE_URL}/editMessageText", json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Telegram edit failed: {e}")

def _answer_callback(callback_query_id: str, text: str = ""):
    try:
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": callback_query_id, "text": text
        }, timeout=10)
    except: pass

def handle_update(update: dict):
    try:
        # 🌟 CRITICAL FIX: Handle Button Clicks FIRST
        if "callback_query" in update:
            handle_callback_query(update["callback_query"])
            return

        msg = update.get("message")
        if not msg: return
        
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()
        
        if chat_id != SETTINGS.telegram_chat_id: return

        # 🌟 Handle Text Input for C2DM Setup (State Machine)
        state = db.get_telegram_state(chat_id)
        if state and state.get("action") == "c2dm_setup" and not text.startswith("/"):
            handle_c2dm_text_input(chat_id, text, state)
            return

        if not text.startswith("/"): return
        
        db.clear_telegram_state(chat_id) 
        
        cmd_parts = text.split()
        cmd = cmd_parts[0].lower().split("@")[0]
        args = cmd_parts[1:]

        if cmd == "/start":
            _send(chat_id, "🦚 <b>Krishna Bot Active</b>\n\nUse /status to check health or /help for commands.")
        
        elif cmd == "/status":
            from database import get_stats
            from gemini_client import get_model_status
            stats = get_stats()
            status_text = (
                f"🦚 <b>Krishna Bot Status</b>\n\n"
                f"Env: <code>{SETTINGS.environment}</code>\n"
                f"Paused: <code>{stats['bot_paused']}</code>\n"
                f"Gemini: <code>{stats['gemini_enabled']}</code>\n"
                f"Safe Mode: <code>{stats['safe_mode']}</code>\n\n"
                f"Total Replies: {stats['total_comments_replied']}\n"
                f"Last 24h: {stats['last_24h_replies']}\n"
                f"{get_model_status()}"
            )
            _send(chat_id, status_text)

        elif cmd == "/pause":
            db.set_state("bot_paused", "true")
            _send(chat_id, "⏸ <b>Bot Paused</b>. Webhooks will be ignored.")

        elif cmd == "/resume":
            db.set_state("bot_paused", "false")
            _send(chat_id, "▶️ <b>Bot Resumed</b>. Listening for events.")

        elif cmd == "/panic":
            db.set_state("safe_mode", "true")
            db.set_state("bot_paused", "true")
            _send(chat_id, "🚨 <b>PANIC MODE</b>: Bot paused and Safe Mode enabled.")

        elif cmd == "/gemini_on":
            db.set_state("gemini_enabled", "true")
            _send(chat_id, "🤖 <b>Gemini Enabled</b>.")

        elif cmd == "/gemini_off":
            db.set_state("gemini_enabled", "false")
            _send(chat_id, "🚫 <b>Gemini Disabled</b>. Using hardcoded replies.")

        elif cmd == "/addkeyword":
            if len(args) < 2:
                _send(chat_id, "Usage: <code>/addkeyword [word] [reply]</code>")
            else:
                kw = args[0]
                reply = " ".join(args[1:])
                db.add_keyword(kw, reply)
                _send(chat_id, f"✅ Keyword added: <b>{kw}</b>")

        elif cmd == "/removekeyword":
            if not args:
                _send(chat_id, "Usage: <code>/removekeyword [word]</code>")
            else:
                kw = args[0]
                if db.remove_keyword(kw):
                    _send(chat_id, f"✅ Keyword removed: <b>{kw}</b>")
                else:
                    _send(chat_id, f"❌ Keyword not found: <b>{kw}</b>")

        elif cmd == "/keywords":
            kws = db.list_keywords()
            if not kws:
                _send(chat_id, "No custom keywords set.")
            else:
                lines = ["🔑 <b>Custom Keywords:</b>"]
                for k in kws:
                    lines.append(f"• <b>{k['keyword']}</b>: {k['reply'][:50]}")
                _send(chat_id, "\n".join(lines))

        elif cmd == "/caption":
            if not args:
                _send(chat_id, "Usage: <code>/caption [topic]</code>")
            else:
                from gemini_client import generate_caption
                topic = " ".join(args)
                _send(chat_id, "🪄 Generating caption...")
                cap = generate_caption(topic)
                if cap:
                    _send(chat_id, f"📝 <b>Caption:</b>\n\n{cap}")
                else:
                    _send(chat_id, "❌ Failed to generate caption.")

        elif cmd == "/setsleep":
            if len(args) != 2:
                _send(chat_id, "Usage: <code>/setsleep [start_hour] [end_hour]</code> (0-23)")
            else:
                db.set_state("sleep_start", args[0])
                db.set_state("sleep_end", args[1])
                _send(chat_id, f"✅ Sleep hours set to {args[0]}:00 - {args[1]}:00 IST")

        elif cmd == "/logs":
            activity = db.get_recent_activity(10)
            if not activity:
                _send(chat_id, "No recent activity.")
            else:
                lines = ["📜 <b>Recent Activity:</b>"]
                for row in activity:
                    lines.append(f"• {row['created_at'].strftime('%H:%M')} | {row['action']}")
                _send(chat_id, "\n".join(lines))

        elif cmd == "/ping":
            _send(chat_id, "🏓 Pong! Bot is active.")

        elif cmd == "/help":
            help_text = (
                "🦚 <b>Krishna Bot Help</b>\n\n"
                "/status - Check bot health\n"
                "/pause / /resume - Toggle bot\n"
                "/gemini_on / /gemini_off - Toggle AI\n"
                "/c2dm - Comment-to-DM Setup\n"
                "/addkeyword [kw] [reply] - Add keyword\n"
                "/removekeyword [kw] - Remove keyword\n"
                "/keywords - List keywords\n"
                "/caption [topic] - AI Caption\n"
                "/setsleep [h1] [h2] - Set silent hours\n"
                "/logs - Recent activity\n"
                "/panic - Emergency stop"
            )
            _send(chat_id, help_text)

        elif cmd == "/c2dm":
            show_c2dm_main_menu(chat_id)

        logger.info(f"Command processed: {cmd}")
    except Exception:
        logger.error(f"Telegram command failed:\n{traceback.format_exc()}")


# 🌟 PASTE THESE C2DM UI FUNCTIONS AT THE VERY BOTTOM OF THE FILE:
def handle_callback_query(query: dict):
    chat_id = str(query["message"]["chat"]["id"])
    msg_id = query["message"]["message_id"]
    data = query["data"]
    query_id = query["id"]

    if chat_id != SETTINGS.telegram_chat_id:
        _answer_callback(query_id, "Unauthorized")
        return

    _answer_callback(query_id, "Processing...")

    if data == "c2dm_menu": show_c2dm_main_menu(chat_id, msg_id)
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
    ]
    if msg_id: _edit_message(chat_id, msg_id, text, buttons)
    else: _send_with_buttons(chat_id, text, buttons)

def show_c2dm_list(chat_id: str, msg_id: int):
    triggers = db.get_c2dm_triggers()
    if not triggers:
        text = "📋 <b>Active Triggers</b>\n\n<i>No triggers set up yet.</i>"
        buttons = [[{"text": "🔙 Back", "callback_data": "c2dm_menu"}]]
        _edit_message(chat_id, msg_id, text, buttons)
        return

    text = "📋 <b>Active Triggers</b>\n\n<i>Click a trigger to delete it.</i>\n"
    buttons = []
    for t in triggers:
        kw = t['keyword']
        pub = t['public_reply'][:20]
        text += f"\n🔑 <b>{kw}</b>\n  ↳ Public: <i>{pub}...</i>\n"
        buttons.append([{"text": f"❌ Delete '{kw}'", "callback_data": f"c2dm_del_{t['id']}"}])

    buttons.append([{"text": "🔙 Back", "callback_data": "c2dm_menu"}])
    _edit_message(chat_id, msg_id, text, buttons)

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
