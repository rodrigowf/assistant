#!/usr/bin/env python3
"""
YouTube API Client

A reusable client for interacting with the YouTube Data API using OAuth2 tokens.
Handles token refresh automatically.

Usage:
    from youtube_client import get_youtube_client

    youtube = get_youtube_client()
    subscriptions = youtube.subscriptions().list(part='snippet', mine=True).execute()
"""

import json
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Project paths
PROJECT_ROOT = Path(__file__).parent.parent
TOKENS_PATH = PROJECT_ROOT / 'secrets' / 'youtube_tokens.json'

# YouTube API settings
API_SERVICE_NAME = 'youtube'
API_VERSION = 'v3'


def load_credentials() -> Credentials:
    """Load OAuth2 credentials from the saved tokens file."""
    if not TOKENS_PATH.exists():
        raise FileNotFoundError(
            f"YouTube tokens not found at {TOKENS_PATH}. "
            "Run 'python youtube_auth.py generate' to authorize."
        )

    with open(TOKENS_PATH) as f:
        tokens = json.load(f)

    credentials = Credentials(
        token=tokens['token'],
        refresh_token=tokens.get('refresh_token'),
        token_uri=tokens['token_uri'],
        client_id=tokens['client_id'],
        client_secret=tokens['client_secret'],
        scopes=tokens.get('scopes', [])
    )

    # Refresh the token if expired
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        # Save the refreshed token
        save_credentials(credentials)

    return credentials


def save_credentials(credentials: Credentials) -> None:
    """Save updated credentials back to the tokens file."""
    tokens = {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': list(credentials.scopes) if credentials.scopes else [],
    }

    with open(TOKENS_PATH, 'w') as f:
        json.dump(tokens, f, indent=2)


def get_youtube_client():
    """
    Build and return an authenticated YouTube API client.

    Returns:
        googleapiclient.discovery.Resource: YouTube API client

    Example:
        youtube = get_youtube_client()
        response = youtube.channels().list(part='snippet', mine=True).execute()
    """
    credentials = load_credentials()
    return build(API_SERVICE_NAME, API_VERSION, credentials=credentials)


def test_api():
    """Test the YouTube API by fetching user's channel info and subscriptions."""
    print("Building YouTube API client...")
    youtube = get_youtube_client()

    # Test 1: Get the authenticated user's channel
    print("\n" + "=" * 60)
    print("TEST 1: Fetching your YouTube channel info...")
    print("=" * 60)

    channels_response = youtube.channels().list(
        part='snippet,statistics',
        mine=True
    ).execute()

    if channels_response.get('items'):
        channel = channels_response['items'][0]
        snippet = channel['snippet']
        stats = channel.get('statistics', {})

        print(f"\nChannel: {snippet['title']}")
        print(f"Description: {snippet.get('description', 'N/A')[:100]}...")
        print(f"Subscribers: {stats.get('subscriberCount', 'Hidden')}")
        print(f"Total views: {stats.get('viewCount', 'N/A')}")
        print(f"Video count: {stats.get('videoCount', 'N/A')}")
    else:
        print("No channel found for this account.")

    # Test 2: List subscriptions
    print("\n" + "=" * 60)
    print("TEST 2: Fetching your subscriptions (first 10)...")
    print("=" * 60)

    subs_response = youtube.subscriptions().list(
        part='snippet',
        mine=True,
        maxResults=10,
        order='alphabetical'
    ).execute()

    if subs_response.get('items'):
        print(f"\nTotal subscriptions: {subs_response.get('pageInfo', {}).get('totalResults', 'Unknown')}")
        print("\nFirst 10 subscriptions:")
        for i, item in enumerate(subs_response['items'], 1):
            title = item['snippet']['title']
            channel_id = item['snippet']['resourceId']['channelId']
            print(f"  {i}. {title}")
    else:
        print("No subscriptions found.")

    # Test 3: List playlists
    print("\n" + "=" * 60)
    print("TEST 3: Fetching your playlists (first 5)...")
    print("=" * 60)

    playlists_response = youtube.playlists().list(
        part='snippet,contentDetails',
        mine=True,
        maxResults=5
    ).execute()

    if playlists_response.get('items'):
        print(f"\nTotal playlists: {playlists_response.get('pageInfo', {}).get('totalResults', 'Unknown')}")
        print("\nFirst 5 playlists:")
        for i, item in enumerate(playlists_response['items'], 1):
            title = item['snippet']['title']
            video_count = item['contentDetails']['itemCount']
            print(f"  {i}. {title} ({video_count} videos)")
    else:
        print("No playlists found.")

    print("\n" + "=" * 60)
    print("All tests completed successfully!")
    print("=" * 60)

    return {
        "status": "success",
        "channel": channels_response.get('items', [{}])[0].get('snippet', {}).get('title'),
        "subscriptions_count": subs_response.get('pageInfo', {}).get('totalResults', 0),
        "playlists_count": playlists_response.get('pageInfo', {}).get('totalResults', 0)
    }


if __name__ == '__main__':
    result = test_api()
    print(f"\nSummary: {json.dumps(result, indent=2)}")
