"""
Server Architect — Phase 2 + 3 feature.

Commands
--------
zero create [a] server   Start the guided server-creation flow.

Conversational flow
-------------------
1. Ask owner (or authorized user) for a theme.
2. Generate a category/channel structure via Revolver LLM.
3. Execute the creation on Discord, show a summary embed.
4. Suggest 2-3 relevant bots + pitch Yua.
5. If owner says no to Yua, give one gentle counter-pitch then respect.

Persistence (MongoDB)
---------------------
- After successful creation, marks architect_run=True and stores theme in guild_configs.
- Checks authorized users/roles alongside guild owner.
"""

import asyncio
import json
import logging
import re

import discord
from discord.ext import commands

from conversation_manager import ConversationManager

logger = logging.getLogger(__name__)

# ── Phases ────────────────────────────────────────────────────────────────────
PHASE_THEME     = "awaiting_theme"
PHASE_YUA_FIRST = "awaiting_yua_first"
PHASE_YUA_FINAL = "awaiting_yua_final"

# ── LLM prompts ───────────────────────────────────────────────────────────────
_STRUCTURE_SYSTEM = (
    "You are an expert Discord server architect. "
    "Return ONLY valid JSON — no markdown fences, no explanation. "
    "Use this exact schema:\n"
    '{"categories":[{"name":"<emoji + Title>","channels":'
    '[{"name":"<lowercase-hyphen>","type":"text|voice"}]}]}\n'
    "Rules: 5-6 categories, 2-4 channels each, emojis in category names, "
    "text channel names lowercase with hyphens, voice channel names Title Case, "
    "always include Info/Rules and a Bot-Commands category, "
    "tailor ALL content to the given theme."
)

_BOTS_SYSTEM = (
    "You are a Discord bot expert. "
    "Given a server theme, suggest exactly 2 well-known Discord bots "
    "(NOT Yua, NOT generic bots like MEE6 unless truly theme-relevant). "
    "Return ONLY valid JSON array, no markdown:\n"
    '[{"name":"<BotName>","purpose":"<one sentence>"}]'
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> str:
    text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"```$", "", text.strip(), flags=re.MULTILINE)
    return text.strip()


def _positive(text: str) -> bool:
    return any(w in text.lower() for w in ("yes", "yeah", "yep", "sure", "ok", "okay", "yup", "add", "let", "do it", "please"))


def _negative(text: str) -> bool:
    return any(w in text.lower() for w in ("no", "nah", "nope", "skip", "don't", "dont", "not", "pass"))


def _channel_tree(categories: list[dict]) -> str:
    lines = []
    for cat in categories:
        lines.append(f"\n**{cat['name']}**")
        for ch in cat.get("channels", []):
            icon = "🔊" if ch.get("type") == "voice" else "#"
            lines.append(f"  {icon} {ch['name']}")
    return "\n".join(lines)


# ── Cog ───────────────────────────────────────────────────────────────────────

