#!/bin/bash
#
# Root launcher for LLM Telemetry Dashboard & Proxy Gateway.
#
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/dashboard/dashboard.sh" "$@"
