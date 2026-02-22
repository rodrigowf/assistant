#!/usr/bin/env python3
"""
Test script to authenticate with Google Home and list all devices.
Uses OAuth 2.0 flow to get credentials and queries the Home Graph API.
"""

import json
import os
import sys
from pathlib import Path
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle

# Scopes required for Home Graph API
SCOPES = [
    'https://www.googleapis.com/auth/homegraph',
    'https://www.googleapis.com/auth/sdm.service'
]

# Path to store the token
TOKEN_PATH = Path(__file__).parent.parent / 'secrets' / 'google_home_token.pickle'


def get_credentials(client_secret_path):
    """Get valid user credentials from storage or initiate OAuth flow."""
    creds = None

    # Load existing token if available
    if TOKEN_PATH.exists():
        print(f"Loading existing token from {TOKEN_PATH}")
        with open(TOKEN_PATH, 'rb') as token:
            creds = pickle.load(token)

    # If there are no (valid) credentials available, let the user log in
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing expired token...")
            creds.refresh(Request())
        else:
            print("Starting OAuth flow...")
            flow = InstalledAppFlow.from_client_secrets_file(
                client_secret_path, SCOPES)
            creds = flow.run_local_server(port=8080)

        # Save the credentials for the next run
        print(f"Saving token to {TOKEN_PATH}")
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_PATH, 'wb') as token:
            pickle.dump(creds, token)

    return creds


def list_devices(creds):
    """Query the Home Graph API to list all devices."""
    try:
        # Build the Home Graph API service
        service = build('homegraph', 'v1', credentials=creds)

        print("\n" + "="*80)
        print("Querying Google Home Graph API for devices...")
        print("="*80 + "\n")

        # Query devices
        # Note: The Home Graph API has limited endpoints for end users
        # We'll try the agentUsers.delete endpoint structure but for listing
        request = service.devices().query(body={})
        response = request.execute()

        print("Raw API Response:")
        print(json.dumps(response, indent=2))

        return response

    except Exception as e:
        print(f"Error querying devices: {e}")
        print(f"Error type: {type(e).__name__}")

        # Try alternative: Smart Device Management API
        print("\n" + "="*80)
        print("Trying Smart Device Management API instead...")
        print("="*80 + "\n")

        try:
            sdm_service = build('smartdevicemanagement', 'v1', credentials=creds)
            # List enterprises/structures/devices
            # Note: Requires project setup in Google Cloud Console
            enterprises = sdm_service.enterprises()
            print("SDM API service built successfully")
            print("Note: You may need to set up a project in Google Cloud Console")
            print("and link it to your Google Home account.")
        except Exception as e2:
            print(f"SDM API error: {e2}")

        return None


def main():
    # Get credentials path from environment or use default
    creds_path = os.environ.get('GOOGLE_HOME_CREDENTIALS_PATH')
    if not creds_path:
        # Try relative path
        creds_path = Path(__file__).parent.parent / 'secrets' / 'client_secret_686393938713-n647q5rb9d1480a6e2jkptvg8u2s7agq.apps.googleusercontent.com.json'
    else:
        # Make it absolute if it's relative
        creds_path = Path(__file__).parent.parent / creds_path

    if not Path(creds_path).exists():
        print(f"Error: Credentials file not found at {creds_path}")
        print(f"Set GOOGLE_HOME_CREDENTIALS_PATH or place the file at the expected location")
        sys.exit(1)

    print(f"Using credentials from: {creds_path}")

    # Get authenticated credentials
    creds = get_credentials(str(creds_path))

    if creds:
        print("\n✅ Authentication successful!")
        print(f"Token: {creds.token[:50]}...")
        print(f"Scopes: {creds.scopes}")

        # List devices
        devices = list_devices(creds)

        if devices:
            print("\n" + "="*80)
            print("DEVICES FOUND")
            print("="*80 + "\n")
            print(json.dumps(devices, indent=2))
        else:
            print("\n⚠️  No devices found or API returned empty response")
            print("\nPossible reasons:")
            print("1. No devices linked to this Google account")
            print("2. API permissions not properly configured")
            print("3. Need to use Smart Device Management API instead")
            print("4. Need to enable specific APIs in Google Cloud Console")
    else:
        print("\n❌ Authentication failed")
        sys.exit(1)


if __name__ == '__main__':
    main()
