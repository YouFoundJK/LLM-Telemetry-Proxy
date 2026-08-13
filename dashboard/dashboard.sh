#!/bin/bash
#
# Telemetry Dashboard control script.
#
# Usage:
#   ./dashboard.sh start [port]   — start dashboard server (default port 9091)
#   ./dashboard.sh stop           — kill dashboard server
#   ./dashboard.sh restart [port] — restart
#   ./dashboard.sh status          — check if running
#   ./dashboard.sh url             — print the URL
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/.dashboard.pid"
LOG_FILE="$SCRIPT_DIR/.dashboard.log"
PORT="${2:-9118}"
PYTHON="${PYTHON:-python3}"

is_running() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$PID_FILE"
    fi
    return 1
}

case "${1:-}" in
    start)
        if is_running; then
            echo "Dashboard already running (PID $(cat "$PID_FILE"))"
            echo "URL: http://localhost:$(cat "$SCRIPT_DIR/.dashboard.port" 2>/dev/null || echo "$PORT")"
            exit 0
        fi
        PORT="${2:-9118}"
        echo "Starting telemetry dashboard on port $PORT..."
        cd "$SCRIPT_DIR"
        nohup "$PYTHON" server.py --port "$PORT" > "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo "$PORT" > "$SCRIPT_DIR/.dashboard.port"
        sleep 1
        if is_running; then
            echo "✅ Dashboard running on http://localhost:$PORT"
            echo "   PID: $(cat "$PID_FILE")"
            echo "   Log: $LOG_FILE"
        else
            echo "❌ Failed to start. Check log:"
            cat "$LOG_FILE"
            exit 1
        fi
        ;;

    stop)
        if is_running; then
            local_pid=$(cat "$PID_FILE")
            echo "Stopping dashboard (PID $local_pid)..."
            kill "$local_pid" 2>/dev/null || true
            sleep 1
            kill -9 "$local_pid" 2>/dev/null || true
            rm -f "$PID_FILE" "$SCRIPT_DIR/.dashboard.port"
            echo "✅ Dashboard stopped."
        else
            echo "Dashboard not running."
        fi
        ;;

    restart)
        "$0" stop
        sleep 1
        "$0" start "${2:-9091}"
        ;;

    status)
        if is_running; then
            local_port=$(cat "$SCRIPT_DIR/.dashboard.port" 2>/dev/null || echo "?")
            echo "✅ Running (PID $(cat "$PID_FILE"), port $local_port)"
            echo "   URL: http://localhost:$local_port"
        else
            echo "❌ Not running."
        fi
        ;;

    url)
        if is_running; then
            local_port=$(cat "$SCRIPT_DIR/.dashboard.port" 2>/dev/null || echo "$PORT")
            echo "http://localhost:$local_port"
        else
            echo "Dashboard not running. Start with: $0 start"
        fi
        ;;

    *)
        echo "Usage: $0 {start [port]|stop|restart [port]|status|url}"
        exit 1
        ;;
esac
