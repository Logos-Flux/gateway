# Security

## Reporting a vulnerability

Please report suspected security issues privately:

- Email: **lf@logosflux.io** with `[gateway security]` in the subject
- Or use **GitHub's private vulnerability reporting** on this repository

Allow up to 14 days for an initial response. Please do not open a public issue
or PR for security reports until a fix has been coordinated.

## Supported versions

| Version | Supported       |
| ------- | --------------- |
| 1.0.x   | ✅ active        |
| < 1.0   | ❌ pre-release   |

## Security model & deployment posture

Gateway is a **control-plane API**. Its job is to start, stop, and dispatch
work to long-running services on a host — typically GPU-bound services. To do
this it talks directly to the host's Docker daemon and probes services on
arbitrary local ports.

The default `docker-compose.yml` therefore:

- Mounts the host Docker socket (`/var/run/docker.sock`) read/write
- Sets `network_mode: host`
- Sets `pid: host`
- Runs the container process as `root`

**This is effectively root-equivalent on the host.** It is appropriate for a
trusted single-tenant machine. It is **not** appropriate for a multi-tenant
host or any network where untrusted clients can reach the API port.

## Hardening checklist before production use

Read all of these. The defaults are tuned for "single trusted operator on
their own GPU box," not for a public-facing API.

| ⚠️ / ✅ | Item | Why |
|---|---|---|
| ⚠️ | **Set `GATEWAY_API_TOKEN`** to a long random string (e.g. `openssl rand -hex 32`) | With it unset (default), the API has **no authentication** and anyone reaching the port can start/stop services, submit jobs, register endpoints, and indirectly drive Docker on the host. |
| ⚠️ | **CORS is wildcard** by default (`allow_origins=["*"]`) | Browser-based callers from any origin can hit every endpoint. If you front the API for a browser app, restrict to a known origin. |
| ⚠️ | **The API binds `0.0.0.0:8080`** inside the container, and `network_mode: host` exposes that on every host interface | Bind the host port behind a firewall, or reverse-proxy through a TLS terminator (Caddy / nginx / Traefik) and bind the gateway to `127.0.0.1`. |
| ⚠️ | **Host Docker socket is mounted** | This grants root-equivalent access to anything inside the container. Required for service lifecycle, but treat the API as a privileged surface. |
| ⚠️ | **The container runs as root** | Combined with the docker socket mount, container escape is trivial. A future release will switch to a non-root user. |
| ⚠️ | **`health_endpoint`, `progress_endpoint`, and `callback_url` are not validated** for SSRF | A caller with API access who registers a service with a crafted endpoint string can cause the gateway to make outbound requests on each scheduler tick. Only allow registry writes from trusted callers. |
| ⚠️ | **ntfy default topic is `gateway`** on the public `ntfy.sh` instance, which is **publicly readable** | Anyone subscribing to `https://ntfy.sh/gateway` can see notifications from every install that uses the default. Set `NTFY_TOPIC` to a private random string before enabling notifications. |
| ⚠️ | **Token comparison uses `==`** rather than `secrets.compare_digest` | Theoretical timing leak. Generally mitigated by network jitter; flagged for transparency. |
| ⚠️ | **Job payloads are stored verbatim in SQLite** | Do not put credentials inside the `payload` field of a submitted job — they will persist on disk in plaintext. |
| ✅ | Token is read from env, never logged, never persisted | Safe handling once set. |
| ✅ | All `subprocess` calls use list arguments (no shell injection surface) | |
| ✅ | All SQL is parameterized | |
| ✅ | No HTML rendering, so no XSS surface | |

## Roadmap for hardened defaults

A future release will move several of the items above to opt-in (require
`GATEWAY_API_TOKEN` to be set, default-deny CORS, validate registry endpoint
strings, run as non-root). These changes are deliberately deferred from
v1.0.0 so the deployment surface does not change underneath existing users
without a major version bump.
