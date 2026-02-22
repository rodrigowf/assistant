---
name: tv-remote
description: Control the Fire TV like a remote - launch/close apps, play/pause media, navigate menus, adjust volume
argument-hint: "[command] [args...]"
---

# TV Remote Control

Full remote control of the Fire TV Stick via ADB. Acts as a glorified remote control with complete app and media control.

## Connection

Always ensure Fire TV is connected before sending commands. If connection fails, try auto-discovery:

```bash
# Check connection
adb devices | grep 192.168.0.16

# If not connected, use discovery script
/home/rodrigo/AndroidStudioProjects/TvServerHub/scripts/discover-firetv.sh
```

## Available Commands

### App Control

**Launch apps:**
```bash
# TvServerHub (your launcher)
adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.MainActivity

# YouTube
adb -s 192.168.0.16:5555 shell am start -n com.amazon.firetv.youtube/dev.cobalt.app.MainActivity

# Netflix
adb -s 192.168.0.16:5555 shell am start -n com.netflix.ninja/.MainActivity

# Prime Video
adb -s 192.168.0.16:5555 shell am start -n com.amazon.avod.thirdpartyclient/.LauncherActivity

# Settings
adb -s 192.168.0.16:5555 shell am start -a android.settings.SETTINGS
```

**Open any web page:**
```bash
# Method 1: Using -e url parameter (recommended)
adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.WebPageViewActivity -e url "https://example.com"

# Method 2: Using -d data URI
adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.WebPageViewActivity -d "https://example.com"

# Examples:
# Open Google
adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.WebPageViewActivity -e url "https://www.google.com"

# Open Jellyfin (local server)
adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.WebPageViewActivity -e url "http://192.168.0.200:8096"

# Open Agentic Assistant
adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.WebPageViewActivity -e url "https://192.168.0.200/agentic"

# Open Copyparty file server
adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.WebPageViewActivity -e url "http://192.168.0.200:3923"

# Open any arbitrary URL
adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.WebPageViewActivity -e url "https://youtube.com"
```

**Features of WebPageViewActivity:**
- Full WebView with JavaScript enabled
- Hardware-accelerated rendering
- Supports both HTTP and HTTPS
- Auto-grants camera/microphone permissions
- Remote debugging enabled (Chrome DevTools)
- Back button navigates WebView history
- Home button exits to launcher
- singleTask mode (reuses same activity for multiple URLs)

**Close apps:**
```bash
# Force stop any app by package name
adb -s 192.168.0.16:5555 shell am force-stop <package-name>

# Examples:
# Close YouTube
adb -s 192.168.0.16:5555 shell am force-stop com.amazon.firetv.youtube

# Close Netflix
adb -s 192.168.0.16:5555 shell am force-stop com.netflix.ninja

# Close TvServerHub
adb -s 192.168.0.16:5555 shell am force-stop com.example.tvserverhub
```

**Check what's running:**
```bash
# See current foreground app
adb -s 192.168.0.16:5555 shell dumpsys activity activities | grep mResumedActivity
```

### Media Controls

```bash
# Play/Pause toggle (most reliable)
adb -s 192.168.0.16:5555 shell input keyevent 85

# Separate play and pause
adb -s 192.168.0.16:5555 shell input keyevent 126  # Play
adb -s 192.168.0.16:5555 shell input keyevent 127  # Pause

# Skip forward/backward
adb -s 192.168.0.16:5555 shell input keyevent 87   # Next
adb -s 192.168.0.16:5555 shell input keyevent 88   # Previous

# Fast forward/rewind
adb -s 192.168.0.16:5555 shell input keyevent 90   # Fast forward
adb -s 192.168.0.16:5555 shell input keyevent 89   # Rewind

# Stop
adb -s 192.168.0.16:5555 shell input keyevent 86
```

### Navigation

```bash
# D-pad directions
adb -s 192.168.0.16:5555 shell input keyevent 19   # Up
adb -s 192.168.0.16:5555 shell input keyevent 20   # Down
adb -s 192.168.0.16:5555 shell input keyevent 21   # Left
adb -s 192.168.0.16:5555 shell input keyevent 22   # Right

# Select (OK/Enter)
adb -s 192.168.0.16:5555 shell input keyevent 23   # D-pad center

# Back button
adb -s 192.168.0.16:5555 shell input keyevent 4

# Home button
adb -s 192.168.0.16:5555 shell input keyevent 3
```

### Volume Controls

```bash
# Volume up/down
adb -s 192.168.0.16:5555 shell input keyevent 24   # Volume up
adb -s 192.168.0.16:5555 shell input keyevent 25   # Volume down

# Mute toggle
adb -s 192.168.0.16:5555 shell input keyevent 164
```

### Utilities

```bash
# Take screenshot
adb -s 192.168.0.16:5555 exec-out screencap -p > /tmp/tv-screenshot-$(date +%Y%m%d-%H%M%S).png

# Get TV status
echo "=== Fire TV Status ==="
echo "Connection:"
adb devices | grep 192.168.0.16

echo -e "\nForeground App:"
adb -s 192.168.0.16:5555 shell dumpsys activity activities | grep -A 2 "mResumedActivity"

echo -e "\nTvServerHub Status:"
PID=$(adb -s 192.168.0.16:5555 shell pidof com.example.tvserverhub)
if [ -n "$PID" ]; then
  echo "Running (PID: $PID)"
else
  echo "Not running"
fi
```

## Usage Examples

When user says:
- "Launch YouTube on TV" → Use YouTube launch command
- "Open Google on the TV" → Use WebPageViewActivity with Google URL
- "Show Jellyfin on TV" → Use WebPageViewActivity with Jellyfin URL (http://192.168.0.200:8096)
- "Open this website on TV: [URL]" → Use WebPageViewActivity with provided URL
- "Pause the TV" → Use play/pause toggle keyevent 85
- "Go back" → Use back button keyevent 4
- "Take a screenshot" → Use screencap command
- "What's playing on TV?" → Check foreground app with dumpsys
- "Close Netflix" → Use force-stop with netflix package
- "Navigate right" → Use D-pad right keyevent 22
- "Volume up" → Use volume up keyevent 24

## Command Mapping Reference

Parse natural language to ADB commands:

**Apps:**
- "launch/open/start [app]" → `am start -n [package]/[activity]`
- "open/show/display [url] on TV" → `am start -n com.example.tvserverhub/.WebPageViewActivity -e url "[URL]"`
- "close/stop/quit [app]" → `am force-stop [package]`

**Media:**
- "play" → keyevent 126
- "pause" → keyevent 127
- "play/pause toggle" → keyevent 85
- "next/skip" → keyevent 87
- "previous/back" → keyevent 88
- "stop" → keyevent 86

**Navigation:**
- "up/down/left/right" → keyevent 19/20/21/22
- "select/ok/enter" → keyevent 23
- "back" → keyevent 4
- "home" → keyevent 3

**Volume:**
- "volume up/louder" → keyevent 24
- "volume down/quieter" → keyevent 25
- "mute" → keyevent 164

## Notes

- Commands are sent to whatever app is currently in the foreground
- Media controls work with most video apps (YouTube, Netflix, Jellyfin, Prime Video)
- Some apps may interpret commands differently (e.g., "next" in YouTube skips to next video)
- The Fire TV must be on and connected to the network
- If commands seem unresponsive, check connection with `adb devices`
