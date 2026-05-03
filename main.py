"""
Gateway — GPU and service visibility + lifecycle management.

Phase 1: Read-only visibility (GPU, services, health).
Phase 2: Container lifecycle, job queue, scheduler loop.
Phase 3: Priority-based preemption, progress tracking, ntfy notifications.
Phase 4: Dynamic service registry via SQLite.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import asyncio
import logging
import os
import re
import subprocess

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import httpx

from job_queue import JobQueue
from registry import ServiceRegistry
from scheduler import Scheduler
from resources import get_vram_state, can_fit_service

# --- Logging ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gateway")

# --- Docker Client ------------------------------------------------------------

try:
    import docker

    _docker_client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
    _docker_available = True
except Exception:
    _docker_client = None
    _docker_available = False
    docker = None

# --- Auth ---------------------------------------------------------------------

API_TOKEN = os.environ.get("GATEWAY_API_TOKEN", "")
_bearer_scheme = HTTPBearer(auto_error=False)


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
):
    """Validate Bearer token. Rejects if GATEWAY_API_TOKEN is set and token doesn't match."""
    if not API_TOKEN:
        return  # No token configured — open access (local dev)
    if not credentials or credentials.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


# --- Job Queue + Registry (initialized in lifespan) --------------------------

DB_PATH = os.environ.get("QUEUE_DB_PATH", "/app/data/queue.db")
job_queue = JobQueue(db_path=DB_PATH)
registry = ServiceRegistry(db_path=DB_PATH)

# --- Lifespan -----------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure data directory exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    # Init queue and registry
    await job_queue.init()
    await registry.init()
    app.state.registry = registry

    # Start scheduler
    sched = Scheduler(
        queue=job_queue,
        registry=registry,
        docker_client=_docker_client,
        docker_available=_docker_available,
    )
    app.state.scheduler = sched
    task = asyncio.create_task(sched.start())
    logger.info("Gateway started (Phase 4 — dynamic registry)")

    yield

    # Shutdown
    await sched.stop()
    task.cancel()
    await job_queue.close()
    await registry.close()
    logger.info("Gateway stopped")