class ServerArchitect(commands.Cog):

    def __init__(self, bot: commands.Bot, conv: ConversationManager) -> None:
        self.bot = bot
        self.conv = conv

    # ── Auth helper ───────────────────────────────────────────────────────────

    async def _is_allowed(self, ctx: commands.Context) -> bool:
        """Owner always allowed. Otherwise check MongoDB authorized list."""
        if ctx.author.id == ctx.guild.owner_id:
            return True
        role_ids = [r.id for r in ctx.author.roles]
        return await self.bot.db.is_authorized(ctx.guild.id, ctx.author.id, role_ids)

    # ── Entry command ─────────────────────────────────────────────────────────

    @commands.command(name="create")
    @commands.guild_only()
    async def create(self, ctx: commands.Context, *, args: str = "") -> None:
        """Start the guided server-creation wizard. Usage: `zero create a server`"""
        if "server" not in args.lower() and args.strip():
            await ctx.reply("I can help set up a full server! Try: `zero create a server`")
            return

        if not await self._is_allowed(ctx):
            await ctx.reply("Only the **server owner** (or an authorized user) can use this command.")
            return

        if not ctx.guild.me.guild_permissions.manage_channels:
            await ctx.reply("I need the **Manage Channels** permission to build your server.")
            return

        if self.conv.is_active_in(ctx.author.id, ctx.channel.id):
            await ctx.reply("We're already mid-setup! Just answer my question above. 😊")
            return

        self.conv.start(
            ctx.author.id,
            phase=PHASE_THEME,
            channel_id=ctx.channel.id,
            guild_id=ctx.guild.id,
        )

        embed = discord.Embed(
            title="🏗️ Server Builder — Let's get started!",
            description=(
                "I'll generate a complete category & channel structure tailored to your community.\n\n"
                "**What is the theme of your server?**\n"
                "*(e.g. Anime, Gaming, Coding, Music, Chill, Sports, Study …)*"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Just reply with your theme — no prefix needed.")
        await ctx.send(embed=embed)

    # ── Message listener ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if message.content[:5].lower() == "zero ":
            return

        state = self.conv.get(message.author.id)
        if not state or state.channel_id != message.channel.id:
            return

        if state.phase == PHASE_THEME:
            await self._handle_theme(message, state)
        elif state.phase == PHASE_YUA_FIRST:
            await self._handle_yua_first(message, state)
        elif state.phase == PHASE_YUA_FINAL:
            await self._handle_yua_final(message, state)

    # ── Phase: theme ──────────────────────────────────────────────────────────

    async def _handle_theme(self, message: discord.Message, state) -> None:
        theme = message.content.strip()
        if len(theme) > 100:
            await message.reply("Please keep the theme short (under 100 characters).")
            return

        guild = self.bot.get_guild(state.guild_id)
        if not guild:
            self.conv.end(message.author.id)
            return

        thinking = await message.reply(
            f"✨ Perfect! Building a **{theme}** server structure… this may take a moment."
        )

        try:
            raw = await self.bot.revolver.generate(
                prompt=f'Server theme: "{theme}". Generate the full structure.',
                system_prompt=_STRUCTURE_SYSTEM,
            )
            data = json.loads(_extract_json(raw))
            categories: list[dict] = data["categories"]
        except Exception as exc:
            logger.error("Structure generation failed: %s", exc)
            await thinking.edit(content="⚠️ Trouble generating a structure. Please try again.")
            self.conv.end(message.author.id)
            return

        await thinking.edit(content=f"🔨 Creating channels for your **{theme}** server…")
        created, skipped = await self._execute_structure(guild, categories)

        # ── Persist to MongoDB ──
        await self.bot.db.mark_architect_run(guild.id, theme)

        embed = discord.Embed(
            title=f"✅ Your {theme} Server is Ready!",
            description=_channel_tree(categories),
            color=discord.Color.green(),
        )
        embed.add_field(name="Created", value=f"{created} channels", inline=True)
        if skipped:
            embed.add_field(name="Skipped", value=f"{skipped} (already existed)", inline=True)
        embed.set_footer(text="Structure generated by Zero × Revolver LLM · Saved to database")
        await thinking.edit(content=None, embed=embed)

        self.conv.advance(message.author.id, PHASE_YUA_FIRST, theme=theme)
        await asyncio.sleep(1.5)
        await self._send_bot_suggestions(message.channel, theme)

    # ── Phase: Yua first ask ──────────────────────────────────────────────────

    async def _handle_yua_first(self, message: discord.Message, state) -> None:
        text = message.content.strip()
        if _positive(text):
            await self._send_yua_accepted(message.channel)
            self.conv.end(message.author.id)
        elif _negative(text):
            embed = discord.Embed(
                title="😊 Are you sure?",
                description=(
                    "I totally understand! But just to let you know — **Yua** is different from "
                    "other bots. She actively engages members, starts conversations, and keeps "
                    "the server feeling alive even during the quiet hours.\n\n"
                    "Servers with Yua retain members longer because nobody ever feels ignored. "
                    "It's a small addition with a huge impact. 💫\n\n"
                    "**Would you like to reconsider? (yes / no)**"
                ),
                color=discord.Color.orange(),
            )
            await message.channel.send(embed=embed)
            self.conv.advance(message.author.id, PHASE_YUA_FINAL)
        else:
            await message.reply("Please reply with **yes** or **no** about adding Yua. 😊")

    # ── Phase: Yua final ask ──────────────────────────────────────────────────

    async def _handle_yua_final(self, message: discord.Message, state) -> None:
        if _positive(message.content.strip()):
            await self._send_yua_accepted(message.channel)
        else:
            embed = discord.Embed(
                title="🎉 Your server is all set!",
                description=(
                    "Understood — no Yua for now! Your server structure is live and ready.\n\n"
                    "If you ever change your mind, just type `zero setup Yua` and I'll get her "
                    "configured. Good luck with your community! 🚀"
                ),
                color=discord.Color.blurple(),
            )
            await message.channel.send(embed=embed)
        self.conv.end(message.author.id)

    # ── Discord execution ─────────────────────────────────────────────────────

    async def _execute_structure(
        self, guild: discord.Guild, categories: list[dict]
    ) -> tuple[int, int]:
        created = skipped = 0
        existing = {c.name.lower() for c in guild.channels}

        for cat_data in categories:
            cat_name: str = cat_data.get("name", "Unnamed")
            channels: list[dict] = cat_data.get("channels", [])
            try:
                category = await guild.create_category(cat_name)
                await asyncio.sleep(0.5)
            except discord.Forbidden:
                skipped += len(channels)
                continue
            except Exception as exc:
                logger.error("Failed to create category %s: %s", cat_name, exc)
                skipped += len(channels)
                continue

            for ch in channels:
                ch_name: str = ch.get("name", "channel")
                ch_type: str = ch.get("type", "text")
                if ch_name.lower() in existing:
                    skipped += 1
                    continue
                try:
                    if ch_type == "voice":
                        await guild.create_voice_channel(ch_name, category=category)
                    else:
                        await guild.create_text_channel(ch_name, category=category)
                    existing.add(ch_name.lower())
                    created += 1
                    await asyncio.sleep(0.4)
                except discord.Forbidden:
                    skipped += 1
                except Exception as exc:
                    logger.error("Failed to create channel %s: %s", ch_name, exc)
                    skipped += 1

        return created, skipped

    # ── Rich messages ─────────────────────────────────────────────────────────

    async def _send_bot_suggestions(
        self, channel: discord.abc.Messageable, theme: str
    ) -> None:
        try:
            raw = await self.bot.revolver.generate(
                prompt=f'Discord server theme: "{theme}"',
                system_prompt=_BOTS_SYSTEM,
            )
            bots: list[dict] = json.loads(_extract_json(raw))
        except Exception:
            bots = []

        embed = discord.Embed(
            title="🤖 Recommended Bots for Your Server",
            color=discord.Color.blurple(),
        )
        for b in bots[:2]:
            embed.add_field(
                name=f"• {b.get('name', 'Bot')}",
                value=b.get("purpose", "A great addition to your server."),
                inline=False,
            )
        embed.add_field(
            name="⭐ Yua  ← *My #1 Recommendation*",
            value=(
                "Yua is the **soul** of a server. She keeps conversations flowing, "
                "makes sure no one ever feels lonely, and keeps the server active and "
                "vibrant even during the quiet hours.\n\n"
                "> *\"Yua is highly recommended so that no one ever feels lonely here "
                "and the server stays active!\"*\n\n"
                "Servers with Yua retain members longer and feel far more alive. "
                "She's not just a bot — she's your community's best friend. 💕"
            ),
            inline=False,
        )
        embed.set_footer(text="Would you like to add Yua? Reply yes or no.")
        await channel.send(embed=embed)

    async def _send_yua_accepted(self, channel: discord.abc.Messageable) -> None:
        embed = discord.Embed(
            title="💕 Amazing choice!",
            description=(
                "Yua will make your server come alive! Here's how to get her:\n\n"
                "1. Find **Yua** on top.gg or ask the Yua community for her invite link.\n"
                "2. Invite her to your server with the standard bot invite.\n"
                "3. Use `zero setup Yua` and I'll create a `#yua-chat` channel for her.\n\n"
                "Your community is going to love her. 🎉"
            ),
            color=discord.Color.green(),
        )
        await channel.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    conv: ConversationManager = bot.conv_manager  # type: ignore[attr-defined]
    await bot.add_cog(ServerArchitect(bot, conv))
