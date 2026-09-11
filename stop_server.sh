#!/usr/bin/env bash
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "🛑 Stopping de-job-intelligence platform services..."

# Graceful shutdown via PID files
if [ -f "logs/mcp.pid" ]; then
    PID=$(cat logs/mcp.pid)
    kill "$PID" 2>/dev/null && echo "Stopped MCP Server (PID $PID)" || true
    rm -f logs/mcp.pid
fi

if [ -f "logs/dashboard.pid" ]; then
    PID=$(cat logs/dashboard.pid)
    kill "$PID" 2>/dev/null && echo "Stopped Dashboard (PID $PID)" || true
    rm -f logs/dashboard.pid
fi

# Fallback cleanup for ports 8000 and 5001
fuser -k 8000/tcp 2>/dev/null || true
fuser -k 5001/tcp 2>/dev/null || true

echo "✅ All services stopped."