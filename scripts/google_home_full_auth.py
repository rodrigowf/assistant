#!/usr/bin/env python3
"""
Google Home authentication with comprehensive scopes for device access.
"""

import json
import os
import sys
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import pickle

# Try comprehensive scopes that might give device access
SCOPES = [
    'https://www.googleapis.com/auth/assistant-sdk-prototype',
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
]

TOKEN_PATH = Path(__file__).parent.parent / 'context' / 'secrets' / 'google_home_full_token.pickle'


def authenticate():
    """Authenticate with comprehensive scopes."""
    creds_path = Path(__file__).parent.parent / 'context' / os.environ.get(
        'GOOGLE_HOME_CREDENTIALS_PATH',
        'secrets/client_secret_686393938713-n647q5rb9d1480a6e2jkptvg8u2s7agq.apps.googleusercontent.com.json'
    )

    if not creds_path.exists():
        print(f"❌ Credentials file not found at {creds_path}")
        sys.exit(1)

    print("="*80)
    print("GOOGLE HOME - COMPREHENSIVE AUTHENTICATION")
    print("="*80)
    print(f"\nUsing credentials: {creds_path}")
    print(f"\nRequesting scopes:")
    for scope in SCOPES:
        print(f"   - {scope}")

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
    print("\n📝 Starting OAuth 2.0 flow with comprehensive scopes...")
    print("   A browser window will open for authentication.\n")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(creds_path),
            SCOPES
        )

        print("🌐 Opening browser for authentication...")

        creds = flow.run_local_server(
            port=8080,
            prompt='consent',
            success_message='✅ Authentication successful! You can close this window.',
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


def test_apis(creds):
    """Test various Google APIs to find which ones work."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    print("\n" + "="*80)
    print("TESTING APIS WITH CURRENT CREDENTIALS")
    print("="*80)

    results = {}

    # Test 1: User Info
    print("\n1️⃣  User Info API...")
    try:
        service = build('oauth2', 'v2', credentials=creds)
        user_info = service.userinfo().get().execute()
        print(f"✅ Success!")
        print(f"   Email: {user_info.get('email')}")
        print(f"   Name: {user_info.get('name')}")
        results['userinfo'] = user_info
    except Exception as e:
        print(f"❌ Failed: {e}")

    # Test 2: Home Graph API
    print("\n2️⃣  Home Graph API...")
    try:
        service = build('homegraph', 'v1', credentials=creds)
        response = service.devices().query(body={}).execute()
        print(f"✅ Success!")
        print(json.dumps(response, indent=2))
        results['homegraph'] = response
    except HttpError as e:
        print(f"❌ Failed: {e.status_code} - {e.reason}")
    except Exception as e:
        print(f"❌ Failed: {e}")

    # Test 3: Try to discover available APIs
    print("\n3️⃣  Checking token info...")
    try:
        import requests
        token_info_url = f"https://www.googleapis.com/oauth2/v1/tokeninfo?access_token={creds.token}"
        response = requests.get(token_info_url)
        if response.ok:
            token_info = response.json()
            print(f"✅ Token info:")
            print(json.dumps(token_info, indent=2))
            results['token_info'] = token_info
        else:
            print(f"❌ Failed: {response.status_code}")
    except Exception as e:
        print(f"❌ Failed: {e}")

    return results


def main():
    print("🏠 Google Home - Comprehensive Device Access Test\n")

    # Authenticate
    creds = authenticate()

    if creds:
        print("\n✅ Authentication successful!")
        print(f"\n📋 Token details:")
        print(f"   - Valid: {creds.valid}")
        print(f"   - Scopes: {', '.join(creds.scopes)}")
        print(f"   - Refresh token: {'Yes' if creds.refresh_token else 'No'}")

        # Test APIs
        results = test_apis(creds)

        # Save results
        if results:
            output_file = Path(__file__).parent.parent / 'context' / 'secrets' / 'api_test_results.json'
            with open(output_file, 'w') as f:
                json.dump(results, f, indent=2, default=str)
            print(f"\n💾 Results saved to: {output_file}")
    else:
        print("\n❌ Authentication failed")
        sys.exit(1)


if __name__ == '__main__':
    main()
