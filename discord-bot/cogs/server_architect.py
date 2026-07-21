"""
Server Architect — AI-powered multi-stage server setup system.

Commands
--------
zero design [theme] [style]  — Generate and save a server blueprint (draft).
zero create a server         — Legacy alias; starts the design flow.
zero preview                 — Show the current saved blueprint.
zero modify <request>        — AI-edit the blueprint and display a diff.
zero build                   — Apply the blueprint to the server.
zero wipe                    — Destructive full wipe (owner only). Auto-backs up first.
zero backup [name]           — Manually snapshot the current server structure.
zero restore                 — List saved backups and restore one.

Workflow
--------
design → (preview / modify as many times as needed) → build

Partial Build Handling
----------------------
If `zero build` is interrupted (e.g. rate limit after 5 of 10 channels), the partial
IDs are saved to MongoDB and the blueprint stays intact.  On the next `zero build`,
Zero detects the partial state, offers to clean it up, and retries from scratch using
the saved blueprint.  The blueprint is ONLY cleared on FULL success.

Styles
------
Four Zero-branded styles alter how the AI generates the structure:
  Architect (default) — rich, full structure
  Minimal             — lean, essential channels only
  Aesthetic           — emoji-heavy, vibe-forward
  Pro                 — no emojis, enterprise naming

Persistence (MongoDB)
---------------------
guild_configs.current_blueprint  — draft blueprint (cleared after full build)
guild_configs.partial_build      — True if last build was incomplete
guild_configs.*_ids              — IDs of every Zero-created resource
server_backups                   — per-guild structure snapshots
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import discord
from discord.ext import commands

from conversation_manager import ConversationManager

logger = logging.getLogger(__name__)

# ── Conversation phases ────────────────────────────────────────────────────────
PHASE_DESIGN_THEME = "awaiting_design_theme"
PHASE_YUA_FIRST    = "awaiting_yua_first"
PHASE_YUA_FINAL    = "awaiting_yua_final"

# ── Styles ────────────────────────────────────────────────────────────────────
STYLE_ALIASES: dict[str, str] = {
    "architect": "Architect",
    "minimal":   "Minimal",
    "aesthetic": "Aesthetic",
    "pro":       "Pro",
}
STYLE_EMOJIS: dict[str, str] = {
    "Architect": "🏛️",
    "Minimal":   "⚡",
    "Aesthetic": "✨",
    "Pro":       "💼",
}
_STYLE_DESCRIPTIONS: dict[str, str] = {
    "Architect": "Rich & detailed — 5-6 categories, themed channels, full role set.",
    "Minimal":   "Lean & clean — 3-4 categories, essentials only, few roles.",
    "Aesthetic": "Vibe-forward — heavy emojis, expressive channel names, lifestyle feel.",
    "Pro":       "Professional — no emojis in names, enterprise-style structure.",
}
_STYLE_INSTRUCTIONS: dict[str, str] = {
    "Architect": (
        "Generate a rich structure: 5-6 categories, 3-4 channels each, emoji-prefixed "
        "category names, themed channel topics, 5-6 roles with theme-appropriate names."
    ),
    "Minimal": (
        "Keep it lean: 3-4 categories max, 2-3 essential channels each, minimal emojis, "
        "3-4 basic roles only (Admin, Moderator, Member, Bot)."
    ),
    "Aesthetic": (
        "Go vibe-forward: heavy emoji use in ALL names, expressive channel names "
        "(e.g. ✦ vibe-chat, ꒰🌸꒱ selfies), lifestyle formatting, 4-5 stylized roles "
        "with aesthetic names and pastel hex colors."
    ),
    "Pro": (
        "Use professional naming: NO emojis anywhere, enterprise-style channel names "
        "(e.g. team-updates, resource-library, announcements), clean role names "
        "(Administrator, Moderator, Member, Guest), business-appropriate structure."
    ),
}

# ── LLM prompts ───────────────────────────────────────────────────────────────

def _structure_system(style: str) -> str:
    """Build the structure-generation system prompt with the chosen style injected."""
    instruction = _STYLE_INSTRUCTIONS.get(style, _STYLE_INSTRUCTIONS["Architect"])
    return (
        "You are an expert Discord server architect. "
        "Return ONLY valid JSON — no markdown fences, no explanation. "
        "Use this exact schema:\n"
        '{"categories":[{"name":"<category name>","channels":[{"name":"<channel-name>","type":"text|voice"}]}],'
        '"roles":[{"name":"<Role Name>","color":"<6-digit hex no #>","hoist":true}]}\n'
        f"Style instruction: {instruction}\n"
        "Always include an Info/Rules category and a Bot-Commands category. "
        "Tailor ALL content to the given theme."
    )


_MODIFY_SYSTEM = (
    "You are an expert Discord server architect editing an existing server blueprint. "
    "You receive the CURRENT blueprint as JSON and a modification request. "
    "Return ONLY the COMPLETE updated blueprint as valid JSON — no markdown, no explanation. "
    "Apply the change precisely and minimally, preserving everything else. "
    'Schema: {"categories":[{"name":"...","channels":[{"name":"...","type":"text|voice"}]}],'
    '"roles":[{"name":"...","color":"...","hoist":true}]}'
)

_BOTS_SYSTEM = (
    "You are a Discord bot expert. "
    "Given a server theme, suggest exactly 2 well-known Discord bots "
    "(NOT Yua, NOT generic bots like MEE6 unless truly theme-relevant). "
    "Return ONLY a valid JSON array, no markdown:\n"
    '[{"name":"<BotName>","purpose":"<one sentence>"}]'
)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _extract_json(text: str) -> str:
    text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"```$", "", text.strip(), flags=re.MULTILINE)
    return text.strip()


def _positive(text: str) -> bool:
    return any(w in text.lower() for w in ("yes", "yeah", "yep", "sure", "ok", "okay", "yup", "add", "do it", "please"))


def _negative(text: str) -> bool:
    return any(w in text.lower() for w in ("no", "nah", "nope", "skip", "don't", "dont", "pass"))


def _parse_theme_style(args: str) -> tuple[str, str]:
    """
    Split e.g. 'Gaming Minimal' → ('Gaming', 'Minimal').
    If the last word is a known style alias it is extracted; otherwise the full
    string is treated as the theme and style defaults to 'Architect'.
    """
    parts = args.strip().split()
    if len(parts) >= 2 and parts[-1].lower() in STYLE_ALIASES:
        return " ".join(parts[:-1]), STYLE_ALIASES[parts[-1].lower()]
    return args.strip(), "Architect"


def _parse_color(hex_str: str) -> discord.Color:
    try:
        return discord.Color(int(hex_str.strip().lstrip("#"), 16))
    except Exception:
        return discord.Color.blurple()


def _channel_tree(categories: list[dict]) -> str:
    lines = []
    for cat in categories:
        lines.append(f"\n**{cat['name']}**")
        for ch in cat.get("channels", []):
            icon = "🔊" if ch.get("type") == "voice" else "#"
            lines.append(f"  {icon} {ch['name']}")
    return "\n".join(lines)


def _blueprint_summary_embed(theme: str, style: str, categories: list[dict], roles: list[dict]) -> discord.Embed:
    """Compact embed shown right after a design or modify operation."""
    total_channels = sum(len(c.get("channels", [])) for c in categories)
    emoji = STYLE_EMOJIS.get(style, "🏗️")
    embed = discord.Embed(
        title=f"📐 Blueprint Ready — **{theme}**",
        description=(
            f"Style: {emoji} **{style}** — {_STYLE_DESCRIPTIONS.get(style, '')}\n\n"
            f"**{len(categories)}** categories · **{total_channels}** channels · **{len(roles)}** roles\n\n"
            "Use `zero preview` for the full structure, `zero modify <request>` to tweak it, "
            "or `zero build` to create everything."
        ),
        color=discord.Color.blurple(),
    )
    return embed


def _full_preview_embed(theme: str, style: str, categories: list[dict], roles: list[dict]) -> discord.Embed:
    """Full preview embed with channel tree and role list."""
    total_channels = sum(len(c.get("channels", [])) for c in categories)
    emoji = STYLE_EMOJIS.get(style, "🏗️")
    embed = discord.Embed(
        title=f"🏗️ Blueprint Preview — {emoji} **{theme}** ({style})",
        description="Here's the full planned structure. Use `zero build` to create it.",
        color=discord.Color.blurple(),
    )
    tree = _channel_tree(categories)
    if len(tree) > 1020:
        tree = tree[:1017] + "…"
    embed.add_field(name="📁 Categories & Channels", value=tree or "None", inline=False)
    if roles:
        role_lines = "\n".join(
            f"• **{r['name']}**" + (" *(hoisted)*" if r.get("hoist") else "")
            for r in roles
        )
        if len(role_lines) > 1020:
            role_lines = role_lines[:1017] + "…"
        embed.add_field(name="🎭 Roles", value=role_lines, inline=False)
    embed.set_footer(text=f"{len(categories)} categories · {total_channels} channels · {len(roles)} roles")
    return embed


def _compute_diff(old: dict, new: dict) -> str:
    """Return a human-readable summary of structural changes between two blueprints."""
    lines: list[str] = []

    old_cats = {c["name"] for c in old.get("categories", [])}
    new_cats = {c["name"] for c in new.get("categories", [])}
    for name in sorted(new_cats - old_cats):
        lines.append(f"`+` 📁 Category **{name}** added")
    for name in sorted(old_cats - new_cats):
        lines.append(f"`-` 📁 Category **{name}** removed")

    old_chs = {ch["name"] for c in old.get("categories", []) for ch in c.get("channels", [])}
    new_chs = {ch["name"] for c in new.get("categories", []) for ch in c.get("channels", [])}
    for name in sorted(new_chs - old_chs):
        lines.append(f"`+` # Channel **{name}** added")
    for name in sorted(old_chs - new_chs):
        lines.append(f"`-` # Channel **{name}** removed")

    old_roles = {r["name"] for r in old.get("roles", [])}
    new_roles = {r["name"] for r in new.get("roles", [])}
    for name in sorted(new_roles - old_roles):
        lines.append(f"`+` 🎭 Role **{name}** added")
    for name in sorted(old_roles - new_roles):
        lines.append(f"`-` 🎭 Role **{name}** removed")

    return "\n".join(lines) if lines else "No structural changes detected."


# ── Discord UI Views ──────────────────────────────────────────────────────────

class ConfirmBuildView(discord.ui.View):
    """Confirm/cancel before executing a build. Works for first-time, replace, and retry."""

    def __init__(
        self,
        cog: "ServerArchitect",
        author_id: int,
        guild: discord.Guild,
        theme: str,
        style: str,
        categories: list[dict],
        roles: list[dict],
        needs_reset: bool = False,
        is_partial_retry: bool = False,
    ) -> None:
        super().__init__(timeout=120)
        self.cog             = cog
        self.author_id       = author_id
        self.guild           = guild
        self.theme           = theme
        self.style           = style
        self.categories      = categories
        self.roles           = roles
        self.needs_reset     = needs_reset
        self.is_partial_retry = is_partial_retry
        self.message: discord.Message | None = None

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message:
            try:
                await self.message.edit(
                    content="⏱️ Timed out. Run `zero build` again when you're ready.",
                    embed=None, view=self,
                )
            except Exception:
                pass

    @discord.ui.button(label="🔨 Confirm Build", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your build.", ephemeral=True)
            return
        self._disable_all()
        await interaction.response.edit_message(content="⚙️ Starting build…", embed=None, view=self)
        progress_msg = interaction.message
        try:
            await self.cog._execute_full_build(
                channel=interaction.channel,
                guild=self.guild,
                theme=self.theme,
                style=self.style,
                categories=self.categories,
                roles=self.roles,
                author_id=self.author_id,
                progress_msg=progress_msg,
                needs_reset=self.needs_reset,
                is_partial_retry=self.is_partial_retry,
            )
        except Exception as exc:
            logger.error("Full build failed unexpectedly: %s", exc)
            try:
                await progress_msg.edit(
                    content="⚠️ Something went wrong. Blueprint is still saved — `zero build` to retry.",
                    embed=None, view=None,
                )
            except Exception:
                pass
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your build.", ephemeral=True)
            return
        self._disable_all()
        await interaction.response.edit_message(
            content="Build cancelled. Your blueprint is still saved — `zero build` anytime.",
            embed=None, view=self,
        )
        self.stop()


class ConfirmWipeView(discord.ui.View):
    """Shown for `zero wipe` — requires explicit confirmation before destroying everything."""

    def __init__(self, cog: "ServerArchitect", ctx: commands.Context, has_blueprint: bool) -> None:
        super().__init__(timeout=60)
        self.cog           = cog
        self.ctx           = ctx
        self.has_blueprint = has_blueprint
        self.message: discord.Message | None = None

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="💣 Confirm Wipe", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Only the server owner can confirm this.", ephemeral=True)
            return
        self._disable_all()
        await interaction.response.edit_message(content="🗑️ Backing up and wiping…", embed=None, view=self)
        progress_msg = interaction.message
        try:
            await self.cog._execute_wipe(
                channel=interaction.channel,
                guild=self.ctx.guild,
                author_id=self.ctx.author.id,
                has_blueprint=self.has_blueprint,
                progress_msg=progress_msg,
            )
        except Exception as exc:
            logger.error("Wipe failed: %s", exc)
            try:
                await progress_msg.edit(content="⚠️ Wipe failed. Server may be in a partial state.", embed=None, view=None)
            except Exception:
                pass
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Only the server owner can cancel this.", ephemeral=True)
            return
        self._disable_all()
        await interaction.response.edit_message(content="Wipe cancelled. Nothing was changed.", embed=None, view=self)
        self.stop()


class RestoreSelectView(discord.ui.View):
    """Numbered buttons (up to 5) for selecting a backup to restore."""

    def __init__(
        self,
        cog: "ServerArchitect",
        author_id: int,
        guild: discord.Guild,
        backups: list[dict],
    ) -> None:
        super().__init__(timeout=120)
        self.cog       = cog
        self.author_id = author_id
        self.guild     = guild
        self.backups   = backups
        self.message: discord.Message | None = None

        for i, backup in enumerate(backups[:5]):
            label = f"{i + 1}. {backup['name'][:38]}"
            btn   = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=i)
            btn.callback = self._make_callback(backup)
            self.add_item(btn)

    def _make_callback(self, backup: dict):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.author_id:
                await interaction.response.send_message("This isn't your restore session.", ephemeral=True)
                return
            for item in self.children:
                item.disabled = True  # type: ignore[attr-defined]
            await interaction.response.edit_message(view=self)
            await self.cog._confirm_restore(
                interaction.channel, self.guild, self.author_id, backup
            )
            self.stop()
        return callback

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class ConfirmRestoreView(discord.ui.View):
    """Final confirm/cancel before recreating a backup's structure."""

    def __init__(
        self,
        cog: "ServerArchitect",
        author_id: int,
        guild: discord.Guild,
        backup: dict,
    ) -> None:
        super().__init__(timeout=120)
        self.cog       = cog
        self.author_id = author_id
        self.guild     = guild
        self.backup    = backup
        self.message: discord.Message | None = None

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="🔁 Restore This Backup", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your restore session.", ephemeral=True)
            return
        self._disable_all()
        await interaction.response.edit_message(content="🔁 Restoring…", embed=None, view=self)
        progress_msg = interaction.message
        try:
            full_backup = await self.cog.bot.db.get_backup(self.guild.id, self.backup["backup_id"])
            if not full_backup:
                await progress_msg.edit(content="⚠️ Backup not found — it may have been pruned.", embed=None, view=None)
                return
            await self.cog._restore_from_snapshot(self.guild, full_backup["snapshot"], progress_msg)
        except Exception as exc:
            logger.error("Restore failed: %s", exc)
            try:
                await progress_msg.edit(content="⚠️ Restore failed unexpectedly.", embed=None, view=None)
            except Exception:
                pass
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your restore session.", ephemeral=True)
            return
        self._disable_all()
        await interaction.response.edit_message(content="Restore cancelled.", embed=None, view=self)
        self.stop()


