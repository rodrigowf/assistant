#!/usr/bin/env python3
"""
Manual Google Home authentication.
Generates an OAuth URL that the user can open manually.
"""

import json
import os
import sys
from pathlib import Path
from google_auth_oauthlib.flow import Flow
import pickle
from urllib.parse import urlencode

# Scopes required for Home Graph API
SCOPES = [
    'https://www.googleapis.com/auth/homegraph',
]

# Path to store the token (in context submodule)
TOKEN_PATH = Path(__file__).parent.parent / 'context' / 'secrets' / 'google_home_token.pickle'


def get_auth_url():
    """Generate the OAuth authorization URL."""
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
    print("GOOGLE HOME MANUAL AUTHENTICATION")
    print("="*80)
    print("\n📋 Step 1: Open this URL in your browser:\n")
    print(auth_url)
    print("\n📋 Step 2: Authorize the application")
    print("\n📋 Step 3: Copy the ENTIRE URL from the redirect page")
    print("   (It should start with http://localhost/?code=...)")
    print("\n📋 Step 4: Run this script again with --code flag:")
    print(f"   python {__file__} --code 'PASTE_REDIRECT_URL_HERE'")
    print("\n" + "="*80)

    # Save flow state
    flow_state_path = TOKEN_PATH.parent / 'flow_state.pickle'
    flow_state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(flow_state_path, 'wb') as f:
        pickle.dump({
            'client_config': flow.client_config,
            'scopes': SCOPES,
            'redirect_uri': flow.redirect_uri
        }, f)


def exchange_code(redirect_url):
    """Exchange the authorization code for tokens."""
    flow_state_path = TOKEN_PATH.parent / 'flow_state.pickle'

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

        # Test with API call
        test_api(creds)

    except Exception as e:
        print(f"\n❌ Token exchange failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def test_api(creds):
    """Test the credentials with a simple API call."""
    from googleapiclient.discovery import build

    print("\n" + "="*80)
    print("TESTING API ACCESS")
    print("="*80)

    try:
        # Try to build the service
        service = build('homegraph', 'v1', credentials=creds)
        print("✅ Home Graph API service created successfully!")

        # Note about API limitations
        print("\n⚠️  Note: The Home Graph API is designed for smart home device")
        print("   integrations (Actions on Google), not direct end-user queries.")
        print("\n   For listing Google Home devices, you may need:")
        print("   - Google Assistant SDK (for voice control)")
        print("   - Local discovery (mDNS/Zeroconf)")
        print("   - Unofficial libraries like pychromecast")

    except Exception as e:
        print(f"❌ API test failed: {e}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--code':
        if len(sys.argv) < 3:
            print("❌ Please provide the redirect URL")
            print(f"Usage: {sys.argv[0]} --code 'http://localhost/?code=...'")
            sys.exit(1)
        exchange_code(sys.argv[2])
    else:
        get_auth_url()


if __name__ == '__main__':
    main()
