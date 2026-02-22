---
name: create-viz
description: Create interactive HTML visualizations that can be displayed in browser or on Fire TV
argument-hint: "[filename] [options]"
user-invocable: true
allowed-tools: Bash(scripts/create_visualization.py), Bash(scripts/get_visualization_url.py), Bash(adb)
---

# Create Visualization

Generate interactive HTML visualizations that can be viewed in a browser or displayed on Fire TV.

## What This Skill Does

1. Creates a new HTML file in `frontend/public/visualizations/`
2. Uses built-in templates or custom content
3. Automatically generates local and network URLs
4. Optionally displays the visualization on Fire TV via ADB

## Usage

The skill accepts arguments in a flexible format. Parse the user's request and extract:

- **filename**: The name of the HTML file (e.g., "my-chart", "dashboard")
- **title**: Display title for the visualization
- **type**: Template type (basic, chart, dashboard, tv-remote) - optional
- **content**: Custom HTML content - optional
- **template**: Path to custom template file - optional
- **display-tv**: Whether to show on Fire TV immediately - optional

## Built-in Templates

- **basic**: Simple centered content with gradient background
- **chart**: Chart.js integration for data visualization
- **dashboard**: Multi-card grid layout for metrics
- **tv-remote**: Fire TV remote-controlled menu interface

## Command Format

The script is called via:

```
scripts/run.sh scripts/create_visualization.py --filename NAME --title "TITLE" [OPTIONS]
```

### Options

- `--type TYPE` - Use built-in template (basic, chart, dashboard, tv-remote)
- `--content HTML` - Provide raw HTML content
- `--template PATH` - Use custom template file
- `--show-url` - Display the network URL after creation
- `--display-tv` - Open on Fire TV immediately (requires ADB connection)
- `--port PORT` - Specify frontend port (default: 5173)

## Examples

### Example 1: Create with built-in template

User says: "Create a chart visualization called sales-report"

Run:
```
scripts/run.sh scripts/create_visualization.py \
  --filename sales-report \
  --title "Sales Report" \
  --type chart \
  --show-url
```

### Example 2: Create and display on TV

User says: "Create a dashboard called system-monitor and show it on the TV"

Run:
```
scripts/run.sh scripts/create_visualization.py \
  --filename system-monitor \
  --title "System Monitor" \
  --type dashboard \
  --display-tv
```

### Example 3: Create with custom content

User says: "Create a visualization called welcome with custom HTML"

If user provides HTML content in their message, extract it and run:
```
scripts/run.sh scripts/create_visualization.py \
  --filename welcome \
  --title "Welcome" \
  --content '<h1>Welcome!</h1>' \
  --show-url
```

### Example 4: Simple basic visualization

User says: "Create a basic visualization called test-viz"

Run:
```
scripts/run.sh scripts/create_visualization.py \
  --filename test-viz \
  --title "Test Visualization" \
  --type basic \
  --show-url
```

## Workflow

1. **Parse the user's request** to extract filename, title, and options
2. **Run the creation script** with appropriate arguments
3. **Report the results** including:
   - Success message with file path
   - Local URL: `https://localhost:5173/visualizations/filename.html`
   - Network URL: `https://<local-ip>:5173/visualizations/filename.html`
   - If display-tv was used, confirm whether it opened successfully
4. **Provide next steps**:
   - How to view it in browser
   - How to edit the file if needed
   - How to display on TV if not already done

## Getting URLs Later

If the user wants the URL for an existing visualization, use:

```
scripts/run.sh scripts/get_visualization_url.py FILENAME --network
```

## Fire TV Display

To display an existing visualization on Fire TV:

```
adb shell am start -a android.intent.action.VIEW \
  -d "https://<local-ip>:5173/visualizations/filename.html"
```

Or use the `/tv-remote` skill if available.

## Error Handling

- If ADB is not available, inform the user they can install it with `sudo apt install adb`
- If Fire TV is not connected, suggest connecting with `adb connect <fire-tv-ip>:5555` or using `/connect-tv`
- If frontend is not running, inform the user to start it
- If the file already exists, it will be overwritten

## Available Templates Detail

### Basic Template
- Simple centered content
- Gradient background
- Clean typography
- Good starting point for custom visualizations

### Chart Template
- Includes Chart.js library via CDN
- Pre-configured bar chart with sample data
- Easy to customize data and chart type
- Responsive canvas container

### Dashboard Template
- 4-column grid layout (responsive)
- Card-based design
- Auto-updating sample metrics
- Perfect for real-time data displays

### TV-Remote Template
- Vertical menu navigation
- Arrow key controls (Fire TV compatible)
- Visual selection feedback
- Optimized for 10-foot UI

## Tips

- Filenames are automatically given `.html` extension if not provided
- The `{title}` placeholder in templates is automatically replaced
- All visualizations are immediately accessible (no build step)
- Use `--show-url` to get the URL in the output
- Network URLs use the machine's local IP automatically

## Related Skills

- `/tv-remote` - Control Fire TV (launch apps, navigate)
- `/connect-tv` - Connect to Fire TV via ADB
- Gallery available at: `https://localhost:5173/visualizations/index.html`
