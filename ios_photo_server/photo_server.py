# photo_server.py — API-first HTTP photo server
# Serves the iOS photo library over the local network with a JSON REST API.
# Primary consumer: AI agents. Secondary: human browser.
#
# Depends on ios_photo_bridge.py for photo library access.

import json
import os
import re
import socket
import time
import mimetypes
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

# Will be set by the launcher
bridge = None


class PhotoRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler with REST API + HTML browse UI."""

    server_version = 'iOSPhotoServer/1.0'

    # Suppress default stderr logging per-request
    def log_message(self, format, *args):
        print('[HTTP] {} - {}'.format(self.client_address[0], format % args))

    # ------------------------------------------------------------------
    # Request routing
    # ------------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path).rstrip('/')
        qs = parse_qs(parsed.query)
        # Flatten single-value params
        params = {k: v[0] if len(v) == 1 else v for k, v in qs.items()}

        try:
            # API routes
            if path == '/api/status':
                return self._api_status()
            elif path == '/api/assets':
                return self._api_assets(params)
            elif path == '/api/albums':
                return self._api_albums()
            elif path == '/api/search':
                return self._api_search(params)
            elif path == '/api/refresh':
                return self._api_refresh()
            elif re.match(r'^/api/assets/(.+)$', path):
                asset_id = re.match(r'^/api/assets/(.+)$', path).group(1)
                return self._api_asset_detail(asset_id)

            # Media routes
            elif re.match(r'^/media/([^/]+)/(full|thumb|preview)$', path):
                m = re.match(r'^/media/([^/]+)/(full|thumb|preview)$', path)
                return self._serve_media(m.group(1), m.group(2))

            # HTML browser routes
            elif path in ('', '/'):
                return self._html_index(params)
            elif path == '/browse':
                return self._html_browse(params)

            # 404
            else:
                return self._send_error(404, 'Not Found')

        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send_error(500, str(e))

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self._add_cors_headers()
        self.send_header('Content-Length', '0')
        self.end_headers()

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------

    def _add_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    # ------------------------------------------------------------------
    # JSON helpers
    # ------------------------------------------------------------------

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2, default=str).encode('utf-8')
        self.send_response(status)
        self._add_cors_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        self._send_json({'error': message, 'status': status}, status=status)

    def _send_html(self, html, status=200):
        body = html.encode('utf-8')
        self.send_response(status)
        self._add_cors_headers()
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # API endpoints
    # ------------------------------------------------------------------

    def _api_status(self):
        status = bridge.get_status()
        albums = bridge.list_albums()
        status['albums'] = len(albums)
        status['status'] = 'ok'
        status['server_version'] = self.server_version
        status['server_uptime'] = int(time.time() - self.server.start_time)
        self._send_json(status)

    def _api_assets(self, params):
        kwargs = {
            'media_type': params.get('type'),
            'album': params.get('album'),
            'after': params.get('after'),
            'before': params.get('before'),
            'sort': params.get('sort', 'date_desc'),
            'offset': int(params.get('offset', 0)),
            'limit': int(params.get('limit', 100)),
            'search': params.get('q') or params.get('search'),
        }
        fav = params.get('favorite')
        if fav is not None:
            kwargs['favorite'] = fav.lower() in ('true', '1', 'yes')
        result = bridge.list_assets(**kwargs)
        self._send_json(result)

    def _api_asset_detail(self, asset_id):
        info = bridge.get_asset_info(asset_id)
        if info is None:
            return self._send_error(404, 'Asset not found: {}'.format(asset_id))
        self._send_json(info)

    def _api_albums(self):
        albums = bridge.list_albums()
        self._send_json({'albums': albums})

    def _api_search(self, params):
        query = params.get('q', '')
        if not query:
            return self._send_error(400, 'Missing search query parameter "q"')
        result = bridge.list_assets(
            search=query,
            media_type=params.get('type'),
            sort=params.get('sort', 'date_desc'),
            offset=int(params.get('offset', 0)),
            limit=int(params.get('limit', 100)),
        )
        self._send_json(result)

    def _api_refresh(self):
        """Force re-enumeration of the photo library."""
        bridge.invalidate_cache()
        status = bridge.get_status()
        status['refreshed'] = True
        self._send_json(status)

    # ------------------------------------------------------------------
    # Media serving
    # ------------------------------------------------------------------

    def _serve_media(self, asset_id, quality):
        info = bridge.get_asset_info(asset_id)
        if info is None:
            return self._send_error(404, 'Asset not found: {}'.format(asset_id))

        media_type = info['media_type']

        # Thumbnails work for both photos and videos
        if quality in ('thumb', 'preview'):
            if media_type == 'image':
                result = bridge.get_photo_data(asset_id, quality=quality)
            else:
                max_size = 300 if quality == 'thumb' else 1200
                result = bridge.get_video_thumbnail(asset_id, max_size=max_size)
            if result is None:
                return self._send_error(404, 'Could not generate {} for {}'.format(quality, asset_id))
            data, filename, content_type = result
            self.send_response(200)
            self._add_cors_headers()
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.end_headers()
            self.wfile.write(data)
            return

        # Full quality
        if media_type == 'image':
            result = bridge.get_photo_data(asset_id, quality='full')
            if result is None:
                return self._send_error(404, 'Could not read photo: {}'.format(asset_id))
            data, filename, content_type = result
            self.send_response(200)
            self._add_cors_headers()
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Disposition',
                             'inline; filename="{}"'.format(filename))
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.end_headers()
            self.wfile.write(data)
        elif media_type == 'video':
            self._serve_video(asset_id)
        else:
            self._send_error(400, 'Unknown media type: {}'.format(media_type))

    def _serve_video(self, asset_id):
        """Serve video file with Range request support."""
        result = bridge.get_video_data(asset_id)
        if result is None:
            return self._send_error(404, 'Could not export video: {}'.format(asset_id))

        file_path, filename, content_type, file_size = result

        # Parse Range header
        range_header = self.headers.get('Range')
        if range_header:
            try:
                range_match = re.match(r'bytes=(\d+)-(\d*)', range_header)
                if range_match:
                    start = int(range_match.group(1))
                    end = int(range_match.group(2)) if range_match.group(2) else file_size - 1
                    end = min(end, file_size - 1)
                    length = end - start + 1

                    self.send_response(206)
                    self._add_cors_headers()
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(length))
                    self.send_header('Content-Range',
                                     'bytes {}-{}/{}'.format(start, end, file_size))
                    self.send_header('Accept-Ranges', 'bytes')
                    self.send_header('Content-Disposition',
                                     'inline; filename="{}"'.format(filename))
                    self.end_headers()

                    with open(file_path, 'rb') as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk_size = min(65536, remaining)
                            chunk = f.read(chunk_size)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    return
            except (ValueError, AttributeError):
                pass  # Fall through to full response

        # Full response
        self.send_response(200)
        self._add_cors_headers()
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(file_size))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Disposition',
                         'inline; filename="{}"'.format(filename))
        self.end_headers()

        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # ------------------------------------------------------------------
    # HTML browser UI (secondary interface)
    # ------------------------------------------------------------------

    def _html_index(self, params):
        """Landing page with library summary and links."""
        status = bridge.get_status()
        albums = bridge.list_albums()
        host = self.headers.get('Host', 'localhost')

        album_rows = ''.join(
            '<tr><td><a href="/browse?album={name}">{name}</a></td>'
            '<td>{count}</td><td>{type}</td></tr>'.format(**a)
            for a in albums
        )

        html = HTML_TEMPLATE.format(
            title='iOS Photo Server',
            body='''
            <h1>iOS Photo Server</h1>
            <div class="stats">
                <div class="stat"><span class="num">{photos}</span><br>Photos</div>
                <div class="stat"><span class="num">{videos}</span><br>Videos</div>
                <div class="stat"><span class="num">{total}</span><br>Total</div>
            </div>
            <h2>Browse</h2>
            <ul>
                <li><a href="/browse?type=photo">All Photos</a></li>
                <li><a href="/browse?type=video">All Videos</a></li>
                <li><a href="/browse">All Media</a></li>
            </ul>
            <h2>Albums</h2>
            <table>
                <tr><th>Name</th><th>Count</th><th>Type</th></tr>
                {album_rows}
            </table>
            <h2>API</h2>
            <ul class="api-links">
                <li><code><a href="/api/status">GET /api/status</a></code></li>
                <li><code><a href="/api/assets">GET /api/assets</a></code></li>
                <li><code><a href="/api/albums">GET /api/albums</a></code></li>
                <li><code><a href="/api/assets?type=photo&amp;limit=10">GET /api/assets?type=photo&amp;limit=10</a></code></li>
            </ul>
            '''.format(
                photos=status['photo_count'],
                videos=status['video_count'],
                total=status['total_count'],
                album_rows=album_rows,
            )
        )
        self._send_html(html)

    def _html_browse(self, params):
        """Grid view of assets with thumbnails."""
        result = bridge.list_assets(
            media_type=params.get('type'),
            album=params.get('album'),
            after=params.get('after'),
            before=params.get('before'),
            sort=params.get('sort', 'date_desc'),
            offset=int(params.get('offset', 0)),
            limit=int(params.get('limit', 50)),
        )

        title_parts = []
        if params.get('type'):
            title_parts.append(params['type'].title() + 's')
        if params.get('album'):
            title_parts.append(params['album'])
        title = ' - '.join(title_parts) if title_parts else 'All Media'

        items = ''
        for a in result['assets']:
            badge = ''
            if a['media_type'] == 'video':
                dur = a.get('duration', 0) or 0
                mins, secs = divmod(int(dur), 60)
                badge = '<span class="badge">&#9654; {}:{:02d}</span>'.format(mins, secs)
            elif a.get('favorite'):
                badge = '<span class="badge fav">&#9733;</span>'

            items += '''
            <div class="item">
                <a href="{full_url}" target="_blank">
                    <img src="{thumb_url}" loading="lazy" alt="{filename}">
                    {badge}
                </a>
                <div class="caption">{filename}<br>
                    <small>{date}</small>
                </div>
            </div>
            '''.format(
                full_url=a['urls']['full'],
                thumb_url=a['urls']['thumb'],
                filename=a['filename'],
                badge=badge,
                date=(a.get('creation_date') or '')[:10],
            )

        # Pagination
        offset = result['offset']
        limit = result['limit']
        total = result['total']
        nav = ''
        base_qs = '&'.join('{}={}'.format(k, v) for k, v in params.items()
                           if k not in ('offset',))
        if offset > 0:
            prev_off = max(0, offset - limit)
            nav += '<a href="/browse?{}&offset={}">&#8592; Previous</a> '.format(base_qs, prev_off)
        nav += '<span>Showing {}-{} of {}</span>'.format(
            offset + 1, min(offset + limit, total), total)
        if offset + limit < total:
            nav += ' <a href="/browse?{}&offset={}">Next &#8594;</a>'.format(base_qs, offset + limit)

        html = HTML_TEMPLATE.format(
            title=title,
            body='''
            <h1>{title}</h1>
            <div class="nav">{nav}</div>
            <div class="grid">{items}</div>
            <div class="nav">{nav}</div>
            '''.format(title=title, nav=nav, items=items)
        )
        self._send_html(html)


# ------------------------------------------------------------------
# HTML template
# ------------------------------------------------------------------

HTML_TEMPLATE = '''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 16px;
       background: #1a1a2e; color: #e0e0e0; }}
a {{ color: #64b5f6; }}
h1 {{ color: #fff; margin-top: 0; }}
h2 {{ color: #90caf9; border-bottom: 1px solid #333; padding-bottom: 4px; }}
.stats {{ display: flex; gap: 24px; margin: 16px 0; }}
.stat {{ background: #16213e; padding: 16px 24px; border-radius: 8px; text-align: center; }}
.stat .num {{ font-size: 28px; font-weight: bold; color: #64b5f6; }}
table {{ border-collapse: collapse; width: 100%; max-width: 600px; }}
th, td {{ text-align: left; padding: 6px 12px; border-bottom: 1px solid #333; }}
th {{ color: #90caf9; }}
.api-links li {{ margin: 4px 0; }}
.api-links code {{ background: #16213e; padding: 2px 6px; border-radius: 3px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 8px; }}
.item {{ position: relative; background: #16213e; border-radius: 6px; overflow: hidden; }}
.item img {{ width: 100%; aspect-ratio: 1; object-fit: cover; display: block; }}
.item .badge {{ position: absolute; top: 6px; right: 6px; background: rgba(0,0,0,0.7);
               color: #fff; padding: 2px 6px; border-radius: 3px; font-size: 12px; }}
.item .badge.fav {{ color: #ffd700; }}
.caption {{ padding: 4px 8px; font-size: 11px; white-space: nowrap; overflow: hidden;
           text-overflow: ellipsis; }}
.nav {{ margin: 12px 0; display: flex; gap: 16px; align-items: center; }}
.nav a {{ background: #16213e; padding: 6px 14px; border-radius: 4px; text-decoration: none; }}
</style>
</head>
<body>{body}</body>
</html>'''


# ------------------------------------------------------------------
# Server factory
# ------------------------------------------------------------------

class PhotoHTTPServer(HTTPServer):
    """HTTPServer subclass that tracks start time."""
    def __init__(self, *args, **kwargs):
        self.start_time = time.time()
        super().__init__(*args, **kwargs)


def get_local_ip():
    """Get the device's local IP address on the network."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def start_server(photo_bridge, host='0.0.0.0', port=3691):
    """Start the photo HTTP server.

    Args:
        photo_bridge: a PhotoBridge or StubPhotoBridge instance
        host: bind address (default 0.0.0.0 for LAN access)
        port: listen port (default 3691)

    Returns:
        (server, thread) tuple. Call server.shutdown() to stop.
    """
    global bridge
    bridge = photo_bridge

    server = PhotoHTTPServer((host, port), PhotoRequestHandler)

    local_ip = get_local_ip()
    print()
    print('=' * 50)
    print('  iOS Photo Server running')
    print('  Local:   http://127.0.0.1:{}'.format(port))
    print('  Network: http://{}:{}'.format(local_ip, port))
    print()
    print('  API:     http://{}:{}/api/status'.format(local_ip, port))
    print('  Browse:  http://{}:{}/'.format(local_ip, port))
    print('=' * 50)
    print()

    return server
