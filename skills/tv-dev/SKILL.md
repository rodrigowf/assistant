---
name: tv-dev
description: Develop, deploy, and debug the TvServerHub Android TV app - build, deploy, troubleshoot screensaver/services, and modify the application
argument-hint: "[action]"
allowed-tools: Read, Write, Edit, Bash(*)
---

# TvServerHub Development

Complete development workflow for the TvServerHub Android TV application running on Fire TV Stick.

## Project Location

**Root:** `/home/rodrigo/AndroidStudioProjects/TvServerHub`

**Documentation:**
- `CLAUDE.md` - Build, deploy, and debugging commands reference
- `DEBUGGING.md` - Detailed WebView debugging, ADB commands, troubleshooting guide

**Key Files:**
- `app/src/main/java/com/example/tvserverhub/MainActivity.kt` - Entry point
- `app/src/main/java/com/example/tvserverhub/WebViewScreen.kt` - WebView rendering
- `app/src/main/java/com/example/tvserverhub/AppLauncher.kt` - App grid launcher
- `app/src/main/java/com/example/tvserverhub/ScreensaverMonitorService.kt` - Inactivity monitor (10s interval, 80s timeout)
- `app/src/main/java/com/example/tvserverhub/BootReceiver.kt` - Auto-start on boot
- `app/src/main/java/com/example/tvserverhub/AnimatedGradientBackground.kt` - Animated background
- `app/src/main/AndroidManifest.xml` - Permissions and components
- `app/build.gradle.kts` - Build configuration
- `build.gradle.kts` - Root build file

## Connection

Always ensure Fire TV is connected:
```bash
# Check connection
adb devices | grep 192.168.0.16

# Auto-discover if not connected
/home/rodrigo/AndroidStudioProjects/TvServerHub/scripts/discover-firetv.sh
```

## Development Workflow

### 1. Build the APK

```bash
cd /home/rodrigo/AndroidStudioProjects/TvServerHub

# Clean build
./gradlew clean assembleDebug

# Regular build
./gradlew assembleDebug

# Output location: app/build/outputs/apk/debug/app-debug.apk (~11MB)
```

### 2. Deploy to Fire TV

```bash
# Install (or reinstall with -r flag)
adb -s 192.168.0.16:5555 install -r app/build/outputs/apk/debug/app-debug.apk

# Complete build + deploy in one command
cd /home/rodrigo/AndroidStudioProjects/TvServerHub && \
./gradlew assembleDebug && \
adb -s 192.168.0.16:5555 install -r app/build/outputs/apk/debug/app-debug.apk
```

### 3. Grant Required Permissions

**Required after every fresh install:**
```bash
# Grant usage stats (for ScreensaverMonitorService to detect foreground apps)
adb -s 192.168.0.16:5555 shell "appops set com.example.tvserverhub android:get_usage_stats allow"

# Grant system alert window (for background activity launches)
adb -s 192.168.0.16:5555 shell "appops set com.example.tvserverhub SYSTEM_ALERT_WINDOW allow"
```

### 4. Launch and Test

```bash
# Launch the app
adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.MainActivity

# Full workflow: build, deploy, grant permissions, launch
cd /home/rodrigo/AndroidStudioProjects/TvServerHub && \
./gradlew assembleDebug && \
adb -s 192.168.0.16:5555 install -r app/build/outputs/apk/debug/app-debug.apk && \
adb -s 192.168.0.16:5555 shell "appops set com.example.tvserverhub android:get_usage_stats allow" && \
adb -s 192.168.0.16:5555 shell "appops set com.example.tvserverhub SYSTEM_ALERT_WINDOW allow" && \
adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.MainActivity
```

## Debugging

### View Logs

```bash
# View all app logs (filtered)
adb -s 192.168.0.16:5555 logcat TvServerHub:* WebViewScreen:* ScreensaverMonitor:* *:S

# View specific debug tags
adb -s 192.168.0.16:5555 logcat CLICK_DEBUG:* WebView-JS-LOG:* WebView-JS-ERROR:* TvServerHub:* ScreensaverMonitor:* *:S

# Clear logs before launching (for clean output)
adb -s 192.168.0.16:5555 logcat -c && \
adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.MainActivity && \
adb -s 192.168.0.16:5555 logcat TvServerHub:* WebViewScreen:* CLICK_DEBUG:* *:S

# Save logs to file
adb -s 192.168.0.16:5555 logcat -d > /tmp/firetv-logs-$(date +%Y%m%d-%H%M%S).txt

# Search recent logs (when tag filtering doesn't work)
adb logcat -d -t 500 | grep -i "ScreensaverMonitor\|TvServerHub"
```

