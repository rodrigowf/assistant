---
name: recall
description: Search memory and conversation history for relevant information. Use when you need to find past decisions, patterns, or context.
argument-hint: "<query> [--n COUNT]"
allowed-tools: Bash(scripts/run.sh *), Read
---

# Recall: $ARGUMENTS

Search the vector index for information related to the query.

## Arguments

- `$0`: The search query (required)
- `--n COUNT`: Number of results per collection (default: 5, max recommended: 20)

## What Gets Searched

- **memory**: Claude Code's auto-memory files (patterns, preferences, insights)
- **history**: Past conversation sessions (all user/assistant exchanges)

The index is updated automatically:
- Memory: indexed immediately when files change (file watcher)
- History: indexed every 2 minutes by the API server (if files changed)

## Steps

1. Parse arguments. Extract the query and optional `--n` value from `$ARGUMENTS`.
   Default to 5 results if not specified. Increase for broader searches, decrease for focused lookups.

2. Run the search script against both collections:

   For memory: scripts/run.sh scripts/search.py <query> --collection memory --n <count>
   For history: scripts/run.sh scripts/search.py <query> --collection history --n <count>

3. Review the results. Each result includes:
   - `text`: The matched chunk content
   - `file_path`: Source file path
   - `start_line` / `end_line`: Line range in source
   - `distance`: Similarity score (lower = more relevant)

4. For the most relevant results (distance < 0.5), read the original files at the indicated lines to get full context.

5. Synthesize and present the findings to the user, citing the source files.

## Notes

- If the index is empty, run: scripts/run.sh scripts/index-memory.py
- Distance interpretation: < 0.3 = highly relevant, 0.3-0.7 = relevant, > 1.0 = likely noise
- Memory files are at: context/memory/
- Session files are at: context/*.jsonl
