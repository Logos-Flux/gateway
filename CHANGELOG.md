# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-05-03

Initial public release of Gateway, a GPU-aware service lifecycle manager.

### Added

- Gateway daemon (FastAPI) with REST API for service lifecycle management.
- Scheduler that brings registered services up and down based on demand and
  GPU availability.
- Job queue backed by SQLite for asynchronous task submission, status
  tracking, and result delivery.
- Service registry for declaring managed endpoints (with optional
  `health_endpoint` and `progress_endpoint` hooks).
- ntfy-based notification hooks for job lifecycle events.
- Example ComfyUI wrapper service demonstrating the registration pattern.
- Image processing pipeline workflow as a reference for chaining services
  through the queue.

### Security

- Authentication is **opt-in**: set `GATEWAY_API_TOKEN` to a long random
  string before exposing the service. With it unset, the API has no
  authentication and anyone reaching the port can drive the host.
- **Read [SECURITY.md](SECURITY.md) before deploying.** The default
  `docker-compose.yml` mounts the host docker socket and uses host
  networking and PID namespace, which is effectively root-equivalent on
  the host.

[1.0.0]: https://github.com/Logos-Flux/gateway/releases/tag/v1.0.0