### WebView Debugging (Chrome DevTools)

```bash
# Get app process ID
PID=$(adb -s 192.168.0.16:5555 shell pidof com.example.tvserverhub)

# Forward WebView DevTools port
adb -s 192.168.0.16:5555 forward tcp:9222 localabstract:webview_devtools_remote_${PID}

# List available WebView pages
curl -s http://localhost:9222/json | python3 -m json.tool

# Open in Chrome browser:
# 1. Go to chrome://inspect
# 2. Or directly open: http://localhost:9222
```

### Check App Status

```bash
# Check if running
adb -s 192.168.0.16:5555 shell pidof com.example.tvserverhub

# Check foreground activity
adb -s 192.168.0.16:5555 shell "dumpsys activity activities | grep -A 10 'mResumedActivity'"

# Check ScreensaverMonitorService status (IMPORTANT for screensaver issues)
adb -s 192.168.0.16:5555 shell "dumpsys activity services com.example.tvserverhub"

# Get app version
adb -s 192.168.0.16:5555 shell "dumpsys package com.example.tvserverhub | grep versionCode"

# Check all TvServerHub permissions
adb shell "appops get com.example.tvserverhub"
```

### Troubleshooting: Screensaver Not Appearing

**How the screensaver works:** TvServerHub implements its own "screensaver" by:
1. Disabling the native Android screensaver (`screensaver_enabled=0`)
2. Setting screen timeout to maximum (`2147483647`)
3. Running `ScreensaverMonitorService` as a foreground service
4. Monitoring inactivity every 10 seconds
5. After 80 seconds idle (no app switching, no media, no input), bringing MainActivity to front

**If the screensaver isn't appearing, check:**

```bash
# 1. Is the ScreensaverMonitorService running?
adb shell "dumpsys activity services com.example.tvserverhub" | grep -A 10 "ScreensaverMonitorService"
# Should show: isForeground=true, startForegroundCount>=1

# 2. Check system screensaver settings (should be disabled)
adb shell "settings get secure screensaver_enabled"     # Should be: 0
adb shell "settings get system screen_off_timeout"      # Should be: 2147483647

# 3. Check required permissions
adb shell "appops get com.example.tvserverhub"
# Must show: GET_USAGE_STATS: allow, SYSTEM_ALERT_WINDOW: allow, START_FOREGROUND: allow
```

**Common fix: Restart the app to reinitialize the service:**
```bash
adb shell "am force-stop com.example.tvserverhub && sleep 1 && am start -n com.example.tvserverhub/.MainActivity"
```

**Root cause:** The `ScreensaverMonitorService` can be killed by Android under low-memory conditions. While it uses `START_STICKY`, the service may not restart automatically. Restarting the MainActivity triggers `initializeEnvironment()` → `startScreensaverMonitor()`.

**Verify fix worked:**
```bash
# Service should now be listed
adb shell "dumpsys activity services com.example.tvserverhub" | grep "ScreensaverMonitorService"
```

### Screenshots

```bash
# Take screenshot
adb -s 192.168.0.16:5555 exec-out screencap -p > /tmp/tv-screenshot-$(date +%Y%m%d-%H%M%S).png
```

## App Control

```bash
# Force stop
adb -s 192.168.0.16:5555 shell am force-stop com.example.tvserverhub

# Clear all app data (reset to fresh state)
adb -s 192.168.0.16:5555 shell pm clear com.example.tvserverhub

# Uninstall
adb -s 192.168.0.16:5555 uninstall com.example.tvserverhub
```

## Modifying the App

### Key Components

**1. ScreensaverMonitorService** (`ScreensaverMonitorService.kt`)
- Runs as a **foreground service** (notification in tray: "Screensaver monitor active")
- Monitors inactivity every 10 seconds (CHECK_INTERVAL = 10000L)
- Returns to TvServerHub after 80 seconds idle (IDLE_TIMEOUT = 80)
- Tracks: app switches, media playback, AND remote control input
- Detects input from DPAD, GAMEPAD, and KEYBOARD sources via InputDevice API
- Uses `UsageStatsManager` to detect current foreground app (fallback: `getRunningTasks`)
- Checks `AudioManager.isMusicActive` to avoid interrupting media playback
- Requires permissions: `PACKAGE_USAGE_STATS`, `SYSTEM_ALERT_WINDOW`, `FOREGROUND_SERVICE`
- Uses `START_STICKY` but **can be killed by system** - see Troubleshooting section

