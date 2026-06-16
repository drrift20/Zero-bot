import discord
from discord.ext import commands


class General(commands.Cog):
    """General utility commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        """Check bot latency and responsiveness."""
        latency_ms = round(self.bot.latency * 1000)
        db_status = "🟢 Connected" if self.bot.db.ready else "🔴 Disconnected"
        await ctx.reply(f"Pong! Latency: **{latency_ms} ms** | Database: {db_status}")

    @commands.command(name="help")
    async def help_command(self, ctx: commands.Context) -> None:
        """Show available commands."""
        embed = discord.Embed(
            title="Zero — Command Reference",
            description="Use `zero <command>` to interact with me.",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="⚙️ Utility",
            value=(
                "`zero ping` — Latency check + database status.\n"
                "`zero help` — Show this message."
            ),
            inline=False,
        )

        embed.add_field(
            name="🏗️ Server Builder  *(owner or authorized)*",
            value=(
                "`zero create a server` — Guided server-setup wizard.\n"
                "  Zero asks for a theme and auto-generates all categories & channels."
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
                "`zero authorized` — List all authorized users & roles.\n"
                "`zero bots` — List all bots Zero has integrated."
            ),
            inline=False,
        )

        embed.set_footer(text="Powered by Zero × Revolver LLM · Persistence by MongoDB")
        await ctx.reply(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
