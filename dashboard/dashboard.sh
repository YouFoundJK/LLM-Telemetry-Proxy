#!/bin/bash
#
# Telemetry Dashboard & Proxy Gateway control script.
#
# Usage:
#   ./dashboard.sh start [port] [--with-proxy] — start dashboard server (default port 9118)
#   ./dashboard.sh stop [--all]                — kill dashboard server (and proxy if --all)
#   ./dashboard.sh restart [port] [--with-proxy] — restart dashboard server
#   ./dashboard.sh status                     — check status of dashboard and proxy
#   ./dashboard.sh url                        — print the dashboard URL
#   ./dashboard.sh proxy {start|stop|restart|status|logs} — proxy subcommands
#
set -euo pipefail

# Activate virtualenv if present
if [[ -f "$HOME/server/.venv/bin/activate" ]]; then
    source "$HOME/server/.venv/bin/activate"
elif [[ -f "${BASH_SOURCE[0]%/*}/../.venv/bin/activate" ]]; then
    source "${BASH_SOURCE[0]%/*}/../.venv/bin/activate"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DASHBOARD_PID_FILE="$SCRIPT_DIR/.dashboard.pid"
DASHBOARD_PORT_FILE="$SCRIPT_DIR/.dashboard.port"
DASHBOARD_LOG_FILE="$SCRIPT_DIR/.dashboard.log"
PROXY_PID_FILE="$REPO_ROOT/data/.proxy.pid"
PROXY_LOG_FILE="$REPO_ROOT/data/proxy.log"

DEFAULT_PORT="9118"
PROXY_PORT="9090"
PYTHON="${PYTHON:-python3}"

# ── Port & PID Helpers ───────────────────────────────────────────────────────

get_pids_on_port() {
    local port="$1"
    local pids=""
    if command -v lsof >/dev/null 2>&1; then
        pids=$(lsof -ti "tcp:$port" 2>/dev/null || true)
    fi
    if [[ -z "$pids" ]] && command -v fuser >/dev/null 2>&1; then
        pids=$(fuser "$port/tcp" 2>/dev/null || true)
    fi
    if [[ -z "$pids" ]] && command -v ss >/dev/null 2>&1; then
        pids=$(ss -tlpn "sport = :$port" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 || true)
    fi
    echo "$pids"
}

get_dashboard_pids() {
    local pids=""
    if [[ -f "$DASHBOARD_PID_FILE" ]]; then
        local file_pid
        file_pid=$(cat "$DASHBOARD_PID_FILE" 2>/dev/null || true)
        if [[ -n "$file_pid" ]] && kill -0 "$file_pid" 2>/dev/null; then
            pids="$file_pid"
        fi
    fi

    local target_port
    target_port=$(cat "$DASHBOARD_PORT_FILE" 2>/dev/null || echo "$DEFAULT_PORT")
    local port_pids
    port_pids=$(get_pids_on_port "$target_port")
    if [[ -n "$port_pids" ]]; then
        pids=$(echo -e "${pids}\n${port_pids}" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u || true)
    fi

    if command -v pgrep >/dev/null 2>&1; then
        local pgrep_pids
        pgrep_pids=$(pgrep -f "server.py.*--port" 2>/dev/null || true)
        if [[ -n "$pgrep_pids" ]]; then
            pids=$(echo -e "${pids}\n${pgrep_pids}" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u || true)
        fi
    fi

    echo "$pids"
}

get_proxy_pids() {
    local pids=""
    if [[ -f "$PROXY_PID_FILE" ]]; then
        local file_pid
        file_pid=$(cat "$PROXY_PID_FILE" 2>/dev/null || true)
        if [[ -n "$file_pid" ]] && kill -0 "$file_pid" 2>/dev/null; then
            pids="$file_pid"
        fi
    fi

    local port_pids
    port_pids=$(get_pids_on_port "$PROXY_PORT")
    if [[ -n "$port_pids" ]]; then
        pids=$(echo -e "${pids}\n${port_pids}" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u || true)
    fi

    if command -v pgrep >/dev/null 2>&1; then
        local pgrep_pids
        pgrep_pids=$(pgrep -f "llm_telemetry_proxy.py" 2>/dev/null || true)
        if [[ -n "$pgrep_pids" ]]; then
            pids=$(echo -e "${pids}\n${pgrep_pids}" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u || true)
        fi
    fi

    echo "$pids"
}

