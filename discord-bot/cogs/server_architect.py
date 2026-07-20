"""
Server Architect — AI-powered server setup wizard.

Commands
--------
zero create [a] server   Start the guided server-creation flow.

Flow
----
1. Check permissions (manage_channels + manage_roles).
2. If a prior setup exists → destructive confirmation embed (Yes / Cancel buttons).
3. Ask for server theme.
4. AI generates categories, channels, and roles via Revolver LLM.
5. Preview embed with full structure (Confirm / Cancel buttons).
6. [If reset] Delete only the channels/categories/roles Zero previously created.
7. Create roles, then categories + channels with live progress updates.
8. Save all created Discord snowflake IDs to MongoDB.
9. Final summary → bot suggestions → Yua pitch.

Persistence (MongoDB)
---------------------
- Stores theme, architect_run flag, and created_category_ids / created_channel_ids /
  created_role_ids per guild so future rebuilds only delete what Zero made.
"""

import asyncio
import json
import logging
import re

import discord
from discord.ext import commands

from conversation_manager import ConversationManager

logger = logging.getLogger(__name__)

# ── Conversation phases ────────────────────────────────────────────────────────
PHASE_THEME            = "awaiting_theme"
PHASE_AWAITING_CONFIRM = "awaiting_confirm"   # preview shown; user must use buttons
PHASE_YUA_FIRST        = "awaiting_yua_first"
PHASE_YUA_FINAL        = "awaiting_yua_final"

