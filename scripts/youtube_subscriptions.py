#!/usr/bin/env python3
"""
Usage: scripts/youtube_subscriptions.py [--limit N] [--json]
Description: List your YouTube subscriptions
"""

import json
import argparse

from youtube_client import get_youtube_client


def get_subscriptions(youtube, limit: int = None) -> list[dict]:
    """Fetch subscriptions for the authenticated user."""
    subs = []
    next_page = None
    max_results = limit or 1000  # Default high limit

    while len(subs) < max_results:
        response = youtube.subscriptions().list(
            part='snippet',
            mine=True,
            maxResults=min(50, max_results - len(subs)),
            pageToken=next_page,
            order='alphabetical'
        ).execute()

        for item in response.get('items', []):
            subs.append({
                'title': item['snippet']['title'],
                'channel_id': item['snippet']['resourceId']['channelId'],
                'description': item['snippet'].get('description', '')[:100],
            })

        next_page = response.get('nextPageToken')
        if not next_page:
            break

    return subs


def main():
    parser = argparse.ArgumentParser(
        description='List your YouTube subscriptions'
    )
    parser.add_argument('--limit', '-l', type=int, default=50,
                        help='Maximum number of subscriptions to show (default: 50)')
    parser.add_argument('--json', '-j', action='store_true',
                        help='Output as JSON')

    args = parser.parse_args()

    youtube = get_youtube_client()
    subs = get_subscriptions(youtube, args.limit)

    if args.json:
        print(json.dumps({
            'total_shown': len(subs),
            'subscriptions': subs
        }, indent=2))
    else:
        print(f"Subscriptions ({len(subs)} shown)")
        print("=" * 60)

        for i, s in enumerate(subs, 1):
            print(f"{i}. {s['title']}")


if __name__ == '__main__':
    main()
