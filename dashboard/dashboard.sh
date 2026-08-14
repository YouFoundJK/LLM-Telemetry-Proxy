#!/bin/bash
#
# Telemetry Dashboard & Proxy Gateway control script.
#
# Usage:
#   ./dashboard.sh start [port] [--with-proxy] — start dashboard server (default port 9118)
#   ./dashboard.sh stop [--all]                — kill dashboard server (and proxy if --all)
#   ./dashboard.sh restart [port]             — restart dashboard (and proxy if it was running)
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
DATA_DIR="$REPO_ROOT/data"
DASHBOARD_PID_FILE="$DATA_DIR/.dashboard.pid"
DASHBOARD_PORT_FILE="$DATA_DIR/.dashboard.port"
DASHBOARD_LOG_FILE="$DATA_DIR/dashboard.log"
PROXY_PID_FILE="$DATA_DIR/.proxy.pid"
PROXY_LOG_FILE="$DATA_DIR/proxy.log"

# Clean up legacy files from dashboard source dir if present
rm -f "$SCRIPT_DIR/.dashboard.pid" "$SCRIPT_DIR/.dashboard.port" "$SCRIPT_DIR/.dashboard.log" 2>/dev/null || true

DEFAULT_PORT="9118"
PROXY_PORT="9090"
PYTHON="${PYTHON:-python3}"

# ── Port & PID Helpers ───────────────────────────────────────────────────────

# Returns the cmdline for a given PID
get_proc_cmdline() {
    local pid="$1"
    [[ -z "$pid" ]] && return 1
    if [[ -f "/proc/$pid/cmdline" ]]; then
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true
    else
        ps -p "$pid" -o args= 2>/dev/null || true
    fi
}

# Verify if a PID belongs to the dashboard server
is_dashboard_pid() {
    local pid="$1"
    [[ -z "$pid" ]] && return 1
    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi
    local cmd
    cmd=$(get_proc_cmdline "$pid")
    if [[ "$cmd" =~ server\.py ]] && [[ "$cmd" =~ dashboard ]]; then
        return 0
    elif [[ "$cmd" =~ server\.py ]]; then
        return 0
    fi
    return 1
}

# Verify if a PID belongs to the LLM telemetry proxy
is_proxy_pid() {
    local pid="$1"
    [[ -z "$pid" ]] && return 1
    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi
    local cmd
    cmd=$(get_proc_cmdline "$pid")
    if [[ "$cmd" =~ llm_telemetry_proxy\.py ]]; then
        return 0
    fi
    return 1
}

# Get ONLY the PID that is actively LISTENING on a given port (never client sockets)
get_listening_pid_on_port() {
    local port="$1"
    local pid=""
    if command -v lsof >/dev/null 2>&1; then
        pid=$(lsof -ti "tcp:$port" -sTCP:LISTEN 2>/dev/null | head -n1 || true)
    fi
    if [[ -z "$pid" ]] && command -v ss >/dev/null 2>&1; then
        pid=$(ss -tlpn "sport = :$port" 2>/dev/null | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2 || true)
    fi
    echo "$pid"
}

# Get dashboard PID (strictly verified)
get_dashboard_pid() {
    if [[ -f "$DASHBOARD_PID_FILE" ]]; then
        local file_pid
        file_pid=$(cat "$DASHBOARD_PID_FILE" 2>/dev/null || true)
        if [[ -n "$file_pid" ]] && is_dashboard_pid "$file_pid"; then
            echo "$file_pid"
            return 0
        fi
        # Stale PID file
        rm -f "$DASHBOARD_PID_FILE"
    fi

    # Fallback: check listening port ONLY if the process is verified to be our dashboard
    local target_port
    target_port=$(cat "$DASHBOARD_PORT_FILE" 2>/dev/null || echo "$DEFAULT_PORT")
    local listen_pid
    listen_pid=$(get_listening_pid_on_port "$target_port")
    if [[ -n "$listen_pid" ]] && is_dashboard_pid "$listen_pid"; then
        echo "$listen_pid" > "$DASHBOARD_PID_FILE"
        echo "$listen_pid"
        return 0
    fi

    return 1
}

# Get proxy PID (strictly verified)
get_proxy_pid() {
    if [[ -f "$PROXY_PID_FILE" ]]; then
        local file_pid
        file_pid=$(cat "$PROXY_PID_FILE" 2>/dev/null || true)
        if [[ -n "$file_pid" ]] && is_proxy_pid "$file_pid"; then
            echo "$file_pid"
            return 0
        fi
        # Stale PID file
        rm -f "$PROXY_PID_FILE"
    fi

    # Fallback: check listening port ONLY if the process is verified to be our proxy
    local listen_pid
    listen_pid=$(get_listening_pid_on_port "$PROXY_PORT")
    if [[ -n "$listen_pid" ]] && is_proxy_pid "$listen_pid"; then
        echo "$listen_pid" > "$PROXY_PID_FILE"
        echo "$listen_pid"
        return 0
    fi

    return 1
}

