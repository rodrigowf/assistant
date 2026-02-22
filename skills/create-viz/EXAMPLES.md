# Create-Viz Skill - Usage Examples

## Quick Start Examples

### 1. Hello World - Simplest Usage

```
/create-viz filename=hello title="Hello World"
```

Output:
- Creates: `frontend/public/visualizations/hello.html`
- Uses: Basic template (default)
- URL: `https://192.168.0.28:5173/visualizations/hello.html`

---

### 2. Data Chart

```
/create-viz filename=sales-2024 title="Sales Report 2024" type=chart
```

Output:
- Creates: `frontend/public/visualizations/sales-2024.html`
- Uses: Chart template with Chart.js
- Includes: Sample bar chart with demo data
- Ready to customize with your own data

---

### 3. Dashboard Display

```
/create-viz filename=metrics title="System Metrics Dashboard" type=dashboard
```

Output:
- Creates: `frontend/public/visualizations/metrics.html`
- Uses: Dashboard template with 4-card grid
- Features: Auto-updating sample metrics
- Perfect for real-time monitoring

---

### 4. Fire TV Remote Demo

```
/create-viz filename=tv-menu title="TV Navigation Menu" type=tv-remote
```

Output:
- Creates: `frontend/public/visualizations/tv-menu.html`
- Uses: TV-remote template
- Features: Arrow key navigation, selection feedback
- Optimized for 10-foot UI

---

### 5. Create and Display on TV

```
/create-viz filename=welcome title="Welcome Screen" type=basic display-tv
```

Output:
- Creates the visualization
- Generates network URL
- Automatically opens on Fire TV
- Requires ADB connection

---

## Natural Language Examples

Claude will understand these natural requests:

### Example 1
**User**: "Create a chart visualization called monthly-stats"

**Claude interprets as**:
- filename: monthly-stats
- title: Monthly Stats (inferred from filename)
- type: chart (explicitly mentioned)

### Example 2
**User**: "Make a dashboard for system monitoring and show it on the TV"

**Claude interprets as**:
- filename: system-monitoring (inferred)
- title: System Monitoring
- type: dashboard (explicitly mentioned)
- display-tv: yes (user said "show it on the TV")

### Example 3
**User**: "I need a basic visualization named test-page with the title 'Test Page'"

**Claude interprets as**:
- filename: test-page
- title: Test Page
- type: basic (explicitly mentioned)

### Example 4
**User**: "Create a TV-friendly menu called main-menu"

**Claude interprets as**:
- filename: main-menu
- title: Main Menu
- type: tv-remote (inferred from "TV-friendly")

---

## Advanced Examples

### Custom HTML Content

**Direct script call**:
```bash
scripts/run.sh scripts/create_visualization.py \
  --filename custom \
  --title "Custom Viz" \
  --content '<div style="text-align:center;"><h1>Custom Content</h1></div>' \
  --show-url
```

**Via skill** (provide HTML in message):
```
Create a visualization called custom with this HTML:
<div style="text-align:center;">
  <h1>Custom Content</h1>
</div>
```

---

### Using Custom Template File

1. Create your template:
```html
<!-- my-template.html -->
<!DOCTYPE html>
<html>
<head><title>{title}</title></head>
<body><h1>{title}</h1></body>
</html>
```

2. Use it:
```bash
scripts/run.sh scripts/create_visualization.py \
  --filename from-template \
  --title "From Template" \
  --template my-template.html \
  --show-url
```

---

### Display Existing Visualization on TV

**Get the URL**:
```bash
scripts/run.sh scripts/get_visualization_url.py my-viz --network
```

**Display on TV**:
```bash
adb shell am start -a android.intent.action.VIEW \
  -d "$(scripts/run.sh scripts/get_visualization_url.py my-viz --network)"
```

**Or just ask**:
```
Show the my-viz visualization on Fire TV
```

---

## Real-World Use Cases

### Use Case 1: System Monitoring Dashboard

```
/create-viz filename=server-stats title="Server Statistics" type=dashboard
```

Then edit the file to connect to your monitoring API:
```javascript
// Add to the created HTML file
async function updateMetrics() {
  const response = await fetch('/api/stats');
  const data = await response.json();

  document.getElementById('cpu').textContent = data.cpu + '%';
  document.getElementById('memory').textContent = data.memory + '%';
  // ... update other metrics
}

setInterval(updateMetrics, 5000);
```

---

### Use Case 2: Data Visualization

```
/create-viz filename=covid-trends title="COVID-19 Trends" type=chart
```

Then customize with your data:
```javascript
// Edit the chart configuration
data: {
  labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May'],
  datasets: [{
    label: 'Cases',
    data: [1200, 1900, 3000, 2500, 2200],
    backgroundColor: 'rgba(255, 99, 132, 0.8)',
  }]
}
```

---

