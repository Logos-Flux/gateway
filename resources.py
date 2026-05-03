"""Resource accounting — GPU memory and system resources."""

import logging
import subprocess
import re

logger = logging.getLogger(__name__)


def _safe_int(val: str, default: int = 0) -> int:
    if val in ("[N/A]", "N/A", "Not Supported", ""):
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _get_total_memory_mb() -> int:
    """Read total system memory from /proc/meminfo (unified with GPU on some systems)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def _get_gpu_memory_mb() -> tuple[int, int, int]:
    """Try CSV query for discrete GPU memory. Returns (used, total, free)."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            parts = [p.strip() for p in result.stdout.strip().split(",")]
            used = _safe_int(parts[0])
            total = _safe_int(parts[1])
            free = _safe_int(parts[2])
            return used, total, free
    except Exception:
        pass
    return 0, 0, 0


def _get_gpu_process_memory_mb() -> int:
    """Sum GPU process memory from full nvidia-smi output."""
    try:
        result = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return 0
        pattern = re.compile(r"\|\s+\d+\s+\S+\s+\S+\s+\d+\s+[CGM]+\s+.+?\s+(\d+)MiB\s+\|")
        return sum(int(m.group(1)) for m in pattern.finditer(result.stdout))
    except Exception:
        return 0


def get_vram_state(services: dict, docker_client, docker_available: bool) -> dict:
    """
    Current VRAM allocation picture.

    Uses nvidia-smi for actual memory usage and cross-references with
    service registry declared vram_gb values.
    """
    _, csv_total, _ = _get_gpu_memory_mb()

    if csv_total > 0:
        # Discrete GPU
        used_csv, total, free_csv = _get_gpu_memory_mb()
        actual_used = used_csv
        actual_free = free_csv
    else:
        # Unified memory systems
        total = _get_total_memory_mb()
        actual_used = _get_gpu_process_memory_mb()
        actual_free = total - actual_used if total > 0 else 0

    # Build per-service allocation list.
    # Deduplicate by container name — if multiple services share a container,
    # only count the highest VRAM reservation once.
    allocated = []
    container_vram: dict[str, int] = {}
    for name, svc in services.items():
        running = False
        if docker_available and docker_client:
            try:
                container = docker_client.containers.get(svc["container"])
                running = container.status == "running"
            except Exception:
                pass
        svc_vram_mb = svc["vram_gb"] * 1024
        allocated.append(
            {
                "service": name,
                "vram_gb": svc["vram_gb"],
                "vram_mb": svc_vram_mb,
                "container_running": running,
            }
        )
        if running and svc["vram_gb"] > 0:
            container_vram[svc["container"]] = max(
                container_vram.get(svc["container"], 0), svc_vram_mb
            )

    reserved_mb = sum(container_vram.values())

    return {
        "total_mb": total,
        "used_mb": actual_used,
        "free_mb": actual_free,
        "allocated_services": allocated,
        "reserved_mb": reserved_mb,
        "available_for_new_mb": actual_free,
    }


def can_fit_service(
    service_name: str,
    services: dict,
    docker_client,
    docker_available: bool,
) -> tuple[bool, dict]:
    """
    Check if a service can be started given current VRAM.

    Returns (can_fit, details). Details includes what would need to stop
    if it can't fit.
    """
    if service_name not in services:
        return False, {"error": f"Unknown service: {service_name}"}

    svc = services[service_name]
    required_mb = svc["vram_gb"] * 1024

    # Zero-VRAM services always fit
    if required_mb == 0:
        return True, {"required_mb": 0, "available_mb": 0, "note": "No VRAM needed"}

    # If the container is already running (shared with another service),
    # no additional VRAM is needed
    if docker_available and docker_client:
        try:
            existing = docker_client.containers.get(svc["container"])
            if existing.status == "running":
                return True, {
                    "required_mb": 0,
                    "available_mb": 0,
                    "note": f"Container '{svc['container']}' already running",
                }
        except Exception:
            pass

    state = get_vram_state(services, docker_client, docker_available)
    available = state["available_for_new_mb"]

    if available >= required_mb:
        return True, {
            "required_mb": required_mb,
            "available_mb": available,
        }

    # Can't fit — figure out what's using VRAM
    running_gpu_services = [
        s for s in state["allocated_services"] if s["container_running"] and s["vram_gb"] > 0
    ]

    return False, {
        "error": "insufficient_vram",
        "required_mb": required_mb,
        "available_mb": available,
        "running_services": [s["service"] for s in running_gpu_services],
        "running_vram_mb": sum(s["vram_mb"] for s in running_gpu_services),
    }
