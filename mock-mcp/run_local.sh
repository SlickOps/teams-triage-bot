#!/usr/bin/env bash
# Launches all three mock MCP server personalities locally in the background,
# on the ports the phase-3 contract fixes for local dev. Prints the full MCP
# URLs (incl. /mcp path) to export as the brain's JENKINS_MCP_URL /
# DATADOG_MCP_URL / ARGOCD_MCP_URL.
#
# Usage:
#   ./run_local.sh start   # launch all three (default if no arg given)
#   ./run_local.sh stop    # kill them
set -euo pipefail

cd "$(dirname "$0")"

PIDFILE=".run_local.pids"

start() {
    if [ ! -d .venv ]; then
        python3.12 -m venv .venv
        .venv/bin/pip install -q -r requirements.txt
    fi

    : > "$PIDFILE"

    # macOS ships bash 3.2 (no associative arrays) -- keep this portable.
    for pair in jenkins:8801 datadog:8802 argocd:8803; do
        kind="${pair%%:*}"
        port="${pair##*:}"
        MCP_SERVER_KIND="$kind" PORT="$port" .venv/bin/python server.py \
            > "/tmp/mock-mcp-${kind}.log" 2>&1 &
        echo "$!" >> "$PIDFILE"
        echo "started ${kind} (pid $!) -> http://127.0.0.1:${port}/mcp  (log: /tmp/mock-mcp-${kind}.log)"
    done

    sleep 1
    echo
    echo "export JENKINS_MCP_URL=http://127.0.0.1:8801/mcp"
    echo "export DATADOG_MCP_URL=http://127.0.0.1:8802/mcp"
    echo "export ARGOCD_MCP_URL=http://127.0.0.1:8803/mcp"
}

stop() {
    if [ -f "$PIDFILE" ]; then
        while read -r pid; do
            kill "$pid" 2>/dev/null || true
        done < "$PIDFILE"
        rm -f "$PIDFILE"
        echo "stopped."
    else
        echo "no pidfile ($PIDFILE) -- nothing to stop."
    fi
}

case "${1:-start}" in
    start) start ;;
    stop) stop ;;
    *) echo "usage: $0 [start|stop]"; exit 1 ;;
esac
