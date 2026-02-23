# Interactive Visualization System

## Overview

A complete system for creating and displaying HTML-based interactive visualizations that can be accessed via URLs and displayed on Fire TV or any web browser. Built on top of the Assistant project's Vite-powered frontend.

## ✅ System Status

- **Static File Serving:** ✅ Working (via Vite)
- **Local IP:** `192.168.0.28`
- **Frontend Port:** `5173`
- **Visualizations Created:** 4 (index, example, system-stats, remote-demo)
- **Fire TV Ready:** ✅ Yes

## 📁 Structure

```
frontend/public/visualizations/
├── index.html              # Gallery homepage listing all visualizations
├── example.html            # Interactive animated rectangle demo
├── system-stats.html       # Real-time system monitoring dashboard
├── remote-demo.html        # Fire TV remote control integration demo
├── README.md               # Comprehensive documentation
└── QUICKSTART.md           # Quick reference guide

context/scripts/
├── get_visualization_url.py       # Utility to generate URLs
└── test_visualization_on_tv.sh    # Fire TV testing script
```

## 🌐 Access URLs

### Gallery Index
- **Local:** https://localhost:5173/visualizations/index.html
- **Network:** https://192.168.0.28:5173/visualizations/index.html

### Individual Visualizations
| Name | Description | URL |
|------|-------------|-----|
| Example | Animated interactive rectangle | https://192.168.0.28:5173/visualizations/example.html |
| System Stats | Real-time monitoring dashboard | https://192.168.0.28:5173/visualizations/system-stats.html |
| Remote Demo | Fire TV remote control demo | https://192.168.0.28:5173/visualizations/remote-demo.html |

## 🚀 Quick Start

### 1. View in Browser

Simply open any URL in your browser. No build step needed!

```bash
# Local access
xdg-open https://localhost:5173/visualizations/index.html

# Or use the helper script
context/scripts/run.sh context/scripts/get_visualization_url.py index
```

### 2. Display on Fire TV

**Option A: Using the test script**
```bash
context/scripts/test_visualization_on_tv.sh
# Then select which visualization to display
```

**Option B: Direct ADB command**
```bash
adb shell am start -a android.intent.action.VIEW \
    -d "https://192.168.0.28:5173/visualizations/example.html"
```

**Option C: Via /tv-remote skill** (if available)
```
Ask Claude: "Display the example visualization on Fire TV"
```

### 3. Create New Visualization

```bash
# Create new HTML file
touch frontend/public/visualizations/my-viz.html

# Edit the file with your content
# (Use example.html as a template)

# Access immediately - no build needed!
https://localhost:5173/visualizations/my-viz.html
```

## 🛠️ Utilities

### get_visualization_url.py

Generate URLs for visualizations:

```bash
# Get localhost URL
context/scripts/run.sh context/scripts/get_visualization_url.py example

# Get network URL (for Fire TV)
context/scripts/run.sh context/scripts/get_visualization_url.py example --network

# Specify custom port
context/scripts/run.sh context/scripts/get_visualization_url.py example --network --port 5173
```

### test_visualization_on_tv.sh

Interactive Fire TV testing:

```bash
# Interactive mode
context/scripts/test_visualization_on_tv.sh

# Direct selection
context/scripts/test_visualization_on_tv.sh 1  # Gallery
context/scripts/test_visualization_on_tv.sh 2  # Example
context/scripts/test_visualization_on_tv.sh 3  # System Stats
context/scripts/test_visualization_on_tv.sh 4  # Remote Demo
context/scripts/test_visualization_on_tv.sh 5  # Cycle through all
```

## 📊 Visualization Examples

### 1. Example Visualization
- **Purpose:** Demonstrate basic functionality
- **Features:**
  - Animated gradient background
  - Interactive color-changing rectangle (click to change)
  - Responsive design
  - Network information display
- **Best for:** Testing the system, template for new visualizations

### 2. System Stats Dashboard
- **Purpose:** Real-time system monitoring
- **Features:**
  - CPU usage with progress bar
  - Memory usage statistics
  - Disk usage monitoring
  - Network information
  - Auto-refreshing activity log
  - Retro terminal aesthetic
- **Best for:** System monitoring on TV, dashboard displays

### 3. Fire TV Remote Demo
- **Purpose:** Demonstrate remote control integration
- **Features:**
  - 3x3 grid navigation
  - Arrow key navigation (Fire TV remote compatible)
  - Enter to select
  - Visual feedback on selection
  - Key press history log
  - Fire TV user agent detection
- **Best for:** Testing 10-foot UI, remote control interactions

## 🎨 Creating Visualizations

### Best Practices

1. **Self-contained:** Keep all CSS/JS inline in the HTML file
2. **Responsive:** Use viewport meta tag and flexible layouts
3. **TV-friendly:** Design for 1920x1080, use large fonts and high contrast
4. **Interactive:** Fire TV remote sends keyboard events
5. **Performance:** Minimize dependencies, keep it lightweight

### Basic Template

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>My Visualization</title>
    <style>
        body {
            background: #000;
            color: #fff;
            font-family: Arial, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
        }
    </style>
</head>
<body>
    <h1>Hello from my visualization!</h1>

    <script>
        // Your interactive code here
        console.log('Visualization loaded');
    </script>
