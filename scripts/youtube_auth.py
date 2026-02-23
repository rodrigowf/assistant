#!/usr/bin/env python3
"""
YouTube OAuth2 Authorization Flow

This script handles the OAuth2 flow for YouTube Data API access.
It generates an authorization URL and exchanges the auth code for tokens.

Usage:
    # Generate auth URL (outputs structured JSON for orchestrator)
    python youtube_auth.py generate

    # Exchange auth code for tokens
    python youtube_auth.py exchange <authorization_code>
"""

import os
import sys
import json
import webbrowser
from pathlib import Path

# Allow Google to return additional scopes (e.g., openid, userinfo.email)
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

from google_auth_oauthlib.flow import Flow

# YouTube Data API scopes
SCOPES = [
    'https://www.googleapis.com/auth/youtube.readonly',  # View account info
    'https://www.googleapis.com/auth/youtube.force-ssl',  # Manage YouTube account
]

# Project paths (secrets are in context submodule)
PROJECT_ROOT = Path(__file__).parent.parent
TOKENS_PATH = PROJECT_ROOT / 'context' / 'secrets' / 'youtube_tokens.json'
FLOW_STATE_PATH = PROJECT_ROOT / 'context' / 'secrets' / '.youtube_oauth_state.json'


def get_credentials_path() -> Path:
    """Get the credentials file path from environment variable."""
    creds_path = os.environ.get('YOUTUBE_CREDENTIALS_PATH')
    if not creds_path:
        return None, "YOUTUBE_CREDENTIALS_PATH environment variable not set"

    path = Path(creds_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    if not path.exists():
        return None, f"Credentials file not found: {path}"

    return path, None


def create_flow(creds_path: Path) -> Flow:
    """Create OAuth flow from credentials file."""
    return Flow.from_client_secrets_file(
        str(creds_path),
        scopes=SCOPES,
        redirect_uri='urn:ietf:wg:oauth:2.0:oob'
    )


def generate_auth_url() -> dict:
    """Generate the OAuth2 authorization URL and return structured response."""
    creds_path, error = get_credentials_path()
    if error:
        return {
            "action": "error",
            "error": error
        }

    flow = create_flow(creds_path)

    # Generate the authorization URL
    auth_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )

    # Save flow state for later token exchange
    # We need to save the client config since Flow can't be pickled
    with open(creds_path) as f:
        client_config = json.load(f)

    state_data = {
        'state': state,
        'client_config': client_config,
        'scopes': SCOPES,
        'redirect_uri': 'urn:ietf:wg:oauth:2.0:oob'
    }
    FLOW_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FLOW_STATE_PATH, 'w') as f:
        json.dump(state_data, f)

    # Open the URL in the user's default browser
    webbrowser.open(auth_url)

    # Return structured response for orchestrator
    return {
        "action": "browser_opened",
        "url": auth_url,
        "title": "YouTube Authorization",
        "description": "Browser opened. Sign in with your Google account and authorize YouTube access.",
        "next_step": "After authorizing, copy the authorization code and run: python youtube_auth.py exchange <code>"
    }


def exchange_code_for_tokens(auth_code: str) -> dict:
    """Exchange the authorization code for access and refresh tokens."""
    # Load saved flow state
    if not FLOW_STATE_PATH.exists():
        return {
            "action": "error",
            "error": "No pending OAuth flow. Run 'generate' first."
        }

    with open(FLOW_STATE_PATH) as f:
        state_data = json.load(f)

    # Recreate flow from saved state
    client_config = state_data['client_config']
    flow = Flow.from_client_config(
        client_config,
        scopes=state_data['scopes'],
        redirect_uri=state_data['redirect_uri']
    )

    try:
        flow.fetch_token(code=auth_code)
    except Exception as e:
        return {
            "action": "error",
            "error": f"Failed to exchange code: {str(e)}"
        }

    credentials = flow.credentials

    # Save tokens to file
    tokens = {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': list(credentials.scopes),
    }

    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKENS_PATH, 'w') as f:
        json.dump(tokens, f, indent=2)

    # Clean up state file
    FLOW_STATE_PATH.unlink(missing_ok=True)

    return {
        "action": "success",
        "message": "YouTube OAuth2 authorization completed successfully!",
        "tokens_path": str(TOKENS_PATH),
        "scopes": list(credentials.scopes),
        "has_refresh_token": bool(credentials.refresh_token)
    }


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python youtube_auth.py generate    - Generate auth URL")
        print("  python youtube_auth.py exchange <code>  - Exchange code for tokens")
        sys.exit(1)

    command = sys.argv[1]

    if command == 'generate':
        result = generate_auth_url()
    elif command == 'exchange':
        if len(sys.argv) < 3:
            print("Error: Authorization code required")
            print("Usage: python youtube_auth.py exchange <code>")
            sys.exit(1)
        auth_code = sys.argv[2]
        result = exchange_code_for_tokens(auth_code)
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

    # Output as JSON for orchestrator parsing
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
