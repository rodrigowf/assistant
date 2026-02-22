#!/usr/bin/env python3
"""
YouTube Playlist Search

Search through all your YouTube playlists for videos matching a keyword.
Searches video titles and descriptions.

Usage:
    python youtube_search_playlists.py <keyword>
    python youtube_search_playlists.py "guitar lesson"
    python youtube_search_playlists.py jazz --limit 50
    python youtube_search_playlists.py jazz --sort date --order desc
    python youtube_search_playlists.py tutorial --sort title --order asc
"""

import sys
import json
import argparse
import re
from datetime import datetime
from typing import Generator, Callable

from youtube_client import get_youtube_client


# Sort key functions
def sort_by_date(match: dict) -> datetime:
    """Sort by video publish date."""
    date_str = match.get('published_at', '')
    if date_str:
        try:
            return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        except ValueError:
            pass
    return datetime.min


def sort_by_title(match: dict) -> str:
    """Sort by video title (case-insensitive)."""
    return match.get('title', '').lower()


def sort_by_playlist(match: dict) -> str:
    """Sort by playlist name (case-insensitive)."""
    return match.get('playlist', '').lower()


def sort_by_channel(match: dict) -> str:
    """Sort by channel name (case-insensitive)."""
    return match.get('channel', '').lower()


SORT_FUNCTIONS: dict[str, Callable] = {
    'date': sort_by_date,
    'title': sort_by_title,
    'playlist': sort_by_playlist,
    'channel': sort_by_channel,
}


def get_all_playlists(youtube) -> Generator[dict, None, None]:
    """Fetch all playlists for the authenticated user."""
    next_page_token = None

    while True:
        response = youtube.playlists().list(
            part='snippet,contentDetails',
            mine=True,
            maxResults=50,
            pageToken=next_page_token
        ).execute()

        for item in response.get('items', []):
            yield {
                'id': item['id'],
                'title': item['snippet']['title'],
                'description': item['snippet'].get('description', ''),
                'video_count': item['contentDetails']['itemCount'],
            }

        next_page_token = response.get('nextPageToken')
        if not next_page_token:
            break


def get_playlist_videos(youtube, playlist_id: str) -> Generator[dict, None, None]:
    """Fetch all videos in a playlist."""
    next_page_token = None

    while True:
        response = youtube.playlistItems().list(
            part='snippet,contentDetails',
            playlistId=playlist_id,
            maxResults=50,
            pageToken=next_page_token
        ).execute()

        for item in response.get('items', []):
            snippet = item['snippet']
            # Get the video's actual publish date from contentDetails
            published_at = item['contentDetails'].get('videoPublishedAt', '')

            yield {
                'video_id': snippet['resourceId']['videoId'],
                'title': snippet['title'],
                'description': snippet.get('description', ''),
                'channel': snippet.get('videoOwnerChannelTitle', 'Unknown'),
                'position': snippet['position'],
                'published_at': published_at,
                'added_at': snippet.get('publishedAt', ''),  # When added to playlist
            }

        next_page_token = response.get('nextPageToken')
        if not next_page_token:
            break


def search_playlists(keyword: str, case_sensitive: bool = False, limit: int = None) -> dict:
    """
    Search all playlists for videos matching a keyword.

    Args:
        keyword: Search term to match in video titles/descriptions
        case_sensitive: Whether to match case (default: False)
        limit: Maximum number of results to return (default: unlimited)

    Returns:
        dict with matches grouped by playlist
    """
    youtube = get_youtube_client()

    # Compile regex pattern
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(keyword), flags)

    results = {
        'keyword': keyword,
        'playlists_searched': 0,
        'videos_searched': 0,
        'matches': [],
        'by_playlist': {}
    }

    print(f"Searching for: '{keyword}'")
    print("=" * 60)

    # Iterate through all playlists
    for playlist in get_all_playlists(youtube):
        results['playlists_searched'] += 1
        playlist_title = playlist['title']
        playlist_id = playlist['id']

        # Skip empty playlists
        if playlist['video_count'] == 0:
            continue

        print(f"\rSearching playlist: {playlist_title[:40]:<40} ({playlist['video_count']} videos)", end='', flush=True)

        # Search videos in this playlist
        for video in get_playlist_videos(youtube, playlist_id):
            results['videos_searched'] += 1

            # Check if keyword matches title or description
            title_match = pattern.search(video['title'])
            desc_match = pattern.search(video['description'])

            if title_match or desc_match:
                match = {
                    'playlist': playlist_title,
                    'playlist_id': playlist_id,
                    'video_id': video['video_id'],
                    'title': video['title'],
                    'channel': video['channel'],
                    'published_at': video['published_at'],
                    'added_at': video['added_at'],
                    'match_in': 'title' if title_match else 'description',
                    'url': f"https://www.youtube.com/watch?v={video['video_id']}"
                }
                results['matches'].append(match)

                # Group by playlist
                if playlist_title not in results['by_playlist']:
                    results['by_playlist'][playlist_title] = []
                results['by_playlist'][playlist_title].append(match)

                # Check limit
                if limit and len(results['matches']) >= limit:
                    print(f"\n\nReached limit of {limit} results.")
                    return results

    print("\n")  # Clear the progress line
    return results


