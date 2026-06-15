"""
Zero — Discord bot entry point.

Start order:
  1. Flask keep-alive server (background thread, port 8080)
  2. Discord bot (blocking, main thread)
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands

from keep_alive import keep_alive
from revolver import Revolver

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("zero")

# ---------------------------------------------------------------------------
# Prefix — case-insensitive "zero " (any capitalisation + trailing space)
# ---------------------------------------------------------------------------

def get_prefix(bot: commands.Bot, message: discord.Message) -> list[str]:
    """Accept any capitalisation of 'zero ' as a valid prefix."""
    if message.content[:5].lower() == "zero ":
        return [message.content[:5]]
    return []


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=get_prefix,
    intents=intents,
    help_command=None,  # we provide our own
    case_insensitive=True,
)

# Attach a shared Revolver instance so cogs can access it via bot.revolver
bot.revolver = Revolver()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready() -> None:
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
    else:
        logger.error("Unhandled command error: %s", error)
        await ctx.reply("Something went wrong. Please try again.")


# ---------------------------------------------------------------------------
# Cog loader
# ---------------------------------------------------------------------------

async def load_cogs() -> None:
    cogs = [
        "cogs.general",
    ]
    for cog in cogs:
        await bot.load_extension(cog)
        logger.info("Loaded cog: %s", cog)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise EnvironmentError(
            "DISCORD_TOKEN is not set. Add it as a secret in the Replit Secrets tab."
        )

    async with bot:
        await load_cogs()
        await bot.start(token)


if __name__ == "__main__":
    keep_alive()
    logger.info("Keep-alive server started on port 8080")
    asyncio.run(main())