# ── LLM prompts ───────────────────────────────────────────────────────────────
_STRUCTURE_SYSTEM = (
    "You are an expert Discord server architect. "
    "Return ONLY valid JSON — no markdown fences, no explanation. "
    "Use this exact schema:\n"
    '{"categories":[{"name":"<emoji + Title>","channels":[{"name":"<lowercase-hyphen>","type":"text|voice"}]}],'
    '"roles":[{"name":"<Role Name>","color":"<6-digit hex no #>","hoist":true}]}\n'
    "Rules for categories: 5-6 categories, 2-4 channels each, emojis in category names, "
    "text channel names lowercase with hyphens, voice channel names Title Case, "
    "always include an Info/Rules category and a Bot-Commands category, "
    "tailor ALL content to the given theme.\n"
    "Rules for roles: 4-6 roles ordered highest to lowest rank "
    "(e.g. Admin, Moderator, VIP, Member, Bot), "
    "use theme-appropriate names where sensible, valid 6-digit hex colors without #, "
    "hoist=true for the top 2-3 roles so they appear separately in the member list."
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


def _parse_color(hex_str: str) -> discord.Color:
    """Convert a hex string (with or without #) to discord.Color, defaulting to blurple."""
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


def _preview_embed(theme: str, categories: list[dict], roles: list[dict]) -> discord.Embed:
    """Build the preview embed shown before anything is created."""
    total_channels = sum(len(c.get("channels", [])) for c in categories)
    embed = discord.Embed(
        title=f"🏗️ Preview — **{theme}** Server",
        description="Here's exactly what I'll build. Click **Build it!** to create everything.",
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

    embed.set_footer(
        text=f"{len(categories)} categories · {total_channels} channels · {len(roles)} roles — confirm to build"
    )
    return embed


# ── Discord UI Views ──────────────────────────────────────────────────────────

class ConfirmResetView(discord.ui.View):
    """Shown when a prior setup exists — confirms destructive replacement."""

    def __init__(self, cog: "ServerArchitect", ctx: commands.Context, old_theme: str) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.old_theme = old_theme
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

    @discord.ui.button(label="✅ Yes, rebuild", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This setup wizard isn't for you.", ephemeral=True)
            return
        self._disable_all()
        await interaction.response.edit_message(
            content=f"Got it! Preparing to replace the **{self.old_theme}** setup…",
            embed=None,
            view=self,
        )
        self.cog.conv.start(
            self.ctx.author.id,
            phase=PHASE_THEME,
            channel_id=self.ctx.channel.id,
            guild_id=self.ctx.guild.id,
            needs_reset=True,
        )
        embed = discord.Embed(
            title="🏗️ Server Builder — New Theme",
            description=(
                "The previous setup will be removed once you confirm the preview.\n\n"
                "**What is the theme of your new server?**\n"
                "*(e.g. Anime, Gaming, Coding, Music, Chill, Sports, Study …)*"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Just reply with your theme — no prefix needed.")
        await self.ctx.channel.send(embed=embed)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This setup wizard isn't for you.", ephemeral=True)
            return
        self._disable_all()
        await interaction.response.edit_message(
            content="Setup cancelled. Your existing server structure is unchanged.",
            embed=None,
            view=self,
        )
        self.stop()


class ConfirmBuildView(discord.ui.View):
    """Shown after structure preview — starts the actual build on confirm."""

    def __init__(
        self,
        cog: "ServerArchitect",
        author_id: int,
        guild: discord.Guild,
        theme: str,
        categories: list[dict],
        roles: list[dict],
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.author_id = author_id
        self.guild = guild
        self.theme = theme
        self.categories = categories
        self.roles = roles
        self.message: discord.Message | None = None

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]

    async def on_timeout(self) -> None:
        self._disable_all()
        self.cog.conv.end(self.author_id)
        if self.message:
            try:
                await self.message.edit(
                    content="⏱️ Preview timed out. Run `zero create a server` to try again.",
                    embed=None,
                    view=self,
                )
            except Exception:
                pass

    @discord.ui.button(label="🔨 Build it!", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup wizard isn't for you.", ephemeral=True)
            return
        self._disable_all()
        await interaction.response.edit_message(
            content="⚙️ Starting build…",
            embed=None,
            view=self,
        )
        progress_msg = interaction.message
        try:
            await self.cog._execute_full_build(
                channel=interaction.channel,
                guild=self.guild,
                theme=self.theme,
                categories=self.categories,
                roles=self.roles,
                author_id=self.author_id,
                progress_msg=progress_msg,
            )
        except Exception as exc:
            logger.error("Full build failed unexpectedly: %s", exc)
            try:
                await progress_msg.edit(
                    content="⚠️ Something went wrong during setup. Please try again.",
                    embed=None,
                    view=None,
                )
            except Exception:
                pass
            self.cog.conv.end(self.author_id)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup wizard isn't for you.", ephemeral=True)
            return
        self._disable_all()
        self.cog.conv.end(self.author_id)
        await interaction.response.edit_message(
            content="Setup cancelled. No changes were made.",
            embed=None,
            view=self,
        )
        self.stop()


# ── Cog ───────────────────────────────────────────────────────────────────────

class ServerArchitect(commands.Cog):

    def __init__(self, bot: commands.Bot, conv: ConversationManager) -> None:
        self.bot = bot
        self.conv = conv

    # ── Auth helper ───────────────────────────────────────────────────────────

    async def _is_allowed(self, ctx: commands.Context) -> bool:
        """Server owner is always allowed; others check MongoDB authorized list."""
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

        # Check both required permissions upfront
        me = ctx.guild.me
        missing: list[str] = []
        if not me.guild_permissions.manage_channels:
            missing.append("Manage Channels")
        if not me.guild_permissions.manage_roles:
            missing.append("Manage Roles")
        if missing:
            perms = " and ".join(f"**{p}**" for p in missing)
            await ctx.reply(f"I need the {perms} permission(s) to build your server.")
            return

        if self.conv.is_active_in(ctx.author.id, ctx.channel.id):
            await ctx.reply("We're already mid-setup! Just answer my question above. 😊")
            return

        # Check for prior run → destructive confirmation
        config = await self.bot.db.get_guild_config(ctx.guild.id)
        if config and config.get("architect_run"):
            old_theme = config.get("theme", "Unknown")
            snapshot = await self.bot.db.get_architect_snapshot(ctx.guild.id)
            cat_count = len(snapshot.get("created_category_ids", []))
            ch_count  = len(snapshot.get("created_channel_ids", []))
            role_count = len(snapshot.get("created_role_ids", []))

            def _plural(n: int, word: str) -> str:
                return f"{n} {word}{'s' if n != 1 else ''}"

            embed = discord.Embed(
                title="⚠️ Previous Setup Detected",
                description=(
                    f"This server was already built around the **{old_theme}** theme.\n\n"
                    f"Continuing will **permanently delete** "
                    f"{_plural(cat_count, 'category')}, "
                    f"{_plural(ch_count, 'channel')}, and "
                    f"{_plural(role_count, 'role')} that Zero created — "
                    f"then rebuild from scratch with a new theme.\n\n"
                    f"*Channels and roles you created manually are safe — "
                    f"Zero only removes what it originally made.*"
                ),
                color=discord.Color.red(),
            )
            view = ConfirmResetView(self, ctx, old_theme)
            msg = await ctx.send(embed=embed, view=view)
            view.message = msg
            return

        # First-time setup — go straight to theme question
        self.conv.start(
            ctx.author.id,
            phase=PHASE_THEME,
            channel_id=ctx.channel.id,
            guild_id=ctx.guild.id,
            needs_reset=False,
        )
        embed = discord.Embed(
            title="🏗️ Server Builder — Let's get started!",
            description=(
                "I'll generate a complete category, channel, and role structure "
                "tailored to your community.\n\n"
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
        elif state.phase == PHASE_AWAITING_CONFIRM:
            await message.reply("Please use the **buttons above** to confirm or cancel the build. 😊")
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
            f"✨ Perfect! Designing your **{theme}** server — generating structure…"
        )

        try:
            raw = await self.bot.revolver.generate(
                prompt=(
                    f'Server theme: "{theme}". '
                    "Generate the full structure with categories, channels, and roles."
                ),
                system_prompt=_STRUCTURE_SYSTEM,
            )
            data = json.loads(_extract_json(raw))
            categories: list[dict] = data.get("categories", [])
            roles: list[dict] = data.get("roles", [])
        except Exception as exc:
            logger.error("Structure generation failed: %s", exc)
            await thinking.edit(content="⚠️ Trouble generating a structure. Please try again.")
            self.conv.end(message.author.id)
            return

        if not categories:
            await thinking.edit(content="⚠️ The AI returned an empty structure. Please try again.")
            self.conv.end(message.author.id)
            return

        # Advance to awaiting-confirm so stray text messages don't re-trigger generation
        self.conv.advance(message.author.id, PHASE_AWAITING_CONFIRM, theme=theme)

        preview = _preview_embed(theme, categories, roles)
        view = ConfirmBuildView(self, message.author.id, guild, theme, categories, roles)
        await thinking.edit(content=None, embed=preview, view=view)
        view.message = thinking

    # ── Core build logic ──────────────────────────────────────────────────────

    async def _execute_full_build(
        self,
        channel: discord.abc.Messageable,
        guild: discord.Guild,
        theme: str,
        categories: list[dict],
        roles: list[dict],
        author_id: int,
        progress_msg: discord.Message,
    ) -> None:
        state = self.conv.get(author_id)
        needs_reset = state.data.get("needs_reset", False) if state else False

        total_cats     = len(categories)
        total_channels = sum(len(c.get("channels", [])) for c in categories)
        total_roles    = len(roles)

        def _progress(cats_done: int, chs_done: int, roles_done: int) -> str:
            return (
                f"🔨 Building **{theme}** server…\n"
                f"> 🎭 Roles: **{roles_done}/{total_roles}**  "
                f"📁 Categories: **{cats_done}/{total_cats}**  "
                f"# Channels: **{chs_done}/{total_channels}**"
            )

        # ── Step 1: Remove previous bot-created resources ──────────────────
        if needs_reset:
            await progress_msg.edit(content="🗑️ Removing previous setup…", embed=None, view=None)
            snapshot = await self.bot.db.get_architect_snapshot(guild.id)
            if snapshot:
                await self._delete_snapshot(guild, snapshot)

        # ── Step 2: Create roles ───────────────────────────────────────────
        created_role_ids: list[int] = []
        for i, role_data in enumerate(roles):
            await progress_msg.edit(content=_progress(0, 0, i))
            role_id = await self._create_role(guild, role_data)
            if role_id:
                created_role_ids.append(role_id)
            await asyncio.sleep(0.3)

        # ── Step 3: Create categories + channels ───────────────────────────
        created_category_ids: list[int] = []
        created_channel_ids: list[int] = []
        channels_done = 0

        for i, cat_data in enumerate(categories):
            await progress_msg.edit(
                content=_progress(i, channels_done, len(created_role_ids))
            )
            cat_id, ch_ids = await self._create_category_with_channels(guild, cat_data)
            if cat_id:
                created_category_ids.append(cat_id)
            created_channel_ids.extend(ch_ids)
            channels_done += len(ch_ids)
            await asyncio.sleep(0.5)

        # ── Step 4: Save to MongoDB ────────────────────────────────────────
        await self.bot.db.mark_architect_run(
            guild.id,
            theme,
            created_category_ids=created_category_ids,
            created_channel_ids=created_channel_ids,
            created_role_ids=created_role_ids,
        )

        # ── Step 5: Final summary embed ────────────────────────────────────
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
                f"**{len(created_category_ids)}** categor{'y' if len(created_category_ids) == 1 else 'ies'} · "
                f"**{len(created_channel_ids)}** channel{'s' if len(created_channel_ids) != 1 else ''} · "
                f"**{len(created_role_ids)}** role{'s' if len(created_role_ids) != 1 else ''} created"
            ),
            inline=False,
        )
        if roles:
            role_names = ", ".join(f"`{r['name']}`" for r in roles)
            if len(role_names) > 1020:
                role_names = role_names[:1017] + "…"
            embed.add_field(name="🎭 Roles Created", value=role_names, inline=False)

        embed.set_footer(text="Generated by Zero × Revolver LLM · Saved to MongoDB")
        await progress_msg.edit(content=None, embed=embed, view=None)

        # ── Step 6: Yua pitch ──────────────────────────────────────────────
        self.conv.advance(author_id, PHASE_YUA_FIRST, theme=theme)
        await asyncio.sleep(1.5)
        await self._send_bot_suggestions(channel, theme)

    # ── Delete previously tracked resources ───────────────────────────────────

    async def _delete_snapshot(self, guild: discord.Guild, snapshot: dict) -> None:
        """Delete only the channels, categories, and roles Zero originally created."""

        # Channels first — categories must be empty before deletion
        for ch_id in snapshot.get("created_channel_ids", []):
            obj = guild.get_channel(ch_id)
            if obj:
                try:
                    await obj.delete(reason="Zero: replacing previous server setup")
                    await asyncio.sleep(0.3)
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    logger.warning("No permission to delete channel %s", ch_id)
                except Exception as exc:
                    logger.warning("Could not delete channel %s: %s", ch_id, exc)

        # Categories
        for cat_id in snapshot.get("created_category_ids", []):
            obj = guild.get_channel(cat_id)
            if obj:
                try:
                    await obj.delete(reason="Zero: replacing previous server setup")
                    await asyncio.sleep(0.3)
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    logger.warning("No permission to delete category %s", cat_id)
                except Exception as exc:
                    logger.warning("Could not delete category %s: %s", cat_id, exc)

        # Roles
        for role_id in snapshot.get("created_role_ids", []):
            role = guild.get_role(role_id)
            if role:
                try:
                    await role.delete(reason="Zero: replacing previous server setup")
                    await asyncio.sleep(0.3)
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    logger.warning("No permission to delete role %s", role_id)
                except Exception as exc:
                    logger.warning("Could not delete role %s: %s", role_id, exc)

    # ── Individual creation helpers ───────────────────────────────────────────

    async def _create_role(self, guild: discord.Guild, role_data: dict) -> int | None:
        """Create a single Discord role. Returns the role ID, or None on failure."""
        name  = role_data.get("name", "Member")
        color = _parse_color(role_data.get("color", "5865F2"))
        hoist = bool(role_data.get("hoist", False))
        try:
            role = await guild.create_role(
                name=name, color=color, hoist=hoist, reason="Zero: server setup"
            )
            logger.info("Created role: %s (ID: %s)", name, role.id)
            return role.id
        except discord.Forbidden:
            logger.error("No permission to create role: %s", name)
            return None
        except discord.HTTPException as exc:
            if exc.status == 429:
                logger.warning("Rate limited creating role %s — retrying after 2s", name)
                await asyncio.sleep(2)
                try:
                    role = await guild.create_role(
                        name=name, color=color, hoist=hoist, reason="Zero: server setup"
                    )
                    return role.id
                except Exception as retry_exc:
                    logger.error("Role retry failed for %s: %s", name, retry_exc)
                    return None
            logger.error("Failed to create role %s: %s", name, exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error creating role %s: %s", name, exc)
            return None

    async def _create_category_with_channels(
        self, guild: discord.Guild, cat_data: dict
    ) -> tuple[int | None, list[int]]:
        """Create a category and all its child channels. Returns (category_id, [channel_ids])."""
        cat_name = cat_data.get("name", "Unnamed")
        channel_ids: list[int] = []

        # Create the category
        try:
            category = await guild.create_category(cat_name, reason="Zero: server setup")
            await asyncio.sleep(0.5)
        except discord.Forbidden:
            logger.error("No permission to create category: %s", cat_name)
            return None, []
        except discord.HTTPException as exc:
            if exc.status == 429:
                logger.warning("Rate limited creating category %s — retrying after 2s", cat_name)
                await asyncio.sleep(2)
                try:
                    category = await guild.create_category(cat_name, reason="Zero: server setup")
                except Exception as retry_exc:
                    logger.error("Category retry failed for %s: %s", cat_name, retry_exc)
                    return None, []
            else:
                logger.error("Failed to create category %s: %s", cat_name, exc)
                return None, []
        except Exception as exc:
            logger.error("Unexpected error creating category %s: %s", cat_name, exc)
            return None, []

        # Create child channels
        existing_names = {c.name.lower() for c in guild.channels}
        for ch in cat_data.get("channels", []):
            ch_name = ch.get("name", "channel")
            ch_type = ch.get("type", "text")
            if ch_name.lower() in existing_names:
                logger.debug("Skipping already-existing channel: %s", ch_name)
                continue
            try:
                if ch_type == "voice":
                    ch_obj = await guild.create_voice_channel(
                        ch_name, category=category, reason="Zero: server setup"
                    )
                else:
                    ch_obj = await guild.create_text_channel(
                        ch_name, category=category, reason="Zero: server setup"
                    )
                channel_ids.append(ch_obj.id)
                existing_names.add(ch_name.lower())
                logger.info("Created channel: %s (ID: %s)", ch_name, ch_obj.id)
                await asyncio.sleep(0.4)
            except discord.Forbidden:
                logger.error("No permission to create channel: %s", ch_name)
            except discord.HTTPException as exc:
                if exc.status == 429:
                    logger.warning("Rate limited on channel %s — retrying after 2s", ch_name)
                    await asyncio.sleep(2)
                    try:
                        if ch_type == "voice":
                            ch_obj = await guild.create_voice_channel(
                                ch_name, category=category, reason="Zero: server setup"
                            )
                        else:
                            ch_obj = await guild.create_text_channel(
                                ch_name, category=category, reason="Zero: server setup"
                            )
                        channel_ids.append(ch_obj.id)
                        existing_names.add(ch_name.lower())
                    except Exception as retry_exc:
                        logger.error("Channel retry failed for %s: %s", ch_name, retry_exc)
                else:
                    logger.error("Failed to create channel %s: %s", ch_name, exc)
            except Exception as exc:
                logger.error("Unexpected error creating channel %s: %s", ch_name, exc)

        return category.id, channel_ids

    # ── Yua phases (unchanged) ────────────────────────────────────────────────

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

    # ── Bot suggestions ───────────────────────────────────────────────────────

    async def _send_bot_suggestions(self, channel: discord.abc.Messageable, theme: str) -> None:
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
