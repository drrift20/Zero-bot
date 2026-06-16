"""
Admin cog — manage authorized users and roles who can run owner-level commands.

Commands (owner only)
---------------------
zero authorize @user          — Grant a user owner-level access to Zero commands.
zero authorize @role          — Grant a role owner-level access.
zero deauthorize @user        — Revoke a user's access.
zero deauthorize @role        — Revoke a role's access.
zero authorized               — List all authorized users and roles.
zero bots                     — List all bots Zero has integrated in this server.
"""

import logging

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)


class Admin(commands.Cog):
    """Server admin utilities — authorization management."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── authorize ─────────────────────────────────────────────────────────────

    @commands.command(name="authorize")
    @commands.guild_only()
    async def authorize(
        self,
        ctx: commands.Context,
        target: discord.Member | discord.Role,
    ) -> None:
        """Grant a user or role permission to use owner-level Zero commands."""
        if ctx.author.id != ctx.guild.owner_id:
            await ctx.reply("Only the **server owner** can authorize users or roles.")
            return

        db = self.bot.db
        if isinstance(target, discord.Member):
            await db.add_authorized_user(ctx.guild.id, target.id)
            await ctx.reply(f"✅ **{target.display_name}** is now authorized to use Zero commands.")
        else:
            await db.add_authorized_role(ctx.guild.id, target.id)
            await ctx.reply(f"✅ Members with the **{target.name}** role are now authorized.")

    @authorize.error
    async def authorize_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.BadUnionArgument):
            await ctx.reply("Please mention a valid **@user** or **@role**.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply("Usage: `zero authorize @user` or `zero authorize @role`")
        else:
            logger.error("authorize error: %s", error)

    # ── deauthorize ───────────────────────────────────────────────────────────

    @commands.command(name="deauthorize")
    @commands.guild_only()
    async def deauthorize(
        self,
        ctx: commands.Context,
        target: discord.Member | discord.Role,
    ) -> None:
        """Revoke a user's or role's Zero command access."""
        if ctx.author.id != ctx.guild.owner_id:
            await ctx.reply("Only the **server owner** can revoke authorization.")
            return

        db = self.bot.db
        if isinstance(target, discord.Member):
            await db.remove_authorized_user(ctx.guild.id, target.id)
            await ctx.reply(f"🚫 **{target.display_name}**'s authorization has been revoked.")
        else:
            await db.remove_authorized_role(ctx.guild.id, target.id)
            await ctx.reply(f"🚫 The **{target.name}** role's authorization has been revoked.")

    # ── authorized (list) ─────────────────────────────────────────────────────

    @commands.command(name="authorized")
    @commands.guild_only()
    async def authorized_list(self, ctx: commands.Context) -> None:
        """List all authorized users and roles in this server."""
        config = await self.bot.db.get_guild_config(ctx.guild.id)

        embed = discord.Embed(
            title="🔑 Authorized Users & Roles",
            color=discord.Color.blurple(),
        )

        user_ids: list[int] = config.get("authorized_users", []) if config else []
        role_ids: list[int] = config.get("authorized_roles", []) if config else []

        if user_ids:
            user_lines = []
            for uid in user_ids:
                member = ctx.guild.get_member(uid)
                user_lines.append(member.mention if member else f"`{uid}` *(left server)*")
            embed.add_field(name="Users", value="\n".join(user_lines), inline=False)

        if role_ids:
            role_lines = []
            for rid in role_ids:
                role = ctx.guild.get_role(rid)
                role_lines.append(role.mention if role else f"`{rid}` *(deleted role)*")
            embed.add_field(name="Roles", value="\n".join(role_lines), inline=False)

        if not user_ids and not role_ids:
            embed.description = "No users or roles have been authorized yet."

        embed.set_footer(text="Use `zero authorize @user/role` to add more.")
        await ctx.reply(embed=embed)

    # ── bots (list integrated bots) ───────────────────────────────────────────

    @commands.command(name="bots")
    @commands.guild_only()
    async def list_bots(self, ctx: commands.Context) -> None:
        """List all bots Zero has set up in this server."""
        records = await self.bot.db.get_custom_bots(ctx.guild.id)

        embed = discord.Embed(
            title="🤖 Integrated Bots",
            color=discord.Color.blurple(),
        )

        if not records:
            embed.description = (
                "No bots have been integrated yet.\n"
                "Use `zero setup <bot>` or tell me `Zero, I added <bot>` to get started."
            )
        else:
            for rec in records:
                channels = ", ".join(f"`#{c}`" for c in rec.get("channels_created", []))
                added_by = ctx.guild.get_member(rec.get("added_by", 0))
                value = f"Channels: {channels or 'none'}"
                if added_by:
                    value += f"\nAdded by: {added_by.mention}"
                embed.add_field(name=rec["bot_name"], value=value, inline=False)

        await ctx.reply(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
