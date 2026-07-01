# Zero — Discord Bot

An AI-powered Discord bot that helps server owners build their server from scratch. Provide a theme and Zero generates a full category/channel structure using AI, creates the channels automatically, and recommends bots (including Yua) to keep the community alive.

## Run & Operate

- `cd discord-bot && python main.py` — run the bot
- The bot uses a Flask health-check server on port 5001 alongside the Discord bot process

## Stack

- Python 3.11
- discord.py — Discord bot framework
- Flask — lightweight health-check web server
- Google Gemini (`gemini-2.0-flash`) — primary LLM provider
- Groq (`llama-3.3-70b-versatile`) — fallback LLM provider
- MongoDB (Motor async driver) — persistence for guild configs and bot logs

## Where things live

- `discord-bot/main.py` — entry point: starts Flask health server + Discord bot
- `discord-bot/revolver.py` — cascading LLM failover (Gemini → Groq)
- `discord-bot/db.py` — async MongoDB abstraction layer
- `discord-bot/conversation_manager.py` — in-memory conversation state
- `discord-bot/cogs/` — bot commands (server_architect, bot_integrator, admin, general)

## Architecture decisions

- Revolver pattern: all LLM calls go through `Revolver.generate()` which cascades Gemini Key 1 → Gemini Key 2 → Groq on rate-limit errors.
- Flask runs in a daemon thread so port binds immediately; Discord bot runs in the main thread.
- MongoDB is optional — bot degrades gracefully if `MONGO_URI` is not set.
- TLS note: MongoDB Atlas must allow TLS 1.2 (set in Atlas Security → Advanced → Minimum TLS Version) due to Replit's OpenSSL 3.6.0.

## Required Secrets

- `DISCORD_TOKEN` — Discord bot token from the Discord Developer Portal
- `GEMINI_KEY_1` — Primary Google Gemini API key
- `GEMINI_KEY_2` — Secondary Google Gemini API key (failover)
- `GROQ_KEY` — Groq API key (final fallback)
- `MONGO_URI` — MongoDB Atlas connection string (optional, bot runs without it)

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._
