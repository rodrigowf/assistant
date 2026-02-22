---
name: youtube
description: YouTube API manager - search playlists, manage videos, explore subscriptions, and execute any YouTube Data API operation
argument-hint: "<command> [args...] | <natural language request>"
allowed-tools: Bash(scripts/run.sh *), Write, Read
---

# YouTube: $ARGUMENTS

Flexible YouTube API manager. Handle requests using existing scripts or dynamic API calls.

## Routing Logic

1. **Parse the request** — Identify the operation type
2. **Check for existing script** — Use optimized scripts when available
3. **Execute dynamically** — For new operations, use the API client directly
4. **Capture patterns** — If a dynamic operation is useful, suggest adding it as a script

## Pre-built Operations

Use these scripts for common operations (faster, tested, full-featured):

### search — Search all playlists
```bash
scripts/run.sh scripts/youtube_search_playlists.py "<keyword>" [--sort date|title|channel] [--limit N] [--json]
```

### playlist — Videos from one playlist
```bash
scripts/run.sh scripts/youtube_playlist.py "<name>" [--limit N] [--json]
```

### playlists — List all playlists
```bash
scripts/run.sh scripts/youtube_playlists.py [--filter TERM] [--sort name|count] [--json]
```

### subscriptions — List subscriptions
```bash
scripts/run.sh scripts/youtube_subscriptions.py [--limit N] [--json]
```

### info — Account statistics
```bash
scripts/run.sh scripts/youtube_client.py
```

### auth — Re-authenticate
```bash
scripts/run.sh scripts/youtube_auth.py generate
# Then after user provides code:
scripts/run.sh scripts/youtube_auth.py exchange <code>
```

## Dynamic API Operations

For operations without a dedicated script, execute Python directly using the API client.

### Pattern for Dynamic Calls

```bash
scripts/run.sh -c "
from scripts.youtube_client import get_youtube_client
youtube = get_youtube_client()

# Your API call here
result = youtube.<resource>().<method>(<params>).execute()

# Process and print results
import json
print(json.dumps(result, indent=2))
"
```

### Common API Resources

**videos** — Video metadata, statistics, content details
```python
# Get video details
youtube.videos().list(part='snippet,statistics', id='VIDEO_ID').execute()

# Search public videos
youtube.search().list(part='snippet', q='query', type='video', maxResults=10).execute()
```

**channels** — Channel info and statistics
```python
# Get channel by ID
youtube.channels().list(part='snippet,statistics', id='CHANNEL_ID').execute()

# Get channel by username
youtube.channels().list(part='snippet,statistics', forUsername='USERNAME').execute()
```

**playlists** — Playlist management
```python
# Create playlist
youtube.playlists().insert(part='snippet,status', body={
    'snippet': {'title': 'New Playlist', 'description': 'Description'},
    'status': {'privacyStatus': 'private'}
}).execute()

# Delete playlist
youtube.playlists().delete(id='PLAYLIST_ID').execute()
```

**playlistItems** — Add/remove videos from playlists
```python
# Add video to playlist
youtube.playlistItems().insert(part='snippet', body={
    'snippet': {
        'playlistId': 'PLAYLIST_ID',
        'resourceId': {'kind': 'youtube#video', 'videoId': 'VIDEO_ID'}
    }
}).execute()

# Remove from playlist
youtube.playlistItems().delete(id='PLAYLIST_ITEM_ID').execute()
```

**comments** — Video comments
```python
# Get comments on a video
youtube.commentThreads().list(part='snippet', videoId='VIDEO_ID', maxResults=20).execute()
```

**captions** — Video captions/subtitles
```python
# List captions for a video
youtube.captions().list(part='snippet', videoId='VIDEO_ID').execute()
```

## Example Requests

### Using Pre-built Scripts

| Request | Action |
|---------|--------|
| "search for jazz videos" | `youtube_search_playlists.py "jazz"` |
| "show my teori musical playlist" | `youtube_playlist.py "teori musical"` |
| "list all playlists with music" | `youtube_playlists.py --filter music` |
| "show my subscriptions" | `youtube_subscriptions.py` |

### Dynamic Operations

