# Image Processing Pipeline

Togglable ComfyUI workflow for image processing via API.

## Steps (all togglable)

| Step | Default | Node | Notes |
|------|---------|------|-------|
| Background Removal | ON | RMBG-2.0 | Also supports BEN2 (best for hair/video), BiRefNet-HR (best for anime) |
| SAM2 Segmentation | OFF | sam2.1_hiera_large | Text-prompted segmentation |
| 4x Upscale | ON | 4x-UltraSharp (ESRGAN) | Fast, faithful GAN upscaling |
| SeedVR2 Enhance | OFF | seedvr2_ema_3b_fp8 | Diffusion-based restoration/upscale (slower, higher quality) |
| Color Correction | ON | ColorCorrect | Temperature, hue, brightness, contrast, saturation, gamma |

## CLI Usage

```bash
# Default pipeline (rmbg + upscale + color correction)
python run_pipeline.py photo.jpg

# Everything enabled
python run_pipeline.py photo.jpg --sam "person" --seedvr2

# Just background removal with BEN2
python run_pipeline.py photo.jpg --rmbg-model BEN2 --no-upscale --no-color

# Just upscale, no processing
python run_pipeline.py photo.jpg --no-rmbg --no-color

# SeedVR2 enhance to 4K
python run_pipeline.py photo.jpg --no-rmbg --no-upscale --seedvr2 --seedvr2-res 2160

# Color grade
python run_pipeline.py photo.jpg --no-rmbg --no-upscale --temperature 20 --contrast 10 --saturation 15
```

## API Usage (direct ComfyUI)

POST to `http://localhost:8188/prompt` with the workflow JSON. Toggle steps by setting
the `boolean` field on switch nodes:

- Node 11: `boolean: true/false` — background removal
- Node 21: `boolean: true/false` — SAM2 segmentation
- Node 32: `boolean: true/false` — 4x upscale
- Node 43: `boolean: true/false` — SeedVR2 enhance
- Node 51: `boolean: true/false` — color correction

## Recommended Models

| Task | Best Model | Alternative |
|------|-----------|-------------|
| Background Removal (photos) | RMBG-2.0 | BEN2 |
| Background Removal (anime) | BiRefNet-HR | — |
| Upscale (fast) | 4x-UltraSharp | RealESRGAN_x4plus |
| Upscale (quality) | SeedVR2 3B fp8 | SeedVR2 7B |
| Segmentation | SAM2.1 hiera_large | SAM3 (when installed) |
| Color Correction | ColorCorrect | EasyColorCorrector (install for VAE fix) |

## Missing but Recommended

Install these for even better results:
- **SAM3** (`ComfyUI-SAM3`) — text-prompted segmentation without GroundingDINO
- **ComfyUI-EasyColorCorrector** — VAE color correction, film emulation
- **RealESRGAN_x4plus.pth** model — alternative upscaler for photos
