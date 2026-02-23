#!/usr/bin/env python3
"""
Google Home authentication helper.
Generates an OAuth URL and exchanges the authorization code for tokens.
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
]

# Path to store the token (in context submodule)
TOKEN_PATH = Path(__file__).parent.parent / 'context' / 'secrets' / 'google_home_token.pickle'


def authenticate():
    """Start OAuth flow and save credentials."""
    creds_path = Path(__file__).parent.parent / 'context' / os.environ.get(
        'GOOGLE_HOME_CREDENTIALS_PATH',
        'secrets/client_secret_686393938713-n647q5rb9d1480a6e2jkptvg8u2s7agq.apps.googleusercontent.com.json'
    )

    if not creds_path.exists():
        print(f"❌ Credentials file not found at {creds_path}")
        sys.exit(1)

    print(f"Using credentials from: {creds_path}")
    print("\n" + "="*80)
    print("GOOGLE HOME AUTHENTICATION")
    print("="*80)

    # Check for existing token
    if TOKEN_PATH.exists():
        print(f"\n✅ Token file already exists at {TOKEN_PATH}")
        with open(TOKEN_PATH, 'rb') as token:
            creds = pickle.load(token)

        if creds.valid:
            print("✅ Token is valid!")
            return creds
        elif creds.expired and creds.refresh_token:
            print("🔄 Token expired, refreshing...")
            creds.refresh(Request())
            with open(TOKEN_PATH, 'wb') as token:
                pickle.dump(creds, token)
            print("✅ Token refreshed!")
            return creds

    # Start new OAuth flow
    print("\n📝 Starting OAuth 2.0 flow...")
    print("This will open a browser window for authentication.")
    print("Please authorize the application and complete the flow.\n")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(creds_path),
            SCOPES,
            redirect_uri='http://localhost:8080'
        )

        # Run local server to receive callback
        creds = flow.run_local_server(
            port=8080,
            prompt='consent',
            success_message='✅ Authentication successful! You can close this window.'
        )

        # Save credentials
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_PATH, 'wb') as token:
            pickle.dump(creds, token)

        print(f"\n✅ Credentials saved to {TOKEN_PATH}")
        return creds

    except Exception as e:
        print(f"\n❌ Authentication failed: {e}")
        sys.exit(1)


def list_devices(creds):
    """Query Google Home devices."""
    print("\n" + "="*80)
    print("QUERYING GOOGLE HOME DEVICES")
    print("="*80 + "\n")

    try:
        # Try Home Graph API
        print("Attempting Home Graph API (v1)...")
        service = build('homegraph', 'v1', credentials=creds)

        # The Home Graph API is primarily for smart home actions
        # Direct device listing may not be available for end users
        print("⚠️  Note: The Home Graph API is designed for smart home integrations,")
        print("   not direct end-user device queries. You may need different APIs.")

        # Try to call a test endpoint
        try:
            response = service.devices().query(body={}).execute()
            print("\n✅ API Response:")
            print(json.dumps(response, indent=2))
            return response
        except Exception as e:
            print(f"❌ Query failed: {e}\n")

    except Exception as e:
        print(f"❌ Service creation failed: {e}\n")

    # Alternative approach suggestions
    print("\n" + "="*80)
    print("ALTERNATIVE APPROACHES")
    print("="*80)
    print("""
The Google Home Graph API has limited direct access for end users.
For listing and controlling Google Home devices, consider:

1. **Google Assistant SDK** - For voice control integration
2. **Smart Device Management API** - For Nest devices (requires project setup)
3. **Local Device Discovery** - Use mDNS/Zeroconf to find devices on your network
4. **Google Home Python Library** - Unofficial libraries like 'pychromecast'

For now, your authentication is working. The token is saved and can be used
with other Google APIs that support device management.
    """)

    return None


def main():
    print("🏠 Google Home Authentication & Device Listing\n")

    # Authenticate
    creds = authenticate()

    if creds:
        print("\n✅ Authentication successful!")
        print(f"📋 Token details:")
        print(f"   - Token: {creds.token[:50]}...")
        print(f"   - Valid: {creds.valid}")
        print(f"   - Scopes: {', '.join(creds.scopes)}")

        # Try to list devices
        list_devices(creds)
    else:
        print("\n❌ Authentication failed")
        sys.exit(1)


if __name__ == '__main__':
    main()