**2. MainActivity** (`MainActivity.kt`)
- Sets up Compose UI with navigation (Home screen ↔ WebView screen)
- On launch, runs `initializeEnvironment()` which:
  - Starts `ScreensaverMonitorService` as foreground service
  - Configures display settings (disables native screensaver)
  - Launches YouTube in background for Chromecast functionality
- **Display settings configured:**
  - `screensaver_enabled` → 0 (disables native Android screensaver)
  - `screen_off_timeout` → 2147483647 (prevents screen from turning off)
  - `stay_on_while_plugged_in` → 7 (screen always on when powered)
- Uses shared preferences to track initialization state per session

**3. WebViewScreen** (`WebViewScreen.kt`)
- Renders web apps in fullscreen WebView
- JavaScript enabled with console.log capture
- SSL certificate handling (accepts all for local dev)
- Microphone permission support
- Chrome DevTools remote debugging enabled

**4. AppLauncher** (`AppLauncher.kt`)
- Displays app grid with focus-based animations
- Supports Agentic Assistant, Jellyfin, Copyparty apps
- First item spans full width, rest in 2-column grid
- Grayscale unfocused tiles, color on focus

**5. AnimatedGradientBackground** (`AnimatedGradientBackground.kt`)
- Animated gradient backdrop for launcher

### Common Modifications

**Change inactivity timeout:**
Edit `ScreensaverMonitorService.kt`:
```kotlin
private const val CHECK_INTERVAL = 10000L  // Check interval in ms
private const val IDLE_TIMEOUT = 80        // Timeout in seconds
```

**Add new app to launcher:**
Edit `MainActivity.kt`, add to apps list:
```kotlin
AppItem(
    name = "App Name",
    imageName = "app_image",  // Drawable resource name
    url = "https://url.to.app"
)
```

**Modify permissions:**
Edit `app/src/main/AndroidManifest.xml`:
```xml
<uses-permission android:name="android.permission.NAME" />
```

**Change app behavior:**
- Read relevant .kt file with Read tool
- Make changes with Edit tool
- Build and deploy to test

### After Making Changes

1. Save all files
2. Build the APK: `./gradlew assembleDebug`
3. Deploy to Fire TV: `adb -s 192.168.0.16:5555 install -r app/build/outputs/apk/debug/app-debug.apk`
4. Launch and test: `adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.MainActivity`
5. Watch logs for errors: `adb -s 192.168.0.16:5555 logcat TvServerHub:* *:S`

## Reference Documentation

**For detailed information, always consult:**

1. **`/home/rodrigo/AndroidStudioProjects/TvServerHub/CLAUDE.md`**
   - Complete build and deploy commands
   - ADB usage patterns
   - Quick reference guide

2. **`/home/rodrigo/AndroidStudioProjects/TvServerHub/DEBUGGING.md`**
   - WebView debugging setup
   - Chrome DevTools configuration
   - Troubleshooting common issues
   - Complete ADB command reference

3. **Memory File:** `.claude_config/projects/-home-rodrigo-Projects-assistant/memory/television_integration_project.md`
   - Complete project documentation
   - Network configuration
   - App architecture details
   - Example skills and workflows

## Usage Examples

When user says:
- "Build and deploy the TV app" → Run full build + deploy + permissions + launch workflow
- "Check the TV app logs" → View filtered logcat output
- "Modify the screensaver timeout" → Edit ScreensaverMonitorService.kt, then build + deploy
- "Add a new app to the launcher" → Edit MainActivity.kt apps list, then build + deploy
- "Debug the WebView" → Set up DevTools port forwarding and provide Chrome inspect URL
- "Take a screenshot of the TV" → Use screencap command
- "What's the app version?" → Check with dumpsys package command
- "The screensaver isn't appearing" / "Debug the screensaver" → Check ScreensaverMonitorService status, restart app if needed
- "The TV app isn't returning to home" → Verify ScreensaverMonitorService is running with foreground notification
- "Check if the TV app services are running" → Use `dumpsys activity services com.example.tvserverhub`

## Package Information

- **Package Name:** `com.example.tvserverhub`
- **Main Activity:** `.MainActivity`
- **Service:** `.ScreensaverMonitorService`
- **Receiver:** `.BootReceiver`
- **APK Size:** ~11MB (debug build)
- **Min SDK:** Android TV compatible
- **Target Device:** Amazon Fire TV Stick (AFTKM), Android 11

## Fire TV Details

- **IP:** 192.168.0.16:5555 (current, subject to change)
- **MAC:** 90:39:5f:bd:1f:13 (for DHCP reservation)
- **ADB Port:** 5555
- **Auto-discovery:** `/home/rodrigo/AndroidStudioProjects/TvServerHub/scripts/discover-firetv.sh`
