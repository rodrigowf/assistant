#!/usr/bin/env python3
"""
Create a new visualization HTML file.

Usage:
    scripts/create_visualization.py --filename NAME --title TITLE [--template PATH] [--content HTML]
    scripts/create_visualization.py --filename NAME --title TITLE --type TYPE

Options:
    --filename NAME     Output filename (e.g., 'my-viz.html' or 'my-viz')
    --title TITLE       Visualization title
    --template PATH     Path to template HTML file
    --content HTML      Raw HTML content
    --type TYPE         Built-in template type: basic, chart, dashboard, tv-remote
    --show-url          Print the network URL after creation
    --display-tv        Open on Fire TV after creation (requires ADB)

Examples:
    # Create from built-in template
    scripts/create_visualization.py --filename my-viz --title "My Chart" --type chart

    # Create with custom content
    scripts/create_visualization.py --filename custom --title "Custom Viz" --content "<h1>Hello</h1>"

    # Create and display on TV
    scripts/create_visualization.py --filename demo --title "Demo" --type basic --display-tv
"""

import argparse
import socket
import subprocess
import sys
from pathlib import Path

# Get project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
VIZ_DIR = PROJECT_ROOT / "frontend" / "public" / "visualizations"

# Built-in templates
TEMPLATES = {
    "basic": """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            color: white;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 40px;
        }}
        .container {{
            text-align: center;
            background: rgba(255, 255, 255, 0.1);
            padding: 60px;
            border-radius: 20px;
            backdrop-filter: blur(10px);
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
        }}
        h1 {{
            font-size: 3rem;
            margin-bottom: 20px;
            text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.5);
        }}
        .content {{
            font-size: 1.5rem;
            opacity: 0.9;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{title}</h1>
        <div class="content">
            <p>Your visualization content goes here</p>
        </div>
    </div>
    <script>
        console.log('Visualization loaded:', new Date().toLocaleString());
    </script>
</body>
</html>""",

    "chart": """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            font-family: Arial, sans-serif;
            color: white;
            padding: 40px;
            min-height: 100vh;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        h1 {{
            text-align: center;
            font-size: 3rem;
            margin-bottom: 40px;
            text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.5);
        }}
        .chart-container {{
            position: relative;
            background: rgba(255, 255, 255, 0.1);
            padding: 30px;
            border-radius: 15px;
            backdrop-filter: blur(10px);
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{title}</h1>
        <div class="chart-container">
            <canvas id="myChart"></canvas>
        </div>
    </div>
    <script>
        const ctx = document.getElementById('myChart');
        new Chart(ctx, {{
            type: 'bar',
            data: {{
                labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'],
                datasets: [{{
                    label: 'Sample Data',
                    data: [12, 19, 3, 5, 2, 3],
                    backgroundColor: 'rgba(102, 126, 234, 0.8)',
                    borderColor: 'rgba(102, 126, 234, 1)',
                    borderWidth: 2
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: true,
                scales: {{
                    y: {{ beginAtZero: true }}
                }}
            }}
        }});
    </script>
</body>
</html>""",

    "dashboard": """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: linear-gradient(135deg, #232526 0%, #414345 100%);
            font-family: Arial, sans-serif;
            color: white;
            padding: 40px;
            min-height: 100vh;
        }}
        h1 {{
            text-align: center;
            font-size: 3rem;
            margin-bottom: 40px;
            text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.5);
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 30px;
            max-width: 1400px;
            margin: 0 auto;
        }}
        .card {{
            background: rgba(255, 255, 255, 0.1);
            border-radius: 15px;
            padding: 30px;
            backdrop-filter: blur(10px);
            border: 2px solid rgba(255, 255, 255, 0.2);
            transition: transform 0.3s ease;
        }}
        .card:hover {{ transform: translateY(-5px); }}
        .card-title {{
            font-size: 1.5rem;
            margin-bottom: 15px;
            border-bottom: 2px solid rgba(255, 255, 255, 0.3);
            padding-bottom: 10px;
        }}
        .card-value {{
            font-size: 2.5rem;
            font-weight: bold;
            text-align: center;
            margin: 20px 0;
        }}
    </style>
</head>
<body>
    <h1>{title}</h1>
    <div class="grid">
        <div class="card">
            <div class="card-title">📊 Metric 1</div>
            <div class="card-value">42</div>
        </div>
        <div class="card">
            <div class="card-title">📈 Metric 2</div>
            <div class="card-value">87%</div>
        </div>
        <div class="card">
            <div class="card-title">⚡ Metric 3</div>
            <div class="card-value">1.2K</div>
        </div>
        <div class="card">
            <div class="card-title">🎯 Metric 4</div>
            <div class="card-value">95%</div>
        </div>
    </div>
    <script>
        // Update metrics every 3 seconds
        setInterval(() => {{
            document.querySelectorAll('.card-value').forEach(el => {{
                if (el.textContent.includes('%')) {{
                    el.textContent = Math.floor(Math.random() * 100) + '%';
                }}
            }});
        }}, 3000);
    </script>
</body>
</html>""",

    "tv-remote": """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: linear-gradient(135deg, #0f2027 0%, #203a43 50%, #2c5364 100%);
            font-family: Arial, sans-serif;
            color: white;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 40px;
        }}
        h1 {{
            font-size: 3rem;
            margin-bottom: 40px;
            text-align: center;
        }}
        .menu {{
            display: flex;
            flex-direction: column;
            gap: 20px;
            width: 600px;
        }}
        .menu-item {{
            background: rgba(255, 255, 255, 0.1);
            border: 3px solid rgba(255, 255, 255, 0.3);
            border-radius: 15px;
            padding: 30px;
            font-size: 1.5rem;
            transition: all 0.3s ease;
        }}
        .menu-item.selected {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-color: white;
            transform: scale(1.05);
            box-shadow: 0 10px 30px rgba(102, 126, 234, 0.6);
        }}
        .key-hint {{
            position: fixed;
            bottom: 20px;
            text-align: center;
            opacity: 0.7;
            font-size: 1.1rem;
        }}
    </style>
</head>
<body>
    <h1>{title}</h1>
    <div class="menu" id="menu">
        <div class="menu-item selected">Option 1</div>
        <div class="menu-item">Option 2</div>
        <div class="menu-item">Option 3</div>
        <div class="menu-item">Option 4</div>
    </div>
    <div class="key-hint">
        Use ⬆️ ⬇️ arrow keys to navigate | Press Enter to select
    </div>
    <script>
        let selected = 0;
        const items = document.querySelectorAll('.menu-item');

        function updateSelection() {{
            items.forEach((item, i) => {{
                item.classList.toggle('selected', i === selected);
            }});
        }}

        document.addEventListener('keydown', (e) => {{
            switch(e.key) {{
                case 'ArrowUp':
                    e.preventDefault();
                    selected = Math.max(0, selected - 1);
                    updateSelection();
                    break;
                case 'ArrowDown':
                    e.preventDefault();
                    selected = Math.min(items.length - 1, selected + 1);
                    updateSelection();
                    break;
                case 'Enter':
                    alert(`Selected: ${{items[selected].textContent}}`);
                    break;
            }}
        }});
    </script>
</body>
</html>"""
}


