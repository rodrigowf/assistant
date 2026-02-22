#!/usr/bin/env python3
"""
Usage: scripts/youtube_api.py <resource> <method> [--params KEY=VALUE ...] [--body JSON]
Description: Execute any YouTube Data API call dynamically

Examples:
    # Get video details
    youtube_api.py videos list --params part=snippet,statistics id=dQw4w9WgXcQ

    # Search for videos
    youtube_api.py search list --params part=snippet q="python tutorial" type=video maxResults=5

    # Get channel info
    youtube_api.py channels list --params part=snippet,statistics id=UC_x5XG1OV2P6uZZ5FSM9Ttw

    # Create a playlist
    youtube_api.py playlists insert --params part=snippet,status --body '{"snippet":{"title":"Test"},"status":{"privacyStatus":"private"}}'

    # Add video to playlist
    youtube_api.py playlistItems insert --params part=snippet --body '{"snippet":{"playlistId":"PLxxx","resourceId":{"kind":"youtube#video","videoId":"xxx"}}}'
"""

import sys
import json
import argparse

from youtube_client import get_youtube_client


def parse_params(param_list: list[str]) -> dict:
    """Parse KEY=VALUE parameters into a dict."""
    params = {}
    for param in param_list:
        if '=' in param:
            key, value = param.split('=', 1)
            # Try to parse as JSON for complex values
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                # Try to parse as int
                try:
                    value = int(value)
                except ValueError:
                    pass  # Keep as string
            params[key] = value
    return params


def execute_api_call(resource: str, method: str, params: dict, body: dict = None) -> dict:
    """Execute a YouTube API call."""
    youtube = get_youtube_client()

    # Get the resource
    resource_obj = getattr(youtube, resource, None)
    if resource_obj is None:
        raise ValueError(f"Unknown resource: {resource}")

    resource_instance = resource_obj()

    # Get the method
    method_obj = getattr(resource_instance, method, None)
    if method_obj is None:
        raise ValueError(f"Unknown method: {method} on resource {resource}")

    # Build the call
    if body:
        params['body'] = body

    return method_obj(**params).execute()


def main():
    parser = argparse.ArgumentParser(
        description='Execute YouTube Data API calls',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Resources: videos, channels, playlists, playlistItems, subscriptions,
           search, comments, commentThreads, captions, activities

Methods: list, insert, update, delete (varies by resource)

Examples:
  %(prog)s videos list --params part=snippet,statistics id=VIDEO_ID
  %(prog)s search list --params part=snippet q="search term" maxResults=10
  %(prog)s playlists list --params part=snippet mine=true
        """
    )
    parser.add_argument('resource', help='API resource (videos, channels, playlists, etc.)')
    parser.add_argument('method', help='Method to call (list, insert, update, delete)')
    parser.add_argument('--params', '-p', nargs='*', default=[],
                        help='Parameters as KEY=VALUE pairs')
    parser.add_argument('--body', '-b', type=str, default=None,
                        help='Request body as JSON string (for insert/update)')
    parser.add_argument('--pretty', action='store_true', default=True,
                        help='Pretty print JSON output (default: true)')
    parser.add_argument('--compact', action='store_true',
                        help='Compact JSON output')

    args = parser.parse_args()

    # Parse parameters
    params = parse_params(args.params)

    # Parse body if provided
    body = None
    if args.body:
        try:
            body = json.loads(args.body)
        except json.JSONDecodeError as e:
            print(f"Error parsing body JSON: {e}", file=sys.stderr)
            sys.exit(1)

    try:
        result = execute_api_call(args.resource, args.method, params, body)

        if args.compact:
            print(json.dumps(result))
        else:
            print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({
            'error': str(e),
            'resource': args.resource,
            'method': args.method,
            'params': params
        }, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
