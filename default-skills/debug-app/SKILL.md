---
name: debug-app
description: Start and debug the assistant application (backend + frontend). Launches servers, automates browser testing via Chrome DevTools MCP, and helps diagnose issues.
argument-hint: "[action]"
allowed-tools: Read, Write, Bash(*), mcp__chrome-devtools__*
---

# Debug the Assistant Application

This skill provides a complete workflow for starting, testing, and debugging the assistant application.

## Quick Reference

**Start everything**: Run both backend and frontend servers with logging.
**Browser automation**: Use Chrome DevTools MCP tools to interact with the UI.
**Debug issues**: Read logs, check console messages, trace WebSocket connections.

Action requested: **$ARGUMENTS**

---

## 1. Starting the Servers

### Prerequisites

Ensure the logs directory exists. Create it if missing by running mkdir for the logs folder at the project root.

### Backend Server

Start the FastAPI backend with uvicorn, using the factory pattern. The command structure is:

Run the uvicorn module through context/scripts/run.sh with the api.app:create_app factory on port 8000. Pipe output through tee to save a timestamped log file in the logs directory. Run in background with ampersand.

Example log filename format: api_YYYYMMDD_HHMMSS.log

### Frontend Server

Start the Vite dev server from the frontend directory. Pipe output through tee to save a timestamped log in the logs directory (use parent path since you change directory). Run in background.

Example log filename format: frontend_YYYYMMDD_HHMMSS.log

### Verify Startup

After starting both servers:
1. Wait a few seconds for servers to initialize
2. Check that processes are running with ps aux filtered for uvicorn and vite
3. Backend should be on port 8000, frontend on port 5173

---

## 2. Browser Automation with Chrome DevTools MCP

Use these MCP tools to automate browser interaction with the frontend.

### Opening the Application

Use the new_page tool to open a browser tab at the frontend URL (https://localhost:5173). This creates a new Chrome page you can control.

### Taking Snapshots

Use take_snapshot to capture the current UI state. This returns a text representation of the page with unique identifiers (uid) for each element. Always take a fresh snapshot before interacting — element uids may change after page updates.

### Clicking Elements

After taking a snapshot, identify the target element's uid and use the click tool. For buttons, links, or any interactive element, pass the uid from the snapshot.

### Filling Input Fields

Use the fill tool with a uid and value to type into input fields or text areas. For the chat input, find its uid in the snapshot and fill with your test message.

### Waiting for Content

Use wait_for with a text string to pause until that text appears on the page. This is essential after actions that trigger async operations (like sending a message and waiting for a response).

### Checking Console Messages

Use list_console_messages to see JavaScript console output. This reveals errors, warnings, and debug logs from the frontend. Use get_console_message with a specific msgid for detailed information.

### Page Navigation

Use navigate_page to:
- Reload the page (type: reload)
- Go back (type: back)
- Go forward (type: forward)
- Navigate to URL (type: url, with url parameter)

### Taking Screenshots

Use take_screenshot to capture a visual image of the page. Add fullPage: true for the entire page, or pass a uid to screenshot a specific element.

---

## 3. Reading Logs for Debugging

### Log File Locations

All logs are stored in the logs directory at the project root:
- Backend logs: api_YYYYMMDD_HHMMSS.log
- Frontend logs: frontend_YYYYMMDD_HHMMSS.log

### Reading Log Files

Use the Read tool to view log contents. For recent logs, list the logs directory first to find the latest files, then read them.

### Live Log Monitoring

For real-time output, use tail with the follow flag on the log file. Run in background or with a limited number of lines.

### What to Look For

**Backend logs**:
- Startup messages confirming the server is running
- WebSocket connection events (connect, disconnect)
- API request/response details
- Python tracebacks and exceptions
- Session manager events

**Frontend logs**:
- Vite compilation status
- Hot module replacement (HMR) updates
- Build errors or warnings

---

## 4. Common Debugging Scenarios

### WebSocket Connection Issues

**Symptoms**: UI shows disconnected state, messages not sending/receiving.

**Debug steps**:
1. Check browser console for WebSocket errors using list_console_messages
2. Verify backend is running and accepting connections
3. Look for CORS or proxy issues in backend logs
4. Check the Vite proxy configuration if in dev mode

### Session Start Failures

**Symptoms**: Cannot start new chat, session errors in console.

**Debug steps**:
1. Check backend logs for session manager errors
2. Verify Claude Code SDK is properly initialized
3. Look for authentication issues (OAuth flow may be needed)
4. Check if there are existing sessions that need cleanup

### Frontend Rendering Problems

**Symptoms**: UI not updating, components missing, blank screen.

**Debug steps**:
1. Take a snapshot to see current DOM state
2. Check console for React errors or warnings
3. Verify WebSocket events are being received
4. Look for JavaScript exceptions in console messages

### API Errors

**Symptoms**: REST calls failing, 4xx/5xx responses.

**Debug steps**:
1. Check backend logs for the specific endpoint
2. Use list_network_requests in Chrome DevTools to see request/response details
3. Verify request format matches expected schema
4. Look for validation errors in the response body

---

## 5. Typical Debug Session Workflow

1. **Start servers** — Launch backend and frontend with logging
2. **Open browser** — Use new_page to open the frontend
3. **Take initial snapshot** — Capture the starting state
4. **Interact with UI** — Click, fill, wait as needed
5. **Monitor for issues** — Check console messages and network requests
6. **Read logs on failure** — Examine backend and frontend logs
7. **Iterate** — Make code changes, reload, test again

---

## 6. Useful Commands Summary

**Check running processes**: Use ps aux with grep for uvicorn and vite/node.

**Kill servers**: Use pkill or kill with the process IDs found above.

**Check port usage**: Use lsof or ss to verify ports 8000 and 5173.

**Restart cleanly**: Kill existing processes, then start fresh with new log files.
