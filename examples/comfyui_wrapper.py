"""
ComfyUI Wrapper — HTTP bridge between the Gateway scheduler and ComfyUI API.

Translates job payloads from the scheduler into ComfyUI workflow submissions,
polls for completion, and returns processed image info.

Endpoints:
  POST /process     — download input image, run pipeline, return output info
  GET  /health      — proxy to ComfyUI health
  GET  /output/{fn} — serve processed images from ComfyUI output dir

Usage:
  uvicorn comfyui_wrapper:app --host 0.0.0.0 --port 8189
"""

import json
import logging
import os
import time
import tempfile
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

# --- Config -------------------------------------------------------------------

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://localhost:8188")
COMFYUI_OUTPUT_DIR = os.environ.get("COMFYUI_OUTPUT_DIR", "./output")
POLL_INTERVAL = 2  # seconds
DEFAULT_TIMEOUT = 600  # 10 minutes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("comfyui-wrapper")


# --- Pydantic models ---------------------------------------------------------


class ProcessingSteps(BaseModel):
    rmbg: bool = True
    rmbg_model: str = "RMBG-2.0"
    sam: str | None = None
    sam_model: str = "sam2.1_hiera_large"
    upscale: bool = True
    seedvr2: bool = False
    seedvr2_res: int = 2160
    color: bool = True
    temperature: float = 0
    hue: float = 0
    brightness: float = 0
    contrast: float = 0
    saturation: float = 0
    gamma: float = 1.0


class ProcessRequest(BaseModel):
    input_image_url: str
    steps: ProcessingSteps = ProcessingSteps()
    output_prefix: str = "i2i_output"


class OutputImage(BaseModel):
    filename: str
    url: str
    size_bytes: int


class ProcessResponse(BaseModel):
    success: bool
    output_images: list[OutputImage] = []
    steps_executed: list[str] = []
    duration_ms: int = 0
    prompt_id: str = ""
    error: str | None = None


# --- Workflow template --------------------------------------------------------

WORKFLOW_DIR = Path(__file__).parent.parent / "workflows"


def load_workflow() -> dict:
    with open(WORKFLOW_DIR / "image_processing_pipeline.json") as f:
        return json.load(f)


def build_pipeline(image_name: str, steps: ProcessingSteps, output_prefix: str) -> dict:
    """Build ComfyUI workflow from template + step config."""
    wf = load_workflow()
    p = wf["prompt"]

    # Input image
    p["1"]["inputs"]["image"] = image_name

    # Background Removal (node 10/11)
    p["11"]["inputs"]["boolean"] = steps.rmbg
    if steps.rmbg:
        p["10"]["inputs"]["model"] = steps.rmbg_model

    # SAM2 Segmentation (node 20/21)
    p["21"]["inputs"]["boolean"] = steps.sam is not None
    if steps.sam:
        p["20"]["inputs"]["prompt"] = steps.sam
        p["20"]["inputs"]["sam2_model"] = steps.sam_model

    # 4x-UltraSharp Upscale (node 30-32)
    p["32"]["inputs"]["boolean"] = steps.upscale

    # SeedVR2 Enhance (node 40-43)
    p["43"]["inputs"]["boolean"] = steps.seedvr2
    if steps.seedvr2:
        p["42"]["inputs"]["resolution"] = steps.seedvr2_res

    # Color Correction (node 50/51)
    p["51"]["inputs"]["boolean"] = steps.color
    if steps.color:
        p["50"]["inputs"]["temperature"] = steps.temperature
        p["50"]["inputs"]["hue"] = steps.hue
        p["50"]["inputs"]["brightness"] = steps.brightness
        p["50"]["inputs"]["contrast"] = steps.contrast
        p["50"]["inputs"]["saturation"] = steps.saturation
        p["50"]["inputs"]["gamma"] = steps.gamma

    # Output prefix
    p["90"]["inputs"]["filename_prefix"] = output_prefix

    return wf


# --- App ----------------------------------------------------------------------

app = FastAPI(title="ComfyUI Wrapper", version="0.1.0")


