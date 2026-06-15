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
        await ctx.reply(f"Pong! Latency: **{latency_ms} ms**")

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
                "`zero ping` — Check responsiveness and latency.\n"
                "`zero help` — Show this message."
            ),
            inline=False,
        )

        embed.add_field(
            name="🏗️ Server Builder  *(owner only)*",
            value=(
                "`zero create a server` — Start the guided server-setup wizard.\n"
                "  Zero will ask for your theme and auto-generate all categories & channels."
            ),
            inline=False,
        )

        embed.add_field(
            name="🤖 Bot Integrator  *(Manage Channels required)*",
            value=(
                "`zero setup <bot>` — Create dedicated channels for a bot.\n"
                "`Zero, I added <bot>` — Zero detects it and provisions channels automatically."
            ),
            inline=False,
        )

        embed.set_footer(text="Powered by Zero × Revolver LLM")
        await ctx.reply(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