def get_local_ip():
    """Get the local IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        return "127.0.0.1"


def ensure_html_extension(filename):
    """Ensure filename has .html extension."""
    if not filename.endswith('.html'):
        return f"{filename}.html"
    return filename


def create_visualization(filename, title, content):
    """Create the visualization file."""
    # Ensure directory exists
    VIZ_DIR.mkdir(parents=True, exist_ok=True)

    # Write file
    output_path = VIZ_DIR / filename
    output_path.write_text(content)

    return output_path


def get_network_url(filename, port=5173):
    """Generate the network URL for the visualization."""
    local_ip = get_local_ip()
    return f"https://{local_ip}:{port}/visualizations/{filename}"


def display_on_tv(url):
    """Open visualization on Fire TV via ADB."""
    try:
        result = subprocess.run(
            ["adb", "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            return True, "Successfully opened on Fire TV"
        else:
            return False, f"ADB command failed: {result.stderr}"

    except FileNotFoundError:
        return False, "ADB not found. Install with: sudo apt install adb"
    except subprocess.TimeoutExpired:
        return False, "ADB command timed out"
    except Exception as e:
        return False, f"Error: {e}"


def main():
    parser = argparse.ArgumentParser(
        description="Create a new visualization HTML file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument("--filename", required=True, help="Output filename")
    parser.add_argument("--title", required=True, help="Visualization title")
    parser.add_argument("--template", help="Path to template HTML file")
    parser.add_argument("--content", help="Raw HTML content")
    parser.add_argument("--type", choices=list(TEMPLATES.keys()), help="Built-in template type")
    parser.add_argument("--show-url", action="store_true", help="Print network URL")
    parser.add_argument("--display-tv", action="store_true", help="Display on Fire TV")
    parser.add_argument("--port", type=int, default=5173, help="Frontend port (default: 5173)")

    args = parser.parse_args()

    # Ensure filename has .html extension
    filename = ensure_html_extension(args.filename)

    # Determine content source
    if args.content:
        # Use provided content directly
        content = args.content
    elif args.template:
        # Load from template file
        template_path = Path(args.template)
        if not template_path.exists():
            print(f"❌ Template file not found: {template_path}", file=sys.stderr)
            sys.exit(1)
        content = template_path.read_text()
        # Replace {title} placeholder if present
        content = content.replace("{title}", args.title)
    elif args.type:
        # Use built-in template
        content = TEMPLATES[args.type].format(title=args.title)
    else:
        # Default to basic template
        content = TEMPLATES["basic"].format(title=args.title)

    # Create the visualization
    try:
        output_path = create_visualization(filename, args.title, content)
        print(f"✅ Created: {output_path}")
    except Exception as e:
        print(f"❌ Failed to create visualization: {e}", file=sys.stderr)
        sys.exit(1)

    # Generate network URL
    network_url = get_network_url(filename, args.port)

    if args.show_url or args.display_tv:
        print(f"🌐 Local URL: https://localhost:{args.port}/visualizations/{filename}")
        print(f"🌐 Network URL: {network_url}")

    # Display on TV if requested
    if args.display_tv:
        print("\n📺 Opening on Fire TV...")
        success, message = display_on_tv(network_url)
        if success:
            print(f"✅ {message}")
        else:
            print(f"⚠️  {message}", file=sys.stderr)

    # Output URL for scripting
    if args.show_url:
        print(f"\nURL: {network_url}")


if __name__ == "__main__":
    main()
