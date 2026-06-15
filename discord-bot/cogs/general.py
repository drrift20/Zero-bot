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
        embed.add_field(name="`zero ping`", value="Check responsiveness and latency.", inline=False)
        embed.add_field(name="`zero help`", value="Show this message.", inline=False)
        embed.set_footer(text="More commands coming in future phases.")
        await ctx.reply(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