@app.get("/health")
async def health():
    """Proxy health check to ComfyUI."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{COMFYUI_URL}/system_stats")
            if resp.status_code < 400:
                return {"status": "ok", "comfyui": "reachable"}
    except Exception as e:
        return {"status": "degraded", "comfyui": "unreachable", "error": str(e)}
    return {"status": "degraded", "comfyui": f"status {resp.status_code}"}


@app.get("/output/{filename}")
async def serve_output(filename: str):
    """Serve a processed image from ComfyUI output directory."""
    # Prevent path traversal
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    filepath = Path(COMFYUI_OUTPUT_DIR) / filename
    if not filepath.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    return FileResponse(filepath)


@app.post("/process", response_model=ProcessResponse)
async def process_image(req: ProcessRequest):
    """
    Download input image, run through ComfyUI pipeline, return output info.

    1. Download input image from URL -> temp file
    2. Upload to ComfyUI via /upload/image
    3. Build workflow JSON with toggle nodes set
    4. Queue via /prompt
    5. Poll /history/{prompt_id} until done
    6. Return output image info
    """
    start = time.time()
    steps = req.steps
    steps_executed: list[str] = []

    # Track which steps are enabled
    if steps.rmbg:
        steps_executed.append("rmbg")
    if steps.sam is not None:
        steps_executed.append("sam")
    if steps.upscale:
        steps_executed.append("upscale")
    if steps.seedvr2:
        steps_executed.append("seedvr2")
    if steps.color:
        steps_executed.append("color")

    timeout = DEFAULT_TIMEOUT
    if steps.seedvr2:
        timeout = 900  # 15 min for SeedVR2

    logger.info(
        f"Processing: url={req.input_image_url[:80]}... "
        f"steps={steps_executed} prefix={req.output_prefix}"
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Download input image
        try:
            dl_resp = await client.get(req.input_image_url, follow_redirects=True)
            dl_resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to download input image: {e}")
            return ProcessResponse(
                success=False,
                error=f"Failed to download input image: {e}",
                duration_ms=_elapsed_ms(start),
            )

        # Write to temp file
        suffix = _guess_extension(req.input_image_url, dl_resp.headers.get("content-type", ""))
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(dl_resp.content)
            tmp_path = tmp.name

        try:
            # 2. Upload to ComfyUI
            filename = os.path.basename(tmp_path)
            boundary = "----ComfyUIWrapperBoundary"
            with open(tmp_path, "rb") as f:
                image_data = f.read()

            body = (
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
                    f"Content-Type: application/octet-stream\r\n\r\n"
                ).encode()
                + image_data
                + f"\r\n--{boundary}--\r\n".encode()
            )

            upload_resp = await client.post(
                f"{COMFYUI_URL}/upload/image",
                content=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            upload_resp.raise_for_status()
            upload_result = upload_resp.json()
            image_name = upload_result["name"]
            logger.info(f"Uploaded as: {image_name}")

            # 3. Build workflow
            wf = build_pipeline(image_name, steps, req.output_prefix)

            # 4. Queue prompt
            queue_resp = await client.post(
                f"{COMFYUI_URL}/prompt",
                json={"prompt": wf["prompt"]},
            )
            queue_resp.raise_for_status()
            prompt_id = queue_resp.json()["prompt_id"]
            logger.info(f"Queued prompt: {prompt_id}")

            # 5. Poll for completion
            poll_start = time.time()
            history = None
            while time.time() - poll_start < timeout:
                await _async_sleep(POLL_INTERVAL)
                hist_resp = await client.get(f"{COMFYUI_URL}/history/{prompt_id}")
                if hist_resp.status_code == 200:
                    hist_data = hist_resp.json()
                    if prompt_id in hist_data:
                        history = hist_data[prompt_id]
                        break

            if history is None:
                return ProcessResponse(
                    success=False,
                    error=f"Prompt {prompt_id} did not complete within {timeout}s",
                    prompt_id=prompt_id,
                    steps_executed=steps_executed,
                    duration_ms=_elapsed_ms(start),
                )

            # Check for errors in history
            status_str = history.get("status", {}).get("status_str", "")
            if status_str == "error":
                error_msgs = history.get("status", {}).get("messages", [])
                return ProcessResponse(
                    success=False,
                    error=f"ComfyUI error: {json.dumps(error_msgs)}",
                    prompt_id=prompt_id,
                    steps_executed=steps_executed,
                    duration_ms=_elapsed_ms(start),
                )

            # 6. Collect output images
            outputs = history.get("outputs", {})
            save_node = outputs.get("90", {})
            images = save_node.get("images", [])

            output_images: list[OutputImage] = []
            for img in images:
                fn = img["filename"]
                subfolder = img.get("subfolder", "")
                file_path = Path(COMFYUI_OUTPUT_DIR)
                if subfolder:
                    file_path = file_path / subfolder
                file_path = file_path / fn

                size = file_path.stat().st_size if file_path.is_file() else 0
                output_images.append(
                    OutputImage(
                        filename=fn,
                        url=f"/output/{fn}",
                        size_bytes=size,
                    )
                )

            logger.info(
                f"Done: {len(output_images)} output(s), "
                f"{_elapsed_ms(start)}ms, steps={steps_executed}"
            )

            return ProcessResponse(
                success=True,
                output_images=output_images,
                steps_executed=steps_executed,
                duration_ms=_elapsed_ms(start),
                prompt_id=prompt_id,
            )

        finally:
            # Clean up temp file
            os.unlink(tmp_path)


# --- Helpers ------------------------------------------------------------------

import asyncio  # noqa: E402  (sectioned import, kept with helpers)


async def _async_sleep(seconds: float):
    await asyncio.sleep(seconds)


def _elapsed_ms(start: float) -> int:
    return int((time.time() - start) * 1000)


def _guess_extension(url: str, content_type: str) -> str:
    """Guess file extension from URL or content-type."""
    url_lower = url.lower().split("?")[0]
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"):
        if url_lower.endswith(ext):
            return ext

    ct = content_type.lower()
    if "png" in ct:
        return ".png"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "webp" in ct:
        return ".webp"

    return ".png"  # default