app = FastAPI(title="Gateway", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Helpers ------------------------------------------------------------------


async def _get_svc(name: str) -> dict:
    """Get service from registry or raise 404."""
    svc = await registry.get(name)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Unknown service: {name}")
    return svc


# --- GPU Status ---------------------------------------------------------------


def _safe_int(val: str, default: int = 0) -> int:
    if val in ("[N/A]", "N/A", "Not Supported", ""):
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _safe_float(val: str, default: float = 0.0) -> float:
    if val in ("[N/A]", "N/A", "Not Supported", ""):
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _get_unified_memory_mb() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def _parse_nvidia_smi_processes(output: str) -> list[dict]:
    processes = []
    pattern = re.compile(r"\|\s+\d+\s+\S+\s+\S+\s+(\d+)\s+([CGM]+)\s+(.+?)\s+(\d+)MiB\s+\|")
    for match in pattern.finditer(output):
        processes.append(
            {
                "pid": int(match.group(1)),
                "type": match.group(2),
                "name": match.group(3).strip(),
                "memory_mb": int(match.group(4)),
            }
        )
    return processes


@app.get("/gpu", dependencies=[Depends(require_auth)])
async def get_gpu_status():
    """Current GPU utilization, memory, temperature, running processes."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
                "memory.free,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "nvidia-smi returned non-zero")

        parts = [p.strip() for p in result.stdout.strip().split(",")]

        full_result = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=5)
        processes = _parse_nvidia_smi_processes(
            full_result.stdout if full_result.returncode == 0 else ""
        )

        mem_used_csv = _safe_int(parts[2])
        mem_total_csv = _safe_int(parts[3])
        mem_free_csv = _safe_int(parts[4])

        if mem_total_csv > 0:
            mem_total = mem_total_csv
            mem_used = mem_used_csv
            mem_free = mem_free_csv
        else:
            # Unified memory systems — GPU shares system RAM
            mem_total = _get_unified_memory_mb()
            mem_used = sum(p["memory_mb"] for p in processes)
            mem_free = mem_total - mem_used if mem_total > 0 else 0

        return {
            "available": True,
            "gpu_name": parts[0],
            "gpu_utilization_pct": _safe_int(parts[1]),
            "memory_used_mb": mem_used,
            "memory_total_mb": mem_total,
            "memory_free_mb": mem_free,
            "memory_utilization_pct": (
                round(mem_used / mem_total * 100, 1) if mem_total > 0 else 0
            ),
            "temperature_c": _safe_int(parts[5]),
            "power_draw_w": _safe_float(parts[6]),
            "unified_memory": mem_total_csv == 0,
            "processes": processes,
        }
    except Exception as e:
        return {
            "available": False,
            "error": str(e),
            "gpu_name": None,
            "gpu_utilization_pct": None,
            "memory_used_mb": None,
            "memory_total_mb": None,
            "memory_free_mb": None,
            "memory_utilization_pct": None,
            "temperature_c": None,
            "power_draw_w": None,
            "processes": [],
        }


# --- VRAM endpoint ------------------------------------------------------------


@app.get("/vram", dependencies=[Depends(require_auth)])
async def get_vram():
    """Current VRAM allocation state."""
    services = await registry.get_all(enabled_only=False)
    return get_vram_state(services, _docker_client, _docker_available)


# --- Docker Container Inspection ---------------------------------------------


def _get_container_info(container_name: str) -> dict:
    if not _docker_available:
        return {"exists": False, "running": False, "error": "Docker socket not available"}
    try:
        container = _docker_client.containers.get(container_name)
        return {
            "exists": True,
            "running": container.status == "running",
            "status": container.status,
        }
    except docker.errors.NotFound:
        return {"exists": False, "running": False}
    except Exception as e:
        return {"exists": False, "running": False, "error": str(e)}


# --- Service Status -----------------------------------------------------------


async def _check_service(client: httpx.AsyncClient, name: str, svc: dict) -> dict:
    container_info = _get_container_info(svc["container"])
    container_running = container_info.get("running", False)

    result = {
        "container": svc["container"],
        "type": svc["type"],
        "description": svc.get("description"),
        "vram_gb": svc["vram_gb"],
        "enabled": svc.get("enabled", True),
        "container_running": container_running,
        "healthy": False,
        "port": svc["port"],
        "response_time_ms": None,
    }

    if not container_running:
        return result

    start = asyncio.get_event_loop().time()
    try:
        resp = await client.get(f"http://localhost:{svc['port']}{svc['health_endpoint']}")
        elapsed = (asyncio.get_event_loop().time() - start) * 1000
        result["healthy"] = resp.status_code < 400
        result["response_time_ms"] = round(elapsed, 1)
    except Exception:
        pass

    return result


@app.get("/services", dependencies=[Depends(require_auth)])
async def list_services(
    type: str | None = Query(None, description="Filter by service type"),
    enabled: bool | None = Query(None, description="Filter by enabled status"),
):
    """List all registered services with their current status."""
    if type:
        svc_list = await registry.get_by_type(type, enabled_only=(enabled is True))
        all_svcs = {s["name"]: s for s in svc_list}
    elif enabled is not None:
        all_svcs = await registry.get_all(enabled_only=enabled)
    else:
        all_svcs = await registry.get_all(enabled_only=False)

    services = {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        tasks = {name: _check_service(client, name, svc) for name, svc in all_svcs.items()}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                svc = all_svcs[name]
                services[name] = {
                    "container": svc["container"],
                    "type": svc["type"],
                    "description": svc.get("description"),
                    "vram_gb": svc["vram_gb"],
                    "enabled": svc.get("enabled", True),
                    "container_running": False,
                    "healthy": False,
                    "port": svc["port"],
                    "response_time_ms": None,
                }
            else:
                services[name] = result

    # Enrich with scheduler state
    sched: Scheduler | None = getattr(app.state, "scheduler", None)
    if sched:
        for name, svc_status in services.items():
            state = sched.states.get(name)
            if state:
                svc_status["scheduler_status"] = state.status
                svc_status["active_job_id"] = state.active_job_id

    return {"services": services}


@app.get("/services/{name}", dependencies=[Depends(require_auth)])
async def get_service(name: str):
    svc = await _get_svc(name)
    async with httpx.AsyncClient(timeout=3.0) as client:
        result = await _check_service(client, name, svc)
    sched: Scheduler | None = getattr(app.state, "scheduler", None)
    if sched and name in sched.states:
        state = sched.states[name]
        result["scheduler_status"] = state.status
        result["active_job_id"] = state.active_job_id
    return result


# --- Service Registry CRUD ---------------------------------------------------


@app.post("/services/register", dependencies=[Depends(require_auth)])
async def register_service(body: dict):
    """Register a new service."""
    for field in ("name", "container", "port", "type"):
        if not body.get(field):
            raise HTTPException(status_code=400, detail=f"'{field}' is required")

    try:
        svc = await registry.register(body)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return svc


@app.put("/services/{name}", dependencies=[Depends(require_auth)])
async def update_service(name: str, body: dict):
    """Update an existing service's configuration."""
    result = await registry.update(name, body)
    if not result:
        raise HTTPException(status_code=404, detail=f"Unknown service: {name}")
    return result


@app.delete("/services/{name}", dependencies=[Depends(require_auth)])
async def delete_service(name: str):
    """Remove a service from the registry."""
    await _get_svc(name)

    # Check for active jobs
    sched: Scheduler | None = getattr(app.state, "scheduler", None)
    if sched:
        state = sched.states.get(name)
        if state and state.active_job_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "service_has_active_jobs",
                    "hint": "Stop the service and complete/cancel its jobs first",
                },
            )

    deleted = await registry.delete(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Service not found: {name}")

    # Clean up scheduler state
    if sched and name in sched.states:
        del sched.states[name]

    return {"deleted": name}


@app.post("/services/{name}/enable", dependencies=[Depends(require_auth)])
async def enable_service(name: str):
    """Enable a disabled service (scheduler will resume managing it)."""
    result = await registry.set_enabled(name, True)
    if not result:
        raise HTTPException(status_code=404, detail=f"Unknown service: {name}")
    return result


@app.post("/services/{name}/disable", dependencies=[Depends(require_auth)])
async def disable_service(name: str):
    """Disable a service (stays registered but scheduler ignores it)."""
    result = await registry.set_enabled(name, False)
    if not result:
        raise HTTPException(status_code=404, detail=f"Unknown service: {name}")
    return result


# --- Service Lifecycle --------------------------------------------------------


@app.post("/services/{name}/start", dependencies=[Depends(require_auth)])
async def start_service(name: str):
    """Start a service container."""
    svc = await _get_svc(name)
    if not _docker_available:
        raise HTTPException(status_code=503, detail="Docker not available")

    # Check if container exists
    try:
        container = _docker_client.containers.get(svc["container"])
    except docker.errors.NotFound:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "container_not_found",
                "hint": f"Container '{svc['container']}' does not exist. "
                "Create it with docker-compose first.",
            },
        )

    # Already running?
    if container.status == "running":
        return {"service": name, "status": "already_running", "healthy": True}

    # VRAM check
    services = await registry.get_all(enabled_only=False)
    can_fit, details = can_fit_service(name, services, _docker_client, _docker_available)
    if not can_fit:
        raise HTTPException(status_code=409, detail=details)

    # Start
    start_time = asyncio.get_event_loop().time()
    container.start()
    logger.info(f"Starting container {svc['container']}")

    # Update scheduler state
    sched: Scheduler | None = getattr(app.state, "scheduler", None)
    if sched:
        if name not in sched.states:
            from scheduler import ServiceState

            sched.states[name] = ServiceState()
        sched.states[name].status = "starting"
        sched.states[name].startup_requested = True

    # Wait for health check
    healthy = False
    async with httpx.AsyncClient(timeout=3.0) as client:
        for _ in range(60):  # 60 * 2s = 120s
            await asyncio.sleep(2)
            try:
                resp = await client.get(f"http://localhost:{svc['port']}{svc['health_endpoint']}")
                if resp.status_code < 400:
                    healthy = True
                    break
            except Exception:
                pass

    elapsed = asyncio.get_event_loop().time() - start_time

    # Update scheduler state
    if sched and name in sched.states:
        sched.states[name].startup_requested = False
        sched.states[name].status = "idle" if healthy else "starting"
        if healthy:
            sched.states[name].last_activity = datetime.now(timezone.utc)

    result = {
        "service": name,
        "status": "started",
        "startup_time_s": round(elapsed, 1),
        "healthy": healthy,
    }
    if not healthy:
        result["warning"] = "Health check timed out after 120s"
    return result


