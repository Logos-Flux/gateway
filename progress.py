"""Progress tracking for running jobs.

Supports three modes per service:
- poll:      Gateway polls the service for progress (training/fine-tuning).
- queue:     Service pushes progress updates to the gateway (batch processing).
- stateless: No progress tracking (inference, one-shot requests).
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)


class ProgressTracker:
    """In-memory progress cache with optional service polling."""

    def __init__(self):
        self._progress: dict[str, dict] = {}
        self._poll_tasks: dict[str, asyncio.Task] = {}

    # --- Public API -----------------------------------------------------------

    def get(self, job_id: str) -> dict | None:
        """Get current progress for a job."""
        return self._progress.get(job_id)

    def update(self, job_id: str, data: dict):
        """Update progress from an external push (queue mode)."""
        self._progress[job_id] = {
            **data,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def snapshot(self, job_id: str) -> str | None:
        """Return JSON-serialisable snapshot for persistence during preemption."""
        import json

        prog = self._progress.get(job_id)
        return json.dumps(prog) if prog else None

    def restore(self, job_id: str, snapshot_json: str):
        """Restore progress from a persisted snapshot."""
        import json

        try:
            self._progress[job_id] = json.loads(snapshot_json)
        except Exception:
            pass

    # --- Polling (poll mode) --------------------------------------------------

    def start_polling(
        self,
        job_id: str,
        service_port: int,
        progress_endpoint: str,
        interval: float = 5.0,
    ):
        """Start background polling for a job's progress."""
        if job_id in self._poll_tasks:
            return
        url = f"http://localhost:{service_port}{progress_endpoint}"
        task = asyncio.create_task(self._poll_loop(job_id, url, interval))
        self._poll_tasks[job_id] = task

    def stop_polling(self, job_id: str):
        """Stop polling for a job."""
        task = self._poll_tasks.pop(job_id, None)
        if task:
            task.cancel()

    # --- Cleanup --------------------------------------------------------------

    def clear(self, job_id: str):
        """Remove all progress state for a job."""
        self._progress.pop(job_id, None)
        self.stop_polling(job_id)

    # --- Internal -------------------------------------------------------------

    async def _poll_loop(self, job_id: str, url: str, interval: float):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                while True:
                    try:
                        resp = await client.get(url)
                        if resp.status_code < 400:
                            self._progress[job_id] = {
                                **resp.json(),
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }
                    except Exception as e:
                        logger.debug(f"Progress poll failed for {job_id[:8]}: {e}")
                    await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
