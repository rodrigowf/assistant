# copyparty_ios.py — iOS Photo Server Launcher
# Run this script in Pythonista 3 on your iPhone/iPad.
# It serves your photo library over the local network via a REST API.
#
# Usage:
#   Just run this script in Pythonista. It will:
#   1. Request photo library permission (first run only)
#   2. Start an HTTP server on port 3691
#   3. Display a QR code and URL for easy access
#   4. Serve until you stop the script
#
# Agent access:
#   GET http://<phone-ip>:3691/api/status
#   GET http://<phone-ip>:3691/api/assets?type=photo&limit=10
#   GET http://<phone-ip>:3691/media/<id>/full
#   GET http://<phone-ip>:3691/media/<id>/thumb

import sys
import os
import time
import signal
import argparse

# Add the script's directory to the path so imports work
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from ios_photo_bridge import create_bridge
from photo_server import start_server, get_local_ip


def print_qr_console(url):
    """Print a QR code to the console using ASCII blocks.
    Falls back to just printing the URL if qrcode generation fails."""
    try:
        # Try using the segno library (lightweight, sometimes available)
        import segno
        qr = segno.make(url)
        # Print as ASCII
        matrix = qr.matrix
        print()
        for row in matrix:
            line = ''
            for cell in row:
                line += '\u2588\u2588' if cell else '  '
            print('  ' + line)
        print()
        return
    except ImportError:
        pass

    try:
        # Try qrcode library
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        print()
        return
    except ImportError:
        pass

    # Fallback: just print URL prominently
    print()
    print('  +' + '-' * (len(url) + 2) + '+')
    print('  | {} |'.format(url))
    print('  +' + '-' * (len(url) + 2) + '+')
    print()
    print('  (Install "qrcode" or "segno" for QR code display)')
    print()


def main():
    parser = argparse.ArgumentParser(description='iOS Photo Server')
    parser.add_argument('--port', type=int, default=3691,
                        help='Port to listen on (default: 3691)')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Bind address (default: 0.0.0.0)')
    parser.add_argument('--stub', action='store_true',
                        help='Use stub data (for testing on non-iOS)')
    args = parser.parse_args()

    print()
    print('iOS Photo Server')
    print('================')
    print()

    # Create the photo bridge
    bridge = create_bridge(stub=args.stub)

    # Check permissions
    print('[*] Checking photo library access...')
    if not bridge.check_permission():
        print()
        print('[!] Photo library access denied or not available.')
        print('    Go to Settings > Privacy > Photos > Pythonista')
        print('    and grant access, then run this script again.')
        print()
        return

    # Get library stats
    status = bridge.get_status()
    print('[*] Library: {} photos, {} videos ({} total)'.format(
        status['photo_count'], status['video_count'], status['total_count']))

    # Start server
    server = start_server(bridge, host=args.host, port=args.port)
    local_ip = get_local_ip()
    url = 'http://{}:{}'.format(local_ip, args.port)

    print_qr_console(url)
    print('[*] Press Ctrl+C or stop the script to shut down.')
    print()

    # Handle graceful shutdown
    def shutdown_handler(signum, frame):
        print()
        print('[*] Shutting down...')
        server.shutdown()
        bridge.cleanup()
        print('[*] Done.')
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, shutdown_handler)
        signal.signal(signal.SIGTERM, shutdown_handler)
    except (OSError, ValueError):
        # Pythonista may not support signal handlers in all contexts
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        print('[*] Shutting down...')
    finally:
        server.shutdown()
        bridge.cleanup()
        print('[*] Server stopped.')


if __name__ == '__main__':
    main()
