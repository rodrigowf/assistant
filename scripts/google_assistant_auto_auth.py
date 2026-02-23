#!/usr/bin/env python3
"""
Automatic Google Assistant SDK authentication with browser.
"""

import json
import os
import sys
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle

# Google Assistant SDK scope
SCOPES = ['https://www.googleapis.com/auth/assistant-sdk-prototype']

TOKEN_PATH = Path(__file__).parent.parent / 'context' / 'secrets' / 'google_assistant_token.pickle'


def authenticate():
    """Authenticate with Google Assistant SDK using automatic browser flow."""
    creds_path = Path(__file__).parent.parent / 'context' / os.environ.get(
        'GOOGLE_HOME_CREDENTIALS_PATH',
        'secrets/client_secret_686393938713-n647q5rb9d1480a6e2jkptvg8u2s7agq.apps.googleusercontent.com.json'
    )

    if not creds_path.exists():
        print(f"❌ Credentials file not found at {creds_path}")
        sys.exit(1)

    print("="*80)
    print("GOOGLE ASSISTANT SDK AUTHENTICATION")
    print("="*80)
    print(f"\nUsing credentials: {creds_path}")

    # Check for existing token
    if TOKEN_PATH.exists():
        print(f"\nChecking existing token at {TOKEN_PATH}...")
        with open(TOKEN_PATH, 'rb') as token:
            creds = pickle.load(token)

        if creds.valid:
            print("✅ Existing token is valid!")
            return creds
        elif creds.expired and creds.refresh_token:
            print("🔄 Token expired, refreshing...")
            try:
                creds.refresh(Request())
                with open(TOKEN_PATH, 'wb') as token:
                    pickle.dump(creds, token)
                print("✅ Token refreshed successfully!")
                return creds
            except Exception as e:
                print(f"⚠️  Token refresh failed: {e}")
                print("   Starting new authentication flow...")

    # Start automatic OAuth flow
    print("\n📝 Starting automatic OAuth 2.0 flow...")
    print("   A browser window will open for authentication.")
    print("   Please authorize the application.\n")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(creds_path),
            SCOPES
        )

        # Run local server - this will automatically open the browser
        print("🌐 Opening browser for authentication...")
        print("   Listening on http://localhost:8080")
        print("   (If the browser doesn't open, copy the URL from above)\n")

        creds = flow.run_local_server(
            port=8080,
            prompt='consent',
            success_message='✅ Authentication successful! You can close this window and return to the terminal.',
            open_browser=True
        )

        # Save credentials
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_PATH, 'wb') as token:
            pickle.dump(creds, token)

        print(f"\n✅ Credentials saved to {TOKEN_PATH}")
        return creds

    except Exception as e:
        print(f"\n❌ Authentication failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def query_devices(creds):
    """Query device information using the Assistant SDK credentials."""
    print("\n" + "="*80)
    print("QUERYING GOOGLE ASSISTANT / HOME DEVICES")
    print("="*80)

    print("\n📋 Credential Info:")
    print(f"   - Token: {creds.token[:50]}...")
    print(f"   - Valid: {creds.valid}")
    print(f"   - Scopes: {', '.join(creds.scopes)}")
    print(f"   - Refresh token: {'Yes ✅' if creds.refresh_token else 'No ❌'}")

    # Try to use the credentials with various Google APIs
    print("\n" + "="*80)
    print("ATTEMPTING API QUERIES")
    print("="*80)

    # Try Home Graph API (might work with assistant credentials)
    try:
        print("\n1️⃣  Trying Home Graph API...")
        service = build('homegraph', 'v1', credentials=creds)

        # Try to query - this might not work but let's try
        try:
            response = service.devices().query(body={}).execute()
            print("✅ Home Graph API Response:")
            print(json.dumps(response, indent=2))
        except Exception as e:
            print(f"   ⚠️  Query failed: {str(e)[:100]}")
    except Exception as e:
        print(f"   ⚠️  Service creation failed: {str(e)[:100]}")

    # Try to list available APIs
    print("\n2️⃣  Testing API Discovery...")
    try:
        # Get user info to verify credentials work
        from googleapiclient.discovery import build

        # This should work with most OAuth tokens
        oauth2_service = build('oauth2', 'v2', credentials=creds)
        user_info = oauth2_service.userinfo().get().execute()

        print("✅ User Info (credentials are working):")
        print(f"   - Email: {user_info.get('email', 'N/A')}")
        print(f"   - Name: {user_info.get('name', 'N/A')}")
        print(f"   - ID: {user_info.get('id', 'N/A')}")
    except Exception as e:
        print(f"   ⚠️  Failed: {str(e)[:100]}")

    print("\n" + "="*80)
    print("NOTES")
    print("="*80)
    print("""
✅ Authentication successful with Google Assistant SDK scope!

However, the Assistant SDK requires additional setup:

1. **For text queries to Assistant:**
   - Install: pip install google-assistant-sdk[samples]
   - Register a device model in Google Cloud Console
   - Use the embedded assistant gRPC protocol

2. **For device control:**
   - The Assistant SDK can send commands like "turn on lights"
   - But direct device listing requires manufacturer integration

3. **Alternative: Local network discovery**
   - Use pychromecast for Chromecast/Google Home devices
   - Use mDNS/Zeroconf to find devices on your network
   - Control devices directly without OAuth

The OAuth credentials are saved and can be used for Assistant SDK
text/voice queries once you register a device model.
    """)


def main():
    print("🏠 Google Assistant SDK - Automatic Authentication\n")

    # Authenticate
    creds = authenticate()

    if creds:
        print("\n✅ Authentication successful!")

        # Try to query devices
        query_devices(creds)
    else:
        print("\n❌ Authentication failed")
        sys.exit(1)


if __name__ == '__main__':
    main()
