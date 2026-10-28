#!/usr/bin/env bash
# Start the three host processes together: MCP server, outbox sender, scheduler.
# Ctrl-C (or SIGTERM) stops all three cleanly: each one finishes its current
# item and gives back what it did not send. Logs go to data/logs/.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p data/logs
order-desk init

pids=()
order-desk serve     > data/logs/serve.log 2>&1 & pids+=($!)
order-desk sender    > data/logs/sender.log 2>&1 & pids+=($!)
order-desk scheduler > data/logs/scheduler.log 2>&1 & pids+=($!)

stop() {
  echo "Stopping (SIGTERM) ..."
  kill -TERM "${pids[@]}" 2>/dev/null || true
  wait "${pids[@]}" 2>/dev/null || true
  echo "Stopped."
}
trap stop INT TERM

echo "Running: MCP server on 127.0.0.1:${MCP_PORT:-8710}, outbox sender, scheduler."
echo "Logs: data/logs/*.log   Health: curl http://127.0.0.1:${MCP_PORT:-8710}/health"
wait
