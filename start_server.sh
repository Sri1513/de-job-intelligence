#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [ -d "venv" ]; then
    source venv/bin/activate
fi

mkdir -p logs

echo "🚀 Starting NEW platform (de-job-intelligence)..."

# Clear only its designated ports (8001 and 5002)
for PORT in 8001 5002; do
    STALE_PID=$(lsof -ti :$PORT 2>/dev/null || true)
    if [ -n "$STALE_PID" ]; then
        echo "🧹 Releasing port $PORT (PID: $STALE_PID)..."
        kill -9 $STALE_PID 2>/dev/null || true
    fi
done

# 1. MCP Server on 8001
nohup uvicorn src.protocols.app:app --host 0.0.0.0 --port 8001 > logs/mcp.log 2>&1 &
echo $! > logs/mcp.pid

# 2. Dashboard on 5002
nohup uvicorn src.dashboard.app:app --host 0.0.0.0 --port 5002 > logs/dashboard.log 2>&1 &
echo $! > logs/dashboard.pid

sleep 2

if kill -0 "$(cat logs/mcp.pid)" 2>/dev/null && lsof -Pi :8001 -sTCP:LISTEN -t >/dev/null; then
    echo "✅ New MCP Server running on http://localhost:8001 (PID: $(cat logs/mcp.pid))"
else
    echo "❌ New MCP Server failed to start. Last error from logs/mcp.log:"
    tail -n 12 logs/mcp.log
    exit 1
fi

if kill -0 "$(cat logs/dashboard.pid)" 2>/dev/null && lsof -Pi :5002 -sTCP:LISTEN -t >/dev/null; then
    echo "✅ New Dashboard running on http://localhost:5002 (PID: $(cat logs/dashboard.pid))"
else
    echo "❌ New Dashboard failed to start. Last error from logs/dashboard.log:"
    tail -n 12 logs/dashboard.log
    exit 1
fi