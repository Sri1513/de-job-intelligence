#!/usr/bin/env bash
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "🛑 Stopping NEW platform (de-job-intelligence)..."
for PID_FILE in logs/mcp.pid logs/dashboard.pid; do
    if [ -f "$PID_FILE" ]; then
        kill "$(cat "$PID_FILE")" 2>/dev/null || true
        rm -f "$PID_FILE"
    fi
done

for PORT in 8001 5002; do
    REMAINING=$(lsof -ti :$PORT 2>/dev/null || true)
    if [ -n "$REMAINING" ]; then
        kill -9 $REMAINING 2>/dev/null || true
    fi
done
echo "✅ New platform stopped."
