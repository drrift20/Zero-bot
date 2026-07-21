"""
db.py — MongoDB abstraction layer for Zero Bot.

Collections
-----------
guild_configs   — Per-guild settings: setup status, theme, authorized users/roles,
                  IDs of bot-created channels/categories/roles, and current blueprint draft.
custom_bots     — Bots integrated into a guild via Zero.
server_backups  — Per-guild server structure snapshots (auto and manual).

All public methods are safe to call even when MongoDB is not configured;
they log a warning and return neutral values so the bot still runs without a DB.

TLS note
--------
Replit's NixOS OpenSSL 3.6.0 triggers TLSV1_ALERT_INTERNAL_ERROR when connecting
to MongoDB Atlas clusters that enforce TLS 1.3 policy. Fix on the Atlas side:
  Security → Advanced → Minimum TLS Version → set to TLS 1.2
"""

import logging
from datetime import datetime, timezone

import certifi
import motor.motor_asyncio
from bson import ObjectId
from pymongo import ASCENDING, DESCENDING

logger = logging.getLogger(__name__)


class Database:
    """Async MongoDB wrapper. Call `await db.init()` once at startup."""

    def __init__(self) -> None:
        self._client: motor.motor_asyncio.AsyncIOMotorClient | None = None
        self._db: motor.motor_asyncio.AsyncIOMotorDatabase | None = None
        self.ready = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def init(self, uri: str) -> None:
        self._client = motor.motor_asyncio.AsyncIOMotorClient(
            uri,
            serverSelectionTimeoutMS=10000,
            tlsCAFile=certifi.where(),
        )
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
        await self._db.server_backups.create_index(
            [("guild_id", ASCENDING), ("created_at", DESCENDING)]
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
        if not self._check():
            return
        fields["updated_at"] = self._now()
        await self._db.guild_configs.update_one(
            {"guild_id": guild_id},
            {
                "$set": fields,
                "$setOnInsert": {"guild_id": guild_id, "created_at": self._now()},
            },
            upsert=True,
        )

    async def is_architect_run(self, guild_id: int) -> bool:
        config = await self.get_guild_config(guild_id)
        return bool(config and config.get("architect_run"))

    async def mark_architect_run(
        self,
        guild_id: int,
        theme: str,
        created_category_ids: list[int] | None = None,
        created_channel_ids: list[int] | None = None,
        created_role_ids: list[int] | None = None,
        partial: bool = False,
    ) -> None:
        """
        Record a build run (full or partial).

        partial=True means the build stopped early — blueprint is NOT cleared so the
        user can retry.  partial=False means full success — caller should also call
        clear_blueprint() after this.
        """
        await self.upsert_guild_config(
            guild_id,
            architect_run=True,
            partial_build=partial,
            theme=theme,
            architect_date=self._now(),
            created_category_ids=created_category_ids or [],
            created_channel_ids=created_channel_ids or [],
            created_role_ids=created_role_ids or [],
        )

    async def get_architect_snapshot(self, guild_id: int) -> dict:
        """
        Return IDs of channels, categories, and roles Zero created in the last build.
        Returns an empty dict if no run is recorded or if MongoDB is unavailable.
        """
        config = await self.get_guild_config(guild_id)
        if not config:
            return {}
        return {
            "created_category_ids": config.get("created_category_ids", []),
            "created_channel_ids":  config.get("created_channel_ids", []),
            "created_role_ids":     config.get("created_role_ids", []),
        }

    # ── Blueprint (draft per-guild) ───────────────────────────────────────────

    async def save_blueprint(
        self,
        guild_id: int,
        user_id: int,
        theme: str,
        style: str,
        categories: list[dict],
        roles: list[dict],
    ) -> None:
        """Save (or overwrite) the guild's current blueprint draft."""
        await self.upsert_guild_config(
            guild_id,
            current_blueprint={
                "theme":       theme,
                "style":       style,
                "categories":  categories,
                "roles":       roles,
                "designed_by": user_id,
                "designed_at": self._now(),
            },
        )

    async def get_blueprint(self, guild_id: int) -> dict | None:
        """Return the current blueprint draft, or None if none exists."""
        config = await self.get_guild_config(guild_id)
        if not config:
            return None
        return config.get("current_blueprint")

    async def clear_blueprint(self, guild_id: int) -> None:
        """Remove the blueprint draft after a successful build."""
        if not self._check():
            return
        await self._db.guild_configs.update_one(
            {"guild_id": guild_id},
            {
                "$unset": {"current_blueprint": ""},
                "$set":   {"updated_at": self._now()},
            },
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
        config = await self.get_guild_config(guild_id)
        if not config:
            return False
        if user_id in config.get("authorized_users", []):
            return True
        if set(role_ids) & set(config.get("authorized_roles", [])):
            return True
        return False

    # =========================================================================
    # server_backups
    # =========================================================================

    async def save_backup(
        self,
        guild_id: int,
        name: str,
        backup_type: str,
        created_by: int,
        snapshot: dict,
    ) -> str:
        """
        Save a server structure snapshot.

        backup_type is "auto" or "manual".
        Returns the backup ID string (MongoDB ObjectId as str), or "" on failure.
        Per-guild limit: 5 auto + 5 manual backups; oldest are pruned automatically.
        """
        if not self._check():
            return ""
        doc = {
            "guild_id":   guild_id,
            "name":       name,
            "type":       backup_type,
            "created_by": created_by,
            "created_at": self._now(),
            "snapshot":   snapshot,
        }
        result = await self._db.server_backups.insert_one(doc)
        await self._prune_old_backups(guild_id)
        return str(result.inserted_id)

    async def list_backups(self, guild_id: int) -> list[dict]:
        """
        Return up to 10 most-recent backups for a guild (snapshot field excluded
        to keep the response lightweight).
        """
        if not self._check():
            return []
        cursor = (
            self._db.server_backups
            .find({"guild_id": guild_id}, {"snapshot": 0})
            .sort("created_at", DESCENDING)
            .limit(10)
        )
        docs = await cursor.to_list(length=10)
        for doc in docs:
            doc["backup_id"] = str(doc.pop("_id"))
        return docs

    async def get_backup(self, guild_id: int, backup_id: str) -> dict | None:
        """Fetch a full backup document (including snapshot) by its ID string."""
        if not self._check():
            return None
        try:
            doc = await self._db.server_backups.find_one(
                {"_id": ObjectId(backup_id), "guild_id": guild_id}
            )
            if doc:
                doc["backup_id"] = str(doc.pop("_id"))
            return doc
        except Exception as exc:
            logger.error("get_backup failed for id %s: %s", backup_id, exc)
            return None

    async def _prune_old_backups(self, guild_id: int) -> None:
        """Keep at most 5 auto and 5 manual backups per guild; remove oldest first."""
        if not self._check():
            return
        for btype in ("auto", "manual"):
            cursor = (
                self._db.server_backups
                .find({"guild_id": guild_id, "type": btype}, {"_id": 1})
                .sort("created_at", DESCENDING)
            )
            docs = await cursor.to_list(length=100)
            if len(docs) > 5:
                ids_to_delete = [d["_id"] for d in docs[5:]]
                await self._db.server_backups.delete_many({"_id": {"$in": ids_to_delete}})

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
                    "added_by":         added_by,
                    "updated_at":       self._now(),
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