| Request | Approach |
|---------|----------|
| "get stats for video X" | Dynamic: `videos().list()` |
| "add video to playlist Y" | Dynamic: `playlistItems().insert()` |
| "create a new playlist called Z" | Dynamic: `playlists().insert()` |
| "get comments on video X" | Dynamic: `commentThreads().list()` |
| "find videos by channel ABC" | Dynamic: `search().list(channelId=...)` |

## Capturing New Scripts

When a dynamic operation proves useful, suggest creating a dedicated script:

1. **Identify the pattern** — What parameters does it need? What output format?
2. **Create the script** — Write to `scripts/youtube_<operation>.py`
3. **Update this skill** — Add to the pre-built operations list

Template for new scripts:
```python
#!/usr/bin/env python3
"""
Usage: scripts/youtube_<name>.py <args> [--json]
Description: <what it does>
"""
import json
import argparse
from youtube_client import get_youtube_client

def main():
    parser = argparse.ArgumentParser(description='...')
    # Add arguments
    parser.add_argument('--json', '-j', action='store_true')
    args = parser.parse_args()

    youtube = get_youtube_client()
    # API calls here

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        # Pretty print

if __name__ == '__main__':
    main()
```

## Scripts Library

| Script | Purpose | Status |
|--------|---------|--------|
| `youtube_api.py` | **Generic API caller** | ✅ Ready |
| `youtube_auth.py` | OAuth2 flow | ✅ Ready |
| `youtube_client.py` | API client (importable) | ✅ Ready |
| `youtube_search_playlists.py` | Search all playlists | ✅ Ready |
| `youtube_playlist.py` | List videos from playlist | ✅ Ready |
| `youtube_playlists.py` | List all playlists | ✅ Ready |
| `youtube_subscriptions.py` | List subscriptions | ✅ Ready |

### Generic API Script

Use `youtube_api.py` for any YouTube Data API operation without writing custom code:

```bash
# Get video details
scripts/run.sh scripts/youtube_api.py videos list --params part=snippet,statistics id=VIDEO_ID

# Search public videos
scripts/run.sh scripts/youtube_api.py search list --params part=snippet q="search term" type=video maxResults=10

# Get channel info
scripts/run.sh scripts/youtube_api.py channels list --params part=snippet,statistics id=CHANNEL_ID

# List video comments
scripts/run.sh scripts/youtube_api.py commentThreads list --params part=snippet videoId=VIDEO_ID maxResults=20

# Create a playlist (with body)
scripts/run.sh scripts/youtube_api.py playlists insert --params part=snippet,status --body '{"snippet":{"title":"New Playlist"},"status":{"privacyStatus":"private"}}'
```

## Fire TV Integration

This skill integrates with `/tv-remote` to play found videos directly on the Fire TV.

### Workflow: Search → Play on TV

1. **Search for videos** using this skill
2. **Get the video URL** from results (e.g., `https://www.youtube.com/watch?v=VIDEO_ID`)
3. **Play on Fire TV** using `/tv-remote`:
   - Launch YouTube app: `adb -s 192.168.0.16:5555 shell am start -n com.amazon.firetv.youtube/dev.cobalt.app.MainActivity`
   - Or open specific video via TvServerHub WebView: `adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.WebPageViewActivity -e url "https://www.youtube.com/watch?v=VIDEO_ID"`

### Example Combined Requests

| Request | Skills Used |
|---------|-------------|
| "Find my latest jazz videos and play one on the TV" | `/youtube` → `/tv-remote` |
| "Search for music theory tutorials and display on Fire TV" | `/youtube` → `/tv-remote` |
| "Show videos from my 'teori musical' playlist on the television" | `/youtube` → `/tv-remote` |

### Orchestrator Integration

When using the orchestrator (voice or text mode), requests like "play my jazz videos on the TV" can be handled automatically by combining both skills in sequence.

## Notes

- **Quota**: YouTube API has ~10,000 units/day. Search costs more than list operations.
- **Tokens**: Stored at `secrets/youtube_tokens.json`, auto-refresh when expired.
- **Scopes**: Current auth has `youtube.readonly` and `youtube.force-ssl` (read + manage).
- **JSON output**: All scripts support `--json` for programmatic use.