kill_pids_robustly() {
    local pids="$1"
    local name="${2:-process}"
    if [[ -z "$pids" ]]; then
        return 0
    fi

    echo "Stopping $name (PID(s): $(echo "$pids" | tr '\n' ' '))..."
    for pid in $pids; do
        kill "$pid" 2>/dev/null || true
    done

    # Wait up to 3 seconds for graceful shutdown
    local waited=0
    while (( waited < 6 )); do
        local any_alive=0
        for pid in $pids; do
            if kill -0 "$pid" 2>/dev/null; then
                any_alive=1
                break
            fi
        done
        if (( any_alive == 0 )); then
            break
        fi
        sleep 0.5
        waited=$((waited + 1))
    done

    # Force kill if still alive
    for pid in $pids; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "Force killing $name (PID $pid)..."
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    sleep 0.2
}

ensure_port_freed() {
    local port="$1"
    local name="${2:-service}"
    local pids
    pids=$(get_pids_on_port "$port")
    if [[ -n "$pids" ]]; then
        echo "Clearing lingering process on port $port..."
        kill_pids_robustly "$pids" "$name"
    fi
}

is_dashboard_running() {
    local pids
    pids=$(get_dashboard_pids)
    if [[ -n "$pids" ]]; then
        return 0
    fi
    rm -f "$DASHBOARD_PID_FILE"
    return 1
}

is_proxy_running() {
    local pids
    pids=$(get_proxy_pids)
    if [[ -n "$pids" ]]; then
        return 0
    fi
    rm -f "$PROXY_PID_FILE"
    return 1
}

# ── Service Control Functions ────────────────────────────────────────────────

start_dashboard() {
    local target_port="$DEFAULT_PORT"
    local with_proxy=false

    for arg in "$@"; do
        if [[ "$arg" == "--with-proxy" ]]; then
            with_proxy=true
        elif [[ "$arg" =~ ^[0-9]+$ ]]; then
            target_port="$arg"
        fi
    done

    # If running on requested port, report active
    if is_dashboard_running; then
        local active_port
        active_port=$(cat "$DASHBOARD_PORT_FILE" 2>/dev/null || echo "$target_port")
        echo "Dashboard already running (PID(s): $(echo "$(get_dashboard_pids)" | tr '\n' ' '))"
        echo "URL: http://localhost:$active_port"
        if [[ "$with_proxy" == true ]]; then
            start_proxy "$PROXY_PORT"
        fi
        return 0
    fi

    # Ensure port is clean before binding
    ensure_port_freed "$target_port" "dashboard"

    echo "Starting telemetry dashboard on port $target_port..."
    cd "$SCRIPT_DIR"
    nohup "$PYTHON" server.py --port "$target_port" > "$DASHBOARD_LOG_FILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$DASHBOARD_PID_FILE"
    echo "$target_port" > "$DASHBOARD_PORT_FILE"

    # Verify startup over 3 seconds
    local started=false
    for _ in {1..6}; do
        sleep 0.5
        if kill -0 "$new_pid" 2>/dev/null; then
            # Verify port listening
            local port_pids
            port_pids=$(get_pids_on_port "$target_port")
            if [[ -n "$port_pids" ]] || grep -q "Server starting on" "$DASHBOARD_LOG_FILE" 2>/dev/null; then
                started=true
                break
            fi
        else
            break
        fi
    done

    if [[ "$started" == true ]] && kill -0 "$new_pid" 2>/dev/null; then
        echo "✅ Dashboard running on http://localhost:$target_port"
        echo "   PID: $new_pid"
        echo "   Log: $DASHBOARD_LOG_FILE"
    else
        echo "❌ Failed to start dashboard. Check log:"
        cat "$DASHBOARD_LOG_FILE"
        rm -f "$DASHBOARD_PID_FILE" "$DASHBOARD_PORT_FILE"
        return 1
    fi

    if [[ "$with_proxy" == true ]]; then
        start_proxy "$PROXY_PORT"
    fi
}

stop_dashboard() {
    local pids
    pids=$(get_dashboard_pids)
    local target_port
    target_port=$(cat "$DASHBOARD_PORT_FILE" 2>/dev/null || echo "$DEFAULT_PORT")

    if [[ -n "$pids" ]]; then
        kill_pids_robustly "$pids" "dashboard"
        ensure_port_freed "$target_port" "dashboard"
        rm -f "$DASHBOARD_PID_FILE" "$DASHBOARD_PORT_FILE"
        echo "✅ Dashboard stopped."
    else
        # Still check if target port is occupied
        ensure_port_freed "$target_port" "dashboard"
        rm -f "$DASHBOARD_PID_FILE" "$DASHBOARD_PORT_FILE"
        echo "Dashboard not running."
    fi
}

