#!/usr/bin/env python3
"""
Analyze what we can actually do with the Google Home credentials.
"""

import json
import pickle
from pathlib import Path
import requests

TOKEN_PATH = Path(__file__).parent.parent / 'secrets' / 'google_assistant_token.pickle'

print("="*80)
print("GOOGLE CREDENTIALS ANALYSIS")
print("="*80)

# Load existing credentials
if TOKEN_PATH.exists():
    with open(TOKEN_PATH, 'rb') as token:
        creds = pickle.load(token)

    print("\n📋 Loaded Credentials:")
    print(f"   Token: {creds.token[:50]}...")
    print(f"   Valid: {creds.valid}")
    print(f"   Scopes: {', '.join(creds.scopes)}")
    print(f"   Refresh token: {'Yes' if creds.refresh_token else 'No'}")

    # Get detailed token info
    print("\n" + "="*80)
    print("TOKEN INFO FROM GOOGLE")
    print("="*80)

    try:
        token_info_url = f"https://www.googleapis.com/oauth2/v1/tokeninfo?access_token={creds.token}"
        response = requests.get(token_info_url)
        if response.ok:
            token_info = response.json()
            print("\n✅ Token Info:")
            for key, value in token_info.items():
                print(f"   {key}: {value}")
        else:
            print(f"❌ Failed to get token info: {response.status_code}")
    except Exception as e:
        print(f"❌ Error: {e}")

else:
    print("\n❌ No credentials found")

print("\n" + "="*80)
print("CONCLUSION")
print("="*80)
print("""
The Google Assistant SDK scope is very limited and primarily designed for:
1. Sending voice/text queries to Google Assistant
2. Requires device model registration for full functionality
3. Does NOT provide direct access to list all Google Home devices

📋 ALTERNATIVE APPROACHES FOR DEVICE DISCOVERY:

1. **Local Network Discovery (pychromecast)**
   - Discovers Chromecast/Google Home devices on your local network
   - No OAuth required
   - Works if devices are on the same network segment
   - May be blocked by firewall/network configuration

2. **Google Home Local API (unofficial)**
   - Some community projects can access local Google Home API
   - Requires devices to be on same network
   - Not officially supported by Google

3. **Google Home App**
   - The official way for end users to manage devices
   - No programmatic API for listing all devices

4. **Smart Home Actions (for developers)**
   - Requires being a device manufacturer
   - Can integrate with Google Home through Actions on Google
   - Not applicable for end-user device listing

📋 RECOMMENDATION:

For discovering YOUR Google Home devices programmatically, the best approach is:
- **Local network discovery** using pychromecast or similar libraries
- Devices must be on the same network as the computer running the code
- No OAuth required, works through mDNS/Zeroconf

Would you like me to:
1. Debug why local discovery isn't finding devices?
2. Check network configuration?
3. Try alternative discovery methods?
""")
