#!/bin/bash
# Gateway CLI
# Usage: gateway <command>

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"

AUTH_HEADER=""
if [ -n "$GATEWAY_API_TOKEN" ]; then
  AUTH_HEADER="Authorization: Bearer $GATEWAY_API_TOKEN"
fi

# curl wrapper that includes auth header when set
_curl() {
  if [ -n "$AUTH_HEADER" ]; then
    curl -s -H "$AUTH_HEADER" "$@"
  else
    curl -s "$@"
  fi
}

case "$1" in
  status)
    echo "=== GPU Status ==="
    _curl "$GATEWAY_URL/gpu" | jq '.'
    echo ""
    echo "=== Services ==="
    _curl "$GATEWAY_URL/services" | jq '.services | to_entries[] | "\(.key): \(if .value.enabled == false then "\u26AB disabled" elif .value.container_running then "\ud83d\udfe2 running" else "\ud83d\udd34 stopped" end) \(if .value.healthy then "(healthy)" else if .value.enabled == false then "" else "(unhealthy)" end end) [\(.value.type)] \(.value.vram_gb)GB VRAM"' -r
    ;;
  gpu)
    _curl "$GATEWAY_URL/gpu" | jq '.'
    ;;
  vram)
    _curl "$GATEWAY_URL/vram" | jq '.'
    ;;
  services)
    _curl "$GATEWAY_URL/services" | jq '.'
    ;;
  service)
    if [ -z "$2" ]; then
      echo "Usage: gateway service <name>"
      exit 1
    fi
    _curl "$GATEWAY_URL/services/$2" | jq '.'
    ;;

  # Registry: register / unregister / enable / disable
  register)
    if [ -z "$2" ] || [ -z "$3" ] || [ -z "$4" ] || [ -z "$5" ]; then
      echo "Usage: gateway register <name> <container> <port> <type> [options]"
      echo "  Options:"
      echo "    --vram N              VRAM in GB (default: 0)"
      echo "    --health /endpoint    Health check path (default: /health)"
      echo "    --progress-mode MODE  stateless|queue|poll (default: stateless)"
      echo "    --description \"...\"   Service description"
      exit 1
    fi
    NAME="$2"
    CONTAINER="$3"
    PORT="$4"
    TYPE="$5"
    shift 5

    VRAM=0
    HEALTH="/health"
    PROGRESS_MODE="stateless"
    DESCRIPTION=""

    while [ $# -gt 0 ]; do
      case "$1" in
        --vram) VRAM="$2"; shift 2 ;;
        --health) HEALTH="$2"; shift 2 ;;
        --progress-mode) PROGRESS_MODE="$2"; shift 2 ;;
        --description) DESCRIPTION="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
      esac
    done

    BODY=$(jq -n \
      --arg name "$NAME" \
      --arg container "$CONTAINER" \
      --argjson port "$PORT" \
      --arg type "$TYPE" \
      --argjson vram_gb "$VRAM" \
      --arg health_endpoint "$HEALTH" \
      --arg progress_mode "$PROGRESS_MODE" \
      --arg description "$DESCRIPTION" \
      '{name: $name, container: $container, port: $port, type: $type, vram_gb: $vram_gb, health_endpoint: $health_endpoint, progress_mode: $progress_mode, description: $description}')

    echo "Registering $NAME..."
    _curl -X POST "$GATEWAY_URL/services/register" \
      -H 'Content-Type: application/json' \
      -d "$BODY" | jq '.'
    ;;
  unregister)
    if [ -z "$2" ]; then
      echo "Usage: gateway unregister <name>"
      exit 1
    fi
    echo "Unregistering $2..."
    _curl -X DELETE "$GATEWAY_URL/services/$2" | jq '.'
    ;;
  enable)
    if [ -z "$2" ]; then
      echo "Usage: gateway enable <name>"
      exit 1
    fi
    _curl -X POST "$GATEWAY_URL/services/$2/enable" | jq '.'
    ;;
  disable)
    if [ -z "$2" ]; then
      echo "Usage: gateway disable <name>"
      exit 1
    fi
    _curl -X POST "$GATEWAY_URL/services/$2/disable" | jq '.'
    ;;

  # Lifecycle
  start)
    if [ -z "$2" ]; then
      echo "Usage: gateway start <service>"
      exit 1
    fi
    echo "Starting $2..."
    _curl -X POST "$GATEWAY_URL/services/$2/start" | jq '.'
    ;;
  stop)
    if [ -z "$2" ]; then
      echo "Usage: gateway stop <service>"
      exit 1
    fi
    echo "Stopping $2..."
    _curl -X POST "$GATEWAY_URL/services/$2/stop" | jq '.'
    ;;
  restart)
    if [ -z "$2" ]; then
      echo "Usage: gateway restart <service>"
      exit 1
    fi
    echo "Restarting $2..."
    _curl -X POST "$GATEWAY_URL/services/$2/restart" | jq '.'
    ;;

  # Queue
  queue)
    if [ "$2" = "list" ]; then
      _curl "$GATEWAY_URL/queue/stats" | jq '.'
    else
      _curl "$GATEWAY_URL/queue/stats" | jq '.'
    fi
    ;;
  submit)
    if [ -z "$2" ] || [ -z "$3" ]; then
      echo "Usage: gateway submit <service_type> <payload_file> [-p priority]"
      exit 1
    fi
    SERVICE_TYPE="$2"
    PAYLOAD_FILE="$3"
    PRIORITY=3
    if [ "$4" = "-p" ] && [ -n "$5" ]; then
      PRIORITY="$5"
    fi
    if [ ! -f "$PAYLOAD_FILE" ]; then
      echo "Error: payload file '$PAYLOAD_FILE' not found"
      exit 1
    fi
    PAYLOAD=$(cat "$PAYLOAD_FILE")
    _curl -X POST "$GATEWAY_URL/queue/submit" \
      -H 'Content-Type: application/json' \
      -d "{\"service_type\": \"$SERVICE_TYPE\", \"payload\": $PAYLOAD, \"priority\": $PRIORITY}" | jq '.'
    ;;
  job)
    if [ -z "$2" ]; then
      echo "Usage: gateway job <job_id>"
      exit 1
    fi
    _curl "$GATEWAY_URL/queue/$2" | jq '.'
    ;;
  cancel)
    if [ -z "$2" ]; then
      echo "Usage: gateway cancel <job_id>"
      exit 1
    fi
    _curl -X DELETE "$GATEWAY_URL/queue/$2" | jq '.'
    ;;

  # Preemption
  preempt)
    if [ "$2" = "check" ]; then
      if [ -z "$3" ]; then
        echo "Usage: gateway preempt check <service>"
        exit 1
      fi
      _curl "$GATEWAY_URL/preempt/check/$3" | jq '.'
    elif [ "$2" = "exec" ] || [ "$2" = "execute" ]; then
      if [ -z "$3" ] || [ -z "$4" ]; then
        echo "Usage: gateway preempt exec <service> <preemptor_job_id>"
        exit 1
      fi
      _curl -X POST "$GATEWAY_URL/preempt/execute" \
        -H 'Content-Type: application/json' \
        -d "{\"service_name\": \"$3\", \"preemptor_job_id\": \"$4\"}" | jq '.'
    else
      echo "Usage: gateway preempt <check|exec> ..."
      exit 1
    fi
    ;;
  release)
    if [ -z "$2" ]; then
      echo "Usage: gateway release <service>"
      exit 1
    fi
    _curl -X POST "$GATEWAY_URL/preempt/release/$2" | jq '.'
    ;;
  progress)
    if [ -z "$2" ]; then
      echo "Usage: gateway progress <job_id>"
      exit 1
    fi
    _curl "$GATEWAY_URL/queue/$2/progress" | jq '.'
    ;;

  help|--help|-h|"")
    echo "Gateway CLI"
    echo ""
    echo "Visibility:"
    echo "  status       Show GPU status and all services"
    echo "  gpu          Show GPU details"
    echo "  vram         Show VRAM allocation"
    echo "  services     Show all services"
    echo "  service X    Show specific service"
    echo ""
    echo "Registry:"
    echo "  register NAME CONTAINER PORT TYPE [--vram N] [--health /ep] [--progress-mode MODE] [--description \"...\"]"
    echo "               Register a new service"
    echo "  unregister X Remove a service from the registry"
    echo "  enable X     Enable a disabled service"
    echo "  disable X    Disable a service (keeps config, scheduler ignores)"
    echo ""
    echo "Lifecycle:"
    echo "  start X      Start a service container"
    echo "  stop X       Stop a service container (with drain)"
    echo "  restart X    Restart a service container"
    echo ""
    echo "Queue:"
    echo "  queue        Show queue stats"
    echo "  submit TYPE FILE [-p PRIORITY]"
    echo "               Submit a job (reads payload from JSON file)"
    echo "  job ID       Show job details"
    echo "  cancel ID    Cancel a queued job"
    echo ""
    echo "Preemption:"
    echo "  preempt check SERVICE   Check if service can be preempted"
    echo "  preempt exec SERVICE JOB_ID"
    echo "                          Preempt service for a queued job"
    echo "  release SERVICE         Resume paused jobs for service"
    echo "  progress JOB_ID         Show job progress"
    ;;
  *)
    echo "Unknown command: $1 (try 'gateway help')"
    exit 1
    ;;
esac