start_proxy() {
    local pport="${1:-$PROXY_PORT}"
    if is_proxy_running; then
        echo "Proxy already running (PID(s): $(echo "$(get_proxy_pids)" | tr '\n' ' '))"
        return 0
    fi

    ensure_port_freed "$pport" "proxy"
    echo "Starting LLM telemetry proxy on port $pport..."
    mkdir -p "$REPO_ROOT/data"
    cd "$REPO_ROOT"
    nohup "$PYTHON" "$REPO_ROOT/proxy/llm_telemetry_proxy.py" --port "$pport" > "$PROXY_LOG_FILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PROXY_PID_FILE"

    sleep 1
    if kill -0 "$new_pid" 2>/dev/null; then
        echo "✅ Proxy running on http://localhost:$pport (PID $new_pid)"
        echo "   Log: $PROXY_LOG_FILE"
    else
        echo "❌ Proxy failed to start. Check log: $PROXY_LOG_FILE"
        rm -f "$PROXY_PID_FILE"
        return 1
    fi
}

stop_proxy() {
    local pids
    pids=$(get_proxy_pids)
    if [[ -n "$pids" ]]; then
        kill_pids_robustly "$pids" "proxy"
        ensure_port_freed "$PROXY_PORT" "proxy"
        rm -f "$PROXY_PID_FILE"
        echo "✅ Proxy stopped."
    else
        ensure_port_freed "$PROXY_PORT" "proxy"
        rm -f "$PROXY_PID_FILE"
        echo "Proxy not running."
    fi
}

# ── Main Entry Point ─────────────────────────────────────────────────────────

COMMAND="${1:-status}"
shift || true

case "$COMMAND" in
    start)
        start_dashboard "$@"
        ;;

    stop)
        STOP_ALL=false
        for arg in "$@"; do
            if [[ "$arg" == "--all" ]]; then
                STOP_ALL=true
            fi
        done

        stop_dashboard

        if [[ "$STOP_ALL" == true ]]; then
            stop_proxy
        fi
        ;;

    restart)
        STOP_ALL=false
        for arg in "$@"; do
            if [[ "$arg" == "--all" ]]; then
                STOP_ALL=true
            fi
        done

        stop_dashboard
        if [[ "$STOP_ALL" == true ]]; then
            stop_proxy
        fi

        sleep 1
        start_dashboard "$@"
        ;;

    status)
        echo "=== LLM Telemetry Suite Status ==="
        if is_dashboard_running; then
            local_port=$(cat "$DASHBOARD_PORT_FILE" 2>/dev/null || echo "$DEFAULT_PORT")
            local_pids=$(echo "$(get_dashboard_pids)" | tr '\n' ' ')
            echo "✅ Dashboard: Running (PID(s) $local_pids, port $local_port)"
            echo "   URL: http://localhost:$local_port"
        else
            echo "❌ Dashboard: Stopped"
        fi

        if is_proxy_running; then
            local_pids=$(echo "$(get_proxy_pids)" | tr '\n' ' ')
            echo "✅ Proxy:     Running (PID(s) $local_pids)"
            echo "   Log: $PROXY_LOG_FILE"
        else
            echo "❌ Proxy:     Stopped"
        fi
        ;;

    url)
        if is_dashboard_running; then
            local_port=$(cat "$DASHBOARD_PORT_FILE" 2>/dev/null || echo "$DEFAULT_PORT")
            echo "http://localhost:$local_port"
        else
            echo "Dashboard not running. Start with: ./dashboard.sh start"
        fi
        ;;

    proxy)
        SUB_CMD="${1:-status}"
        shift || true
        case "$SUB_CMD" in
            start)
                start_proxy "${1:-$PROXY_PORT}"
                ;;
            stop)
                stop_proxy
                ;;
            restart)
                stop_proxy
                sleep 1
                start_proxy "${1:-$PROXY_PORT}"
                ;;
            status)
                if is_proxy_running; then
                    echo "✅ Proxy running (PID(s): $(echo "$(get_proxy_pids)" | tr '\n' ' '))"
                else
                    echo "❌ Proxy not running."
                fi
                ;;
            logs)
                tail -n "${1:-50}" "$PROXY_LOG_FILE" 2>/dev/null || echo "No logs found at $PROXY_LOG_FILE"
                ;;
            *)
                echo "Usage: ./dashboard.sh proxy {start [port]|stop|restart [port]|status|logs [lines]}"
                exit 1
                ;;
        esac
        ;;

    *)
        echo "Usage: ./dashboard.sh {start [port] [--with-proxy]|stop [--all]|restart [port] [--with-proxy]|status|url|proxy {start|stop|restart|status|logs}}"
        exit 1
        ;;
esac
