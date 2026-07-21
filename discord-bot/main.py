"""
Zero — Discord bot entry point.

Start order:
  1. Flask web server (background Thread) — binds PORT immediately for Render
  2. Discord bot (main thread, bot.run) — keeps the process alive 24/7
  MongoDB and cogs are loaded inside setup_hook before the bot connects.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from threading import Thread

import discord
from discord.ext import commands
from flask import Flask

from conversation_manager import ConversationManager
from db import Database
from revolver import Revolver

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("zero")

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    return "Zero Bot is Running", 200

@app.route("/health")
def health():
    return {"status": "ok", "bot": "Zero"}, 200

def run_flask():
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

# ── Prefix ────────────────────────────────────────────────────────────────────

def get_prefix(bot: commands.Bot, message: discord.Message) -> list[str]:
    """Accept any capitalisation of 'zero ' as a valid prefix."""
    if message.content[:5].lower() == "zero ":
        return [message.content[:5]]
    return []


# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=get_prefix,
    intents=intents,
    help_command=None,
    case_insensitive=True,
)

# Shared singletons — accessible from all cogs via bot.*
bot.revolver = Revolver()                # type: ignore[attr-defined]
bot.conv_manager = ConversationManager() # type: ignore[attr-defined]
bot.db = Database()                      # type: ignore[attr-defined]
bot.start_time: datetime | None = None   # type: ignore[attr-defined]  set in on_ready


# ── setup_hook — runs before bot connects, inside the event loop ───────────────

async def _setup_hook() -> None:
    """Load cogs and connect to MongoDB before the bot goes online."""
    cogs = [
        "cogs.general",
        "cogs.server_architect",
        "cogs.bot_integrator",
        "cogs.admin",
    ]
    for cog in cogs:
        await bot.load_extension(cog)
        logger.info("Loaded cog: %s", cog)

    mongo_uri = os.environ.get("MONGO_URI")
    if mongo_uri:
        try:
            await bot.db.init(mongo_uri)
        except Exception as exc:
            logger.error("MongoDB connection failed: %s — running without persistence.", exc)
    else:
        logger.warning("MONGO_URI not set — running without persistence.")

bot.setup_hook = _setup_hook  # type: ignore[method-assign]


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready() -> None:
    bot.start_time = datetime.now(timezone.utc)  # type: ignore[attr-defined]
    logger.info("Zero is online as %s (ID: %s)", bot.user, bot.user.id)
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="zero help",
        )
    )


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        await ctx.reply("Unknown command. Try `zero help` for a list of commands.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"Missing argument: `{error.param.name}`. Check `zero help`.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.reply("You don't have permission to use that command.")
    else:
        logger.error("Unhandled command error: %s", error)
        await ctx.reply("Something went wrong. Please try again.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 1. Start Flask in a background thread so Render port binds instantly
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # 2. Run the Discord bot in the main thread to keep the application alive 24/7
    print("Starting Discord Bot...")
    bot.run(os.environ.get("DISCORD_TOKEN"))