</body>
</html>
```

### Fire TV Remote Integration

```javascript
// Listen for Fire TV remote control
document.addEventListener('keydown', (e) => {
    switch(e.key) {
        case 'ArrowUp':
            // Handle up
            break;
        case 'ArrowDown':
            // Handle down
            break;
        case 'ArrowLeft':
            // Handle left
            break;
        case 'ArrowRight':
            // Handle right
            break;
        case 'Enter':
            // Handle select/OK
            break;
    }
});
```

## 🔌 Integration Points

### With Assistant Skills

Create skills that generate and display visualizations:

```yaml
# skill.yaml
name: show-viz
description: Display a visualization on Fire TV
instructions: |
  1. Use get_visualization_url.py to get the network URL
  2. Use ADB to open the URL on Fire TV
  3. Respond with confirmation
```

### With Claude

Just ask Claude to:
- "Create a visualization showing [your data]"
- "Display the system stats on Fire TV"
- "Show me the visualization gallery"

### With Other Tools

- `/connect-tv` - Connect to Fire TV via ADB
- `/tv-remote` - Control Fire TV (launch apps, navigate, etc.)
- `/recall` - Search for past visualization examples

## 🔧 Technical Details

### How It Works

1. **Static File Serving:** Vite automatically serves files from `frontend/public/` at the root URL
2. **No Build Step:** Changes are immediately accessible
3. **Hot Reload:** Not needed - HTML files are served directly
4. **Network Access:** Frontend is accessible on LAN via local IP
5. **Fire TV Integration:** Standard Android intents via ADB

### Architecture

```
Browser/Fire TV
      ↓
   HTTP GET https://192.168.0.28:5173/visualizations/example.html
      ↓
  Vite Dev Server (port 5173)
      ↓
  Serves from: frontend/public/visualizations/example.html
      ↓
  Returns: HTML + inline CSS + inline JS
      ↓
  Browser renders
```

### Requirements

- **Frontend:** Running on port 5173 (check with `lsof -ti:5173`)
- **Network:** Device on same LAN as Fire TV
- **ADB:** For Fire TV integration (install: `sudo apt install adb`)
- **Firewall:** Port 5173 open for LAN access

## 🐛 Troubleshooting

### Visualization not accessible

```bash
# 1. Check frontend is running
lsof -ti:5173

# 2. Verify file exists
ls -la frontend/public/visualizations/

# 3. Test with curl
curl -I https://localhost:5173/visualizations/example.html
# Should return: HTTP/1.1 200 OK
```

### Fire TV can't access visualization

```bash
# 1. Verify ADB connection
adb devices

# 2. Test URL in desktop browser first
xdg-open https://192.168.0.28:5173/visualizations/example.html

# 3. Check firewall
sudo ufw status
# Port 5173 should be open for LAN
```

### Visualization not updating

- Hard refresh: Ctrl + Shift + R (browser)
- No build needed - changes are immediate
- Check browser console for JavaScript errors
- Vite serves files directly from `public/`

## 📈 Use Cases

- **System Monitoring:** Real-time dashboards on TV
- **Data Visualization:** Charts, graphs, live data feeds
- **Information Displays:** News, weather, calendars
- **Interactive Presentations:** Slideshows, demos
- **Gaming:** Scoreboards, leaderboards
- **Media Control:** Custom UIs for media management
- **Home Automation:** Status displays, control panels
- **Development:** Testing UIs on actual TV hardware

## 🎯 Next Steps

### Potential Enhancements

1. **Real-time Data Integration**
   - Connect to Assistant API via WebSocket
   - Push live data to visualizations
   - Two-way communication

2. **Dynamic Data Visualizations**
   - Integrate Chart.js, D3.js, Plotly
   - Generate charts from Assistant data
   - Export conversation stats

3. **Custom Skills**
   - `/create-viz` - Generate visualizations from prompts
   - `/show-stats` - Display system/usage statistics
   - `/tv-dashboard` - Launch custom dashboard on TV

4. **Templates & Generators**
   - Visualization template library
   - CLI generator for common patterns
   - Gallery of community visualizations

## 📚 Resources

- **Documentation:** `frontend/public/visualizations/README.md`
- **Quick Reference:** `frontend/public/visualizations/QUICKSTART.md`
- **Gallery:** https://localhost:5173/visualizations/index.html
- **Examples:** Use existing `.html` files as templates

## 🎉 Success Criteria

All checkboxes met:

- ✅ Static files accessible via URL
- ✅ Network access working (192.168.0.28)
- ✅ Fire TV integration functional
- ✅ Multiple example visualizations created
- ✅ Helper scripts for URL generation and testing
- ✅ Comprehensive documentation
- ✅ Gallery index page
- ✅ Fire TV remote control integration demonstrated

## Summary

You now have a complete, working visualization system that:
- Serves HTML files directly via Vite (no build step)
- Is accessible from any device on your LAN
- Can be displayed on Fire TV via ADB
- Includes example visualizations demonstrating key features
- Provides utilities for easy URL generation and testing
- Has comprehensive documentation and quick-start guides

**Start exploring:** https://localhost:5173/visualizations/index.html