def sort_results(results: dict, sort_by: str, descending: bool = True) -> dict:
    """
    Sort search results by the specified field.

    Args:
        results: Search results dict
        sort_by: Field to sort by ('date', 'title', 'playlist', 'channel')
        descending: Sort in descending order (default: True for date, False for others)

    Returns:
        Results dict with sorted matches
    """
    if sort_by not in SORT_FUNCTIONS:
        print(f"Warning: Unknown sort field '{sort_by}'. Using 'date'.")
        sort_by = 'date'

    sort_func = SORT_FUNCTIONS[sort_by]
    results['matches'] = sorted(
        results['matches'],
        key=sort_func,
        reverse=descending
    )

    # Rebuild by_playlist grouping after sort
    results['by_playlist'] = {}
    for match in results['matches']:
        playlist_title = match['playlist']
        if playlist_title not in results['by_playlist']:
            results['by_playlist'][playlist_title] = []
        results['by_playlist'][playlist_title].append(match)

    results['sorted_by'] = sort_by
    results['sort_order'] = 'desc' if descending else 'asc'

    return results


def format_date(date_str: str) -> str:
    """Format ISO date string for display."""
    if not date_str:
        return 'Unknown date'
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return dt.strftime('%Y-%m-%d')
    except ValueError:
        return date_str[:10] if len(date_str) >= 10 else date_str


def print_results(results: dict, group_by_playlist: bool = True) -> None:
    """Pretty print search results."""
    print("=" * 60)
    print(f"SEARCH RESULTS FOR: '{results['keyword']}'")
    print("=" * 60)
    print(f"Playlists searched: {results['playlists_searched']}")
    print(f"Videos searched: {results['videos_searched']}")
    print(f"Matches found: {len(results['matches'])}")

    if results.get('sorted_by'):
        order = 'descending' if results.get('sort_order') == 'desc' else 'ascending'
        print(f"Sorted by: {results['sorted_by']} ({order})")

    print("=" * 60)

    if not results['matches']:
        print("\nNo matches found.")
        return

    # If sorted by something other than playlist, show flat list
    if results.get('sorted_by') and results['sorted_by'] != 'playlist':
        group_by_playlist = False

    if group_by_playlist:
        # Print grouped by playlist
        for playlist_name, videos in results['by_playlist'].items():
            print(f"\n📁 {playlist_name} ({len(videos)} matches)")
            print("-" * 40)
            for video in videos:
                print(f"  🎬 {video['title']}")
                print(f"     Channel: {video['channel']}")
                print(f"     Published: {format_date(video.get('published_at', ''))}")
                print(f"     URL: {video['url']}")
                print()
    else:
        # Print flat sorted list
        for i, video in enumerate(results['matches'], 1):
            print(f"\n{i}. 🎬 {video['title']}")
            print(f"   📁 Playlist: {video['playlist']}")
            print(f"   📺 Channel: {video['channel']}")
            print(f"   📅 Published: {format_date(video.get('published_at', ''))}")
            print(f"   🔗 {video['url']}")


def main():
    parser = argparse.ArgumentParser(
        description='Search your YouTube playlists for videos matching a keyword',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "guitar"                      # Basic search
  %(prog)s "jazz" --sort date            # Sort by publish date (newest first)
  %(prog)s "tutorial" --sort date --asc  # Sort by date (oldest first)
  %(prog)s "music" --sort title          # Sort alphabetically by title
  %(prog)s "rock" --sort channel         # Sort by channel name
  %(prog)s "coding" --limit 20 --json    # Limit results, output as JSON
        """
    )
    parser.add_argument('keyword', help='Search term to find in video titles/descriptions')
    parser.add_argument('--case-sensitive', '-c', action='store_true',
                        help='Match case when searching')
    parser.add_argument('--limit', '-l', type=int, default=None,
                        help='Maximum number of results to return')
    parser.add_argument('--sort', '-s', choices=['date', 'title', 'playlist', 'channel'],
                        default=None, help='Sort results by: date, title, playlist, or channel')
    parser.add_argument('--asc', action='store_true',
                        help='Sort in ascending order (default is descending for date, ascending for others)')
    parser.add_argument('--desc', action='store_true',
                        help='Sort in descending order')
    parser.add_argument('--json', '-j', action='store_true',
                        help='Output results as JSON')

    args = parser.parse_args()

    results = search_playlists(
        keyword=args.keyword,
        case_sensitive=args.case_sensitive,
        limit=args.limit
    )

    # Apply sorting if requested
    if args.sort:
        # Determine sort order
        # Default: descending for date (newest first), ascending for text fields
        if args.desc:
            descending = True
        elif args.asc:
            descending = False
        else:
            # Default based on sort field
            descending = (args.sort == 'date')

        results = sort_results(results, args.sort, descending)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_results(results)


if __name__ == '__main__':
    main()
