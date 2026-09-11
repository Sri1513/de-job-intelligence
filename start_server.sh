#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# Activate virtual environment
if [ -d "venv" ]; then
    source venv/bin/activate
fi

mkdir -p logs

echo "🚀 Starting de-job-intelligence platform services..."

# 1. Start MCP Protocol Server (Port 8000)
if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null ; then
    echo "⚠️ Port 8000 is already in use. Skipping MCP server start."
else
    nohup uvicorn src.protocols.app:app --host 0.0.0.0 --port 8000 > logs/mcp.log 2>&1 &
    echo $! > logs/mcp.pid
    echo "✅ MCP Protocol Server running on http://localhost:8000 (PID: $(cat logs/mcp.pid))"
fi

# 2. Start Observability Dashboard (Port 5001)
if lsof -Pi :5001 -sTCP:LISTEN -t >/dev/null ; then
    echo "⚠️ Port 5001 is already in use. Skipping Dashboard start."
else
    nohup uvicorn src.dashboard.app:app --host 0.0.0.0 --port 5001 > logs/dashboard.log 2>&1 &
    echo $! > logs/dashboard.pid
    echo "✅ Dashboard running on http://localhost:5001 (PID: $(cat logs/dashboard.pid))"
fi

echo ""
echo "Inspect logs with:"
echo "  tail -f logs/mcp.log"
echo "  tail -f logs/dashboard.log"