---
name: connect-tv
description: Discover and connect to the Fire TV via ADB
---

# Connect TV: Automatic Fire TV Discovery and Connection

Finds the Fire TV by MAC address and establishes ADB connection, even if the IP address has changed.

## Steps

1. Run the Fire TV discovery script:
   ```
   /home/rodrigo/AndroidStudioProjects/TvServerHub/scripts/discover-firetv.sh
   ```

2. If connection succeeds, inform the user of the current IP address.

3. If connection fails, advise the user to:
   - Ensure the Fire TV is powered on
   - Check that they're on the same network
   - Accept the pairing dialog on the TV screen if prompted

## Technical Details

The script uses the Fire TV's MAC address (90:39:5f:bd:1f:13) to locate it on the network:
- First checks the ARP cache (fastest)
- If not found, performs a network-wide ping sweep to populate the ARP cache
- Once found, connects via ADB on port 5555
- Handles already-connected state gracefully
