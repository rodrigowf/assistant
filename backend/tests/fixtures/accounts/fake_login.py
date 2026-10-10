"""A stand-in for a CLI login, for the flow-driver tests.

Usage: fake_login.py <mode>
  code    print a URL, read a code; "good" → "Login successful" exit 0, else "Login failed" exit 1
  device  print a URL and a device code, exit 0 after a moment (the user approved elsewhere)
  token   print a URL, read a code, print a token and wait (like `claude setup-token`)
  tty     like code, but refuse to run without a terminal (Ink / readline TUIs)
  hang    print a URL and never finish
"""

import os
import sys
import time

mode = sys.argv[1]
# Like `codex login` / `claude auth login`: log the current login out the moment the login starts.
if os.environ.get("FAKE_LOGIN_CLOBBER"):
    try:
        os.unlink(os.environ["FAKE_LOGIN_CLOBBER"])
    except FileNotFoundError:
        pass
if mode == "tty" and not sys.stdin.isatty():
    print("stdin is not a terminal", flush=True)
    sys.exit(3)
print("Visit https://example.test/oauth/authorize?state=abc&x=1", flush=True)
if mode == "device":
    print("Enter this one-time code\n   WXYZ-12345", flush=True)
    time.sleep(0.5)
    print("Successfully logged in", flush=True)
    sys.exit(0)
if mode == "hang":
    while True:
        time.sleep(1)
sys.stdout.write("Paste code here if prompted > ")
sys.stdout.flush()
code = sys.stdin.readline().strip()
if mode == "token":
    if code == "good":
        print("\nYour token: sk-ant-oat01-" + "T" * 40, flush=True)
        while True:
            time.sleep(1)
    print("\nOAuth error: bad code", flush=True)
    while True:
        time.sleep(1)
if code == "good":
    if os.environ.get("FAKE_LOGIN_WRITE"):
        with open(os.environ["FAKE_LOGIN_WRITE"], "w") as fh:
            fh.write('{"new": true}')
    if os.environ.get("FAKE_LOGIN_MARKER"):
        with open(os.environ["FAKE_LOGIN_MARKER"], "w") as fh:
            fh.write("ok")
    print("Login successful", flush=True)
    sys.exit(0)
print(f"Login failed: bad code ({len(code)} chars)", flush=True)
sys.exit(1)
