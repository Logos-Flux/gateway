#!/usr/bin/env bash
set -euo pipefail

# Gateway Deploy Script
# Builds and deploys the gateway container.
# Can be run from anywhere — resolves paths relative to this script.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
CONTAINER_NAME="gateway"

usage() {
  echo "Usage: $0 [command]"
  echo ""
  echo "Commands:"
  echo "  up        Build and start (default)"
  echo "  down      Stop and remove container"
  echo "  rebuild   Force rebuild and restart"
  echo "  logs      Tail container logs"
  echo "  status    Show container status"
}

case "${1:-up}" in
  up)
    echo "Building and starting gateway..."
    docker compose -f "$COMPOSE_FILE" up -d --build
    echo "Done. Container: $CONTAINER_NAME"
    docker compose -f "$COMPOSE_FILE" ps
    ;;
  down)
    echo "Stopping gateway..."
    docker compose -f "$COMPOSE_FILE" down
    ;;
  rebuild)
    echo "Rebuilding gateway (no cache)..."
    docker compose -f "$COMPOSE_FILE" build --no-cache
    docker compose -f "$COMPOSE_FILE" up -d
    echo "Done."
    docker compose -f "$COMPOSE_FILE" ps
    ;;
  logs)
    docker compose -f "$COMPOSE_FILE" logs -f --tail=100
    ;;
  status)
    docker compose -f "$COMPOSE_FILE" ps
    echo ""
    # Quick health check
    if curl -sf http://localhost:8080/health > /dev/null 2>&1; then
      echo "Health: OK (health endpoint responding)"
    else
      echo "Health: UNREACHABLE (localhost:8080 not responding)"
    fi
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "Unknown command: $1"
    usage
    exit 1
    ;;
esac
