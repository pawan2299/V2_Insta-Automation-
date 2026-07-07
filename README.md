# 🦚 Krishna Verse AI | Human-Tone Instagram Automation Engine

> A context-aware, human-sounding community management system built for @krishna.verse.ai — designed to feel like a real team member is behind every reply, not a bot.

**Krishna Verse AI** manages thousands of Instagram comments and DMs while sounding genuinely human. It reads what people actually say and reacts to it specifically, matches their tone (casual, funny, formal, or professional), and only leans into devotional language when the moment genuinely calls for it — never as a forced template. Under the hood it runs on Google Gemini's multi-model routing and an enterprise-grade PostgreSQL architecture.

---

## 🌟 Core Capabilities

### 🧠 Human-Tone Reply Engine
* **Reacts to the specific comment/DM**, not generic templates — every reply is grounded in what the person actually said.
* **Tone-Matching:** casual comment → casual reply; polite or professional comment (business/collab enquiries) → polite, professional reply. No forced jokes on serious messages.
* **Natural Length Variation:** replies range from a 2-word reaction to a full sentence, just like real texting — never the same fixed shape every time.
* **Devotional language on merit only:** phrases like "Radhe Radhe" or "Hare Krishna" appear only when the person's own message is genuinely spiritual in tone — never injected by default.
* **Visual & Textual Context:** the AI reads post captions and analyzes images to generate replies specific to what's actually in the post.
* **Semantic Deduplication:** the system remembers its last 5 AI-generated replies and injects them into the prompt so it never repeats itself.
* **Diverse Fallback Pools:** when AI is unavailable (rate-limits, cooldowns), the bot falls back to a 20+ item pool of varied, human-sounding responses instead of a handful of repeating lines — including separate pools for emoji-only comments and Story-mention thank-yous.
* **Smart Routing:** short greetings and emojis get instant hardcoded replies, saving AI calls for complex, long-form comments.

### 🛡️ Reliability: No More Cut-Off Replies
* **Thinking-budget disabled** on all generation calls — earlier, "thinking" models were spending their output-token budget on invisible internal reasoning, causing replies to get cut off mid-sentence. This is now fixed at the root.
* **Token safety buffer** on every generation call, with an automatic trim-to-last-complete-sentence safety net in case a response is ever truncated for any other reason.

### 💌 Intelligent DM Manager (Human-in-the-Loop)
* **Sliding Window Memory:** remembers the last few messages of every conversation for natural, contextual replies.
* **Long-Term Memory Summarization:** longer conversations get automatically summarized so context isn't lost even after the raw messages age out.
* **Auto-Acknowledgment & Escalation:** business, payment, or sensitive queries are safely escalated to the human admin via Telegram, while the user gets a polite "forwarded to admin" reply so no one is left on read.
* **Honest AI Disclosure:** never claims divine authority, never pretends to be something it's not, and matches the user's exact language (Hindi, English, Hinglish).

### 🎛️ Telegram Admin Control Center
* **Full Remote Control:** pause, resume, or trigger "Panic Mode" instantly from your phone.
* **Comment-to-DM (C2DM) Automation:** set trigger keywords in comments — the bot replies publicly and sends a private DM automatically.
* **AI Reply Review:** rate recent AI replies as good/bad or regenerate them directly from Telegram to keep quality high.
* **Live Quota Monitoring:** track daily API usage across every Gemini model in real time.

### 🛡️ Enterprise-Grade Infrastructure
* **Multi-Key & Multi-Model Cascade:** automatically falls back across Gemini models (`3.5 Flash` → `3.1 Lite` → `2.5 Pro` → `2.5 Flash`) and rotates across multiple API keys to avoid rate limits.
* **Circuit Breakers:** automatically disables AI and switches to Safe Mode after consecutive API errors, protecting the Instagram account from failure loops.
* **SSRF Protection:** blocks private/internal IPs before ever fetching an image URL for AI analysis.
* **Advisory-Lock Event Claiming:** prevents duplicate replies even under concurrent webhook retries.

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
                         │ Thinking disabled for│
                         │ instant, complete     │
                         │ short-form replies    │
                         └──────────────────────┘
                                  │
                                  ▼
                         ┌──────────────────────┐
                         │  Telegram Bot API    │
                         │ (Admin Notifications)│
                         └──────────────────────┘
```

---

## 🗄️ Database Schema

A normalized relational schema for fast queries and zero memory leaks:

1. **`system_config`** — core toggles, sleep hours, circuit breaker state.
2. **`reply_logs`** — analytics, semantic deduplication history, event tracking.
3. **`conversation_memory`** — powers the DM "sliding window" context.
4. **`conversation_summaries`** — long-term memory for older DM history.
5. **`gemini_quotas`** — tracks daily requests per Gemini model.
6. **`custom_keywords`** & **`comment_to_dm`** — admin-defined triggers and automation rules.
7. **`dm_cooldowns`** / **`c2dm_cooldowns`** / **`human_handoff_cooldowns`** — prevent spam and manage escalations.
8. **`failed_webhooks`** — captures and allows retrying of any failed event processing.
9. **`reply_feedback`** — good/bad ratings on AI replies from the Telegram review flow.

---

## ⚙️ Environment Variables

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

# Google Gemini AI (supports multiple keys for pooling)
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

| Command | Description |
| :--- | :--- |
| `/menu` | Open the interactive Admin Dashboard. |
| `/status` | View live AI model quotas and system health. |
| `/pause` / `/resume` | Instantly halt or restart all bot operations. |
| `/panic` | **Emergency kill-switch.** Disables AI, forces Safe Mode. |
| `/review` | Review recent AI replies — rate good/bad or regenerate. |
| `/c2dm` | Manage Comment-to-DM automation triggers. |
| `/addkeyword` / `/removekeyword` / `/keywords` | Manage instant keyword-triggered replies. |
| `/caption [topic]` | Generate an aesthetic Instagram caption with hashtags. |
| `/setsleep` | Set silent/inactive hours for the bot. |
| `/logs` | View the latest bot activity and engagement history. |
| `/ping` | Quick health check on bot, database, and Telegram connectivity. |

---

## 🚀 Deployment Guide

Optimized for **Render** (Web Service) + **Neon.tech** (PostgreSQL).

1. **Database Setup:** create a free PostgreSQL database on Neon.tech and copy the connection string.
2. **Render Setup:** create a new Web Service, connect your GitHub repository, and add the environment variables listed above.
3. **Build & Start Commands:**
   * **Build:** `pip install -r requirements.txt`
   * **Start:** `gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --keep-alive 5`
4. **Webhooks:** point your Meta App's Instagram Webhooks to `https://your-app.onrender.com/webhook`.

---

<div align="center">
  <i>Built to feel human, for <b>@krishna.verse.ai</b></i>
</div>
