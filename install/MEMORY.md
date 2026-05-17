# Assistant Memory Index

Reference index to detailed memory files. Keep this under 200 lines.

## Getting Started

Welcome to your personal assistant! This memory file helps the AI remember
important context across sessions.

### How Memory Works

- This file (`MEMORY.md`) is an index — keep it under 200 lines
- Store detailed content in separate `.md` files in this folder
- Add one-line references here: `- filename.md — Brief description`

## Quick Reference

### Running the Assistant

1. Start the backend:

       context/scripts/run.sh -m uvicorn api.app:create_app --factory --port 8765

2. Start the frontend (new terminal):

       cd frontend && npm run dev

3. Open https://localhost:5432

### Useful Commands

| Command | Description |
|---------|-------------|
| `/recall <query>` | Search memory and history |
| `/scaffold-skill` | Create a new skill |
| `/scaffold-agent` | Create a new agent |
| `/help` | List available skills |

## Memory Files

_Add references to topic files as you create them, one line each._

## Tracked Projects

_Add active project entries here, one line each, with a link to the detailed memory file._

## User

_Personalize this section as you learn about the user. Detailed personal context can live in `user_context.md` (or similar) and be linked from here._
