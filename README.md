    ██╗      ██████╗  ██████╗  ██████╗ ███████╗
    ██║     ██╔═══██╗██╔════╝ ██╔═══██╗██╔════╝
    ██║     ██║   ██║██║  ███╗██║   ██║███████╗
    ██║     ██║   ██║██║   ██║██║   ██║╚════██║
    ███████╗╚██████╔╝╚██████╔╝╚██████╔╝███████║
    ╚══════╝ ╚═════╝  ╚═════╝  ╚═════╝ ╚══════╝

            ███████╗██╗     ██╗   ██╗██╗  ██╗
            ██╔════╝██║     ██║   ██║╚██╗██╔╝
            █████╗  ██║     ██║   ██║ ╚███╔╝
            ██╔══╝  ██║     ██║   ██║ ██╔██╗
            ██║     ███████╗╚██████╔╝██╔╝ ██╗
            ╚═╝     ╚══════╝ ╚═════╝ ╚═╝  ╚═╝


# Gateway

A single HTTP control plane for managing GPU-bound services and queueing jobs across them.

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

## Why it exists

Gateway gives you a single HTTP control plane for managing GPU-bound services and queueing jobs across them, with priority-based preemption and health-aware scheduling. It is designed for setups where multiple services contend for one or more GPUs and you want a single point of submit, observe, and preempt.

Concretely: you register Docker-backed services (LLM servers, image-generation workers, code runners, anything with an HTTP health endpoint), submit jobs against a service *type*, and Gateway handles dispatch, container start/stop, VRAM accounting, retries, callbacks, and preemption.

## Features

- **GPU monitoring** — `/gpu` and `/vram` endpoints report utilization, memory, temperature, and per-process usage. Works with discrete GPUs (via `nvidia-smi`) and unified-memory systems.
- **Service registry** — Register services dynamically via API (container name, port, health endpoint, VRAM requirement, type). Persisted in SQLite; no hardcoded service list.
- **Container lifecycle** — Start, stop, restart with VRAM-aware scheduling. Drains in-flight jobs before stopping.
- **Priority job queue** — Queue jobs by service type with priority, TTL, retries, and optional completion callbacks.
- **Preemption** — Higher-priority jobs can preempt lower-priority ones; progress is snapshotted and restored across preemptions.
- **Auto-scaling** — Scheduler starts containers when jobs arrive and stops idle ones to free VRAM.
- **Notifications** — Optional ntfy.sh push notifications for job lifecycle events.
- **Auto-generated API docs** — Interactive OpenAPI docs at `/docs`.

## Quick start

```bash
git clone https://github.com/Logos-Flux/gateway.git
cd gateway

cp .env.example .env
# Edit .env and set GATEWAY_API_TOKEN to a strong secret
# (if left empty, the API runs WITHOUT authentication — see Security below)

docker compose up -d --build

curl http://localhost:8080/health
# => {"status":"ok","version":"0.5.0",...}
```

Once running, browse the interactive API docs at <http://localhost:8080/docs>.

### No GPU?

Remove the `deploy.resources.reservations.devices` block from `docker-compose.yml` to run on hosts without a GPU. The `/gpu` endpoint will then report `available: false`.

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and edit.

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_API_TOKEN` | _(empty)_ | Bearer token required on all API calls. **If unset, authentication is disabled** — see Security. |
| `QUEUE_DB_PATH` | `/app/data/queue.db` | SQLite path for the job queue and service registry. |
| `IDLE_TIMEOUT_SECONDS` | `300` | Auto-stop scheduler-started services after this many seconds idle. |
| `PREEMPT_PRIORITY_GAP` | `2` | Minimum priority gap that triggers automatic preemption of a running job. |
| `NTFY_ENABLED` | `false` | Enable ntfy.sh push notifications for job lifecycle events. |
| `NTFY_URL` | `https://ntfy.sh` | ntfy server URL. |
| `NTFY_TOPIC` | `gateway` | ntfy topic to publish to. |

## API overview

The full, always-up-to-date schema is served at `/docs` (Swagger UI) and `/openapi.json`. The main endpoint groups:

| Group | Purpose |
|---|---|
| `/health` | Liveness probe; returns version. |
| `/gpu`, `/vram` | GPU and VRAM state. |
| `/services`, `/services/{name}` | Registry CRUD plus `start` / `stop` / `restart` / `enable` / `disable`. |
| `/queue/submit`, `/queue/{job_id}` | Submit jobs, fetch status, cancel, push/poll progress. |
| `/preempt/check/{name}`, `/preempt/execute`, `/preempt/release/{name}` | Inspect and trigger preemption. |
| `/available/{service_type}` | Recommendation hint: `use_local`, `use_cloud`, or `queue`. |

All endpoints except `/health` require the `Authorization: Bearer <GATEWAY_API_TOKEN>` header when a token is configured.

### CLI helper

A thin Bash wrapper (`gateway-cli.sh`) is included for the most common operations:

```bash
./gateway-cli.sh status                          # GPU + services overview
./gateway-cli.sh services                        # List all registered services
./gateway-cli.sh register myservice my-container 8000 llm --vram 8
./gateway-cli.sh start myservice
./gateway-cli.sh stop myservice
./gateway-cli.sh help
```

## Examples

The repo includes optional reference material — not required to use Gateway:

- `examples/comfyui_wrapper.py` — a thin HTTP wrapper that adapts a ComfyUI instance to the dispatch contract Gateway expects.
- `workflows/` — a small example pipeline (`image_processing_pipeline.json` + `run_pipeline.py`) that submits a multi-step job through the queue.

These are starting points; build your own service adapters the same way.

## Development

Gateway is a small FastAPI application — straightforward to run outside Docker for local development:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn main:app --reload --port 8080
```

Lint and format with ruff (config in `pyproject.toml`):

```bash
ruff format .
ruff check .
```

There is no automated test suite yet. Contributions in that direction are welcome — see [CONTRIBUTING.md](./CONTRIBUTING.md).

## Security

> **Warning — read before exposing Gateway:** Gateway is a control-plane API that, by design, mounts the host Docker socket and runs the container with `network_mode: host`. The default install has **authentication disabled** (set `GATEWAY_API_TOKEN` to enable it) and uses wildcard CORS. **Do not expose this service to untrusted networks without reading [SECURITY.md](./SECURITY.md) first.**

In particular: any caller who can reach the API can start, stop, and inspect any container known to the Docker daemon Gateway is bound to. Treat access to Gateway as equivalent to access to that Docker daemon.

## Contributing

Bug reports, suggestions, and pull requests are welcome. Please read [CONTRIBUTING.md](./CONTRIBUTING.md) before opening a PR.

## Changelog

See [CHANGELOG.md](./CHANGELOG.md) for release notes.

## License

Apache-2.0 — see [LICENSE](./LICENSE).
