#!/usr/bin/env python3
"""
Get the URL for a visualization file.

Usage:
    python scripts/get_visualization_url.py example.html
    python scripts/get_visualization_url.py example.html --network
"""

import argparse
import socket
import sys
from pathlib import Path


def get_local_ip():
    """Get the local IP address of the machine."""
    try:
        # Create a socket to determine the local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Connect to an external host (doesn't actually send data)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description="Get URL for visualization files")
    parser.add_argument("filename", help="Name of the visualization file")
    parser.add_argument(
        "--network",
        action="store_true",
        help="Return network URL instead of localhost",
    )
    parser.add_argument("--port", type=int, default=5173, help="Frontend port (default: 5173)")

    args = parser.parse_args()

    # Remove .html extension if provided, we'll add it back
    filename = args.filename
    if not filename.endswith(".html"):
        filename = f"{filename}.html"

    # Check if file exists
    vis_dir = Path(__file__).parent.parent / "frontend" / "public" / "visualizations"
    file_path = vis_dir / filename

    if not file_path.exists():
        print(f"❌ File not found: {file_path}", file=sys.stderr)
        print(f"Available files:", file=sys.stderr)
        if vis_dir.exists():
            for f in vis_dir.glob("*.html"):
                print(f"  - {f.name}", file=sys.stderr)
        sys.exit(1)

    # Build URL
    if args.network:
        host = get_local_ip()
    else:
        host = "localhost"

    url = f"http://{host}:{args.port}/visualizations/{filename}"
    print(url)


if __name__ == "__main__":
    main()