@app.post("/services/{name}/stop", dependencies=[Depends(require_auth)])
async def stop_service(name: str, drain: bool = True):
    """Stop a service container."""
    svc = await _get_svc(name)
    if not _docker_available:
        raise HTTPException(status_code=503, detail="Docker not available")

    try:
        container = _docker_client.containers.get(svc["container"])
    except docker.errors.NotFound:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "container_not_found",
                "hint": f"Container '{svc['container']}' does not exist.",
            },
        )

    if container.status != "running":
        return {"service": name, "status": "already_stopped"}

    sched: Scheduler | None = getattr(app.state, "scheduler", None)
    drained = False

    if drain and sched and name in sched.states:
        state = sched.states[name]
        if state.active_job_id:
            state.status = "draining"
            logger.info(f"Draining {name} (active job: {state.active_job_id[:8]})")
            # Wait up to 30s for active job to complete
            for _ in range(30):
                await asyncio.sleep(1)
                if not state.active_job_id:
                    break
            drained = True

    # Stop container
    container.stop(timeout=10)
    logger.info(f"Stopped container {svc['container']}")

    if sched and name in sched.states:
        sched.states[name].status = "idle"
        sched.states[name].active_job_id = None
        sched.states[name].last_activity = None

    return {"service": name, "status": "stopped", "drained": drained}


