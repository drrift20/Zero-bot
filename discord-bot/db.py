"""
db.py — MongoDB abstraction layer for Zero Bot.

Collections
-----------
guild_configs   — Per-guild settings: setup status, theme, authorized users/roles.
custom_bots     — Bots that have been integrated into a guild via Zero.

All public methods are safe to call even when MongoDB is not configured;
they log a warning and return neutral values so the bot still runs.
"""

import logging
import os
from datetime import datetime, timezone

import motor.motor_asyncio
from pymongo import ASCENDING

logger = logging.getLogger(__name__)


class Database:
    """Async MongoDB wrapper. Call `await db.init()` once at startup."""

    def __init__(self) -> None:
        self._client: motor.motor_asyncio.AsyncIOMotorClient | None = None
        self._db: motor.motor_asyncio.AsyncIOMotorDatabase | None = None
        self.ready = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def init(self, uri: str) -> None:
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
        # Ping to verify connection before we declare success
        await self._client.admin.command("ping")
        self._db = self._client["zero_bot"]
        await self._create_indexes()
        self.ready = True
        logger.info("MongoDB connected — database: zero_bot")

    async def close(self) -> None:
        if self._client:
            self._client.close()
            self.ready = False

    async def _create_indexes(self) -> None:
        await self._db.guild_configs.create_index(
            [("guild_id", ASCENDING)], unique=True
        )
        await self._db.custom_bots.create_index(
            [("guild_id", ASCENDING), ("bot_name", ASCENDING)]
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _check(self) -> bool:
        if not self.ready or self._db is None:
            logger.warning("DB not ready — operation skipped.")
            return False
        return True

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    # =========================================================================
    # guild_configs
    # =========================================================================

    async def get_guild_config(self, guild_id: int) -> dict | None:
        if not self._check():
            return None
        return await self._db.guild_configs.find_one(
            {"guild_id": guild_id}, {"_id": 0}
        )

    async def upsert_guild_config(self, guild_id: int, **fields) -> None:
        """Create or partially update a guild config document."""
        if not self._check():
            return
        fields["updated_at"] = self._now()
        await self._db.guild_configs.update_one(
            {"guild_id": guild_id},
            {"$set": fields, "$setOnInsert": {"guild_id": guild_id, "created_at": self._now()}},
            upsert=True,
        )

    async def is_architect_run(self, guild_id: int) -> bool:
        config = await self.get_guild_config(guild_id)
        return bool(config and config.get("architect_run"))

    async def mark_architect_run(self, guild_id: int, theme: str) -> None:
        await self.upsert_guild_config(
            guild_id,
            architect_run=True,
            theme=theme,
            architect_date=self._now(),
        )

    # ── Authorized users & roles ──────────────────────────────────────────────

    async def get_authorized_users(self, guild_id: int) -> list[int]:
        config = await self.get_guild_config(guild_id)
        return list(config.get("authorized_users", [])) if config else []

    async def get_authorized_roles(self, guild_id: int) -> list[int]:
        config = await self.get_guild_config(guild_id)
        return list(config.get("authorized_roles", [])) if config else []

    async def add_authorized_user(self, guild_id: int, user_id: int) -> None:
        if not self._check():
            return
        await self._db.guild_configs.update_one(
            {"guild_id": guild_id},
            {
                "$addToSet": {"authorized_users": user_id},
                "$setOnInsert": {"guild_id": guild_id, "created_at": self._now()},
                "$set": {"updated_at": self._now()},
            },
            upsert=True,
        )

    async def remove_authorized_user(self, guild_id: int, user_id: int) -> None:
        if not self._check():
            return
        await self._db.guild_configs.update_one(
            {"guild_id": guild_id},
            {"$pull": {"authorized_users": user_id}, "$set": {"updated_at": self._now()}},
        )

    async def add_authorized_role(self, guild_id: int, role_id: int) -> None:
        if not self._check():
            return
        await self._db.guild_configs.update_one(
            {"guild_id": guild_id},
            {
                "$addToSet": {"authorized_roles": role_id},
                "$setOnInsert": {"guild_id": guild_id, "created_at": self._now()},
                "$set": {"updated_at": self._now()},
            },
            upsert=True,
        )

    async def remove_authorized_role(self, guild_id: int, role_id: int) -> None:
        if not self._check():
            return
        await self._db.guild_configs.update_one(
            {"guild_id": guild_id},
            {"$pull": {"authorized_roles": role_id}, "$set": {"updated_at": self._now()}},
        )

    async def is_authorized(self, guild_id: int, user_id: int, role_ids: list[int]) -> bool:
        """Return True if user_id or any of their role_ids are authorized."""
        config = await self.get_guild_config(guild_id)
        if not config:
            return False
        if user_id in config.get("authorized_users", []):
            return True
        if set(role_ids) & set(config.get("authorized_roles", [])):
            return True
        return False

    # =========================================================================
    # custom_bots
    # =========================================================================

    async def log_custom_bot(
        self,
        guild_id: int,
        bot_name: str,
        channels_created: list[str],
        added_by: int,
    ) -> None:
        if not self._check():
            return
        await self._db.custom_bots.update_one(
            {"guild_id": guild_id, "bot_name": bot_name},
            {
                "$set": {
                    "channels_created": channels_created,
                    "added_by": added_by,
                    "updated_at": self._now(),
                },
                "$setOnInsert": {
                    "guild_id": guild_id,
                    "bot_name": bot_name,
                    "added_at": self._now(),
                },
            },
            upsert=True,
        )

    async def get_custom_bots(self, guild_id: int) -> list[dict]:
        if not self._check():
            return []
        cursor = self._db.custom_bots.find({"guild_id": guild_id}, {"_id": 0})
        return await cursor.to_list(length=50)
