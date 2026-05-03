"""Scheduler loop — manages service lifecycle based on queue state.

Runs every 10s:
- Starts services when jobs are waiting
- Dispatches jobs to healthy services
- Auto-preempts lower-priority jobs when higher-priority work arrives
- Resumes paused jobs when preemptors complete
- Stops idle services to free VRAM
- Fires callbacks and ntfy notifications on completion/failure
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import httpx

from job_queue import JobQueue
from notifications import job_completed, job_failed, job_preempted, job_resumed
from progress import ProgressTracker
from resources import can_fit_service

logger = logging.getLogger(__name__)

IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "300"))
PREEMPT_PRIORITY_GAP = int(os.environ.get("PREEMPT_PRIORITY_GAP", "2"))

# Dispatch endpoint patterns per service type
DISPATCH_ENDPOINTS = {
    "llm": "/v1/chat/completions",
    "image-generation": "/process",
    "image-processing": "/process",
    "code-runner": "/execute",
    "vlm": "/v1/chat/completions",
}


class ServiceState:
    """In-memory per-service state (not persisted — rebuilt on restart)."""

    __slots__ = (
        "active_job_id",
        "last_activity",
        "status",
        "startup_requested",
        "started_by_scheduler",
    )

    def __init__(self):
        self.active_job_id: str | None = None
        self.last_activity: datetime | None = None
        self.status: str = "idle"  # idle, busy, starting, stopping, draining
        self.startup_requested: bool = False
        self.started_by_scheduler: bool = False

    def to_dict(self) -> dict:
        return {
            "active_job_id": self.active_job_id,
            "last_activity": self.last_activity.isoformat() if self.last_activity else None,
            "status": self.status,
            "startup_requested": self.startup_requested,
            "started_by_scheduler": self.started_by_scheduler,
        }


class Scheduler:
    def __init__(
        self,
        queue: JobQueue,
        registry,  # ServiceRegistry — avoid circular import
        docker_client,
        docker_available: bool,
    ):
        self.queue = queue
        self.registry = registry
        self.docker_client = docker_client
        self.docker_available = docker_available
        self.running = False
        self.services: dict[str, dict] = {}  # refreshed each tick from registry
        self.states: dict[str, ServiceState] = {}
        self.progress = ProgressTracker()

    async def start(self):
        """Start the scheduler loop."""
        self.running = True
        logger.info(
            f"Scheduler started (idle timeout: {IDLE_TIMEOUT}s, "
            f"preempt gap: {PREEMPT_PRIORITY_GAP})"
        )
        # Initial load from registry
        self.services = await self.registry.get_all(enabled_only=True)
        self._ensure_states()
        await self._sync_states()
        while self.running:
            try:
                await self.tick()
            except Exception as e:
                logger.error(f"Scheduler tick failed: {e}", exc_info=True)
            await asyncio.sleep(10)

    async def stop(self):
        self.running = False
        logger.info("Scheduler stopped")

    def _ensure_states(self):
        """Ensure every service in self.services has a ServiceState entry."""
        for name in self.services:
            if name not in self.states:
                self.states[name] = ServiceState()

    async def _sync_states(self):
        """Sync service states with Docker on startup."""
        if not self.docker_available:
            return
        for name, svc in self.services.items():
            try:
                container = self.docker_client.containers.get(svc["container"])
                if container.status == "running":
                    self.states[name].status = "idle"
                    self.states[name].last_activity = datetime.now(timezone.utc)
            except Exception:
                pass

    async def tick(self):
        """Single scheduler iteration."""
        # Refresh services from registry each tick
        self.services = await self.registry.get_all(enabled_only=True)
        self._ensure_states()

        stats = await self.queue.get_stats()

        # 1. Clean up expired jobs
        await self.queue.cleanup_expired()

        # 2. Check for services waiting to become healthy after startup
        await self._check_startup_waiters()

        # 3. Resume paused jobs whose preemptors have finished
        await self._resume_paused_jobs()

        # 4. For each service type with queued jobs, try to dispatch or preempt
        queued_types = await self.queue.get_queued_service_types()
        for stype in queued_types:
            await self._process_type(stype)

        # 5. Stop idle services
        await self._check_idle_services()

        if stats["total_queued"] > 0 or stats["total_running"] > 0:
            logger.info(f"Tick: {stats['total_queued']} queued, {stats['total_running']} running")

    # --- Core dispatch / preempt logic ----------------------------------------

    async def _process_type(self, service_type: str):
        """Process queued jobs for a service type."""
        matching = {name: svc for name, svc in self.services.items() if svc["type"] == service_type}
        if not matching:
            return

        for name, svc in matching.items():
            state = self.states[name]

            # Skip services in transitional states
            if state.status in ("starting", "stopping", "draining"):
                continue

            # If busy, check if auto-preemption should happen
            if state.status == "busy":
                await self._maybe_auto_preempt(name, svc, service_type)
                continue

            # Not running? Try to start it.
            if not self._is_container_running(svc["container"]):
                if not state.startup_requested:
                    can_fit, details = can_fit_service(
                        name, self.services, self.docker_client, self.docker_available
                    )
                    if can_fit:
                        logger.info(f"Scheduler: starting {name} for queued {service_type} jobs")
                        await self._start_service(name, svc)
                    else:
                        logger.warning(f"Cannot start {name}: {details.get('error', 'unknown')}")
                continue

            # Service is running — check health
            healthy = await self._check_health(svc)
            if not healthy:
                continue

            # Healthy and idle — dispatch next job
            if state.status == "idle":
                job = await self.queue.get_next(service_type=service_type, service_name=name)
                if not job:
                    job = await self.queue.get_next(service_type=service_type)
                if job:
                    asyncio.create_task(self._dispatch_job(name, svc, job))
                    return  # One dispatch per tick per type

    async def _maybe_auto_preempt(self, service_name: str, svc: dict, service_type: str):
        """Check if a queued job should preempt the running job on this service."""
        state = self.states[service_name]
        if not state.active_job_id:
            return

        running_job = await self.queue.get_job(state.active_job_id)
        if not running_job:
            return

        queued_job = await self.queue.get_highest_queued(service_type)
        if not queued_job:
            return

        priority_diff = running_job["priority"] - queued_job["priority"]
        if priority_diff < PREEMPT_PRIORITY_GAP:
            return

        logger.info(
            f"Auto-preempt: P{queued_job['priority']} job {queued_job['id'][:8]} "
            f"preempts P{running_job['priority']} job {running_job['id'][:8]} "
            f"on {service_name} (gap={priority_diff})"
        )
        await self.preempt_service(service_name, queued_job["id"])

    # --- Preemption (called by scheduler and API) -----------------------------

    async def preempt_service(self, service_name: str, preemptor_job_id: str) -> dict:
        """
        Preempt the running job on a service.

        1. Save progress snapshot
        2. Pause the running job in the queue
        3. Mark service idle so scheduler dispatches the preemptor next tick
        """
        state = self.states.get(service_name)
        if not state or not state.active_job_id:
            return {"error": "no_active_job", "service": service_name}

        victim_job_id = state.active_job_id

        # Snapshot progress
        snapshot = self.progress.snapshot(victim_job_id)
        self.progress.clear(victim_job_id)

        # Pause the victim in the queue
        await self.queue.pause_job(
            victim_job_id,
            paused_by_job_id=preemptor_job_id,
            progress_snapshot=snapshot,
        )

        # Mark service idle (the dispatch task may still be running — it will
        # see the status change and exit gracefully on the next iteration)
        state.status = "idle"
        state.active_job_id = None
        state.last_activity = datetime.now(timezone.utc)

        await job_preempted(victim_job_id, preemptor_job_id, service_name)

        return {
            "preempted_job_id": victim_job_id,
            "preemptor_job_id": preemptor_job_id,
            "service": service_name,
            "progress_saved": snapshot is not None,
        }

    def check_preemption(self, service_name: str) -> dict:
        """Check what would happen if we preempted a service (dry run)."""
        state = self.states.get(service_name)
        if not state or state.status != "busy" or not state.active_job_id:
            return {
                "preemptable": False,
                "reason": "service_not_busy",
                "service": service_name,
            }

        return {
            "preemptable": True,
            "service": service_name,
            "active_job_id": state.active_job_id,
            "scheduler_status": state.status,
        }

    # --- Resume paused jobs ---------------------------------------------------

    async def _resume_paused_jobs(self):
        """Check if any preemptor jobs have finished — resume their victims."""
        paused_jobs = await self.queue.get_paused_jobs()
        for pjob in paused_jobs:
            preemptor_id = pjob.get("paused_by_job_id")
            if not preemptor_id:
                # Orphaned pause — just resume
                await self.queue.resume_job(pjob["id"])
                await job_resumed(pjob["id"], pjob.get("service_name", "unknown"))
                continue

            preemptor = await self.queue.get_job(preemptor_id)
            if not preemptor:
                # Preemptor deleted — resume
                await self.queue.resume_job(pjob["id"])
                await job_resumed(pjob["id"], pjob.get("service_name", "unknown"))
                continue

            if preemptor["status"] in ("completed", "failed", "cancelled"):
                # Preemptor done — restore and resume
                if pjob.get("progress_snapshot"):
                    self.progress.restore(pjob["id"], pjob["progress_snapshot"])
                await self.queue.resume_job(pjob["id"])
                await job_resumed(pjob["id"], pjob.get("service_name", "unknown"))
                logger.info(
                    f"Resumed paused job {pjob['id'][:8]} "
                    f"(preemptor {preemptor_id[:8]} is {preemptor['status']})"
                )

    # --- Dispatch -------------------------------------------------------------

    async def _dispatch_job(self, service_name: str, svc: dict, job: dict):
        """Dispatch a job to a service."""
        state = self.states[service_name]
        job_id = job["id"]
        state.status = "busy"
        state.active_job_id = job_id

        # Tag the job with which service it's running on
        await self.queue.update_status(job_id, "running")
        await self.queue.increment_attempts(job_id)
        logger.info(f"Dispatching job {job_id[:8]} to {service_name}")

        # Start progress tracking if service supports it
        progress_mode = svc.get("progress_mode", "stateless")
        if progress_mode == "poll" and svc.get("progress_endpoint"):
            self.progress.start_polling(job_id, svc["port"], svc["progress_endpoint"])

        try:
            result = await self._call_service(svc, job)
            await self.queue.update_status(job_id, "completed", result=result)
            logger.info(f"Job {job_id[:8]} completed on {service_name}")
            await job_completed(job_id, service_name)

            # Fire callback
            if job.get("callback_url"):
                await self._fire_callback(job["callback_url"], job_id, "completed", result=result)
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Job {job_id[:8]} failed on {service_name}: {error_msg}")

            # Check retry
            updated_job = await self.queue.get_job(job_id)
            if updated_job and updated_job["attempts"] < updated_job["max_attempts"]:
                logger.info(
                    f"Re-queuing job {job_id[:8]} "
                    f"(attempt {updated_job['attempts']}/{updated_job['max_attempts']})"
                )
                await self.queue.update_status(job_id, "queued", error=error_msg)
            else:
                await self.queue.update_status(job_id, "failed", error=error_msg)
                await job_failed(job_id, service_name, error_msg)
                if job.get("callback_url"):
                    await self._fire_callback(
                        job["callback_url"], job_id, "failed", error=error_msg
                    )
        finally:
            self.progress.clear(job_id)
            state.status = "idle"
            state.active_job_id = None
            state.last_activity = datetime.now(timezone.utc)

    async def _call_service(self, svc: dict, job: dict) -> dict:
        """Forward job payload to the service."""
        endpoint = DISPATCH_ENDPOINTS.get(svc["type"], "/process")
        url = f"http://localhost:{svc['port']}{endpoint}"
        payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]

        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()

    async def _fire_callback(
        self,
        callback_url: str,
        job_id: str,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ):
        """Fire-and-forget callback."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    callback_url,
                    json={
                        "job_id": job_id,
                        "status": status,
                        "result": result,
                        "error": error,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
                logger.info(f"Callback fired for job {job_id[:8]} -> {callback_url}")
        except Exception as e:
            logger.warning(f"Callback failed for job {job_id[:8]}: {e}")

    # --- Service lifecycle helpers --------------------------------------------

    async def _start_service(self, name: str, svc: dict):
        """Request container start (non-blocking)."""
        state = self.states[name]
        state.startup_requested = True
        state.started_by_scheduler = True
        state.status = "starting"

        try:
            container = self.docker_client.containers.get(svc["container"])
            if container.status != "running":
                container.start()
                logger.info(f"Started container {svc['container']} (by scheduler)")
        except Exception as e:
            logger.error(f"Failed to start {svc['container']}: {e}")
            state.startup_requested = False
            state.status = "idle"

    async def _check_startup_waiters(self):
        """Check services that are starting up — clear flag when healthy."""
        for name, state in self.states.items():
            if not state.startup_requested:
                continue
            svc = self.services.get(name)
            if not svc:
                continue
            if self._is_container_running(svc["container"]):
                healthy = await self._check_health(svc)
                if healthy:
                    state.startup_requested = False
                    state.status = "idle"
                    state.last_activity = datetime.now(timezone.utc)
                    logger.info(f"Service {name} is now healthy")

    async def _check_idle_services(self):
        """Stop services idle longer than IDLE_TIMEOUT.

        Only applies to services started by the scheduler. Manually started
        containers (via API or external process) are left alone.
        """
        now = datetime.now(timezone.utc)
        for name, svc in self.services.items():
            state = self.states.get(name)
            if not state:
                continue
            if svc["vram_gb"] == 0:
                continue  # Don't auto-stop zero-VRAM services
            if not state.started_by_scheduler:
                continue  # Don't auto-stop manually started services
            if state.status != "idle":
                continue
            if not self._is_container_running(svc["container"]):
                continue
            if state.last_activity is None:
                continue

            idle_seconds = (now - state.last_activity).total_seconds()
            if idle_seconds < IDLE_TIMEOUT:
                continue

            # Don't stop if a sibling service sharing the same container is active
            if self._sibling_is_active(name):
                continue

            # Check if there are queued or paused jobs of this type
            queued_types = await self.queue.get_queued_service_types()
            if svc["type"] in queued_types:
                continue

            paused = await self.queue.get_paused_jobs()
            if any(p["service_type"] == svc["type"] for p in paused):
                continue

            logger.info(
                f"Stopping idle service {name} (idle {idle_seconds:.0f}s > {IDLE_TIMEOUT}s)"
            )
            try:
                container = self.docker_client.containers.get(svc["container"])
                container.stop(timeout=10)
                # Reset state for this service and all siblings sharing the container
                for svc_name, svc_def in self.services.items():
                    if svc_def["container"] == svc["container"]:
                        sibling_state = self.states.get(svc_name)
                        if sibling_state:
                            sibling_state.status = "idle"
                            sibling_state.last_activity = None
                            sibling_state.started_by_scheduler = False
                logger.info(f"Stopped idle container {svc['container']}")
            except Exception as e:
                logger.error(f"Failed to stop {svc['container']}: {e}")

    def _sibling_is_active(self, name: str) -> bool:
        """Check if any other service sharing the same container is active."""
        svc = self.services.get(name)
        if not svc:
            return False
        container = svc["container"]
        for other_name, other_svc in self.services.items():
            if other_name == name:
                continue
            if other_svc["container"] == container:
                state = self.states.get(other_name)
                if state and state.status in ("busy", "starting", "draining"):
                    return True
        return False

    def _is_container_running(self, container_name: str) -> bool:
        if not self.docker_available:
            return False
        try:
            container = self.docker_client.containers.get(container_name)
            return container.status == "running"
        except Exception:
            return False

    async def _check_health(self, svc: dict) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"http://localhost:{svc['port']}{svc['health_endpoint']}")
                return resp.status_code < 400
        except Exception:
            return False
