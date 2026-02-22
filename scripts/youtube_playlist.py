#!/usr/bin/env python3
"""
Usage: scripts/youtube_playlist.py <playlist_name> [--limit N] [--json]
Description: List videos from a specific playlist, sorted by most recent first
"""

import sys
import json
import argparse
from datetime import datetime, timezone

from youtube_client import get_youtube_client


def find_playlist(youtube, name: str) -> tuple[str, str] | tuple[None, None]:
    """Find a playlist by name (case-insensitive partial match)."""
    playlists = []
    next_page = None

    while True:
        response = youtube.playlists().list(
            part='snippet,contentDetails',
            mine=True,
            maxResults=50,
            pageToken=next_page
        ).execute()
        playlists.extend(response.get('items', []))
        next_page = response.get('nextPageToken')
        if not next_page:
            break

    # Exact match first
    for p in playlists:
        if p['snippet']['title'].lower() == name.lower():
            return p['id'], p['snippet']['title']

    # Partial match
    for p in playlists:
        if name.lower() in p['snippet']['title'].lower():
            return p['id'], p['snippet']['title']

    return None, None


def get_playlist_videos(youtube, playlist_id: str) -> list[dict]:
    """Get all videos from a playlist."""
    videos = []
    next_page = None

    while True:
        response = youtube.playlistItems().list(
            part='snippet,contentDetails',
            playlistId=playlist_id,
            maxResults=50,
            pageToken=next_page
        ).execute()

        for item in response.get('items', []):
            videos.append({
                'title': item['snippet']['title'],
                'channel': item['snippet'].get('videoOwnerChannelTitle', 'Unknown'),
                'published_at': item['contentDetails'].get('videoPublishedAt', ''),
                'video_id': item['snippet']['resourceId']['videoId'],
                'url': f"https://www.youtube.com/watch?v={item['snippet']['resourceId']['videoId']}"
            })

        next_page = response.get('nextPageToken')
        if not next_page:
            break

    return videos


def parse_date(v: dict) -> datetime:
    """Parse video publish date for sorting."""
    d = v.get('published_at', '')
    if d:
        try:
            return datetime.fromisoformat(d.replace('Z', '+00:00'))
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def main():
    parser = argparse.ArgumentParser(
        description='List videos from a YouTube playlist'
    )
    parser.add_argument('playlist_name', help='Name of the playlist (partial match supported)')
    parser.add_argument('--limit', '-l', type=int, default=None,
                        help='Maximum number of videos to show')
    parser.add_argument('--json', '-j', action='store_true',
                        help='Output as JSON')

    args = parser.parse_args()

    youtube = get_youtube_client()

    # Find playlist
    playlist_id, playlist_title = find_playlist(youtube, args.playlist_name)

    if not playlist_id:
        print(f"Playlist not found: {args.playlist_name}")
        sys.exit(1)

    # Get videos
    videos = get_playlist_videos(youtube, playlist_id)

    # Sort by date (most recent first)
    videos.sort(key=parse_date, reverse=True)

    # Apply limit
    if args.limit:
        videos = videos[:args.limit]

    if args.json:
        result = {
            'playlist': playlist_title,
            'playlist_id': playlist_id,
            'total_videos': len(videos),
            'videos': videos
        }
        print(json.dumps(result, indent=2))
    else:
        print(f"Playlist: {playlist_title} ({len(videos)} videos)")
        print("=" * 60)

        for i, v in enumerate(videos, 1):
            date = v['published_at'][:10] if v['published_at'] else 'Unknown'
            print(f"\n{i}. {v['title']}")
            print(f"   Channel: {v['channel']}")
            print(f"   Published: {date}")
            print(f"   URL: {v['url']}")


if __name__ == '__main__':
    main()
