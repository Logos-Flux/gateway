"""Persistent job queue backed by SQLite."""

import aiosqlite
import uuid
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    service_type TEXT NOT NULL,
    service_name TEXT,
    priority INTEGER NOT NULL DEFAULT 3,
    status TEXT NOT NULL DEFAULT 'queued',
    payload TEXT NOT NULL,
    callback_url TEXT,
    result TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    paused_at TEXT,
    error TEXT,
    ttl_seconds INTEGER DEFAULT 3600,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    paused_by_job_id TEXT,
    progress_snapshot TEXT,
    preempt_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_priority ON jobs(status, priority);
CREATE INDEX IF NOT EXISTS idx_jobs_service_type ON jobs(service_type);
"""

MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN paused_by_job_id TEXT",
    "ALTER TABLE jobs ADD COLUMN progress_snapshot TEXT",
    "ALTER TABLE jobs ADD COLUMN preempt_count INTEGER DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_jobs_paused_by ON jobs(paused_by_job_id)",
]


class JobQueue:
    def __init__(self, db_path: str = "/app/data/queue.db"):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        """Initialize database connection and schema."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        # Run migrations for existing databases
        for sql in MIGRATIONS:
            try:
                await self._db.execute(sql)
                await self._db.commit()
            except Exception:
                pass  # Column already exists
        logger.info(f"Job queue initialized at {self.db_path}")

    async def close(self):
        if self._db:
            await self._db.close()

    async def submit(
        self,
        service_type: str,
        payload: dict,
        priority: int = 3,
        callback_url: str | None = None,
        service_name: str | None = None,
        ttl_seconds: int = 3600,
    ) -> str:
        """Submit a job, return job_id."""
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO jobs (id, service_type, service_name, priority, status,
               payload, callback_url, created_at, ttl_seconds)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
            (
                job_id,
                service_type,
                service_name,
                priority,
                json.dumps(payload),
                callback_url,
                now,
                ttl_seconds,
            ),
        )
        await self._db.commit()
        logger.info(f"Job {job_id[:8]} submitted: type={service_type} priority=P{priority}")
        return job_id

    async def get_next(
        self, service_type: str | None = None, service_name: str | None = None
    ) -> dict | None:
        """Get highest priority queued job. Priority ASC (1 first), then FIFO."""
        conditions = ["status = 'queued'"]
        params: list = []

        if service_name:
            conditions.append("(service_name = ? OR service_name IS NULL)")
            params.append(service_name)
        if service_type:
            conditions.append("service_type = ?")
            params.append(service_type)

        where = " AND ".join(conditions)
        cursor = await self._db.execute(
            f"SELECT * FROM jobs WHERE {where} ORDER BY priority ASC, created_at ASC LIMIT 1",
            params,
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_status(
        self,
        job_id: str,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ):
        """Update job status."""
        now = datetime.now(timezone.utc).isoformat()
        updates = ["status = ?"]
        params: list = [status]

        if status == "running":
            updates.append("started_at = ?")
            params.append(now)
        elif status in ("completed", "failed"):
            updates.append("completed_at = ?")
            params.append(now)
        elif status == "paused":
            updates.append("paused_at = ?")
            params.append(now)

        if result is not None:
            updates.append("result = ?")
            params.append(json.dumps(result))
        if error is not None:
            updates.append("error = ?")
            params.append(error)

        params.append(job_id)
        await self._db.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", params)
        await self._db.commit()
        logger.info(f"Job {job_id[:8]} -> {status}")

    async def increment_attempts(self, job_id: str):
        """Increment attempt counter."""
        await self._db.execute("UPDATE jobs SET attempts = attempts + 1 WHERE id = ?", (job_id,))
        await self._db.commit()

    async def get_job(self, job_id: str) -> dict | None:
        """Get job by ID."""
        cursor = await self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_position(self, job_id: str) -> int | None:
        """Get 1-based position of a queued job."""
        job = await self.get_job(job_id)
        if not job or job["status"] != "queued":
            return None
        cursor = await self._db.execute(
            """SELECT COUNT(*) as pos FROM jobs
               WHERE status = 'queued'
               AND (priority < ? OR (priority = ? AND created_at < ?))""",
            (job["priority"], job["priority"], job["created_at"]),
        )
        row = await cursor.fetchone()
        return row["pos"] + 1 if row else None

    async def get_stats(self) -> dict:
        """Queue stats by service_type and priority."""
        # Total queued
        cursor = await self._db.execute("SELECT COUNT(*) as cnt FROM jobs WHERE status = 'queued'")
        row = await cursor.fetchone()
        total_queued = row["cnt"]

        # By type
        cursor = await self._db.execute(
            """SELECT service_type, COUNT(*) as cnt FROM jobs
               WHERE status = 'queued' GROUP BY service_type"""
        )
        by_type = {row["service_type"]: row["cnt"] async for row in cursor}

        # By priority
        cursor = await self._db.execute(
            """SELECT priority, COUNT(*) as cnt FROM jobs
               WHERE status = 'queued' GROUP BY priority"""
        )
        by_priority = {f"P{row['priority']}": row["cnt"] async for row in cursor}

        # Running
        cursor = await self._db.execute("SELECT COUNT(*) as cnt FROM jobs WHERE status = 'running'")
        row = await cursor.fetchone()
        total_running = row["cnt"]

        return {
            "total_queued": total_queued,
            "total_running": total_running,
            "by_type": by_type,
            "by_priority": by_priority,
        }

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a queued job. Returns True if cancelled."""
        cursor = await self._db.execute(
            "UPDATE jobs SET status = 'cancelled', completed_at = ? WHERE id = ? AND status = 'queued'",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )
        await self._db.commit()
        cancelled = cursor.rowcount > 0
        if cancelled:
            logger.info(f"Job {job_id[:8]} cancelled")
        return cancelled

    async def cleanup_expired(self):
        """Remove jobs past their TTL."""
        cursor = await self._db.execute(
            """DELETE FROM jobs
               WHERE status = 'queued'
               AND julianday('now') > julianday(created_at) + (ttl_seconds / 86400.0)"""
        )
        await self._db.commit()
        if cursor.rowcount > 0:
            logger.info(f"Cleaned up {cursor.rowcount} expired jobs")

    async def get_queued_service_types(self) -> list[str]:
        """Get distinct service_types that have queued jobs."""
        cursor = await self._db.execute(
            "SELECT DISTINCT service_type FROM jobs WHERE status = 'queued'"
        )
        return [row["service_type"] async for row in cursor]

    async def get_running_jobs(self) -> list[dict]:
        """Get all currently running jobs."""
        cursor = await self._db.execute("SELECT * FROM jobs WHERE status = 'running'")
        return [dict(row) async for row in cursor]

    # --- Preemption methods (Phase 3) -----------------------------------------

    async def pause_job(
        self,
        job_id: str,
        paused_by_job_id: str,
        progress_snapshot: str | None = None,
    ):
        """Pause a running job due to preemption."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """UPDATE jobs SET status = 'paused', paused_at = ?,
               paused_by_job_id = ?, progress_snapshot = ?,
               preempt_count = preempt_count + 1
               WHERE id = ? AND status = 'running'""",
            (now, paused_by_job_id, progress_snapshot, job_id),
        )
        await self._db.commit()
        logger.info(f"Job {job_id[:8]} paused by {paused_by_job_id[:8]}")

    async def resume_job(self, job_id: str):
        """Move a paused job back to queued (front of queue via original priority)."""
        await self._db.execute(
            """UPDATE jobs SET status = 'queued', paused_at = NULL,
               paused_by_job_id = NULL, started_at = NULL
               WHERE id = ? AND status = 'paused'""",
            (job_id,),
        )
        await self._db.commit()
        logger.info(f"Job {job_id[:8]} resumed (re-queued)")

    async def get_paused_jobs(self) -> list[dict]:
        """Get all paused jobs, ordered by priority."""
        cursor = await self._db.execute(
            "SELECT * FROM jobs WHERE status = 'paused' ORDER BY priority ASC, paused_at ASC"
        )
        return [dict(row) async for row in cursor]

    async def get_paused_by(self, preemptor_job_id: str) -> dict | None:
        """Get the job that was paused by a specific preemptor."""
        cursor = await self._db.execute(
            "SELECT * FROM jobs WHERE paused_by_job_id = ? AND status = 'paused'",
            (preemptor_job_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_running_for_service(self, service_name: str) -> dict | None:
        """Get the running job assigned to a specific service."""
        cursor = await self._db.execute(
            "SELECT * FROM jobs WHERE status = 'running' AND service_name = ?",
            (service_name,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_highest_queued(self, service_type: str) -> dict | None:
        """Get the highest priority queued job for a service type."""
        cursor = await self._db.execute(
            """SELECT * FROM jobs WHERE status = 'queued' AND service_type = ?
               ORDER BY priority ASC, created_at ASC LIMIT 1""",
            (service_type,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
