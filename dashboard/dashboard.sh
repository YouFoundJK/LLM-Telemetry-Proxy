#!/bin/bash
#
# Telemetry Dashboard & Proxy Gateway control script.
#
# Usage:
#   ./dashboard.sh start [port] [--with-proxy] — start dashboard server (default port 9118)
#   ./dashboard.sh stop [--all]                — kill dashboard server (and proxy if --all)
#   ./dashboard.sh restart [port]             — restart dashboard server
#   ./dashboard.sh status                     — check status of dashboard and proxy
#   ./dashboard.sh url                        — print the dashboard URL
#   ./dashboard.sh proxy {start|stop|restart|status|logs} — proxy subcommands
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DASHBOARD_PID_FILE="$SCRIPT_DIR/.dashboard.pid"
DASHBOARD_LOG_FILE="$SCRIPT_DIR/.dashboard.log"
PROXY_PID_FILE="$REPO_ROOT/data/.proxy.pid"
PROXY_LOG_FILE="$REPO_ROOT/data/proxy.log"

PORT="${2:-9118}"
PROXY_PORT="9090"
PYTHON="${PYTHON:-python3}"

is_dashboard_running() {
    if [[ -f "$DASHBOARD_PID_FILE" ]]; then
        local pid
        pid=$(cat "$DASHBOARD_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$DASHBOARD_PID_FILE"
    fi
    return 1
}

is_proxy_running() {
    if [[ -f "$PROXY_PID_FILE" ]]; then
        local pid
        pid=$(cat "$PROXY_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$PROXY_PID_FILE"
    fi
    return 1
}

start_proxy() {
    local pport="${1:-$PROXY_PORT}"
    if is_proxy_running; then
        echo "Proxy already running (PID $(cat "$PROXY_PID_FILE"))"
        return 0
    fi
    echo "Starting LLM telemetry proxy on port $pport..."
    mkdir -p "$REPO_ROOT/data"
    cd "$REPO_ROOT"
    nohup "$PYTHON" "$REPO_ROOT/proxy/llm_telemetry_proxy.py" --port "$pport" > "$PROXY_LOG_FILE" 2>&1 &
    sleep 1
    if is_proxy_running; then
        echo "✅ Proxy running on http://localhost:$pport (PID $(cat "$PROXY_PID_FILE"))"
        echo "   Log: $PROXY_LOG_FILE"
    else
        echo "❌ Proxy failed to start. Check log: $PROXY_LOG_FILE"
        return 1
    fi
}

stop_proxy() {
    if is_proxy_running; then
        local pid
        pid=$(cat "$PROXY_PID_FILE")
        echo "Stopping proxy (PID $pid)..."
        kill "$pid" 2>/dev/null || true
        sleep 1
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$PROXY_PID_FILE"
        echo "✅ Proxy stopped."
    else
        echo "Proxy not running."
    fi
}

case "${1:-}" in
    start)
        WITH_PROXY=false
        TARGET_PORT="9118"

        for arg in "${@:2}"; do
            if [[ "$arg" == "--with-proxy" ]]; then
                WITH_PROXY=true
            elif [[ "$arg" =~ ^[0-9]+$ ]]; then
                TARGET_PORT="$arg"
            fi
        done

        if is_dashboard_running; then
            echo "Dashboard already running (PID $(cat "$DASHBOARD_PID_FILE"))"
            echo "URL: http://localhost:$(cat "$SCRIPT_DIR/.dashboard.port" 2>/dev/null || echo "$TARGET_PORT")"
        else
            echo "Starting telemetry dashboard on port $TARGET_PORT..."
            cd "$SCRIPT_DIR"
            nohup "$PYTHON" server.py --port "$TARGET_PORT" > "$DASHBOARD_LOG_FILE" 2>&1 &
            echo $! > "$DASHBOARD_PID_FILE"
            echo "$TARGET_PORT" > "$SCRIPT_DIR/.dashboard.port"
            sleep 1
            if is_dashboard_running; then
                echo "✅ Dashboard running on http://localhost:$TARGET_PORT"
                echo "   PID: $(cat "$DASHBOARD_PID_FILE")"
                echo "   Log: $DASHBOARD_LOG_FILE"
            else
                echo "❌ Failed to start dashboard. Check log:"
                cat "$DASHBOARD_LOG_FILE"
                exit 1
            fi
        fi

        if [[ "$WITH_PROXY" == true ]]; then
            start_proxy "$PROXY_PORT"
        fi
        ;;

    stop)
        STOP_ALL=false
        for arg in "${@:2}"; do
            if [[ "$arg" == "--all" ]]; then
                STOP_ALL=true
            fi
        done

        if is_dashboard_running; then
            local_pid=$(cat "$DASHBOARD_PID_FILE")
            echo "Stopping dashboard (PID $local_pid)..."
            kill "$local_pid" 2>/dev/null || true
            sleep 1
            kill -9 "$local_pid" 2>/dev/null || true
            rm -f "$DASHBOARD_PID_FILE" "$SCRIPT_DIR/.dashboard.port"
            echo "✅ Dashboard stopped."
        else
            echo "Dashboard not running."
        fi

        if [[ "$STOP_ALL" == true ]]; then
            stop_proxy
        fi
        ;;

    restart)
        "$0" stop
        sleep 1
        "$0" start "${2:-9118}"
        ;;

    status)
        echo "=== LLM Telemetry Suite Status ==="
        if is_dashboard_running; then
            local_port=$(cat "$SCRIPT_DIR/.dashboard.port" 2>/dev/null || echo "?")
            echo "✅ Dashboard: Running (PID $(cat "$DASHBOARD_PID_FILE"), port $local_port)"
            echo "   URL: http://localhost:$local_port"
        else
            echo "❌ Dashboard: Stopped"
        fi

        if is_proxy_running; then
            echo "✅ Proxy:     Running (PID $(cat "$PROXY_PID_FILE"))"
            echo "   Log: $PROXY_LOG_FILE"
        else
            echo "❌ Proxy:     Stopped"
        fi
        ;;

    url)
        if is_dashboard_running; then
            local_port=$(cat "$SCRIPT_DIR/.dashboard.port" 2>/dev/null || echo "$PORT")
            echo "http://localhost:$local_port"
        else
            echo "Dashboard not running. Start with: $0 start"
        fi
        ;;

    proxy)
        case "${2:-}" in
            start)
                start_proxy "${3:-$PROXY_PORT}"
                ;;
            stop)
                stop_proxy
                ;;
            restart)
                stop_proxy
                sleep 1
                start_proxy "${3:-$PROXY_PORT}"
                ;;
            status)
                if is_proxy_running; then
                    echo "✅ Proxy running (PID $(cat "$PROXY_PID_FILE"))"
                else
                    echo "❌ Proxy not running."
                fi
                ;;
            logs)
                tail -n "${3:-50}" "$PROXY_LOG_FILE" 2>/dev/null || echo "No logs found at $PROXY_LOG_FILE"
                ;;
            *)
                echo "Usage: $0 proxy {start [port]|stop|restart [port]|status|logs [lines]}"
                exit 1
                ;;
        esac
        ;;

    *)
        echo "Usage: $0 {start [port] [--with-proxy]|stop [--all]|restart [port]|status|url|proxy {start|stop|restart|status|logs}}"
        exit 1
        ;;
esac

