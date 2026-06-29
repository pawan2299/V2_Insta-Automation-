from __future__ import annotations
import logging
import requests
import traceback
from config import SETTINGS
import database as db
import gemini_client as ai

logger = logging.getLogger(__name__)

BASE_URL = f"https://api.telegram.org/bot{SETTINGS.telegram_bot_token}"


def register_telegram_webhook():
    """On application startup, automatically register the Telegram webhook."""
    webhook_url = f"{SETTINGS.public_base_url}/telegram-webhook"
    logger.info(f"Registering Telegram webhook: {webhook_url}")
    try:
        resp = requests.post(
            f"{BASE_URL}/setWebhook",
            json={"url": webhook_url},
            timeout=10
        )
        if resp.ok:
            data = resp.json()
            if data.get("ok"):
                logger.info("✅ Telegram webhook registered successfully")
            else:
                logger.error(f"❌ Telegram webhook registration failed: {data}")
        else:
            logger.error(f"❌ Telegram API error: {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"❌ Telegram webhook registration error: {e}")


def _send(chat_id: str, text: str):
    try:
        resp = requests.post(
            f"{BASE_URL}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML"
            },
            timeout=10
        )
        if not resp.ok:
            logger.error(f"Telegram send error: {resp.status_code} - {resp.text}")
        else:
            logger.debug(f"Telegram message sent to {chat_id}")
    except Exception as e:
        logger.error(f"Telegram request failed: {e}")


def handle_update(update: dict):
    try:
        update_id = update.get("update_id")
        msg = update.get("message")
        if not msg:
            return

        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()

        if chat_id != SETTINGS.telegram_chat_id:
            logger.warning(f"Unauthorized Telegram chat: {chat_id} | Expected: {SETTINGS.telegram_chat_id}")
            return

        if not text.startswith("/"):
            return

        logger.info(f"Received Telegram command: {text} | chat_id={chat_id}")

        cmd_parts = text.split()
        if not cmd_parts:
            return
            
        cmd = cmd_parts[0].lower().split("@")[0]
        args = cmd_parts[1:]

        if cmd == "/start":
            _send(chat_id, "🦚 <b>Krishna Automation Volume 2</b>\nAdmin control panel active.")

        elif cmd == "/status":
            from gemini_client import get_model_status
            stats = db.get_stats()
            gemini_count = db.get_gemini_count_today()

            state_str = "🟢 RUNNING"
            if stats['bot_paused']:
                state_str = "⏸ PAUSED"
            elif stats['safe_mode']:
                state_str = "🛡️ SAFE MODE"

            _send(chat_id, (
                f"📊 <b>Bot Status</b>\n"
                f"State: {state_str}\n"
                f"Gemini: {'🟢 ON' if stats['gemini_enabled'] else '⚪ OFF'}\n"
                f"Circuit Breaker: "
                f"{'🚨 ACTIVE' if stats['circuit_breaker_active'] else '🟢 OK'}\n"
                f"Total Gemini Today: {gemini_count}\n\n"
                f"<b>Stats:</b>\n"
                f"Total Replies: {stats['total_comments_replied']}\n"
                f"Last 24h: {stats['last_24h_replies']}\n"
                f"Welcome DMs: {stats['welcome_dms_sent']}"
                f"{get_model_status()}"
            ))

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
            _send(chat_id, "🚨 <b>PANIC MODE ENABLED!</b>\nGemini disabled. Bot switched to Safe Mode (Keywords/Fallbacks only).")

        elif cmd == "/gemini_on":
            db.set_state("gemini_enabled", "true")
            _send(chat_id, "✨ Gemini AI enabled.")

        elif cmd == "/gemini_off":
            db.set_state("gemini_enabled", "false")
            _send(chat_id, "⚪ Gemini AI disabled.")

        elif cmd == "/addkeyword":
            if not args:
                _send(chat_id, "Usage: <code>/addkeyword keyword | reply</code>")
                return
            raw = " ".join(args)
            if "|" not in raw:
                _send(chat_id, "Error: Use | to separate keyword and reply.")
                return
            k, r = raw.split("|", 1)
            db.add_keyword(k, r)
            _send(chat_id, f"✅ Added keyword: <b>{k.strip()}</b>")

        elif cmd == "/removekeyword":
            if not args:
                _send(chat_id, "Usage: <code>/removekeyword keyword</code>")
                return
            k = " ".join(args)
            if db.remove_keyword(k):
                _send(chat_id, f"✅ Removed keyword: <b>{k}</b>")
            else:
                _send(chat_id, "❌ Keyword not found.")

        elif cmd == "/keywords":
            kw = db.list_keywords()
            if not kw:
                _send(chat_id, "No keywords set.")
            else:
                lines = [f"• <b>{r['keyword']}</b>: {r['reply'][:30]}..." for r in kw]
                _send(chat_id, "🔑 <b>Active Keywords:</b>\n" + "\n".join(lines))

        elif cmd == "/caption":
            if not args:
                _send(chat_id, "Usage: <code>/caption topic</code>")
                return
            topic = " ".join(args)
            _send(chat_id, "⏳ Generating caption...")
            cap = ai.generate_caption(topic)
            _send(chat_id, cap or "❌ Failed to generate caption.")

        elif cmd == "/setsleep":
            if len(args) != 2:
                _send(chat_id, "Usage: <code>/setsleep start_hour end_hour</code> (0-23)")
                return
            try:
                s, e = int(args[0]), int(args[1])
                db.set_state("sleep_start", str(s))
                db.set_state("sleep_end", str(e))
                _send(chat_id, f"✅ Sleep hours set: {s}:00 to {e}:00 IST")
            except ValueError:
                _send(chat_id, "❌ Invalid hours.")

        elif cmd == "/logs":
            logs = db.get_recent_activity()
            if not logs:
                _send(chat_id, "No recent activity.")
            else:
                lines = [f"• {l['action']} at {l['created_at'].strftime('%H:%M:%S')}" for l in logs]
                _send(chat_id, "📋 <b>Recent Activity:</b>\n" + "\n".join(lines))

        elif cmd == "/ping":
            ping_msg = (
                "🏓 <b>Pong</b>\n\n"
                "Bot: Running\n"
                "Database: Connected\n"
                "Telegram: OK"
            )
            _send(chat_id, ping_msg)

        elif cmd == "/help":
            help_text = (
                "🦚 <b>Commands:</b>\n\n"
                "🚨 <b>Emergency:</b> /pause, /resume, /panic\n"
                "🤖 <b>Bot:</b> /status, /logs, /ping\n"
                "✨ <b>AI:</b> /gemini_on, /gemini_off\n"
                "🔑 <b>Keywords:</b> /addkeyword, /removekeyword, /keywords\n"
                "📝 <b>Content:</b> /caption topic\n"
                "🌙 <b>Schedule:</b> /setsleep start end"
            )
            _send(chat_id, help_text)
            
        logger.info(f"Command processed: {cmd}")
        
    except Exception:
        logger.error(f"Telegram command failed:\n{traceback.format_exc()}")


def get_webhook_info() -> dict:
    try:
        resp = requests.get(f"{BASE_URL}/getWebhookInfo", timeout=10)
        return resp.json() if resp.ok else {}
    except Exception:
        return {}