# ── Cog ───────────────────────────────────────────────────────────────────────

class ServerArchitect(commands.Cog):

    def __init__(self, bot: commands.Bot, conv: ConversationManager) -> None:
        self.bot  = bot
        self.conv = conv

    # ── Shared guards ─────────────────────────────────────────────────────────

    async def _is_allowed(self, ctx: commands.Context) -> bool:
        """Server owner always passes; others checked against DB authorized list."""
        if ctx.author.id == ctx.guild.owner_id:
            return True
        role_ids = [r.id for r in ctx.author.roles]
        return await self.bot.db.is_authorized(ctx.guild.id, ctx.author.id, role_ids)

    def _missing_perms(self, guild: discord.Guild) -> list[str]:
        me      = guild.me
        missing = []
        if not me.guild_permissions.manage_channels:
            missing.append("Manage Channels")
        if not me.guild_permissions.manage_roles:
            missing.append("Manage Roles")
        return missing

    # =========================================================================
    # COMMAND: design
    # =========================================================================

    @commands.command(name="design")
    @commands.guild_only()
    async def design(self, ctx: commands.Context, *, args: str = "") -> None:
        """
        Generate and save a server blueprint.
        Usage: `zero design <theme> [style]`
        Styles: Architect (default), Minimal, Aesthetic, Pro
        Example: `zero design Gaming Minimal`
        """
        if not await self._is_allowed(ctx):
            await ctx.reply("Only the **server owner** (or an authorized user) can use this command.")
            return
        if self.conv.is_active_in(ctx.author.id, ctx.channel.id):
            await ctx.reply("We're mid-setup! Answer my question above first. 😊")
            return

        if not args.strip():
            # Enter conversational mode — ask for theme
            self.conv.start(
                ctx.author.id,
                phase=PHASE_DESIGN_THEME,
                channel_id=ctx.channel.id,
                guild_id=ctx.guild.id,
            )
            embed = discord.Embed(
                title="📐 Server Designer",
                description=(
                    "I'll design a blueprint tailored to your community.\n\n"
                    "**What is the theme of your server?**\n"
                    "*(e.g. Anime, Gaming, Coding, Music, Study …)*\n\n"
                    "💡 Add a style after the theme for different vibes:\n"
                    "`Gaming Minimal` · `Anime Aesthetic` · `Business Pro`"
                ),
                color=discord.Color.blurple(),
            )
            embed.set_footer(text="Just reply with your theme — no prefix needed.")
            await ctx.send(embed=embed)
            return

        theme, style = _parse_theme_style(args)
        if not theme:
            await ctx.reply("Please provide a theme, e.g. `zero design Gaming`.")
            return

        await self._run_design(ctx.channel, ctx.guild, ctx.author, theme, style)

    # ── Legacy alias: zero create a server ────────────────────────────────────

    @commands.command(name="create")
    @commands.guild_only()
    async def create(self, ctx: commands.Context, *, args: str = "") -> None:
        """Legacy entry point. `zero create a server` starts the design flow."""
        if args.strip() and "server" not in args.lower():
            await ctx.reply("Try `zero design <theme>` to get started!")
            return
        await ctx.invoke(self.design)

    # =========================================================================
    # COMMAND: preview
    # =========================================================================

    @commands.command(name="preview")
    @commands.guild_only()
    async def preview(self, ctx: commands.Context) -> None:
        """Show the current saved blueprint. Usage: `zero preview`"""
        if not await self._is_allowed(ctx):
            await ctx.reply("Only the **server owner** (or an authorized user) can view the blueprint.")
            return

        blueprint = await self.bot.db.get_blueprint(ctx.guild.id)
        if not blueprint:
            await ctx.reply("No blueprint on file. Use `zero design <theme>` to create one.")
            return

        theme       = blueprint["theme"]
        style       = blueprint.get("style", "Architect")
        categories  = blueprint["categories"]
        roles       = blueprint["roles"]
        designed_by = blueprint.get("designed_by")
        designed_at = blueprint.get("designed_at")

        embed = _full_preview_embed(theme, style, categories, roles)
        if designed_by and designed_at:
            member = ctx.guild.get_member(designed_by)
            name   = member.display_name if member else f"User {designed_by}"
            date   = designed_at.strftime("%b %d, %Y") if isinstance(designed_at, datetime) else "unknown"
            embed.set_footer(text=f"Designed by {name} on {date} · `zero build` to apply · `zero modify` to adjust")
        await ctx.reply(embed=embed)

    # =========================================================================
    # COMMAND: modify
    # =========================================================================

    @commands.command(name="modify")
    @commands.guild_only()
    async def modify(self, ctx: commands.Context, *, request: str) -> None:
        """
        AI-edit the current blueprint and show a diff.
        Usage: `zero modify add a music-bot-commands channel to Events`
        """
        if not await self._is_allowed(ctx):
            await ctx.reply("Only the **server owner** (or an authorized user) can modify the blueprint.")
            return

        blueprint = await self.bot.db.get_blueprint(ctx.guild.id)
        if not blueprint:
            await ctx.reply("No blueprint on file. Use `zero design <theme>` first.")
            return

        thinking = await ctx.reply(f"🔧 Applying change: *\"{request}\"*…")

        try:
            current_json = json.dumps({
                "categories": blueprint["categories"],
                "roles":      blueprint["roles"],
            })
            prompt = (
                f"Current blueprint:\n{current_json}\n\n"
                f'Modification request: "{request}"\n\nReturn the complete updated blueprint.'
            )
            raw     = await self.bot.revolver.generate(prompt=prompt, system_prompt=_MODIFY_SYSTEM)
            updated = json.loads(_extract_json(raw))
            new_cats  = updated.get("categories", blueprint["categories"])
            new_roles = updated.get("roles",      blueprint["roles"])
        except Exception as exc:
            logger.error("Blueprint modification failed: %s", exc)
            await thinking.edit(content="⚠️ Couldn't apply that change. Try rephrasing and retry.")
            return

        diff = _compute_diff(
            {"categories": blueprint["categories"], "roles": blueprint["roles"]},
            {"categories": new_cats,                "roles": new_roles},
        )

        await self.bot.db.save_blueprint(
            ctx.guild.id,
            ctx.author.id,
            blueprint["theme"],
            blueprint.get("style", "Architect"),
            new_cats,
            new_roles,
        )

        embed = discord.Embed(
            title="✏️ Blueprint Updated",
            description=f"**Change applied:** *{request}*",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="📋 What Changed",
            value=(diff[:1020] + "…") if len(diff) > 1020 else diff,
            inline=False,
        )
        embed.set_footer(text="`zero preview` for full structure · `zero build` to apply · `zero modify` for more changes")
        await thinking.edit(content=None, embed=embed)

    # =========================================================================
    # COMMAND: build
    # =========================================================================

    @commands.command(name="build")
    @commands.guild_only()
    async def build(self, ctx: commands.Context) -> None:
        """
        Apply the current blueprint to the server.
        Requires a blueprint from `zero design`. Handles first builds,
        replacements (auto-backup first), and partial retries cleanly.
        Usage: `zero build`
        """
        if not await self._is_allowed(ctx):
            await ctx.reply("Only the **server owner** (or an authorized user) can build.")
            return

        missing = self._missing_perms(ctx.guild)
        if missing:
            await ctx.reply(f"I need the {' and '.join(f'**{p}**' for p in missing)} permission(s) to build.")
            return

        blueprint = await self.bot.db.get_blueprint(ctx.guild.id)
        if not blueprint:
            await ctx.reply(
                "No blueprint saved yet.\n"
                "Use `zero design <theme>` to design one, then come back here to build it."
            )
            return

        theme      = blueprint["theme"]
        style      = blueprint.get("style", "Architect")
        categories = blueprint["categories"]
        roles      = blueprint["roles"]

        config     = await self.bot.db.get_guild_config(ctx.guild.id)
        prior_run  = bool(config and config.get("architect_run"))
        is_partial = bool(config and config.get("partial_build", False))

        if prior_run:
            snap   = await self.bot.db.get_architect_snapshot(ctx.guild.id)
            cat_c  = len(snap.get("created_category_ids", []))
            ch_c   = len(snap.get("created_channel_ids",  []))
            role_c = len(snap.get("created_role_ids",     []))

            if is_partial:
                embed = discord.Embed(
                    title="🔄 Retry Partial Build",
                    description=(
                        f"A previous build was interrupted after creating "
                        f"**{cat_c}** categories, **{ch_c}** channels, and **{role_c}** roles.\n\n"
                        f"Your **{theme}** ({style}) blueprint is still intact.\n"
                        "Confirming will clean up the partial structure and rebuild from scratch."
                    ),
                    color=discord.Color.orange(),
                )
            else:
                old_theme = config.get("theme", "Unknown")
                embed = discord.Embed(
                    title="⚠️ Replace Existing Setup?",
                    description=(
                        f"This server already has a Zero-built **{old_theme}** structure "
                        f"({cat_c} categories, {ch_c} channels, {role_c} roles).\n\n"
                        f"Confirming will **auto-backup** the current structure, remove it, "
                        f"and rebuild from your **{theme}** ({style}) blueprint.\n\n"
                        "*Channels and roles you created manually are safe — Zero only removes what it originally made.*"
                    ),
                    color=discord.Color.orange(),
                )

            view = ConfirmBuildView(
                self, ctx.author.id, ctx.guild, theme, style, categories, roles,
                needs_reset=True, is_partial_retry=is_partial,
            )
        else:
            embed = _full_preview_embed(theme, style, categories, roles)
            embed.title       = f"🔨 Ready to Build — **{theme}** ({style})"
            embed.description = "Confirm to create this structure in your server."
            view  = ConfirmBuildView(self, ctx.author.id, ctx.guild, theme, style, categories, roles)

        msg = await ctx.send(embed=embed, view=view)
        view.message = msg

    # =========================================================================
    # COMMAND: wipe  (owner only)
    # =========================================================================

    @commands.command(name="wipe")
    @commands.guild_only()
    async def wipe(self, ctx: commands.Context) -> None:
        """
        Destructive full server wipe — owner only.
        Deletes ALL channels, categories, and roles (including manually created ones).
        An automatic backup is saved before anything is deleted.
        Usage: `zero wipe`
        """
        if ctx.author.id != ctx.guild.owner_id:
            await ctx.reply("Only the **server owner** can run `zero wipe`.")
            return

        missing = self._missing_perms(ctx.guild)
        if missing:
            await ctx.reply(f"I need the {' and '.join(f'**{p}**' for p in missing)} permission(s) to wipe.")
            return

        blueprint = await self.bot.db.get_blueprint(ctx.guild.id)
        has_bp    = blueprint is not None
        ch_c   = len([c for c in ctx.guild.channels if not isinstance(c, discord.CategoryChannel)])
        cat_c  = len(ctx.guild.categories)
        role_c = len([r for r in ctx.guild.roles if not r.is_default()])

        embed = discord.Embed(
            title="💣 Full Server Wipe",
            description=(
                f"This will **permanently delete** all **{ch_c}** channels, "
                f"**{cat_c}** categories, and **{role_c}** roles.\n\n"
                "⚠️ This includes **everything** — not just what Zero made.\n\n"
                "✅ A full backup will be saved automatically before anything is deleted."
                + (f"\n\n📐 Your **{blueprint['theme']}** blueprint is saved — `zero build` after to recreate your server." if has_bp else "")
            ),
            color=discord.Color.red(),
        )
        view = ConfirmWipeView(self, ctx, has_bp)
        msg  = await ctx.send(embed=embed, view=view)
        view.message = msg

    # =========================================================================
    # COMMAND: backup
    # =========================================================================

    @commands.command(name="backup")
    @commands.guild_only()
    async def backup(self, ctx: commands.Context, *, name: str = "") -> None:
        """
        Manually snapshot the current server structure.
        Usage: `zero backup` or `zero backup Before the big redesign`
        """
        if not await self._is_allowed(ctx):
            await ctx.reply("Only the **server owner** (or an authorized user) can create backups.")
            return

        thinking = await ctx.reply("📸 Capturing server snapshot…")
        snapshot = await self._capture_server_snapshot(ctx.guild)

        backup_name = (
            name.strip() or
            f"Manual backup — {datetime.now(timezone.utc).strftime('%b %d, %Y %H:%M UTC')}"
        )
        backup_id = await self.bot.db.save_backup(
            ctx.guild.id, backup_name, "manual", ctx.author.id, snapshot
        )

        cat_c = len(snapshot.get("categories", []))
        ch_c  = (
            sum(len(c.get("channels", [])) for c in snapshot.get("categories", []))
            + len(snapshot.get("uncategorized_channels", []))
        )
        role_c = len(snapshot.get("roles", []))

        embed = discord.Embed(
            title="✅ Backup Saved",
            description=f"**{backup_name}**",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Snapshot",
            value=f"**{cat_c}** categories · **{ch_c}** channels · **{role_c}** roles",
            inline=False,
        )
        if backup_id:
            embed.set_footer(text=f"ID: {backup_id} · Use `zero restore` to restore anytime")
        await thinking.edit(content=None, embed=embed)

    # =========================================================================
    # COMMAND: restore  (owner only)
    # =========================================================================

    @commands.command(name="restore")
    @commands.guild_only()
    async def restore(self, ctx: commands.Context) -> None:
        """
        List saved backups and restore one. Non-destructive: recreates missing
        channels/roles without deleting existing content.
        Owner only. Usage: `zero restore`
        """
        if ctx.author.id != ctx.guild.owner_id:
            await ctx.reply("Only the **server owner** can restore from a backup.")
            return

        backups = await self.bot.db.list_backups(ctx.guild.id)
        if not backups:
            await ctx.reply(
                "No backups found.\n"
                "Use `zero backup` to save one. Backups are also created automatically "
                "before `zero build` replacements and `zero wipe`."
            )
            return

        embed = discord.Embed(
            title="🗂️ Saved Backups",
            description=(
                "Pick a backup to restore. This **recreates** the saved structure — "
                "existing channels with the same name are skipped (no deletions)."
            ),
            color=discord.Color.blurple(),
        )
        for i, bk in enumerate(backups[:5]):
            created  = bk.get("created_at")
            date_str = created.strftime("%b %d, %Y %H:%M UTC") if isinstance(created, datetime) else "Unknown"
            btype    = "🤖 Auto" if bk.get("type") == "auto" else "📋 Manual"
            embed.add_field(
                name=f"{i + 1}. {bk['name'][:50]}",
                value=f"{btype} · {date_str}",
                inline=False,
            )
        embed.set_footer(text="Showing up to 5 most recent · Older backups are pruned automatically")

        view = RestoreSelectView(self, ctx.author.id, ctx.guild, backups)
        msg  = await ctx.send(embed=embed, view=view)
        view.message = msg

    # =========================================================================
    # Message listener
    # =========================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if message.content[:5].lower() == "zero ":
            return

        state = self.conv.get(message.author.id)
        if not state or state.channel_id != message.channel.id:
            return

        if state.phase == PHASE_DESIGN_THEME:
            await self._handle_design_theme(message, state)
        elif state.phase == PHASE_YUA_FIRST:
            await self._handle_yua_first(message, state)
        elif state.phase == PHASE_YUA_FINAL:
            await self._handle_yua_final(message, state)

    async def _handle_design_theme(self, message: discord.Message, state) -> None:
        """Handle theme text typed in response to the conversational design prompt."""
        args = message.content.strip()
        if len(args) > 120:
            await message.reply("Please keep the theme under 120 characters.")
            return
        theme, style = _parse_theme_style(args)
        if not theme:
            await message.reply("Please provide a theme, e.g. *Gaming* or *Anime Study*.")
            return
        guild = self.bot.get_guild(state.guild_id)
        if not guild:
            self.conv.end(message.author.id)
            return
        self.conv.end(message.author.id)
        await self._run_design(message.channel, guild, message.author, theme, style)

    # =========================================================================
    # Core: _run_design
    # =========================================================================

    async def _run_design(
        self,
        channel: discord.abc.Messageable,
        guild: discord.Guild,
        author: discord.Member,
        theme: str,
        style: str,
    ) -> None:
        """Call the LLM to generate a blueprint and persist it as the guild's draft."""
        emoji    = STYLE_EMOJIS.get(style, "🏗️")
        thinking = await channel.send(
            f"✨ Designing a {emoji} **{style}** blueprint for **{theme}**…"
        )
        try:
            raw  = await self.bot.revolver.generate(
                prompt=f'Server theme: "{theme}". Generate the full structure.',
                system_prompt=_structure_system(style),
            )
            data       = json.loads(_extract_json(raw))
            categories = data.get("categories", [])
            roles      = data.get("roles", [])
        except Exception as exc:
            logger.error("Blueprint generation failed: %s", exc)
            await thinking.edit(content="⚠️ Trouble generating a blueprint. Please try again.")
            return

        if not categories:
            await thinking.edit(content="⚠️ The AI returned an empty structure. Please try again.")
            return

        await self.bot.db.save_blueprint(guild.id, author.id, theme, style, categories, roles)
        embed = _blueprint_summary_embed(theme, style, categories, roles)
        await thinking.edit(content=None, embed=embed)

    # =========================================================================
    # Core: _execute_full_build
    # =========================================================================

    async def _execute_full_build(
        self,
        channel: discord.abc.Messageable,
        guild: discord.Guild,
        theme: str,
        style: str,
        categories: list[dict],
        roles: list[dict],
        author_id: int,
        progress_msg: discord.Message,
        needs_reset: bool = False,
        is_partial_retry: bool = False,
    ) -> None:
        """
        Apply a blueprint to Discord.

        Partial-failure contract
        ------------------------
        role_failures + cat_failures == 0  → full success.
          - mark_architect_run(partial=False)
          - clear_blueprint()
          - show final summary + Yua pitch

        role_failures + cat_failures > 0   → partial build.
          - mark_architect_run(partial=True)   ← IDs of what WAS created saved
          - blueprint is NOT cleared           ← user can retry with `zero build`
          - show partial summary with counts
        """
        total_roles    = len(roles)
        total_cats     = len(categories)
        total_channels = sum(len(c.get("channels", [])) for c in categories)

        def _progress(cats_done: int, chs_done: int, roles_done: int) -> str:
            return (
                f"🔨 Building **{theme}** ({style})…\n"
                f"> 🎭 Roles: **{roles_done}/{total_roles}**  "
                f"📁 Categories: **{cats_done}/{total_cats}**  "
                f"# Channels: **{chs_done}/{total_channels}**"
            )

        # ── Step 1: Auto-backup + delete previous Zero content ─────────────
        if needs_reset:
            if not is_partial_retry:
                # Full prior build → capture and back up before destroying
                await progress_msg.edit(content="📸 Saving auto-backup before replacing…")
                snap_data  = await self._capture_server_snapshot(guild)
                cfg        = await self.bot.db.get_guild_config(guild.id)
                old_theme  = cfg.get("theme", "previous setup") if cfg else "previous setup"
                await self.bot.db.save_backup(
                    guild.id,
                    f"Auto-backup before {theme} rebuild (replaced {old_theme})",
                    "auto",
                    author_id,
                    snap_data,
                )

            await progress_msg.edit(content="🗑️ Removing previous Zero-created structure…")
            old_snap = await self.bot.db.get_architect_snapshot(guild.id)
            if old_snap:
                await self._delete_snapshot(guild, old_snap)

        # ── Step 2: Create roles ───────────────────────────────────────────
        created_role_ids: list[int] = []
        role_failures = 0
        for i, role_data in enumerate(roles):
            await progress_msg.edit(content=_progress(0, 0, i))
            role_id = await self._create_role(guild, role_data)
            if role_id:
                created_role_ids.append(role_id)
            else:
                role_failures += 1
            await asyncio.sleep(0.3)

        # ── Step 3: Create categories + channels ───────────────────────────
        created_category_ids: list[int] = []
        created_channel_ids:  list[int] = []
        channels_done = 0
        cat_failures  = 0

        for i, cat_data in enumerate(categories):
            await progress_msg.edit(content=_progress(i, channels_done, len(created_role_ids)))
            cat_id, ch_ids = await self._create_category_with_channels(guild, cat_data)
            if cat_id:
                created_category_ids.append(cat_id)
            else:
                cat_failures += 1
            created_channel_ids.extend(ch_ids)
            channels_done += len(ch_ids)
            await asyncio.sleep(0.5)

        # ── Step 4: Partial vs full success ───────────────────────────────
        is_partial = (role_failures > 0 or cat_failures > 0)

        await self.bot.db.mark_architect_run(
            guild.id,
            theme,
            created_category_ids=created_category_ids,
            created_channel_ids=created_channel_ids,
            created_role_ids=created_role_ids,
            partial=is_partial,
        )

        if is_partial:
            # Blueprint stays intact — user retries with `zero build`
            embed = discord.Embed(
                title="⚠️ Partial Build",
                description=(
                    f"The build was interrupted — **{role_failures}** role(s) and "
                    f"**{cat_failures}** category/categories failed to create "
                    f"(likely a permission or rate-limit issue).\n\n"
                    f"**Created so far:** "
                    f"{len(created_category_ids)}/{total_cats} categories · "
                    f"{len(created_channel_ids)} channels · "
                    f"{len(created_role_ids)}/{total_roles} roles\n\n"
                    f"Your **{theme}** blueprint is still saved. "
                    f"Run `zero build` again — Zero will clean up the partial structure "
                    f"and rebuild from scratch."
                ),
                color=discord.Color.orange(),
            )
            await progress_msg.edit(content=None, embed=embed, view=None)
            return

        # ── Step 5: Full success ───────────────────────────────────────────
        await self.bot.db.clear_blueprint(guild.id)

        tree = _channel_tree(categories)
        if len(tree) > 3900:
            tree = tree[:3897] + "…"

        embed = discord.Embed(
            title=f"✅ Your **{theme}** Server is Ready!",
            description=tree,
            color=discord.Color.green(),
        )
        embed.add_field(
            name="📊 Summary",
            value=(
                f"**{len(created_category_ids)}** categories · "
                f"**{len(created_channel_ids)}** channels · "
                f"**{len(created_role_ids)}** roles created"
            ),
            inline=False,
        )
        if roles:
            rnames = ", ".join(f"`{r['name']}`" for r in roles)
            if len(rnames) > 1020:
                rnames = rnames[:1017] + "…"
            embed.add_field(name="🎭 Roles", value=rnames, inline=False)
        embed.set_footer(text=f"Style: {style} · Generated by Zero × Revolver LLM · Saved to MongoDB")
        await progress_msg.edit(content=None, embed=embed, view=None)

        # Yua pitch — only on full success
        self.conv.advance(author_id, PHASE_YUA_FIRST, theme=theme)
        await asyncio.sleep(1.5)
        await self._send_bot_suggestions(channel, theme)

    # =========================================================================
    # Core: _execute_wipe
    # =========================================================================

    async def _execute_wipe(
        self,
        channel: discord.abc.Messageable,
        guild: discord.Guild,
        author_id: int,
        has_blueprint: bool,
        progress_msg: discord.Message,
    ) -> None:
        """Delete ALL channels, categories, and roles (except @everyone and managed roles)."""
        await progress_msg.edit(content="📸 Saving full backup before wipe…")
        snapshot  = await self._capture_server_snapshot(guild)
        backup_id = await self.bot.db.save_backup(
            guild.id,
            f"Auto-backup before wipe — {datetime.now(timezone.utc).strftime('%b %d %H:%M UTC')}",
            "auto",
            author_id,
            snapshot,
        )

        await progress_msg.edit(content="🗑️ Wiping channels…")
        for ch in list(guild.channels):
            if isinstance(ch, discord.CategoryChannel):
                continue
            try:
                await ch.delete(reason="Zero: server wipe")
                await asyncio.sleep(0.3)
            except (discord.Forbidden, discord.NotFound):
                pass
            except Exception as exc:
                logger.warning("Wipe: could not delete channel %s: %s", ch.id, exc)

        for cat in list(guild.categories):
            try:
                await cat.delete(reason="Zero: server wipe")
                await asyncio.sleep(0.3)
            except (discord.Forbidden, discord.NotFound):
                pass
            except Exception as exc:
                logger.warning("Wipe: could not delete category %s: %s", cat.id, exc)

        await progress_msg.edit(content="🗑️ Wiping roles…")
        for role in list(guild.roles):
            if role.is_default() or role.managed:
                continue
            try:
                await role.delete(reason="Zero: server wipe")
                await asyncio.sleep(0.3)
            except (discord.Forbidden, discord.NotFound):
                pass
            except Exception as exc:
                logger.warning("Wipe: could not delete role %s: %s", role.id, exc)

        # Clear the architect record since everything is gone
        await self.bot.db.upsert_guild_config(
            guild.id,
            architect_run=False,
            partial_build=False,
            created_category_ids=[],
            created_channel_ids=[],
            created_role_ids=[],
        )

        embed = discord.Embed(
            title="🧹 Server Wiped",
            description=(
                "All channels, categories, and roles have been removed.\n\n"
                "✅ Full backup saved automatically."
                + (
                    "\n\nYour blueprint is still saved — run `zero build` to rebuild your server!"
                    if has_blueprint else
                    "\n\nUse `zero design <theme>` to plan a new structure, then `zero build` to create it."
                )
            ),
            color=discord.Color.blurple(),
        )
        if backup_id:
            embed.set_footer(text=f"Backup ID: {backup_id} · Use `zero restore` to recover at any time")
        await progress_msg.edit(content=None, embed=embed, view=None)

    # =========================================================================
    # Core: _confirm_restore (called from RestoreSelectView button callback)
    # =========================================================================

    async def _confirm_restore(
        self,
        channel: discord.abc.Messageable,
        guild: discord.Guild,
        author_id: int,
        backup: dict,
    ) -> None:
        """Show a preview of the selected backup and prompt for final confirmation."""
        full_backup = await self.bot.db.get_backup(guild.id, backup["backup_id"])
        if not full_backup:
            await channel.send("⚠️ That backup couldn't be loaded — it may have been pruned.")
            return

        snap     = full_backup["snapshot"]
        cats     = snap.get("categories", [])
        roles    = snap.get("roles", [])

        embed = discord.Embed(
            title=f"🔁 Restore: **{backup['name'][:60]}**",
            description=(
                "This will **recreate** the saved structure. "
                "Channels and roles that already exist with the same name are skipped — "
                "nothing is deleted."
            ),
            color=discord.Color.blurple(),
        )
        cat_lines = [f"**{c['name']}** ({len(c.get('channels', []))} channels)" for c in cats[:8]]
        if len(cats) > 8:
            cat_lines.append(f"*…and {len(cats) - 8} more*")
        embed.add_field(
            name=f"📁 {len(cats)} Categories",
            value="\n".join(cat_lines) or "None",
            inline=False,
        )
        role_names = ", ".join(f"`{r['name']}`" for r in roles[:12])
        embed.add_field(name=f"🎭 {len(roles)} Roles", value=role_names or "None", inline=False)

        view = ConfirmRestoreView(self, author_id, guild, backup)
        msg  = await channel.send(embed=embed, view=view)
        view.message = msg

    # =========================================================================
    # Core: _restore_from_snapshot
    # =========================================================================

    async def _restore_from_snapshot(
        self,
        guild: discord.Guild,
        snapshot: dict,
        progress_msg: discord.Message,
    ) -> None:
        """Recreate categories, channels, and roles from a snapshot (non-destructive)."""
        cats   = snapshot.get("categories", [])
        roles  = snapshot.get("roles", [])
        uncat  = snapshot.get("uncategorized_channels", [])

        created = {"cats": 0, "channels": 0, "roles": 0}
        skipped = {"cats": 0, "channels": 0, "roles": 0}

        existing_names = {c.name.lower() for c in guild.channels}
        existing_roles = {r.name.lower() for r in guild.roles}

        # Roles first
        for role_data in roles:
            await progress_msg.edit(
                content=f"🔁 Restoring roles… {created['roles']}/{len(roles)}"
            )
            if role_data["name"].lower() in existing_roles:
                skipped["roles"] += 1
                continue
            try:
                await guild.create_role(
                    name=role_data["name"],
                    color=discord.Color(role_data.get("color", 0)),
                    hoist=role_data.get("hoist", False),
                    permissions=discord.Permissions(role_data.get("permissions", 0)),
                    reason="Zero: restore from backup",
                )
                created["roles"] += 1
                existing_roles.add(role_data["name"].lower())
                await asyncio.sleep(0.3)
            except Exception as exc:
                logger.warning("Restore: failed to create role %s: %s", role_data["name"], exc)

        # Categories and their channels
        for cat_data in cats:
            await progress_msg.edit(
                content=(
                    f"🔁 Restoring categories… {created['cats']}/{len(cats)} · "
                    f"channels {created['channels']}"
                )
            )
            if cat_data["name"].lower() in existing_names:
                skipped["cats"] += 1
                cat_obj = discord.utils.get(guild.categories, name=cat_data["name"])
            else:
                try:
                    cat_obj = await guild.create_category(cat_data["name"], reason="Zero: restore from backup")
                    existing_names.add(cat_data["name"].lower())
                    created["cats"] += 1
                    await asyncio.sleep(0.4)
                except Exception as exc:
                    logger.warning("Restore: failed to create category %s: %s", cat_data["name"], exc)
                    cat_obj = None

            for ch in cat_data.get("channels", []):
                if ch["name"].lower() in existing_names:
                    skipped["channels"] += 1
                    continue
                try:
                    if ch.get("type") == "voice":
                        await guild.create_voice_channel(ch["name"], category=cat_obj, reason="Zero: restore")
                    else:
                        await guild.create_text_channel(
                            ch["name"], category=cat_obj,
                            topic=ch.get("topic") or "",
                            reason="Zero: restore",
                        )
                    created["channels"] += 1
                    existing_names.add(ch["name"].lower())
                    await asyncio.sleep(0.4)
                except Exception as exc:
                    logger.warning("Restore: failed to create channel %s: %s", ch["name"], exc)

        # Uncategorized channels
        for ch in uncat:
            if ch["name"].lower() in existing_names:
                skipped["channels"] += 1
                continue
            try:
                if ch.get("type") == "voice":
                    await guild.create_voice_channel(ch["name"], reason="Zero: restore")
                else:
                    await guild.create_text_channel(ch["name"], topic=ch.get("topic") or "", reason="Zero: restore")
                created["channels"] += 1
                existing_names.add(ch["name"].lower())
                await asyncio.sleep(0.4)
            except Exception as exc:
                logger.warning("Restore: failed to create uncategorized channel %s: %s", ch["name"], exc)

        embed = discord.Embed(
            title="✅ Restore Complete",
            description=(
                f"**Created:** {created['cats']} categories · "
                f"{created['channels']} channels · {created['roles']} roles\n"
                f"**Skipped (already existed):** {skipped['cats']} categories · "
                f"{skipped['channels']} channels · {skipped['roles']} roles"
            ),
            color=discord.Color.green(),
        )
        await progress_msg.edit(content=None, embed=embed, view=None)

    # =========================================================================
    # Snapshot capture
    # =========================================================================

    async def _capture_server_snapshot(self, guild: discord.Guild) -> dict:
        """Capture the full current server structure for backup/restore."""
        cats = []
        for cat in guild.categories:
            channels = []
            for ch in cat.channels:
                channels.append({
                    "id":       ch.id,
                    "name":     ch.name,
                    "type":     "voice" if isinstance(ch, discord.VoiceChannel) else "text",
                    "position": ch.position,
                    "topic":    getattr(ch, "topic", None),
                })
            cats.append({"id": cat.id, "name": cat.name, "position": cat.position, "channels": channels})

        uncat = []
        for ch in guild.channels:
            if ch.category is None and not isinstance(ch, discord.CategoryChannel):
                uncat.append({
                    "id":       ch.id,
                    "name":     ch.name,
                    "type":     "voice" if isinstance(ch, discord.VoiceChannel) else "text",
                    "position": ch.position,
                    "topic":    getattr(ch, "topic", None),
                })

        roles = []
        for role in guild.roles:
            if role.is_default() or role.managed:
                continue
            roles.append({
                "id":          role.id,
                "name":        role.name,
                "color":       role.color.value,
                "hoist":       role.hoist,
                "position":    role.position,
                "permissions": role.permissions.value,
            })

        return {"categories": cats, "uncategorized_channels": uncat, "roles": roles}

    # =========================================================================
    # Delete Zero's previously created resources (for reset before rebuild)
    # =========================================================================

    async def _delete_snapshot(self, guild: discord.Guild, snapshot: dict) -> None:
        """Delete only channels, categories, and roles Zero originally created."""
        for ch_id in snapshot.get("created_channel_ids", []):
            obj = guild.get_channel(ch_id)
            if obj:
                try:
                    await obj.delete(reason="Zero: replacing previous setup")
                    await asyncio.sleep(0.3)
                except (discord.NotFound, discord.Forbidden):
                    pass
                except Exception as exc:
                    logger.warning("Could not delete channel %s: %s", ch_id, exc)

        for cat_id in snapshot.get("created_category_ids", []):
            obj = guild.get_channel(cat_id)
            if obj:
                try:
                    await obj.delete(reason="Zero: replacing previous setup")
                    await asyncio.sleep(0.3)
                except (discord.NotFound, discord.Forbidden):
                    pass
                except Exception as exc:
                    logger.warning("Could not delete category %s: %s", cat_id, exc)

        for role_id in snapshot.get("created_role_ids", []):
            role = guild.get_role(role_id)
            if role:
                try:
                    await role.delete(reason="Zero: replacing previous setup")
                    await asyncio.sleep(0.3)
                except (discord.NotFound, discord.Forbidden):
                    pass
                except Exception as exc:
                    logger.warning("Could not delete role %s: %s", role_id, exc)

    # =========================================================================
    # Individual creation helpers
    # =========================================================================

    async def _create_role(self, guild: discord.Guild, role_data: dict) -> int | None:
        """Create a single role. Returns the role ID on success, None on failure."""
        name  = role_data.get("name", "Member")
        color = _parse_color(role_data.get("color", "5865F2"))
        hoist = bool(role_data.get("hoist", False))
        try:
            role = await guild.create_role(name=name, color=color, hoist=hoist, reason="Zero: server setup")
            logger.info("Created role: %s (%s)", name, role.id)
            return role.id
        except discord.Forbidden:
            logger.error("No permission to create role: %s", name)
            return None
        except discord.HTTPException as exc:
            if exc.status == 429:
                logger.warning("Rate limited on role %s — retrying in 2s", name)
                await asyncio.sleep(2)
                try:
                    role = await guild.create_role(name=name, color=color, hoist=hoist, reason="Zero: server setup")
                    return role.id
                except Exception as re:
                    logger.error("Role retry failed for %s: %s", name, re)
                    return None
            logger.error("Failed to create role %s: %s", name, exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error creating role %s: %s", name, exc)
            return None

    async def _create_category_with_channels(
        self, guild: discord.Guild, cat_data: dict
    ) -> tuple[int | None, list[int]]:
        """Create a category and its child channels. Returns (category_id, [channel_ids])."""
        cat_name    = cat_data.get("name", "Unnamed")
        channel_ids: list[int] = []

        try:
            category = await guild.create_category(cat_name, reason="Zero: server setup")
            await asyncio.sleep(0.5)
        except discord.Forbidden:
            logger.error("No permission to create category: %s", cat_name)
            return None, []
        except discord.HTTPException as exc:
            if exc.status == 429:
                logger.warning("Rate limited on category %s — retrying in 2s", cat_name)
                await asyncio.sleep(2)
                try:
                    category = await guild.create_category(cat_name, reason="Zero: server setup")
                except Exception as re:
                    logger.error("Category retry failed for %s: %s", cat_name, re)
                    return None, []
            else:
                logger.error("Failed to create category %s: %s", cat_name, exc)
                return None, []
        except Exception as exc:
            logger.error("Unexpected error creating category %s: %s", cat_name, exc)
            return None, []

        existing = {c.name.lower() for c in guild.channels}
        for ch in cat_data.get("channels", []):
            ch_name = ch.get("name", "channel")
            ch_type = ch.get("type", "text")
            if ch_name.lower() in existing:
                continue
            try:
                if ch_type == "voice":
                    ch_obj = await guild.create_voice_channel(ch_name, category=category, reason="Zero: server setup")
                else:
                    ch_obj = await guild.create_text_channel(ch_name, category=category, reason="Zero: server setup")
                channel_ids.append(ch_obj.id)
                existing.add(ch_name.lower())
                await asyncio.sleep(0.4)
            except discord.Forbidden:
                logger.error("No permission to create channel: %s", ch_name)
            except discord.HTTPException as exc:
                if exc.status == 429:
                    logger.warning("Rate limited on channel %s — retrying in 2s", ch_name)
                    await asyncio.sleep(2)
                    try:
                        if ch_type == "voice":
                            ch_obj = await guild.create_voice_channel(ch_name, category=category, reason="Zero: server setup")
                        else:
                            ch_obj = await guild.create_text_channel(ch_name, category=category, reason="Zero: server setup")
                        channel_ids.append(ch_obj.id)
                        existing.add(ch_name.lower())
                    except Exception as re:
                        logger.error("Channel retry failed for %s: %s", ch_name, re)
                else:
                    logger.error("Failed to create channel %s: %s", ch_name, exc)
            except Exception as exc:
                logger.error("Unexpected error creating channel %s: %s", ch_name, exc)

        return category.id, channel_ids

    # =========================================================================
    # Yua flow (unchanged from original)
    # =========================================================================

    async def _handle_yua_first(self, message: discord.Message, state) -> None:
        text = message.content.strip()
        if _positive(text):
            await self._send_yua_accepted(message.channel)
            self.conv.end(message.author.id)
        elif _negative(text):
            embed = discord.Embed(
                title="😊 Are you sure?",
                description=(
                    "I totally understand! But **Yua** is different — she actively engages members, "
                    "starts conversations, and keeps the server alive even during the quiet hours.\n\n"
                    "Servers with Yua retain members longer. It's a small addition with a huge impact. 💫\n\n"
                    "**Would you like to reconsider? (yes / no)**"
                ),
                color=discord.Color.orange(),
            )
            await message.channel.send(embed=embed)
            self.conv.advance(message.author.id, PHASE_YUA_FINAL)
        else:
            await message.reply("Please reply with **yes** or **no** about adding Yua. 😊")

    async def _handle_yua_final(self, message: discord.Message, state) -> None:
        if _positive(message.content.strip()):
            await self._send_yua_accepted(message.channel)
        else:
            embed = discord.Embed(
                title="🎉 Your server is all set!",
                description=(
                    "Understood — no Yua for now! Your server structure is live and ready.\n\n"
                    "If you change your mind, `zero setup Yua` and I'll set her up. "
                    "Good luck with your community! 🚀"
                ),
                color=discord.Color.blurple(),
            )
            await message.channel.send(embed=embed)
        self.conv.end(message.author.id)

    async def _send_bot_suggestions(self, channel: discord.abc.Messageable, theme: str) -> None:
        try:
            raw  = await self.bot.revolver.generate(prompt=f'Discord server theme: "{theme}"', system_prompt=_BOTS_SYSTEM)
            bots = json.loads(_extract_json(raw))
        except Exception:
            bots = []

        embed = discord.Embed(title="🤖 Recommended Bots for Your Server", color=discord.Color.blurple())
        for b in bots[:2]:
            embed.add_field(name=f"• {b.get('name', 'Bot')}", value=b.get("purpose", "A great addition."), inline=False)
        embed.add_field(
            name="⭐ Yua  ← *My #1 Recommendation*",
            value=(
                "Yua is the **soul** of a server. She keeps conversations flowing, "
                "makes sure no one ever feels lonely, and keeps the server active even in the quiet hours.\n\n"
                "> *\"Yua is highly recommended so that no one ever feels lonely here and the server stays active!\"*\n\n"
                "Servers with Yua retain members longer and feel far more alive. She's not just a bot — she's your community's best friend. 💕"
            ),
            inline=False,
        )
        embed.set_footer(text="Would you like to add Yua? Reply yes or no.")
        await channel.send(embed=embed)

    async def _send_yua_accepted(self, channel: discord.abc.Messageable) -> None:
        embed = discord.Embed(
            title="💕 Amazing choice!",
            description=(
                "Yua will make your server come alive!\n\n"
                "1. Find **Yua** on top.gg or ask the Yua community for the invite link.\n"
                "2. Invite her with the standard bot invite.\n"
                "3. Run `zero setup Yua` — I'll create a `#yua-chat` channel for her.\n\n"
                "Your community is going to love her. 🎉"
            ),
            color=discord.Color.green(),
        )
        await channel.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    conv: ConversationManager = bot.conv_manager  # type: ignore[attr-defined]
    await bot.add_cog(ServerArchitect(bot, conv))
