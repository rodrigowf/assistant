# /create-viz - Quick Reference

## One-Line Summary
Create interactive HTML visualizations for browser or Fire TV display.

## Simplest Usage
```
/create-viz filename=NAME title="TITLE"
```

## Common Patterns

### Basic Visualization
```
/create-viz filename=hello title="Hello World"
```
→ Creates simple centered content with gradient background

### Chart Visualization
```
/create-viz filename=sales title="Sales Report" type=chart
```
→ Creates Chart.js bar chart with sample data

### Dashboard
```
/create-viz filename=metrics title="System Metrics" type=dashboard
```
→ Creates 4-card grid layout dashboard

### TV Display
```
/create-viz filename=welcome title="Welcome Screen" display-tv
```
→ Creates and immediately displays on Fire TV

### TV Navigation Menu
```
/create-viz filename=menu title="Main Menu" type=tv-remote display-tv
```
→ Creates remote-controlled menu and displays on TV

## Parameters

| Parameter | Required | Description | Example |
|-----------|----------|-------------|---------|
| filename | ✅ Yes | Output filename | `hello` or `hello.html` |
| title | ✅ Yes | Display title | `"Hello World"` |
| type | ⬜ No | Template type | `basic`, `chart`, `dashboard`, `tv-remote` |
| display-tv | ⬜ No | Show on TV | Add flag to display |

## Templates

| Type | Use For | Libraries |
|------|---------|-----------|
| `basic` | Starting point, custom content | None |
| `chart` | Data visualization, graphs | Chart.js |
| `dashboard` | Real-time metrics, monitoring | None |
| `tv-remote` | TV navigation, 10-foot UI | None |

## Natural Language

Just describe what you want:
- "Create a chart visualization called sales-2024"
- "Make a dashboard for system monitoring"
- "Create a TV menu and show it on the Fire TV"

Claude will parse and execute the appropriate command.

## Output

After creation:
- ✅ File path confirmation
- 🌐 Local URL: `https://localhost:5173/visualizations/NAME.html`
- 🌐 Network URL: `https://192.168.0.28:5173/visualizations/NAME.html`
- 📺 TV status (if display-tv used)

## File Location
```
frontend/public/visualizations/NAME.html
```

## Edit Visualization
```bash
nano frontend/public/visualizations/NAME.html
```
Changes are immediate (no build step).

## Display Existing Viz on TV
```bash
# Get URL
scripts/run.sh scripts/get_visualization_url.py NAME --network

# Display
adb shell am start -a android.intent.action.VIEW -d "URL"
```

## Direct Script Call
```bash
scripts/run.sh scripts/create_visualization.py \
  --filename NAME \
  --title "TITLE" \
  --type TYPE \
  --show-url \
  --display-tv
```

## View All Visualizations
```
https://localhost:5173/visualizations/index.html
```

## Related Skills
- `/tv-remote` - Control Fire TV
- `/connect-tv` - Connect to Fire TV
- `/recall` - Find visualization examples

## Tips
- ✓ No `.html` extension needed (auto-added)
- ✓ No build step - edit and refresh
- ✓ Test in browser before TV
- ✓ Use network URL for TV display
- ✓ Fire TV remote = keyboard arrow keys

## Examples

**Minimal:**
```
/create-viz filename=test title="Test"
```

**With type:**
```
/create-viz filename=chart-demo title="Demo Chart" type=chart
```

**TV display:**
```
/create-viz filename=tv-menu title="TV Menu" type=tv-remote display-tv
```

**Natural:**
```
Create a dashboard visualization called system-stats
```

---

**Documentation:**
- Full guide: `skills/create-viz/README.md`
- Examples: `skills/create-viz/EXAMPLES.md`
- System docs: `VISUALIZATION_SYSTEM.md`
