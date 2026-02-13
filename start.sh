#!/usr/bin/env bash
# Usage: ./start.sh
# Description: Start both backend and frontend servers for the assistant app
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}Starting Assistant Application...${NC}"

# Start backend in background
echo -e "${GREEN}Starting backend (port 8000)...${NC}"
scripts/run.sh -m uvicorn api.app:create_app --factory --port 8000 &
BACKEND_PID=$!

# Give backend a moment to start
sleep 2

# Start frontend in background
echo -e "${GREEN}Starting frontend (port 5173)...${NC}"
cd frontend && npm run dev &
FRONTEND_PID=$!
cd "$SCRIPT_DIR"

echo ""
echo -e "${GREEN}✓ Backend running at:${NC}  http://localhost:8000"
echo -e "${GREEN}✓ Frontend running at:${NC} http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop both servers"

# Handle cleanup on exit
cleanup() {
    echo ""
    echo -e "${BLUE}Shutting down...${NC}"
    kill $BACKEND_PID 2>/dev/null || true
    kill $FRONTEND_PID 2>/dev/null || true
    wait
    echo -e "${GREEN}Done.${NC}"
}
trap cleanup EXIT INT TERM

# Wait for both processes
wait
