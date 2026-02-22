#!/usr/bin/env python3
"""
Helper script to diagnose and fix Google OAuth configuration issues.
"""

import json
from pathlib import Path

print("="*80)
print("GOOGLE OAUTH CONFIGURATION HELPER")
print("="*80)

# Read the credentials file
creds_path = Path(__file__).parent.parent / 'secrets' / 'client_secret_686393938713-n647q5rb9d1480a6e2jkptvg8u2s7agq.apps.googleusercontent.com.json'

with open(creds_path) as f:
    creds = json.load(f)

print("\n📋 Current OAuth Application:")
print(f"   Project ID: {creds['installed']['project_id']}")
print(f"   Client ID: {creds['installed']['client_id']}")

print("\n" + "="*80)
print("ERROR: App is in Testing Mode (Error 403: access_denied)")
print("="*80)

print("""
Your OAuth app needs to be configured to allow your Google account access.

🔧 TO FIX THIS, FOLLOW THESE STEPS:

1. Open Google Cloud Console:
   👉 https://console.cloud.google.com/apis/credentials/consent?project=agentic-471801

2. You should see the "OAuth consent screen" page

3. Look for the "Test users" section (scroll down if needed)

4. Click the "+ ADD USERS" button

5. Enter YOUR Google account email address
   (the email you're using to authenticate)

6. Click "SAVE"

7. Come back here and run the authentication again

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎯 QUICK CHECKLIST:

□ Open the Google Cloud Console link above
□ Navigate to "OAuth consent screen" (if not already there)
□ Find "Test users" section
□ Click "+ ADD USERS"
□ Enter your Google email
□ Click "SAVE"
□ Wait 1-2 minutes for changes to propagate
□ Try authentication again

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📝 ALTERNATIVE: Publish the App (For Production)

If you want anyone to use your app (not just you):

1. On the same OAuth consent screen page
2. Click "PUBLISH APP" button at the top
3. Confirm publication

Note: Publishing may require additional verification for sensitive scopes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

❓ Which email should you add?

Add the Google account email you're trying to authenticate with.
If you're not sure, it's likely the email associated with your Google Home devices.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")

print("\n✅ Once you've completed these steps, let me know and I'll restart")
print("   the authentication process!\n")
