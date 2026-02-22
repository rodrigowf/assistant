#!/bin/bash
# Test script to display visualizations on Fire TV via ADB

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get local IP
LOCAL_IP=$(hostname -I | awk '{print $1}')
PORT=5173

echo -e "${BLUE}=== Fire TV Visualization Test ===${NC}"
echo ""

# Check if ADB is available
if ! command -v adb &> /dev/null; then
    echo -e "${YELLOW}Warning: ADB not found. Install with: sudo apt install adb${NC}"
    exit 1
fi

# Check if device is connected
if ! adb devices | grep -q "device$"; then
    echo -e "${YELLOW}Warning: No Fire TV device connected${NC}"
    echo "Connect with: adb connect <fire-tv-ip>:5555"
    exit 1
fi

echo -e "${GREEN}✓ ADB device connected${NC}"
echo ""

# Function to display visualization
display_viz() {
    local viz_name=$1
    local url="http://${LOCAL_IP}:${PORT}/visualizations/${viz_name}.html"

    echo -e "${BLUE}Displaying: ${viz_name}${NC}"
    echo "URL: $url"

    adb shell am start -a android.intent.action.VIEW -d "$url"

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓ Successfully opened on Fire TV${NC}"
    else
        echo -e "${YELLOW}Failed to open visualization${NC}"
    fi

    echo ""
}

# Show menu
echo "Available visualizations:"
echo "  1) Gallery Index"
echo "  2) Example (animated rectangle)"
echo "  3) System Stats Dashboard"
echo "  4) Fire TV Remote Demo"
echo "  5) All (cycle through all)"
echo ""

# If argument provided, use it, otherwise ask
if [ -n "$1" ]; then
    choice="$1"
else
    read -p "Select visualization (1-5): " choice
fi

case $choice in
    1)
        display_viz "index"
        ;;
    2)
        display_viz "example"
        ;;
    3)
        display_viz "system-stats"
        ;;
    4)
        display_viz "remote-demo"
        ;;
    5)
        echo "Cycling through all visualizations (5 seconds each)..."
        echo ""
        display_viz "index"
        sleep 5
        display_viz "example"
        sleep 5
        display_viz "system-stats"
        sleep 5
        display_viz "remote-demo"
        ;;
    *)
        echo "Invalid choice. Use 1-5."
        exit 1
        ;;
esac

echo -e "${GREEN}Test complete!${NC}"
