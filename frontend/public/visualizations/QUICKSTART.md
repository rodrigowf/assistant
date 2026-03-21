# Visualization System - Quick Start

## ✅ System Status

- ✅ Static file serving: **WORKING**
- ✅ Local IP: `192.168.0.28`
- ✅ Frontend port: `5432`
- ✅ Example visualizations: **2 files ready**

## 🚀 Access Visualizations

### Example Visualization
- **Local:** https://localhost:5432/visualizations/example.html
- **Network:** https://192.168.0.28:5432/visualizations/example.html
- **Features:** Interactive colored rectangle with animations

### System Stats Dashboard
- **Local:** https://localhost:5432/visualizations/system-stats.html
- **Network:** https://192.168.0.28:5432/visualizations/system-stats.html
- **Features:** Real-time CPU/Memory/Disk monitoring (simulated data)

## 🔧 Quick Commands

```bash
# Get URL for any visualization
context/scripts/run.sh context/scripts/get_visualization_url.py <filename> --network

# Examples:
context/scripts/run.sh context/scripts/get_visualization_url.py example --network
context/scripts/run.sh context/scripts/get_visualization_url.py system-stats --network

# Display on Fire TV (requires ADB connection)
adb shell am start -a android.intent.action.VIEW \
    -d "https://192.168.0.28:5432/visualizations/example.html"
```

## 📝 Create New Visualization

1. Create HTML file in this directory:
   ```bash
   touch frontend/public/visualizations/my-viz.html
   ```

2. Edit with your content (see example.html for template)

3. Access immediately at:
   ```
   https://localhost:5432/visualizations/my-viz.html
   ```

No build step required! Files are served directly by Vite.

## 🎯 Fire TV Integration

### Option 1: Via ADB Command
```bash
adb shell am start -a android.intent.action.VIEW \
    -d "https://192.168.0.28:5432/visualizations/<your-file>.html"
```

### Option 2: Via /tv-remote skill (if available)
Ask Claude:
```
"Display the example visualization on Fire TV"
"Show system stats on the TV"
```

## 💡 Tips

- **URL format:** Always use network IP (192.168.0.28) for Fire TV
- **Self-contained:** Keep CSS/JS inline in HTML files for reliability
- **TV-friendly:** Design for 1920x1080, use large fonts and high contrast
- **Interactive:** Fire TV remote sends keyboard events (arrow keys, Enter)

## 📚 More Information

See `README.md` in this directory for comprehensive documentation.
