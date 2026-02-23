#!/usr/bin/env python3
"""
Google Assistant SDK authentication.
This allows us to interact with Google Assistant and potentially query device info.
"""

import json
import os
import sys
from pathlib import Path
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import pickle

# Google Assistant SDK scope
SCOPES = ['https://www.googleapis.com/auth/assistant-sdk-prototype']

TOKEN_PATH = Path(__file__).parent.parent / 'context' / 'secrets' / 'google_assistant_token.pickle'


def get_auth_url():
    """Generate the OAuth authorization URL for Assistant SDK."""
    creds_path = Path(__file__).parent.parent / 'context' / os.environ.get(
        'GOOGLE_HOME_CREDENTIALS_PATH',
        'secrets/client_secret_686393938713-n647q5rb9d1480a6e2jkptvg8u2s7agq.apps.googleusercontent.com.json'
    )

    if not creds_path.exists():
        print(f"❌ Credentials file not found at {creds_path}")
        sys.exit(1)

    # Create the flow
    flow = Flow.from_client_secrets_file(
        str(creds_path),
        scopes=SCOPES,
        redirect_uri='http://localhost'
    )

    # Generate authorization URL
    auth_url, _ = flow.authorization_url(
        prompt='consent',
        access_type='offline'
    )

    print("="*80)
    print("GOOGLE ASSISTANT SDK AUTHENTICATION")
    print("="*80)
    print("\n📋 This will allow us to interact with Google Assistant")
    print("   and potentially query device information.\n")
    print("📋 Step 1: Open this URL in your browser:\n")
    print(auth_url)
    print("\n📋 Step 2: Authorize the application")
    print("\n📋 Step 3: Copy the ENTIRE URL from the redirect page")
    print("   (It should start with http://localhost/?code=...)")
    print("\n📋 Step 4: Run this script again with the URL:")
    print(f"   scripts/run.sh scripts/google_assistant_auth.py --code 'PASTE_URL_HERE'")
    print("\n" + "="*80)

    # Save flow state
    flow_state_path = TOKEN_PATH.parent / 'assistant_flow_state.pickle'
    flow_state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(flow_state_path, 'wb') as f:
        pickle.dump({
            'client_config': flow.client_config,
            'scopes': SCOPES,
            'redirect_uri': flow.redirect_uri
        }, f)


def exchange_code(redirect_url):
    """Exchange the authorization code for tokens."""
    flow_state_path = TOKEN_PATH.parent / 'assistant_flow_state.pickle'

    if not flow_state_path.exists():
        print("❌ Flow state not found. Run without --code first to generate auth URL.")
        sys.exit(1)

    # Load flow state
    with open(flow_state_path, 'rb') as f:
        flow_state = pickle.load(f)

    # Recreate flow
    flow = Flow.from_client_config(
        flow_state['client_config'],
        scopes=flow_state['scopes'],
        redirect_uri=flow_state['redirect_uri']
    )

    # Exchange code for token
    try:
        print("\n🔄 Exchanging authorization code for access token...")
        flow.fetch_token(authorization_response=redirect_url)

        creds = flow.credentials

        # Save credentials
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_PATH, 'wb') as token:
            pickle.dump(creds, token)

        print(f"✅ Credentials saved to {TOKEN_PATH}")
        print("\n📋 Token details:")
        print(f"   - Token: {creds.token[:50]}...")
        print(f"   - Valid: {creds.valid}")
        print(f"   - Scopes: {', '.join(creds.scopes)}")
        print(f"   - Refresh token: {'Yes' if creds.refresh_token else 'No'}")

        print("\n✅ Authentication successful!")
        print("\n📋 You can now use this token to:")
        print("   - Send queries to Google Assistant")
        print("   - Control smart home devices")
        print("   - Get device information")

        return creds

    except Exception as e:
        print(f"\n❌ Token exchange failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def load_credentials():
    """Load existing credentials."""
    if not TOKEN_PATH.exists():
        return None

    with open(TOKEN_PATH, 'rb') as token:
        creds = pickle.load(token)

    # Refresh if expired
    if creds.expired and creds.refresh_token:
        print("🔄 Refreshing expired token...")
        creds.refresh(Request())
        with open(TOKEN_PATH, 'wb') as token:
            pickle.dump(creds, token)

    return creds


def test_assistant_query():
    """Test querying Google Assistant."""
    creds = load_credentials()

    if not creds:
        print("❌ No credentials found. Please authenticate first.")
        sys.exit(1)

    print("\n" + "="*80)
    print("TESTING GOOGLE ASSISTANT API")
    print("="*80)

    # Try to query devices
    try:
        # Install google-assistant-sdk if not installed
        try:
            from google.assistant.embedded.v1alpha2 import embedded_assistant_pb2
            print("✅ Google Assistant SDK library is installed")
        except ImportError:
            print("⚠️  Google Assistant SDK library not installed")
            print("   Install with: pip install google-assistant-sdk[samples]")
            return

        print("\n📋 Note: The Google Assistant SDK requires additional setup:")
        print("   1. Enable the Google Assistant API in Cloud Console")
        print("   2. Register a device model")
        print("   3. Use the embedded assistant protocol")
        print("\n   For device queries, you can send text queries like:")
        print("   - 'What devices do I have?'")
        print("   - 'List my smart home devices'")
        print("   - 'Turn on [device name]'")

    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--code':
        if len(sys.argv) < 3:
            print("❌ Please provide the redirect URL")
            print(f"Usage: {sys.argv[0]} --code 'http://localhost/?code=...'")
            sys.exit(1)
        creds = exchange_code(sys.argv[2])
        if creds:
            test_assistant_query()
    elif len(sys.argv) > 1 and sys.argv[1] == '--test':
        test_assistant_query()
    else:
        get_auth_url()


if __name__ == '__main__':
    main()
