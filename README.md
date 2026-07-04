# 🦚 Krishna Verse AI | Enterprise Instagram Automation Engine

> A highly intelligent, context-aware, and aesthetically driven community management system designed for spiritual content creators.

**Krishna Verse AI** is not just an auto-responder; it is a premium digital ashram manager. Powered by Google Gemini's multi-model routing and an enterprise-grade PostgreSQL architecture, it handles thousands of interactions while maintaining a warm, human-like, and deeply respectful brand voice.

---

## 🌟 Core Capabilities

### 🧠 Context-Aware AI Engagement
* **Visual & Textual Context:** The AI reads post captions and analyzes images to generate highly specific, relevant replies (e.g., recognizing a specific temple or deity).
* **Semantic Deduplication:** The system remembers its last 5 replies and injects them into the prompt, ensuring the AI never sounds repetitive or robotic.
* **Smart Routing:** Short greetings and emojis trigger instant, aesthetic hardcoded replies, saving expensive AI API calls for complex, long-form comments.

### 💌 Intelligent DM Manager (Human-in-the-Loop)
* **Sliding Window Memory:** The bot remembers the last 5 messages of every conversation, allowing it to maintain natural, human-like context across multiple exchanges.
* **Auto-Acknowledgment & Escalation:** If a user asks about collaborations, payments, or sensitive matters, the AI safely escalates it to the human admin via Telegram. Simultaneously, it sends a polite "Forwarded to Admin" auto-reply to the user on Instagram, ensuring no one is left on "read".
* **Strict Persona Guardrails:** Enforces honest AI disclosure, prevents claiming divine authority, and matches the user's exact language (Hindi, English, Hinglish).

### 🎛️ Telegram Admin Control Center
* **Full Remote Control:** Pause, resume, or trigger "Panic Mode" instantly from your phone.
* **Comment-to-DM (C2DM) Automation:** Set trigger keywords in comments. When a user comments the keyword, the bot replies publicly and automatically sends a private DM.
* **Live Quota Monitoring:** Track daily API usage across multiple Gemini models in real-time.

### 🛡️ Enterprise-Grade Reliability
* **Multi-Key & Multi-Model Cascade:** Automatically falls back from `Gemini 3.5 Flash` to `Lite` or `Pro` models, and rotates across multiple API keys to prevent rate limits (429 errors).
* **Circuit Breakers:** Automatically disables AI and switches to Safe Mode if consecutive API errors occur, protecting the Instagram account from infinite loops.
* **Global Rate Limiting:** Hardcoded safety caps to prevent webhook retry storms.

---

## 🏗️ System Architecture

```text
┌─────────────────┐      ┌──────────────────────┐      ┌───────────────────┐
│  Meta Webhooks  │─────▶│  Flask + Gunicorn    │─────▶│  Neon PostgreSQL  │
│ (IG Comments/DMs)│      │  (Render Web Service)│      │ (Normalized Schema)│
└─────────────────┘      └──────────────────────┘      └───────────────────┘
                                  │
                                  ▼
                         ┌──────────────────────┐
                         │  Google Gemini API   │
                         │ (Multi-Model Router) │
                         └──────────────────────┘
                                  │
                                  ▼
                         ┌──────────────────────┐
                         │  Telegram Bot API    │
                         │ (Admin Notifications)│
                         └──────────────────────┘
```

---

## 🗄️ Enterprise Database Schema

Unlike traditional bots that dump data into a single key-value table, Krishna Verse AI uses a **Normalized Relational Schema** for lightning-fast queries and zero memory leaks:

1. **`system_config`**: Core bot toggles, sleep hours, and circuit breaker states.
2. **`reply_logs`**: Analytics, semantic deduplication history, and event tracking.
3. **`conversation_memory`**: Powers the DM "Sliding Window" context (remembers user history).
4. **`gemini_quotas`**: Tracks daily Requests Per Day (RPD) per specific AI model.
5. **`custom_keywords`** & **`comment_to_dm`**: Admin-defined triggers and automation rules.
6. **`dm_cooldowns`**: Prevents spamming Welcome DMs to the same follower.

---

## ⚙️ Environment Variables

To deploy this project, configure the following variables in your hosting provider (e.g., Render):

```env
# Meta / Instagram API
ACCESS_TOKEN=your_long_lived_token
IG_USER_ACCESS_TOKEN=your_igaa_token
PAGE_ACCESS_TOKEN=your_page_token
PAGE_ID=your_page_id
OWN_ACCOUNT_ID=your_personal_ig_id
APP_SECRET=your_meta_app_secret
DM_ACCESS_TOKEN=your_dm_specific_token
VERIFY_TOKEN=your_custom_webhook_verify_string

# Google Gemini AI (Supports multiple keys for pooling)
GEMINI_API_KEY=key_1
GEMINI_API_KEY_2=key_2

# Database (Neon.tech / PostgreSQL)
DATABASE_URL=postgresql://user:pass@host/db?sslmode=require

# Telegram Admin Bot
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_personal_telegram_chat_id

# App Config
PUBLIC_BASE_URL=https://your-app.onrender.com
APP_ENV=production
```

---

## 📱 Telegram Admin Commands

Manage your digital ashram directly from Telegram:

| Command | Description |
| :--- | :--- |
| `/menu` | Open the interactive Admin Dashboard. |
| `/status` | View live AI model quotas and system health. |
| `/pause` / `/resume` | Instantly halt or restart all bot operations. |
| `/panic` | **Emergency Kill-Switch.** Disables AI, forces Safe Mode. |
| `/c2dm` | Manage Comment-to-DM automation triggers. |
| `/festivals` | View upcoming Hindu festivals and AI content ideas. |
| `/caption [topic]` | Generate an aesthetic Instagram caption with hashtags. |
| `/logs` | View the latest bot activity and engagement history. |

---

## 🚀 Deployment Guide

This project is optimized for **Render** (Web Service) + **Neon.tech** (PostgreSQL).

1. **Database Setup:** Create a free PostgreSQL database on Neon.tech and copy the connection string.
2. **Render Setup:** Create a new Web Service, connect your GitHub repository, and add the Environment Variables listed above.
3. **Build & Start Commands:**
   * **Build:** `pip install -r requirements.txt`
   * **Start:** `gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120 --keep-alive 5`
4. **Webhooks:** Point your Meta App's Instagram Webhooks to `https://your-app.onrender.com/webhook`.

---

<div align="center">
  <i>Designed with devotion for <b>@krishna.verse.ai</b></i><br>
  <b>ॐ नमः शान्ति 🌸</b>
</div>