### Use Case 3: Interactive TV Menu

```
/create-viz filename=media-hub title="Media Hub" type=tv-remote display-tv
```

Customize the menu items:
```html
<div class="menu-item selected">🎬 Movies</div>
<div class="menu-item">📺 TV Shows</div>
<div class="menu-item">🎵 Music</div>
<div class="menu-item">🎮 Games</div>
<div class="menu-item">⚙️ Settings</div>
```

Add action handlers:
```javascript
case 'Enter':
  const selected = items[currentSelection].textContent;
  if (selected.includes('Movies')) {
    // Launch movie app or page
  }
  break;
```

---

### Use Case 4: Information Display

```
/create-viz filename=weather title="Weather Dashboard" type=dashboard display-tv
```

Fetch weather data:
```javascript
async function updateWeather() {
  const response = await fetch('https://api.weather.com/...');
  const weather = await response.json();

  document.querySelector('.temperature').textContent =
    weather.temp + '°F';
  document.querySelector('.condition').textContent =
    weather.condition;
}

updateWeather();
setInterval(updateWeather, 600000); // Update every 10 minutes
```

---

## Testing Workflow

### 1. Create visualization
```
/create-viz filename=test title="Test" type=basic
```

### 2. View in browser
```bash
xdg-open https://localhost:5173/visualizations/test.html
```

### 3. Edit if needed
```bash
nano frontend/public/visualizations/test.html
```

### 4. Refresh browser to see changes

### 5. Display on TV when ready
```
Show test visualization on Fire TV
```

---

## Template Comparison

| Feature | Basic | Chart | Dashboard | TV-Remote |
|---------|-------|-------|-----------|-----------|
| Simplicity | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ |
| Customization | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ |
| Data Viz | ⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐ |
| TV-Friendly | ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| Real-time | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ |
| Interactive | ⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ |

---

## Tips & Tricks

### Tip 1: Filename Conventions
- Use lowercase and hyphens: `my-chart` ✅
- Avoid spaces: `my chart` ❌
- Extension optional: `my-chart` or `my-chart.html` both work

### Tip 2: Quick Iteration
- No build step needed
- Edit HTML file directly
- Refresh browser to see changes
- Use browser DevTools for debugging

### Tip 3: TV Testing
- Test in desktop browser first
- Use Firefox's Responsive Design Mode (Ctrl+Shift+M)
- Set to 1920x1080 resolution
- Use keyboard arrows to simulate remote

### Tip 4: URL Management
- Use `--show-url` flag to get URLs in output
- Network URL works on all LAN devices
- Localhost URL only works on same machine
- Save URLs for later reference

### Tip 5: Reusing Visualizations
- Copy existing file as starting point
- Browse gallery for examples: `https://localhost:5173/visualizations/`
- Check out existing visualizations for patterns
- Templates are just starting points - customize freely

---

## Common Patterns

### Pattern 1: Periodic Updates
```javascript
setInterval(() => {
  // Update logic here
}, 5000); // Every 5 seconds
```

### Pattern 2: Fetch Data
```javascript
async function fetchData() {
  const response = await fetch('/api/data');
  const data = await response.json();
  updateVisualization(data);
}
```

### Pattern 3: WebSocket Connection
```javascript
const ws = new WebSocket('ws://192.168.0.28:8000/api/ws');
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  updateVisualization(data);
};
```

### Pattern 4: Remote Navigation
```javascript
let selected = 0;

document.addEventListener('keydown', (e) => {
  switch(e.key) {
    case 'ArrowUp':
      selected = Math.max(0, selected - 1);
      break;
    case 'ArrowDown':
      selected = Math.min(items.length - 1, selected + 1);
      break;
    case 'Enter':
      handleSelection(selected);
      break;
  }
  updateUI();
});
```

---

## Troubleshooting Examples

### Problem: Visualization not showing on TV

**Solution**:
```bash
# Check ADB connection
adb devices

# If no device, connect
/connect-tv

# Try displaying again
/create-viz filename=test title="Test" display-tv
```

---

### Problem: Need to update existing visualization

**Solution**:
```bash
# Just create it again with same filename
/create-viz filename=existing title="Updated Title" type=chart

# Files are overwritten
```

---

### Problem: Want to see all visualizations

**Solution**:
```bash
# Open gallery
xdg-open https://localhost:5173/visualizations/index.html

# Or list files
ls frontend/public/visualizations/
```

---

## Next Steps

After creating visualizations:

1. **Browse gallery**: https://localhost:5173/visualizations/index.html
2. **Edit files**: Customize HTML/CSS/JS directly
3. **Display on TV**: Use `display-tv` flag or `/tv-remote` skill
4. **Integrate with data**: Add API calls or WebSocket connections
5. **Create custom templates**: Save your designs for reuse

Enjoy creating visualizations! 🎨
