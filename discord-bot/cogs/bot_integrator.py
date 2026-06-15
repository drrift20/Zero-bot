"""
Bot Integrator — Phase 2 feature.

Listens for explicit bot-setup requests and automatically creates
dedicated channels tailored to the named bot.

Triggers
--------
Prefix commands:
  zero setup <bot_name>       — explicit setup wizard

Natural-language (on_message, no prefix required):
  "Zero, I added <bot>"
  "Zero I've added <bot>"
  "Zero added <bot>"

Detection works even if the user uses a comma after "Zero" or omits it.
"""

import asyncio
import json
import logging
import re

import discord
from discord.ext import commands

from conversation_manager import ConversationManager

logger = logging.getLogger(__name__)

_BOT_CHANNELS_SYSTEM = (
    "You are a Discord server admin assistant. "
    "Given a bot name or type, suggest 1-2 dedicated text channels to create for it. "
    "Return ONLY a valid JSON array, no markdown, no explanation:\n"
    '[{"name":"<lowercase-hyphen>","topic":"<short channel topic string>"}]'
)

# Patterns for natural-language detection (after stripping the "zero[,]" prefix)
_ADDED_PATTERN = re.compile(
    r"i(?:'ve|have)?\s+(?:just\s+)?added\s+(.+)",
    re.IGNORECASE,
)
_SETUP_PATTERN = re.compile(
    r"set\s*up\s+(?:a\s+|an\s+)?(?:the\s+)?(.+)",
    re.IGNORECASE,
)


def _extract_json(text: str) -> str:
    text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"```$", "", text.strip(), flags=re.MULTILINE)
    return text.strip()


def _strip_zero_prefix(content: str) -> str | None:
    """
    If message starts with 'zero' (optionally followed by comma/space),
    return the rest of the message. Otherwise return None.
    """
    m = re.match(r"^zero[,\s]+(.+)", content.strip(), re.IGNORECASE)
    return m.group(1).strip() if m else None


class BotIntegrator(commands.Cog):
    """Detects bot-setup requests and provisions dedicated channels."""

    def __init__(self, bot: commands.Bot, conv: ConversationManager) -> None:
        self.bot = bot
        self.conv = conv

    # ── Explicit command ───────────────────────────────────────────────────────

    @commands.command(name="setup")
    @commands.has_permissions(manage_channels=True)
    async def setup_bot(self, ctx: commands.Context, *, bot_name: str) -> None:
        """Create dedicated channels for a specific bot. Usage: `zero setup <bot_name>`"""
        await self._provision_bot(ctx.channel, ctx.guild, bot_name, ctx.author)

    @setup_bot.error
    async def setup_bot_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply("Please specify a bot name. Example: `zero setup MEE6`")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.reply("You need the **Manage Channels** permission to use this command.")
        else:
            logger.error("setup_bot error: %s", error)
            await ctx.reply("Something went wrong. Please try again.")

    # ── Natural-language listener ──────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        # Skip if another cog is handling an active conversation for this user
        if self.conv.is_active_in(message.author.id, message.channel.id):
            return

        # Only react if message begins with "zero[,] …"
        after = _strip_zero_prefix(message.content)
        if after is None:
            return

        # Avoid double-processing actual prefix commands (handled by command system)
        # "zero " prefix → command system already handles it; our interest is "zero, …"
        if message.content[:5].lower() == "zero " and not re.match(
            r"^zero,", message.content, re.IGNORECASE
        ):
            return

        # Check for "I added <bot>"
        m = _ADDED_PATTERN.match(after)
        if m:
            bot_name = m.group(1).strip().rstrip(".")
            await self._confirm_and_provision(message, bot_name)
            return

        # Check for "set up <bot>"
        m = _SETUP_PATTERN.match(after)
        if m:
            bot_name = m.group(1).strip().rstrip(".")
            await self._confirm_and_provision(message, bot_name)
            return

    # ── Core logic ────────────────────────────────────────────────────────────

    async def _confirm_and_provision(
        self, message: discord.Message, bot_name: str
    ) -> None:
        """Confirm the bot detection with the user, then provision."""
        if not message.guild.me.guild_permissions.manage_channels:
            await message.reply(
                f"I noticed you added **{bot_name}**! "
                "Unfortunately I need the **Manage Channels** permission to set up its channels."
            )
            return

        await message.reply(
            f"Got it! You've added **{bot_name}**. Let me set up a dedicated space for it… 🛠️"
        )
        await self._provision_bot(message.channel, message.guild, bot_name, message.author)

    async def _provision_bot(
        self,
        channel: discord.abc.Messageable,
        guild: discord.Guild,
        bot_name: str,
        requester: discord.Member,
    ) -> None:
        """Ask LLM for channel suggestions, create them, and report back."""
        # ── Generate channel suggestions ──
        try:
            raw = await self.bot.revolver.generate(
                prompt=f'Bot name / type: "{bot_name}"',
                system_prompt=_BOT_CHANNELS_SYSTEM,
            )
            suggestions: list[dict] = json.loads(_extract_json(raw))
        except Exception as exc:
            logger.error("Bot channel generation failed for %s: %s", bot_name, exc)
            # Fallback to a generic channel
            suggestions = [
                {"name": f"{bot_name.lower().replace(' ', '-')}-commands",
                 "topic": f"Commands for {bot_name}"}
            ]

        # ── Find or create a suitable category ──
        category = discord.utils.get(guild.categories, name="🤖 Bots & Utilities")
        if not category:
            try:
                category = await guild.create_category("🤖 Bots & Utilities")
                await asyncio.sleep(0.5)
            except discord.Forbidden:
                category = None

        # ── Create each suggested channel ──
        created_channels: list[str] = []
        skipped_channels: list[str] = []
        existing_names = {c.name.lower() for c in guild.channels}

        for s in suggestions[:2]:
            ch_name: str = s.get("name", "bot-commands")
            ch_topic: str = s.get("topic", "")

            if ch_name.lower() in existing_names:
                skipped_channels.append(ch_name)
                continue

            try:
                new_ch = await guild.create_text_channel(
                    name=ch_name,
                    category=category,
                    topic=ch_topic,
                )
                created_channels.append(new_ch.mention)
                existing_names.add(ch_name.lower())
                await asyncio.sleep(0.4)
            except discord.Forbidden:
                skipped_channels.append(ch_name)
            except Exception as exc:
                logger.error("Failed to create channel %s: %s", ch_name, exc)
                skipped_channels.append(ch_name)

        # ── Summary embed ──
        embed = discord.Embed(
            title=f"✅ {bot_name} is all set!",
            color=discord.Color.green(),
        )

        if created_channels:
            embed.add_field(
                name="Created channels",
                value="\n".join(created_channels),
                inline=False,
            )
        if skipped_channels:
            embed.add_field(
                name="Already existed (skipped)",
                value=", ".join(f"`{n}`" for n in skipped_channels),
                inline=False,
            )

        embed.description = (
            f"I've prepared a dedicated space for **{bot_name}** in your server. "
            "Configure the bot there and you're good to go! 🎉"
        )
        embed.set_footer(text=f"Requested by {requester.display_name}")
        await channel.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    conv: ConversationManager = bot.conv_manager  # type: ignore[attr-defined]
    await bot.add_cog(BotIntegrator(bot, conv))
