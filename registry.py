"""Dynamic service registry backed by SQLite."""

import aiosqlite
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SERVICES_SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    name TEXT PRIMARY KEY,
    container TEXT NOT NULL,
    port INTEGER NOT NULL,
    health_endpoint TEXT NOT NULL DEFAULT '/health',
    type TEXT NOT NULL,
    description TEXT,
    vram_gb REAL NOT NULL DEFAULT 0,
    progress_mode TEXT NOT NULL DEFAULT 'stateless',
    progress_endpoint TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

SEED_SERVICES = []

_UPDATABLE_FIELDS = {
    "container",
    "port",
    "health_endpoint",
    "type",
    "description",
    "vram_gb",
    "progress_mode",
    "progress_endpoint",
}


class ServiceRegistry:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        """Initialize database connection and schema."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SERVICES_SCHEMA)
        await self._db.commit()
        await self._seed()
        logger.info("Service registry initialized")

    async def close(self):
        if self._db:
            await self._db.close()

    async def _seed(self):
        """Seed default services if table is empty."""
        if not SEED_SERVICES:
            return

        cursor = await self._db.execute("SELECT COUNT(*) as cnt FROM services")
        row = await cursor.fetchone()
        if row["cnt"] > 0:
            return

        now = datetime.now(timezone.utc).isoformat()
        for svc in SEED_SERVICES:
            await self._db.execute(
                """INSERT INTO services (name, container, port, health_endpoint, type,
                   description, vram_gb, progress_mode, progress_endpoint, enabled,
                   created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    svc["name"],
                    svc["container"],
                    svc["port"],
                    svc.get("health_endpoint", "/health"),
                    svc["type"],
                    svc.get("description"),
                    svc.get("vram_gb", 0),
                    svc.get("progress_mode", "stateless"),
                    svc.get("progress_endpoint"),
                    now,
                    now,
                ),
            )
        await self._db.commit()
        logger.info(f"Seeded {len(SEED_SERVICES)} default services")

    @staticmethod
    def _row_to_dict(row) -> dict:
        """Convert a Row to the service dict format used by scheduler/vram."""
        return {
            "name": row["name"],
            "container": row["container"],
            "port": row["port"],
            "health_endpoint": row["health_endpoint"],
            "type": row["type"],
            "description": row["description"],
            "vram_gb": row["vram_gb"],
            "progress_mode": row["progress_mode"],
            "progress_endpoint": row["progress_endpoint"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def get_all(self, enabled_only: bool = True) -> dict[str, dict]:
        """Return all services as a dict keyed by name."""
        if enabled_only:
            cursor = await self._db.execute(
                "SELECT * FROM services WHERE enabled = 1 ORDER BY name"
            )
        else:
            cursor = await self._db.execute("SELECT * FROM services ORDER BY name")
        rows = await cursor.fetchall()
        return {row["name"]: self._row_to_dict(row) for row in rows}

    async def get(self, name: str) -> dict | None:
        """Get a single service by name."""
        cursor = await self._db.execute("SELECT * FROM services WHERE name = ?", (name,))
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def register(self, service: dict) -> dict:
        """Register a new service. Raises ValueError if name already exists."""
        name = service["name"]
        existing = await self.get(name)
        if existing:
            raise ValueError(f"Service '{name}' already exists")

        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO services (name, container, port, health_endpoint, type,
               description, vram_gb, progress_mode, progress_endpoint, enabled,
               created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                name,
                service["container"],
                service["port"],
                service.get("health_endpoint", "/health"),
                service["type"],
                service.get("description"),
                service.get("vram_gb", 0),
                service.get("progress_mode", "stateless"),
                service.get("progress_endpoint"),
                now,
                now,
            ),
        )
        await self._db.commit()
        logger.info(f"Registered service: {name}")
        return await self.get(name)

    async def update(self, name: str, updates: dict) -> dict | None:
        """Update a service's config. Returns updated dict or None if not found."""
        existing = await self.get(name)
        if not existing:
            return None

        sets = []
        params = []
        for key, val in updates.items():
            if key in _UPDATABLE_FIELDS:
                sets.append(f"{key} = ?")
                params.append(val)
        if not sets:
            return existing

        sets.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(name)
        await self._db.execute(f"UPDATE services SET {', '.join(sets)} WHERE name = ?", params)
        await self._db.commit()
        logger.info(f"Updated service: {name}")
        return await self.get(name)

    async def delete(self, name: str) -> bool:
        """Delete a service. Returns True if deleted."""
        cursor = await self._db.execute("DELETE FROM services WHERE name = ?", (name,))
        await self._db.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info(f"Deleted service: {name}")
        return deleted

    async def set_enabled(self, name: str, enabled: bool) -> dict | None:
        """Enable/disable a service. Returns updated dict or None if not found."""
        existing = await self.get(name)
        if not existing:
            return None
        await self._db.execute(
            "UPDATE services SET enabled = ?, updated_at = ? WHERE name = ?",
            (1 if enabled else 0, datetime.now(timezone.utc).isoformat(), name),
        )
        await self._db.commit()
        logger.info(f"{'Enabled' if enabled else 'Disabled'} service: {name}")
        return await self.get(name)

    async def get_by_type(self, service_type: str, enabled_only: bool = True) -> list[dict]:
        """Get all services of a given type."""
        conditions = ["type = ?"]
        params: list = [service_type]
        if enabled_only:
            conditions.append("enabled = 1")
        where = " AND ".join(conditions)
        cursor = await self._db.execute(
            f"SELECT * FROM services WHERE {where} ORDER BY name", params
        )
        return [self._row_to_dict(row) for row in await cursor.fetchall()]