@app.post("/services/{name}/restart", dependencies=[Depends(require_auth)])
async def restart_service(name: str):
    """Restart a service container (stop with drain, then start)."""
    # Stop first
    stop_result = await stop_service(name, drain=True)

    # Give it a moment
    await asyncio.sleep(1)

    # Start
    start_result = await start_service(name)
    start_result["previous_status"] = stop_result["status"]
    start_result["drained"] = stop_result.get("drained", False)
    return start_result


# --- Queue API ----------------------------------------------------------------


@app.post("/queue/submit", dependencies=[Depends(require_auth)])
async def submit_job(body: dict):
    """Submit a job to the queue."""
    service_type = body.get("service_type")
    if not service_type:
        raise HTTPException(status_code=400, detail="service_type is required")

    payload = body.get("payload")
    if not payload:
        raise HTTPException(status_code=400, detail="payload is required")

    job_id = await job_queue.submit(
        service_type=service_type,
        payload=payload,
        priority=body.get("priority", 3),
        callback_url=body.get("callback_url"),
        service_name=body.get("service_name"),
        ttl_seconds=body.get("ttl_seconds", 3600),
    )

    position = await job_queue.get_position(job_id)

    return {
        "job_id": job_id,
        "position": position,
        "estimated_wait": "depends on service availability",
    }


@app.get("/queue/stats", dependencies=[Depends(require_auth)])
async def queue_stats():
    """Queue depth by type and priority."""
    return await job_queue.get_stats()


