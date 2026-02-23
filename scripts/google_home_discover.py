#!/usr/bin/env python3
"""
Google Home device discovery using alternative approaches.
Since Home Graph API requires special access, we'll try:
1. Google Assistant SDK (for device interaction)
2. Local network discovery (mDNS/Zeroconf)
3. Alternative Google APIs
"""

import json
import os
import sys
from pathlib import Path
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import pickle

# Alternative scopes that might work for end users
SCOPE_OPTIONS = {
    'assistant': [
        'https://www.googleapis.com/auth/assistant-sdk-prototype'
    ],
    'nest': [
        'https://www.googleapis.com/auth/sdm.service'
    ],
    'chromecast': [
        'https://www.googleapis.com/auth/cast.devices'
    ],
    'general': [
        'https://www.googleapis.com/auth/userinfo.profile',
        'https://www.googleapis.com/auth/userinfo.email'
    ]
}

TOKEN_PATH = Path(__file__).parent.parent / 'context' / 'secrets' / 'google_home_token.pickle'


def try_scope_set(scope_name, scopes):
    """Try to generate auth URL with a specific scope set."""
    creds_path = Path(__file__).parent.parent / 'context' / os.environ.get(
        'GOOGLE_HOME_CREDENTIALS_PATH',
        'secrets/client_secret_686393938713-n647q5rb9d1480a6e2jkptvg8u2s7agq.apps.googleusercontent.com.json'
    )

    if not creds_path.exists():
        print(f"❌ Credentials file not found at {creds_path}")
        return None

    try:
        # Create the flow
        flow = Flow.from_client_secrets_file(
            str(creds_path),
            scopes=scopes,
            redirect_uri='http://localhost'
        )

        # Generate authorization URL
        auth_url, _ = flow.authorization_url(
            prompt='consent',
            access_type='offline'
        )

        print(f"\n✅ {scope_name.upper()} scopes are available!")
        print(f"   Scopes: {', '.join(scopes)}")
        print(f"\n   Auth URL: {auth_url}")

        return auth_url

    except Exception as e:
        print(f"\n❌ {scope_name.upper()} scopes failed: {e}")
        return None


def discover_local_devices():
    """Try to discover Google Home devices on the local network using Zeroconf."""
    print("\n" + "="*80)
    print("LOCAL NETWORK DISCOVERY (Zeroconf/mDNS)")
    print("="*80)

    try:
        from zeroconf import ServiceBrowser, Zeroconf

        class MyListener:
            def __init__(self):
                self.devices = []

            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name)
                if info:
                    self.devices.append({
                        'name': name,
                        'type': type_,
                        'addresses': [str(addr) for addr in info.parsed_addresses()],
                        'port': info.port,
                        'properties': info.properties
                    })
                    print(f"\n📱 Found device: {name}")
                    print(f"   Type: {type_}")
                    print(f"   Addresses: {info.parsed_addresses()}")
                    print(f"   Port: {info.port}")

            def remove_service(self, zc, type_, name):
                pass

            def update_service(self, zc, type_, name):
                pass

        print("\n🔍 Scanning local network for Google Cast devices...")
        print("   (This will take ~5 seconds)")

        zeroconf = Zeroconf()
        listener = MyListener()

        # Google Cast uses the _googlecast._tcp service
        browser = ServiceBrowser(zeroconf, "_googlecast._tcp.local.", listener)

        import time
        time.sleep(5)

        zeroconf.close()

        if listener.devices:
            print(f"\n✅ Found {len(listener.devices)} Google Cast device(s)!")
            return listener.devices
        else:
            print("\n⚠️  No Google Cast devices found on local network")
            return []

    except ImportError:
        print("\n⚠️  Zeroconf library not installed")
        print("   Install with: pip install zeroconf")
        return []
    except Exception as e:
        print(f"\n❌ Discovery failed: {e}")
        import traceback
        traceback.print_exc()
        return []


def try_pychromecast():
    """Try using pychromecast library to discover devices."""
    print("\n" + "="*80)
    print("PYCHROMECAST DISCOVERY")
    print("="*80)

    try:
        import pychromecast

        print("\n🔍 Discovering Chromecast devices...")
        chromecasts, browser = pychromecast.get_chromecasts()

        if chromecasts:
            print(f"\n✅ Found {len(chromecasts)} Chromecast device(s)!")
            for cc in chromecasts:
                print(f"\n📱 Device: {cc.name}")
                print(f"   Model: {cc.model_name}")
                print(f"   Host: {cc.host}:{cc.port}")
                print(f"   UUID: {cc.uuid}")

            # Stop discovery
            pychromecast.discovery.stop_discovery(browser)
            return chromecasts
        else:
            print("\n⚠️  No Chromecast devices found")
            pychromecast.discovery.stop_discovery(browser)
            return []

    except ImportError:
        print("\n⚠️  pychromecast library not installed")
        print("   Install with: pip install pychromecast")
        return []
    except Exception as e:
        print(f"\n❌ Discovery failed: {e}")
        import traceback
        traceback.print_exc()
        return []


def main():
    print("="*80)
    print("GOOGLE HOME DEVICE DISCOVERY")
    print("="*80)

    print("\n📋 The Home Graph API scope is not available for standard OAuth clients.")
    print("   This is because it's designed for smart home device manufacturers.")
    print("\n   Let's try alternative approaches:\n")

    # Try different scope sets
    print("="*80)
    print("TESTING AVAILABLE OAUTH SCOPES")
    print("="*80)

    for scope_name, scopes in SCOPE_OPTIONS.items():
        try_scope_set(scope_name, scopes)

    # Try local discovery methods
    print("\n" + "="*80)
    print("TRYING LOCAL DISCOVERY METHODS")
    print("="*80)

    # Try pychromecast first (more reliable)
    devices = try_pychromecast()

    # If pychromecast doesn't work, try Zeroconf
    if not devices:
        devices = discover_local_devices()

    # Summary
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)

    if devices:
        print(f"\n✅ Successfully discovered {len(devices)} device(s) on local network!")
        print("\n📋 Next steps:")
        print("   - Use pychromecast library to control these devices")
        print("   - Send commands, play media, adjust volume, etc.")
    else:
        print("\n⚠️  No devices found using local discovery")
        print("\n📋 Possible reasons:")
        print("   1. No Google Home/Chromecast devices on this network")
        print("   2. Devices are on a different network segment")
        print("   3. Firewall blocking mDNS traffic")
        print("\n📋 Alternative approaches:")
        print("   1. Google Assistant SDK - requires special project setup")
        print("   2. Nest API - for Nest devices only")
        print("   3. Google Home app - for end-user device management")


if __name__ == '__main__':
    main()
