# Interactive Visualizations System

This directory contains HTML-based visualizations that can be accessed via URLs and displayed on various devices including Fire TV via ADB.

## 📁 Directory Structure

```
frontend/public/visualizations/
├── README.md          # This file
└── example.html       # Sample visualization
```

## 🌐 Access URLs

Files placed in this directory are automatically served by Vite at:

- **Local access:** `https://localhost:5173/visualizations/<filename>.html`
- **Network access:** `https://192.168.0.28:5173/visualizations/<filename>.html`

## 🚀 Quick Start

### 1. Get URL for a visualization

```bash
# Get localhost URL
scripts/run.sh scripts/get_visualization_url.py example

# Get network URL (for Fire TV or other devices)
scripts/run.sh scripts/get_visualization_url.py example --network
```

### 2. Create a new visualization

Simply create an HTML file in this directory:

```bash
# Create your visualization
cat > frontend/public/visualizations/my-viz.html << 'EOF'
<!DOCTYPE html>
<html>
<head>
    <title>My Visualization</title>
    <style>
        body { background: #000; color: #fff; }
    </style>
</head>
<body>
    <h1>Hello from my visualization!</h1>
</body>
</html>
EOF
```

### 3. Access it immediately

No build step needed! Access at:
- `https://localhost:5173/visualizations/my-viz.html`

### 4. Display on Fire TV

Using the `/tv-remote` skill or ADB commands:

```bash
# Open visualization on Fire TV
adb shell am start -a android.intent.action.VIEW \
    -d "https://192.168.0.28:5173/visualizations/example.html"
```

## 📊 Example Visualization

The included `example.html` demonstrates:
- ✅ Responsive design
- ✅ CSS animations
- ✅ Interactive elements (click the rectangle!)
- ✅ Gradient backgrounds
- ✅ Network info display

## 🎨 Creating Visualizations

### Best Practices

1. **Self-contained:** Keep everything in a single HTML file (inline CSS/JS)
2. **Responsive:** Use viewport meta tag and flexible layouts
3. **TV-friendly:** Consider 1920x1080 resolution and 10-foot UI principles
4. **Performance:** Minimize dependencies, use lightweight libraries

### Template Structure

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Your Visualization</title>
    <style>
        /* Your styles here */
    </style>
</head>
<body>
    <!-- Your content here -->
    <script>
        // Your JavaScript here
    </script>
</body>
</html>
```

### Recommended Libraries (via CDN)

You can include libraries via CDN for more complex visualizations:

```html
<!-- Chart.js for charts -->
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

<!-- D3.js for data visualization -->
<script src="https://d3js.org/d3.v7.min.js"></script>

<!-- Three.js for 3D graphics -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>

<!-- Plotly for interactive plots -->
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
```

## 🔧 Utilities

### Get Visualization URL Script

Location: `scripts/get_visualization_url.py`

```bash
# Usage
scripts/run.sh scripts/get_visualization_url.py <filename> [--network] [--port PORT]

# Examples
scripts/run.sh scripts/get_visualization_url.py example
scripts/run.sh scripts/get_visualization_url.py example --network
scripts/run.sh scripts/get_visualization_url.py example --network --port 5173
```

## 🔌 Integration with Assistant

### From a Skill

You can create skills that generate and display visualizations:

```python
# In your skill
import subprocess

# Generate URL
result = subprocess.run(
    ["scripts/run.sh", "scripts/get_visualization_url.py", "my-viz", "--network"],
    capture_output=True,
    text=True
)
url = result.stdout.strip()

# Display on Fire TV (if /tv-remote is available)
subprocess.run([
    "adb", "shell", "am", "start",
    "-a", "android.intent.action.VIEW",
    "-d", url
])
```

### From Claude

Ask Claude to:
- Create visualizations: "Create a visualization showing system stats"
- Display on TV: "Show the example visualization on Fire TV"
- Generate data visualizations: "Create a chart of my recent activity"

## 🎯 Use Cases

- **System monitoring dashboards**
- **Data visualization charts**
- **Interactive presentations**
- **Information displays for Fire TV**
- **Real-time data feeds**
- **Custom UI overlays**
- **Gaming scoreboards**
- **Weather displays**
- **News tickers**

## 🌟 Advanced Features

### Real-time Updates

Use WebSockets or Server-Sent Events to push updates:

```javascript
// Example: Connect to assistant API for real-time data
const ws = new WebSocket('ws://192.168.0.28:8000/api/ws');
ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    updateVisualization(data);
};
```

### Interactive Controls

Make visualizations that respond to Fire TV remote:

```javascript
// Listen for keyboard events (Fire TV remote sends these)
document.addEventListener('keydown', (e) => {
    switch(e.key) {
        case 'ArrowUp': moveUp(); break;
        case 'ArrowDown': moveDown(); break;
        case 'Enter': select(); break;
    }
});
```

## 🐛 Troubleshooting

### File not accessible

1. Check frontend is running: `lsof -ti:5173`
2. Verify file exists: `ls frontend/public/visualizations/`
3. Check permissions: `ls -la frontend/public/visualizations/`

### Fire TV can't access

1. Ensure both devices on same network
2. Test URL in browser first
3. Check firewall isn't blocking port 5173
4. Use network URL (192.168.0.28), not localhost

### Visualization not updating

1. Hard refresh in browser (Ctrl+Shift+R)
2. Vite serves files directly - no build needed
3. Check browser console for JavaScript errors

## 📚 Resources

- [Vite Static Assets](https://vitejs.dev/guide/assets.html#the-public-directory)
- [Chart.js Documentation](https://www.chartjs.org/docs/latest/)
- [D3.js Examples](https://observablehq.com/@d3/gallery)
- [Fire TV Development](https://developer.amazon.com/docs/fire-tv/getting-started-developing-apps-and-games.html)
