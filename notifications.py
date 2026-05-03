"""Push notifications via ntfy.sh for job lifecycle events."""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

NTFY_ENABLED = os.environ.get("NTFY_ENABLED", "false").lower() in ("true", "1", "yes")
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "gateway")


async def _send(title: str, message: str, priority: str = "default", tags: str = ""):
    """Fire-and-forget notification."""
    if not NTFY_ENABLED:
        return
    try:
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = tags
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{NTFY_URL}/{NTFY_TOPIC}",
                content=message,
                headers=headers,
            )
            if resp.status_code < 300:
                logger.debug(f"ntfy sent: {title}")
            else:
                logger.warning(f"ntfy failed ({resp.status_code}): {title}")
    except Exception as e:
        logger.warning(f"ntfy error: {e}")


async def job_completed(job_id: str, service_name: str):
    await _send(
        title=f"Job completed on {service_name}",
        message=f"Job {job_id[:8]} completed successfully.",
        tags="white_check_mark",
    )


async def job_failed(job_id: str, service_name: str, error: str):
    await _send(
        title=f"Job failed on {service_name}",
        message=f"Job {job_id[:8]} failed: {error}",
        priority="high",
        tags="x",
    )


async def job_preempted(preempted_job_id: str, by_job_id: str, service_name: str):
    await _send(
        title=f"Job preempted on {service_name}",
        message=(
            f"Job {preempted_job_id[:8]} paused — preempted by higher-priority job {by_job_id[:8]}."
        ),
        priority="high",
        tags="warning",
    )


async def job_resumed(job_id: str, service_name: str):
    await _send(
        title=f"Job resumed on {service_name}",
        message=f"Paused job {job_id[:8]} resumed on {service_name}.",
        tags="arrow_forward",
    )
