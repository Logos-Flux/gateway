# Contributing to Gateway

Thanks for your interest in contributing! Gateway is a small, focused
service and we want to keep it that way — but improvements, bug reports,
and patches are all welcome.

## Repo layout

The repo is intentionally flat:

| Path | Purpose |
| --- | --- |
| `main.py` | FastAPI app: routes, app state wiring, lifespan |
| `scheduler.py` | Service lifecycle / GPU-aware scheduler |
| `job_queue.py` | SQLite-backed async job queue |
| `registry.py` | Service registration + persistence |
| `resources.py` | GPU / system resource discovery |
| `progress.py` | Progress reporting helpers |
| `notifications.py` | ntfy notification dispatch |
| `examples/` | Example service wrappers (e.g. ComfyUI) |
| `workflows/` | Reference multi-service workflows |
| `Dockerfile`, `docker-compose.yml`, `deploy.sh` | Container + deployment |
| `gateway-cli.sh` | Convenience CLI wrapper |
| `pyproject.toml` | Project + ruff config |

## Dev setup

You need Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install ruff  # for lint/format
```

Run the gateway locally:

```bash
uvicorn main:app --host 127.0.0.1 --port 8080 --reload
```

(Bind to `127.0.0.1` for local dev — see [SECURITY.md](SECURITY.md) for
why the production posture is different.)

## Testing

There is no automated test suite yet. Manual verification against a real
service (e.g. the included ComfyUI example) is the current bar.
**Contributions adding pytest coverage are very welcome** — please open
an issue first so we can agree on layout and fixtures.

## Code style

We use [Ruff](https://docs.astral.sh/ruff/) for both linting and
formatting. Config is in `pyproject.toml`.

```bash
ruff format .
ruff check .
```

Both should pass cleanly before you open a PR.

## Pull request process

1. **Open an issue first** for anything non-trivial (new feature, API
   change, dependency addition). For small fixes and obvious bugs, a PR
   without a prior issue is fine.
2. Branch from `main`. Keep PRs scoped — one logical change per PR.
3. Update `CHANGELOG.md` under an `## [Unreleased]` section if your
   change is user-visible.
4. Run `ruff format .` and `ruff check .`.
5. Open the PR with a clear description: what changed, why, and how you
   verified it.

No DCO sign-off or CLA is required.

## Reporting security issues

Please **do not** open a public issue for security reports. See
[SECURITY.md](SECURITY.md) for the disclosure process.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
By participating you agree to abide by its terms.
