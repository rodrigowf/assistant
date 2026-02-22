#!/usr/bin/env python3
"""
Usage: scripts/youtube_playlists.py [--filter TERM] [--json]
Description: List all your YouTube playlists with video counts
"""

import json
import argparse

from youtube_client import get_youtube_client


def get_all_playlists(youtube) -> list[dict]:
    """Fetch all playlists for the authenticated user."""
    playlists = []
    next_page = None

    while True:
        response = youtube.playlists().list(
            part='snippet,contentDetails',
            mine=True,
            maxResults=50,
            pageToken=next_page
        ).execute()

        for item in response.get('items', []):
            playlists.append({
                'id': item['id'],
                'title': item['snippet']['title'],
                'description': item['snippet'].get('description', ''),
                'video_count': item['contentDetails']['itemCount'],
            })

        next_page = response.get('nextPageToken')
        if not next_page:
            break

    return playlists


def main():
    parser = argparse.ArgumentParser(
        description='List your YouTube playlists'
    )
    parser.add_argument('--filter', '-f', type=str, default=None,
                        help='Filter playlists by name (case-insensitive)')
    parser.add_argument('--sort', '-s', choices=['name', 'count'], default='count',
                        help='Sort by: name or count (default: count)')
    parser.add_argument('--json', '-j', action='store_true',
                        help='Output as JSON')

    args = parser.parse_args()

    youtube = get_youtube_client()
    playlists = get_all_playlists(youtube)

    # Filter if specified
    if args.filter:
        filter_term = args.filter.lower()
        playlists = [p for p in playlists if filter_term in p['title'].lower()]

    # Sort
    if args.sort == 'count':
        playlists.sort(key=lambda p: p['video_count'], reverse=True)
    else:
        playlists.sort(key=lambda p: p['title'].lower())

    if args.json:
        print(json.dumps({
            'total': len(playlists),
            'playlists': playlists
        }, indent=2))
    else:
        total_videos = sum(p['video_count'] for p in playlists)
        print(f"Your Playlists: {len(playlists)} playlists, {total_videos} total videos")
        print("=" * 60)

        for p in playlists:
            print(f"{p['title']}: {p['video_count']} videos")


if __name__ == '__main__':
    main()
