# Create Visualization Skill

Create interactive HTML visualizations that can be displayed in a browser or on Fire TV.

## Installation

The skill is ready to use! Files created:
- **Skill definition**: `skills/create-viz/SKILL.md`
- **Creation script**: `scripts/create_visualization.py`

## Usage

### Basic Command Format

```bash
/create-viz filename=NAME title="TITLE" [type=TYPE] [display-tv]
```

### Examples

#### 1. Create a basic visualization
```
/create-viz filename=welcome title="Welcome Screen" type=basic
```

#### 2. Create a chart visualization
```
/create-viz filename=sales-chart title="Sales Report 2024" type=chart
```

#### 3. Create a dashboard
```
/create-viz filename=metrics title="System Metrics" type=dashboard
```

#### 4. Create and display on TV
```
/create-viz filename=tv-demo title="TV Demo" type=tv-remote display-tv
```

#### 5. Natural language (Claude will parse)
```
Create a chart visualization called "monthly-stats" with the title "Monthly Statistics"
```

```
Make a dashboard called system-overview and show it on the Fire TV
```

## Built-in Templates

| Type | Description | Best For |
|------|-------------|----------|
| `basic` | Simple centered content with gradient | Starting point, custom content |
| `chart` | Chart.js integration with bar chart | Data visualization, graphs |
| `dashboard` | Multi-card grid layout | Real-time metrics, monitoring |
| `tv-remote` | Fire TV remote-controlled menu | 10-foot UI, TV navigation |

## Parameters

- **filename** (required): Name of the HTML file (e.g., "my-viz" or "my-viz.html")
- **title** (required): Display title for the visualization
- **type** (optional): Template type (basic, chart, dashboard, tv-remote)
- **display-tv** (optional): Show on Fire TV immediately
- **content** (optional): Custom HTML content
- **template** (optional): Path to custom template file

## Output

After creation, you'll receive:
- ✅ Success message with file path
- 🌐 Local URL: `https://localhost:5173/visualizations/filename.html`
- 🌐 Network URL: `https://192.168.0.28:5173/visualizations/filename.html`
- 📺 Fire TV status (if display-tv was used)

## File Locations

Created visualizations are stored in:
```
frontend/public/visualizations/
```

## Direct Script Usage

You can also call the script directly for more control:

```bash
# Basic usage
scripts/run.sh scripts/create_visualization.py \
  --filename my-viz \
  --title "My Visualization" \
  --type basic \
  --show-url

# With custom content
scripts/run.sh scripts/create_visualization.py \
  --filename custom \
  --title "Custom Viz" \
  --content '<h1>Hello World</h1>' \
  --show-url

# Display on TV
scripts/run.sh scripts/create_visualization.py \
  --filename demo \
  --title "Demo" \
  --type chart \
  --display-tv
```

## Template Customization

### Editing Created Files

All visualizations are self-contained HTML files. Edit them directly:

```bash
# Open in editor
nano frontend/public/visualizations/my-viz.html

# Changes are immediately visible (no build step)
```

### Creating Custom Templates

1. Create your template HTML file
2. Use `{title}` placeholder for the title
3. Reference it with `--template` option:

```bash
scripts/run.sh scripts/create_visualization.py \
  --filename my-custom \
  --title "My Custom Viz" \
  --template /path/to/template.html
```

## Integration with Fire TV

### Prerequisites

- Fire TV connected via ADB (use `/connect-tv` skill)
- Frontend running on port 5173

### Display Options

**Option 1: During creation**
```
/create-viz filename=demo title="Demo" display-tv
```

**Option 2: Existing visualization**
```bash
# Get URL
scripts/run.sh scripts/get_visualization_url.py demo --network

# Display on TV
adb shell am start -a android.intent.action.VIEW \
  -d "https://192.168.0.28:5173/visualizations/demo.html"
```

**Option 3: Use tv-remote skill**
```
Ask Claude: "Show the demo visualization on Fire TV"
```

## Template Details

### Basic Template Features
- Gradient background
- Centered container
- Responsive design
- Backdrop blur effect
- Clean typography

### Chart Template Features
- Chart.js library (via CDN)
- Pre-configured bar chart
- Sample data included
- Responsive canvas
- Easy to customize

**Customizing Chart Data:**
```javascript
// Edit the data array
data: [12, 19, 3, 5, 2, 3]

// Change chart type
type: 'line' // or 'pie', 'doughnut', 'radar', etc.

// Update labels
labels: ['Week 1', 'Week 2', ...]
```

### Dashboard Template Features
- Responsive grid layout
- Card-based design
- Auto-updating metrics
- Hover effects
- Multiple metric types

**Customizing Metrics:**
```javascript
// Update metric values
document.querySelector('.card-value').textContent = '123';

// Add more cards
<div class="card">
  <div class="card-title">📊 New Metric</div>
  <div class="card-value">456</div>
</div>
```

### TV-Remote Template Features
- Vertical menu layout
- Arrow key navigation
- Visual selection feedback
- Fire TV compatible
- Enter to select

**Customizing Menu:**
```html
<!-- Add more menu items -->
<div class="menu-item">New Option</div>

<!-- Update selection handler -->
document.addEventListener('keydown', (e) => {
  // Your custom logic
});
```

## Advanced Usage

### With Real-time Data

Connect to Assistant API for live updates:

```javascript
// WebSocket connection
const ws = new WebSocket('ws://192.168.0.28:8000/api/ws');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  updateVisualization(data);
};
```

### With External Libraries

Include via CDN:

```html
<!-- D3.js for advanced visualizations -->
<script src="https://d3js.org/d3.v7.min.js"></script>

<!-- Three.js for 3D graphics -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>

<!-- Plotly for interactive plots -->
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
```

### TV-Optimized Design Tips

1. **Resolution**: Design for 1920x1080
2. **Font sizes**: Minimum 24px for readability at 10 feet
3. **Contrast**: High contrast for visibility
4. **Navigation**: Keep it simple (up/down/left/right/select)
5. **Focus states**: Clear visual feedback
6. **Safe zones**: Keep important content away from edges

## Troubleshooting

### Visualization not accessible

```bash
# Check frontend is running
lsof -ti:5173

# Verify file was created
ls -la frontend/public/visualizations/
```

### Fire TV display fails

```bash
# Check ADB connection
adb devices

# Reconnect if needed
adb connect <fire-tv-ip>:5555

# Or use skill
/connect-tv
```

### URL not working

```bash
# Get correct URL
scripts/run.sh scripts/get_visualization_url.py filename --network

# Test in browser first
xdg-open https://localhost:5173/visualizations/filename.html
```

## Related Skills & Tools

- `/tv-remote` - Control Fire TV
- `/connect-tv` - Connect to Fire TV
- `/recall` - Search for visualization examples
- Gallery: https://localhost:5173/visualizations/index.html

## Examples Gallery

Visit the visualization gallery to see all available visualizations:

```
https://localhost:5173/visualizations/index.html
```

## Tips

- Filenames automatically get `.html` extension
- No build step - changes are immediate
- Use `--show-url` flag to get URLs in output
- Network URLs use local IP automatically
- Templates support `{title}` placeholder
- All visualizations are self-contained (inline CSS/JS)
- Fire TV remote sends keyboard events
- Test in desktop browser before displaying on TV

## Support

For more information, see:
- `frontend/public/visualizations/README.md` - Comprehensive visualization docs
- `frontend/public/visualizations/QUICKSTART.md` - Quick reference
- `VISUALIZATION_SYSTEM.md` - System overview