# Kill only a specific, verified PID
kill_pid_safely() {
    local pid="$1"
    local name="${2:-process}"
    if [[ -z "$pid" ]]; then
        return 0
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
        return 0
    fi

    echo "Stopping $name (PID $pid)..."
    kill "$pid" 2>/dev/null || true

    # Wait up to 3 seconds for graceful termination
    local waited=0
    while (( waited < 6 )); do
        if ! kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        sleep 0.5
        waited=$((waited + 1))
    done

    # Force kill if still alive
    if kill -0 "$pid" 2>/dev/null; then
        echo "Force killing $name (PID $pid)..."
        kill -9 "$pid" 2>/dev/null || true
    fi
    sleep 0.2
}

is_dashboard_running() {
    local pid
    pid=$(get_dashboard_pid || true)
    [[ -n "$pid" ]]
}

is_proxy_running() {
    local pid
    pid=$(get_proxy_pid || true)
    [[ -n "$pid" ]]
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
        local pid
        pid=$(get_dashboard_pid || true)
        echo "Dashboard already running (PID: $pid)"
        echo "URL: http://localhost:$active_port"
        if [[ "$with_proxy" == true ]]; then
            start_proxy "$PROXY_PORT"
        fi
        return 0
    fi

    # Check if another process is listening on the port
    local listen_pid
    listen_pid=$(get_listening_pid_on_port "$target_port")
    if [[ -n "$listen_pid" ]]; then
        if is_dashboard_pid "$listen_pid"; then
            kill_pid_safely "$listen_pid" "dashboard"
        else
            echo "❌ Port $target_port is already in use by PID $listen_pid. Cannot start dashboard."
            return 1
        fi
    fi

    echo "Starting telemetry dashboard on port $target_port..."
    mkdir -p "$DATA_DIR"
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
            local p
            p=$(get_listening_pid_on_port "$target_port")
            if [[ "$p" == "$new_pid" ]] || grep -q "Server starting on" "$DASHBOARD_LOG_FILE" 2>/dev/null; then
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
    local pid
    pid=$(get_dashboard_pid || true)

    if [[ -n "$pid" ]]; then
        kill_pid_safely "$pid" "dashboard"
        rm -f "$DASHBOARD_PID_FILE" "$DASHBOARD_PORT_FILE"
        echo "✅ Dashboard stopped."
    else
        rm -f "$DASHBOARD_PID_FILE" "$DASHBOARD_PORT_FILE"
        echo "Dashboard not running."
    fi
}

start_proxy() {
    local pport="${1:-$PROXY_PORT}"
    if is_proxy_running; then
        local pid
        pid=$(get_proxy_pid || true)
        echo "Proxy already running (PID: $pid)"
        return 0
    fi

    local listen_pid
    listen_pid=$(get_listening_pid_on_port "$pport")
    if [[ -n "$listen_pid" ]]; then
        if is_proxy_pid "$listen_pid"; then
            kill_pid_safely "$listen_pid" "proxy"
        else
            echo "❌ Port $pport is already in use by PID $listen_pid. Cannot start proxy."
            return 1
        fi
    fi

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
    local pid
    pid=$(get_proxy_pid || true)
    if [[ -n "$pid" ]]; then
        kill_pid_safely "$pid" "proxy"
        rm -f "$PROXY_PID_FILE"
        echo "✅ Proxy stopped."
    else
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
        was_proxy_running=false
        if is_proxy_running; then
            was_proxy_running=true
        fi

        stop_dashboard
        if [[ "$was_proxy_running" == true ]]; then
            stop_proxy
        fi

        sleep 1
        start_dashboard "$@"

        if [[ "$was_proxy_running" == true ]] && ! is_proxy_running; then
            start_proxy "$PROXY_PORT"
        fi
        ;;

    status)
        echo "=== LLM Telemetry Suite Status ==="
        if is_dashboard_running; then
            local_port=$(cat "$DASHBOARD_PORT_FILE" 2>/dev/null || echo "$DEFAULT_PORT")
            local_pid=$(get_dashboard_pid || true)
            echo "✅ Dashboard: Running (PID $local_pid, port $local_port)"
            echo "   URL: http://localhost:$local_port"
        else
            echo "❌ Dashboard: Stopped"
        fi

        if is_proxy_running; then
            local_pid=$(get_proxy_pid || true)
            echo "✅ Proxy:     Running (PID $local_pid)"
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
                    echo "✅ Proxy running (PID: $(get_proxy_pid || true))"
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
        echo "Usage: ./dashboard.sh {start [port] [--with-proxy]|stop [--all]|restart [port]|status|url|proxy {start|stop|restart|status|logs}}"
        exit 1
        ;;
esac