@app.get("/queue/{job_id}", dependencies=[Depends(require_auth)])
async def get_job(job_id: str):
    """Get job details."""
    job = await job_queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    result = dict(job)
    if result["status"] == "queued":
        result["position"] = await job_queue.get_position(job_id)
    return result


@app.delete("/queue/{job_id}", dependencies=[Depends(require_auth)])
async def cancel_job(job_id: str):
    """Cancel a queued job."""
    job = await job_queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if job["status"] == "running":
        raise HTTPException(
            status_code=409,
            detail="Cannot cancel a running job — use /preempt/execute to preempt it.",
        )
    if job["status"] not in ("queued", "paused"):
        raise HTTPException(
            status_code=409,
            detail=f"Job is already {job['status']}",
        )

    cancelled = await job_queue.cancel_job(job_id)
    return {"job_id": job_id, "cancelled": cancelled}


# --- Preemption API -----------------------------------------------------------


@app.get("/preempt/check/{name}", dependencies=[Depends(require_auth)])
async def preempt_check(name: str):
    """Check if a service can be preempted (dry run)."""
    svc = await _get_svc(name)

    sched: Scheduler | None = getattr(app.state, "scheduler", None)
    if not sched:
        raise HTTPException(status_code=503, detail="Scheduler not running")

    result = sched.check_preemption(name)

    # If preemptable, enrich with job and queue info
    if result.get("preemptable") and result.get("active_job_id"):
        running_job = await job_queue.get_job(result["active_job_id"])
        if running_job:
            result["running_job_priority"] = running_job["priority"]
            result["running_job_service_type"] = running_job["service_type"]
        # Show highest queued job that could preempt
        queued = await job_queue.get_highest_queued(svc["type"])
        if queued:
            result["highest_queued"] = {
                "job_id": queued["id"],
                "priority": queued["priority"],
                "service_type": queued["service_type"],
            }

    return result


@app.post("/preempt/execute", dependencies=[Depends(require_auth)])
async def preempt_execute(body: dict):
    """Execute preemption: pause the running job, freeing the service."""
    service_name = body.get("service_name")
    preemptor_job_id = body.get("preemptor_job_id")

    if not service_name:
        raise HTTPException(status_code=400, detail="service_name is required")
    await _get_svc(service_name)  # validate exists
    if not preemptor_job_id:
        raise HTTPException(status_code=400, detail="preemptor_job_id is required")

    sched: Scheduler | None = getattr(app.state, "scheduler", None)
    if not sched:
        raise HTTPException(status_code=503, detail="Scheduler not running")

    # Verify preemptor exists and is queued
    preemptor = await job_queue.get_job(preemptor_job_id)
    if not preemptor:
        raise HTTPException(status_code=404, detail=f"Preemptor job not found: {preemptor_job_id}")
    if preemptor["status"] != "queued":
        raise HTTPException(
            status_code=409,
            detail=f"Preemptor job must be queued, is {preemptor['status']}",
        )

    result = await sched.preempt_service(service_name, preemptor_job_id)
    if "error" in result:
        raise HTTPException(status_code=409, detail=result)
    return result


@app.post("/preempt/release/{name}", dependencies=[Depends(require_auth)])
async def preempt_release(name: str):
    """Release a preempted service — resume paused jobs immediately."""
    svc = await _get_svc(name)

    sched: Scheduler | None = getattr(app.state, "scheduler", None)
    if not sched:
        raise HTTPException(status_code=503, detail="Scheduler not running")

    # Find paused jobs for this service type
    paused = await job_queue.get_paused_jobs()
    resumed = []
    for pjob in paused:
        if pjob["service_type"] == svc["type"]:
            if pjob.get("progress_snapshot"):
                sched.progress.restore(pjob["id"], pjob["progress_snapshot"])
            await job_queue.resume_job(pjob["id"])
            resumed.append(pjob["id"])

    return {
        "service": name,
        "resumed_jobs": resumed,
        "count": len(resumed),
    }


