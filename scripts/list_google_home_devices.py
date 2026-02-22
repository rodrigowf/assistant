#!/usr/bin/env python3
"""
List Google Home devices using the authenticated credentials.
"""

import json
import pickle
from pathlib import Path
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

TOKEN_PATH = Path(__file__).parent.parent / 'secrets' / 'google_assistant_token.pickle'


def load_credentials():
    """Load the saved credentials."""
    if not TOKEN_PATH.exists():
        print("❌ No credentials found. Please run authentication first.")
        print("   Run: scripts/run.sh scripts/google_assistant_auto_auth.py")
        return None

    with open(TOKEN_PATH, 'rb') as token:
        creds = pickle.load(token)

    print("✅ Credentials loaded successfully")
    print(f"   Token valid: {creds.valid}")
    print(f"   Scopes: {', '.join(creds.scopes)}")

    return creds


def try_home_graph_api(creds):
    """Try to query devices using Home Graph API."""
    print("\n" + "="*80)
    print("ATTEMPTING: Home Graph API")
    print("="*80)

    try:
        service = build('homegraph', 'v1', credentials=creds)
        print("✅ Service created")

        # Try query endpoint
        print("\nTrying devices.query()...")
        try:
            response = service.devices().query(body={}).execute()
            print("✅ Success!")
            print(json.dumps(response, indent=2))
            return response
        except HttpError as e:
            print(f"❌ Query failed: {e.status_code} - {e.reason}")
            print(f"   Details: {e.error_details}")

    except Exception as e:
        print(f"❌ Failed: {e}")

    return None


def try_smart_device_management(creds):
    """Try Smart Device Management API (for Nest devices)."""
    print("\n" + "="*80)
    print("ATTEMPTING: Smart Device Management API (Nest)")
    print("="*80)

    try:
        service = build('smartdevicemanagement', 'v1', credentials=creds)
        print("✅ Service created")

        # Try to list devices
        # Note: Requires project ID from Nest Device Access
        print("\n⚠️  This API requires:")
        print("   - Device Access Console project setup")
        print("   - Project ID from https://console.nest.google.com/device-access/")

    except HttpError as e:
        print(f"❌ Failed: {e.status_code} - {e.reason}")
    except Exception as e:
        print(f"❌ Failed: {e}")

    return None


def try_local_discovery():
    """Try local network discovery as fallback."""
    print("\n" + "="*80)
    print("ATTEMPTING: Local Network Discovery (pychromecast)")
    print("="*80)

    try:
        import pychromecast
        import time

        print("🔍 Scanning for Chromecast/Google Home devices...")
        print("   (This takes ~5 seconds)")

        services, browser = pychromecast.discovery.discover_chromecasts()

        # Wait for discovery
        time.sleep(5)

        chromecasts, browser = pychromecast.get_listed_chromecasts(friendly_names=None)

        if chromecasts:
            print(f"\n✅ Found {len(chromecasts)} device(s)!\n")

            devices = []
            for cast in chromecasts:
                device_info = {
                    'name': cast.name,
                    'model': cast.model_name,
                    'manufacturer': cast.cast_type,
                    'uuid': str(cast.uuid),
                    'host': cast.host,
                    'port': cast.port,
                    'uri': cast.uri,
                    'status': None
                }

                # Try to get status
                try:
                    cast.wait()
                    device_info['status'] = {
                        'is_active': cast.status.is_active_input if cast.status else None,
                        'volume': cast.status.volume_level if cast.status else None,
                        'app': cast.app_display_name if hasattr(cast, 'app_display_name') else None
                    }
                except:
                    pass

                devices.append(device_info)

                print(f"📱 {cast.name}")
                print(f"   Model: {cast.model_name}")
                print(f"   Type: {cast.cast_type}")
                print(f"   Host: {cast.host}:{cast.port}")
                print(f"   UUID: {cast.uuid}")
                if device_info['status']:
                    print(f"   Volume: {device_info['status'].get('volume', 'N/A')}")
                print()

            # Stop discovery
            pychromecast.discovery.stop_discovery(browser)

            return devices
        else:
            print("⚠️  No devices found on local network")
            pychromecast.discovery.stop_discovery(browser)

    except ImportError:
        print("❌ pychromecast not installed")
        print("   Install: pip install pychromecast")
    except Exception as e:
        print(f"❌ Discovery failed: {e}")
        import traceback
        traceback.print_exc()

    return []


def main():
    print("="*80)
    print("GOOGLE HOME DEVICE LISTING")
    print("="*80)
    print()

    # Load credentials
    creds = load_credentials()
    if not creds:
        return

    # Try different methods to get device list
    devices = None

    # Method 1: Home Graph API
    devices = try_home_graph_api(creds)

    # Method 2: Smart Device Management API
    if not devices:
        devices = try_smart_device_management(creds)

    # Method 3: Local network discovery (doesn't require OAuth)
    if not devices:
        print("\n💡 Falling back to local network discovery...")
        devices = try_local_discovery()

    # Summary
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)

    if devices:
        print(f"\n✅ Successfully found {len(devices)} device(s)!")
        print("\n📋 Device List:")
        print(json.dumps(devices, indent=2))

        # Save to file
        output_file = Path(__file__).parent.parent / 'secrets' / 'google_home_devices.json'
        with open(output_file, 'w') as f:
            json.dump(devices, f, indent=2)
        print(f"\n💾 Devices saved to: {output_file}")
    else:
        print("\n⚠️  No devices found")
        print("\n📋 Possible reasons:")
        print("   1. OAuth scope doesn't allow device listing")
        print("   2. No devices on local network")
        print("   3. Need additional API setup (Device Access Console)")
        print("\n📋 Next steps:")
        print("   1. Check if devices are on the same network")
        print("   2. Try the Google Home app to verify devices exist")
        print("   3. Consider using local discovery methods (pychromecast)")


if __name__ == '__main__':
    main()
