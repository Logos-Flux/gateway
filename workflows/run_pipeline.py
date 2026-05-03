"""
Image Processing Pipeline — ComfyUI API client.

Submit images through a togglable processing pipeline:
  - Background removal  (RMBG-2.0 / BEN2 / INSPYRENET) + alpha strip
  - SAM2 segmentation   (text-prompted, sam2.1_hiera_large)
  - Upscaling           (4x-UltraSharp ESRGAN)
  - SeedVR2 enhance     (diffusion-based restoration/upscale)
  - Color correction     (temperature, hue, brightness, contrast, saturation, gamma)

Usage:
    python run_pipeline.py input.png [options]

    --no-rmbg           Disable background removal (default: on)
    --rmbg-model MODEL  RMBG model: RMBG-2.0, BEN2, INSPYRENET (default: RMBG-2.0)
    --sam PROMPT        Enable SAM2 segmentation with text prompt (default: off)
    --sam-model MODEL   SAM2 model: sam2.1_hiera_large, sam2.1_hiera_base_plus
    --no-upscale        Disable 4x-UltraSharp upscale (default: on)
    --seedvr2           Enable SeedVR2 diffusion enhance (default: off, slower)
    --seedvr2-res RES   SeedVR2 target resolution (default: 2160)
    --no-color          Disable color correction (default: on)
    --temperature T     Color temperature -100..100 (default: 0)
    --hue H             Hue shift -90..90 (default: 0)
    --brightness B      Brightness -100..100 (default: 0)
    --contrast C        Contrast -100..100 (default: 0)
    --saturation S      Saturation -100..100 (default: 0)
    --gamma G           Gamma 0.2..2.2 (default: 1.0)
    --output PREFIX     Output filename prefix (default: pipeline_output)
    --comfyui URL       ComfyUI base URL (default: http://localhost:8188)
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.parse


COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://localhost:8188")


def load_workflow():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "image_processing_pipeline.json")) as f:
        return json.load(f)


def upload_image(filepath: str, base_url: str) -> str:
    """Upload an image to ComfyUI and return the server filename."""
    filename = os.path.basename(filepath)
    with open(filepath, "rb") as f:
        image_data = f.read()

    boundary = "----PipelineBoundary"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + image_data
        + f"\r\n--{boundary}--\r\n".encode()
    )

    req = urllib.request.Request(
        f"{base_url}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    return result["name"]


def queue_prompt(workflow: dict, base_url: str) -> str:
    """Queue a prompt and return the prompt_id."""
    data = json.dumps({"prompt": workflow["prompt"]}).encode()
    req = urllib.request.Request(
        f"{base_url}/prompt",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    return result["prompt_id"]


def poll_until_done(prompt_id: str, base_url: str, timeout: int = 600) -> dict:
    """Poll /history until the prompt completes."""
    start = time.time()
    while time.time() - start < timeout:
        resp = urllib.request.urlopen(f"{base_url}/history/{prompt_id}")
        history = json.loads(resp.read())
        if prompt_id in history:
            return history[prompt_id]
        time.sleep(2)
    raise TimeoutError(f"Prompt {prompt_id} did not complete within {timeout}s")


def build_pipeline(args) -> dict:
    """Load workflow and apply CLI arguments."""
    wf = load_workflow()
    p = wf["prompt"]

    # Input image
    p["1"]["inputs"]["image"] = args.image_name

    # --- Background Removal (node 10/11) ---
    p["11"]["inputs"]["boolean"] = args.rmbg
    if args.rmbg:
        p["10"]["inputs"]["model"] = args.rmbg_model
        p["10"]["inputs"]["background_color"] = args.bg_color

    # --- SAM2 Segmentation (node 20/21) ---
    p["21"]["inputs"]["boolean"] = args.sam is not None
    if args.sam:
        p["20"]["inputs"]["prompt"] = args.sam
        p["20"]["inputs"]["sam2_model"] = args.sam_model

    # --- 4x-UltraSharp Upscale (node 30-32) ---
    p["32"]["inputs"]["boolean"] = args.upscale

    # --- SeedVR2 Enhance (node 40-43) ---
    p["43"]["inputs"]["boolean"] = args.seedvr2
    if args.seedvr2:
        p["42"]["inputs"]["resolution"] = args.seedvr2_res

    # --- Color Correction (node 50/51) ---
    p["51"]["inputs"]["boolean"] = args.color
    if args.color:
        p["50"]["inputs"]["temperature"] = args.temperature
        p["50"]["inputs"]["hue"] = args.hue
        p["50"]["inputs"]["brightness"] = args.brightness
        p["50"]["inputs"]["contrast"] = args.contrast
        p["50"]["inputs"]["saturation"] = args.saturation
        p["50"]["inputs"]["gamma"] = args.gamma

    # Output prefix
    p["90"]["inputs"]["filename_prefix"] = args.output

    return wf


def main():
    parser = argparse.ArgumentParser(description="Image Processing Pipeline")
    parser.add_argument("image", help="Input image file path")

    # Toggle groups
    parser.add_argument("--no-rmbg", dest="rmbg", action="store_false", default=True)
    parser.add_argument(
        "--rmbg-model", default="RMBG-2.0", choices=["RMBG-2.0", "BEN2", "INSPYRENET", "BEN"]
    )
    parser.add_argument(
        "--bg-color",
        default="#FFFFFF",
        help="Background color after removal (default: #FFFFFF white)",
    )
    parser.add_argument(
        "--sam",
        default=None,
        metavar="PROMPT",
        help="Enable SAM2 segmentation with this text prompt",
    )
    parser.add_argument(
        "--sam-model",
        default="sam2.1_hiera_large",
        choices=["sam2.1_hiera_large", "sam2.1_hiera_base_plus"],
    )
    parser.add_argument("--no-upscale", dest="upscale", action="store_false", default=True)
    parser.add_argument("--seedvr2", action="store_true", default=False)
    parser.add_argument("--seedvr2-res", type=int, default=2160)
    parser.add_argument("--no-color", dest="color", action="store_false", default=True)
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--hue", type=float, default=0)
    parser.add_argument("--brightness", type=float, default=0)
    parser.add_argument("--contrast", type=float, default=0)
    parser.add_argument("--saturation", type=float, default=0)
    parser.add_argument("--gamma", type=float, default=1.0)
    parser.add_argument("--output", default="pipeline_output")
    parser.add_argument("--comfyui", default=COMFYUI_URL)

    args = parser.parse_args()

    if not os.path.isfile(args.image):
        print(f"Error: {args.image} not found", file=sys.stderr)
        sys.exit(1)

    base_url = args.comfyui

    # 1. Upload image
    print(f"Uploading {args.image}...")
    args.image_name = upload_image(args.image, base_url)
    print(f"  -> {args.image_name}")

    # 2. Build workflow
    wf = build_pipeline(args)

    steps = []
    if args.rmbg:
        steps.append(f"RMBG ({args.rmbg_model})")
    if args.sam:
        steps.append(f'SAM2 ("{args.sam}")')
    if args.upscale:
        steps.append("4x-UltraSharp")
    if args.seedvr2:
        steps.append(f"SeedVR2 ({args.seedvr2_res}p)")
    if args.color:
        steps.append("ColorCorrect")
    print(f"Pipeline: {' -> '.join(steps) if steps else '(passthrough)'}")

    # 3. Queue
    print("Queuing prompt...")
    prompt_id = queue_prompt(wf, base_url)
    print(f"  -> prompt_id: {prompt_id}")

    # 4. Wait
    print("Processing...", end="", flush=True)
    start = time.time()
    result = poll_until_done(prompt_id, base_url)
    elapsed = time.time() - start
    print(f" done ({elapsed:.1f}s)")

    # 5. Report outputs
    outputs = result.get("outputs", {})
    save_node = outputs.get("90", {})
    images = save_node.get("images", [])
    for img in images:
        print(
            f"Output: {base_url}/view?filename={img['filename']}&subfolder={img.get('subfolder', '')}&type={img.get('type', 'output')}"
        )

    if not images:
        print("Warning: no output images found")
        if result.get("status", {}).get("status_str") == "error":
            print(f"Error: {json.dumps(result.get('status', {}), indent=2)}")


if __name__ == "__main__":
    main()