# --- Progress API -------------------------------------------------------------


@app.get("/queue/{job_id}/progress", dependencies=[Depends(require_auth)])
async def get_job_progress(job_id: str):
    """Get progress for a running/paused job."""
    job = await job_queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    sched: Scheduler | None = getattr(app.state, "scheduler", None)
    progress = None
    if sched:
        progress = sched.progress.get(job_id)

    # If paused with snapshot, return the snapshot
    if not progress and job.get("progress_snapshot"):
        import json

        try:
            progress = json.loads(job["progress_snapshot"])
        except Exception:
            pass

    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": progress,
        "preempt_count": job.get("preempt_count", 0),
    }


@app.post("/queue/{job_id}/progress", dependencies=[Depends(require_auth)])
async def push_job_progress(job_id: str, body: dict):
    """Push progress update for a job (queue mode — service pushes to gateway)."""
    job = await job_queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if job["status"] != "running":
        raise HTTPException(status_code=409, detail=f"Job is {job['status']}, not running")

    sched: Scheduler | None = getattr(app.state, "scheduler", None)
    if sched:
        sched.progress.update(job_id, body)

    return {"ok": True}


# --- Availability Endpoint ----------------------------------------------------


@app.get("/available/{service_type}", dependencies=[Depends(require_auth)])
async def check_availability(service_type: str, mode: str = "waterfall"):
    """
    Check if a service type is available.
    Returns a recommendation: 'use_local', 'use_cloud', or 'queue'.
    """
    # Find enabled services of this type
    services = await registry.get_by_type(service_type, enabled_only=True)
    if not services:
        return {
            "available": False,
            "service": service_type,
            "reason": f"No enabled services of type '{service_type}'",
            "gpu_memory_free_mb": 0,
            "gpu_utilization_pct": 0,
            "recommendation": "use_cloud",
        }

    # Check if any service is healthy
    healthy_services = []
    async with httpx.AsyncClient(timeout=3.0) as client:
        for svc in services:
            try:
                resp = await client.get(f"http://localhost:{svc['port']}{svc['health_endpoint']}")
                if resp.status_code < 400:
                    healthy_services.append(svc)
            except Exception:
                pass

    if not healthy_services:
        # Check scheduler — maybe service just needs starting
        sched: Scheduler | None = getattr(app.state, "scheduler", None)
        has_queued = False
        if sched:
            for svc in services:
                state = sched.states.get(svc["name"])
                if state and state.active_job_id:
                    has_queued = True
                    break

        return {
            "available": False,
            "service": service_type,
            "services_registered": len(services),
            "reason": "No healthy services" + (" (jobs queued)" if has_queued else ""),
            "gpu_memory_free_mb": 0,
            "gpu_utilization_pct": 0,
            "recommendation": "queue" if has_queued else "use_cloud",
        }

    # Check GPU load
    gpu_info = await get_gpu_status()
    gpu_util = gpu_info.get("gpu_utilization_pct") or 0
    gpu_free = gpu_info.get("memory_free_mb") or 0

    # Check queue depth
    queue_depth = 0
    sched = getattr(app.state, "scheduler", None)
    if sched:
        for svc in healthy_services:
            state = sched.states.get(svc["name"])
            if state and state.active_job_id:
                queue_depth += 1

    # Recommend based on load
    if queue_depth > 0:
        recommendation = "queue"
    elif gpu_util > 90:
        recommendation = "queue"
    else:
        recommendation = "use_local"

    return {
        "available": True,
        "service": service_type,
        "healthy_services": len(healthy_services),
        "gpu_memory_free_mb": gpu_free,
        "gpu_utilization_pct": gpu_util,
        "queue_depth": queue_depth,
        "recommendation": recommendation,
    }


# --- Health -------------------------------------------------------------------


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
