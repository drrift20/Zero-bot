"""
General cog — utility commands available to everyone.

Commands
--------
zero ping    — Latency check + DB status.
zero help    — Full command reference embed.
zero status  — Bot health, active AI provider, uptime, server count.
"""

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)


def _fmt_uptime(start: datetime | None) -> str:
    """Return a human-readable uptime string from bot.start_time."""
    if start is None:
        return "Unknown"
    delta = datetime.now(timezone.utc) - start
    days    = delta.days
    hours   = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    parts   = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts) or "< 1m"


class General(commands.Cog):
    """General utility commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── ping ──────────────────────────────────────────────────────────────────

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        """Quick latency + database health check."""
        latency_ms = round(self.bot.latency * 1000)
        db_status  = "🟢 Connected" if self.bot.db.ready else "🔴 Disconnected"
        await ctx.reply(f"Pong! Latency: **{latency_ms} ms** | Database: {db_status}")

    # ── status ────────────────────────────────────────────────────────────────

    @commands.command(name="status")
    async def status(self, ctx: commands.Context) -> None:
        """
        Show Zero's live health: AI provider, uptime, latency, server count.
        Usage: `zero status`
        """
        latency_ms = round(self.bot.latency * 1000)
        db_status  = "🟢 Online" if self.bot.db.ready else "🔴 Offline"
        uptime     = _fmt_uptime(getattr(self.bot, "start_time", None))
        provider   = getattr(self.bot.revolver, "last_used_provider", "Not yet used")
        guild_count = len(self.bot.guilds)

        embed = discord.Embed(
            title="⚙️ Zero — System Status",
            description=(
                "Everything you need to know about what's running under the hood."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="🤖 AI Engine",    value=f"Revolver · **{provider}**", inline=True)
        embed.add_field(name="📡 Latency",      value=f"**{latency_ms} ms**",       inline=True)
        embed.add_field(name="🗄️ Database",     value=db_status,                    inline=True)
        embed.add_field(name="⏱️ Uptime",       value=uptime,                       inline=True)
        embed.add_field(name="🌐 Servers",      value=f"**{guild_count}**",         inline=True)
        embed.add_field(name="🔗 Provider Chain", value="Gemini Key 1 → Gemini Key 2 → Groq", inline=False)
        embed.set_footer(text="Zero × Revolver LLM · Persistence by MongoDB Atlas")
        await ctx.reply(embed=embed)

    # ── help ──────────────────────────────────────────────────────────────────

    @commands.command(name="help")
    async def help_command(self, ctx: commands.Context) -> None:
        """Full command reference embed. Usage: `zero help`"""
        embed = discord.Embed(
            title="Zero — Command Reference",
            description="Use `zero <command>` to interact with me.",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="⚙️ Utility",
            value=(
                "`zero ping` — Latency + database check.\n"
                "`zero status` — AI provider, uptime, server count.\n"
                "`zero help` — Show this message."
            ),
            inline=False,
        )

        embed.add_field(
            name="🏗️ Server Builder  *(owner or authorized)*",
            value=(
                "`zero design <theme> [style]` — Generate and save a blueprint.\n"
                "  Styles: `Architect` (default) · `Minimal` · `Aesthetic` · `Pro`\n"
                "`zero preview` — Show the current saved blueprint.\n"
                "`zero modify <request>` — AI-edit the blueprint and show a diff.\n"
                "`zero build` — Apply the blueprint to the server.\n"
                "`zero create a server` — Legacy alias for `zero design`."
            ),
            inline=False,
        )

        embed.add_field(
            name="💣 Destructive  *(owner only)*",
            value=(
                "`zero wipe` — Wipe **all** channels, categories, and roles.\n"
                "  *(Auto-backup saved first. Separate from normal build flow.)*"
            ),
            inline=False,
        )

        embed.add_field(
            name="🗄️ Backup & Restore  *(owner or authorized / owner only)*",
            value=(
                "`zero backup [name]` — Snapshot the current server structure.\n"
                "`zero restore` — List backups and recreate one (non-destructive)."
            ),
            inline=False,
        )

        embed.add_field(
            name="🤖 Bot Integrator  *(Manage Channels required)*",
            value=(
                "`zero setup <bot>` — Create dedicated channels for a bot.\n"
                "`Zero, I added <bot>` — Auto-detected; channels created instantly."
            ),
            inline=False,
        )

        embed.add_field(
            name="🔑 Authorization  *(owner only)*",
            value=(
                "`zero authorize @user/role` — Grant Zero command access.\n"
                "`zero deauthorize @user/role` — Revoke access.\n"
                "`zero authorized` — List authorized users & roles.\n"
                "`zero bots` — List all bots Zero has integrated."
            ),
            inline=False,
        )

        embed.set_footer(text="Powered by Zero × Revolver LLM · Persistence by MongoDB")
        await ctx.reply(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
