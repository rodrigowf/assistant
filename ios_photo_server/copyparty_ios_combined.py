#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iOS Photo Server — Single-file combined version for Pythonista

Run this script in Pythonista 3 on your iPhone/iPad to serve your
entire photo library over the local network via a REST API.

v1.3.1 — Streaming video export (fixes OOM jetsam on large videos).
v1.3.0 — Ultra-defensive for Pythonista stability:
  - Server starts IMMEDIATELY (before touching photo library)
  - Photo enumeration happens in a background thread
  - Every ObjC property access is individually wrapped in try/except
  - location and media_subtypes are NOT accessed during enumeration
  - Progress logged every 50 assets so you can see where it dies
  - Assets processed in batches to limit peak memory
  - Video export uses PHAssetResourceManager (single callback, no nested async)
  - All output logged to file for post-crash debugging (GET /api/logs)
  - Persistent log survives crashes — previous session log at /api/logs/previous

Agent access (primary):
    GET /api/status              — Library summary (shows loading progress)
    GET /api/assets              — List assets (filterable, paginated)
    GET /api/assets/{id}         — Single asset metadata
    GET /api/albums              — List albums
    GET /api/search?q=IMG_1234   — Search by filename
    GET /api/refresh             — Force re-enumerate library
    GET /api/logs                — Current session log (plain text)
    GET /api/logs/previous       — Previous session log (crash debugging)
    GET /media/{id}/full         — Download full-res photo/video
    GET /media/{id}/thumb        — 300px thumbnail
    GET /media/{id}/preview      — 1200px preview

Browser access (secondary):
    GET /                        — Landing page with stats
    GET /browse                  — Thumbnail grid with pagination

Usage:
    Run in Pythonista:  just tap play
    Run on desktop:     python3 copyparty_ios_combined.py --stub
    Custom port:        python3 copyparty_ios_combined.py --port 8080
"""

__version__ = '1.3.1'
__author__ = 'Assistant'

# v1.3.1 — Streaming video export + size cap:
#   - _export_video now uses requestDataForAssetResource_..._dataReceivedHandler_
#     instead of writeDataForAssetResource_toFile_ to avoid OOM jetsam on
#     videos > ~100 MB (the old API buffered the whole video in RAM).
#   - Pre-flight fileSize check rejects videos > 500 MB with HTTP 413.
#   - Both data and completion handlers are now pinned on self to prevent
#     GC mid-stream (extends the v1.2.1 single-callback pin pattern).

# =====================================================================
# IMPORTS
# =====================================================================

import os
import io
import re
import sys
import json
import time
import socket
import threading
import tempfile
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

try:
    import photos
    HAS_PHOTOS = True
except ImportError:
    HAS_PHOTOS = False

try:
    from objc_util import ObjCClass, ns
    HAS_OBJC = True
except ImportError:
    HAS_OBJC = False


# v1.3.1: sentinel returned by _export_video when the source file exceeds
# MAX_VIDEO_BYTES. _serve_video translates this to HTTP 413.
_VIDEO_TOO_LARGE = '__VIDEO_TOO_LARGE__'


# =====================================================================
# LOGGING — Tee stdout/stderr to a persistent log file
# =====================================================================

_LOG_DIR = None
_LOG_PATH = None
_PREV_LOG_PATH = None


class _TeeWriter:
    """Write to both original stream and a log file simultaneously.
    Flushes after every write so the log survives crashes."""

    def __init__(self, original, log_file):
        self._original = original
        self._log_file = log_file

    def write(self, data):
        try:
            self._original.write(data)
        except Exception:
            pass
        try:
            self._log_file.write(data)
            self._log_file.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass
        try:
            self._log_file.flush()
        except Exception:
            pass

    # Forward any other attribute access to original (encoding, etc.)
    def __getattr__(self, name):
        return getattr(self._original, name)


def _setup_logging():
    """Set up persistent file logging. Called early in main().

    Log files are stored in a 'photo_server_logs' directory next to the
    script or in the system temp dir. The current session writes to
    'current.log'. On startup, any existing 'current.log' is renamed
    to 'previous.log' so crash logs from the last session are preserved.
    """
    global _LOG_DIR, _LOG_PATH, _PREV_LOG_PATH

    # Try script directory first (Pythonista), fall back to temp
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        _LOG_DIR = os.path.join(script_dir, 'photo_server_logs')
    except Exception:
        _LOG_DIR = os.path.join(tempfile.gettempdir(), 'photo_server_logs')

    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
    except Exception:
        _LOG_DIR = tempfile.gettempdir()

    _LOG_PATH = os.path.join(_LOG_DIR, 'current.log')
    _PREV_LOG_PATH = os.path.join(_LOG_DIR, 'previous.log')

    # Rotate: rename current.log → previous.log (preserves crash logs)
    try:
        if os.path.exists(_LOG_PATH):
            # Remove old previous
            if os.path.exists(_PREV_LOG_PATH):
                os.remove(_PREV_LOG_PATH)
            os.rename(_LOG_PATH, _PREV_LOG_PATH)
    except Exception:
        pass

    # Open log file and tee stdout/stderr
    try:
        log_file = open(_LOG_PATH, 'w', encoding='utf-8', buffering=1)
        # Write header
        log_file.write('=== iOS Photo Server v{} ===\n'.format(__version__))
        log_file.write('Started: {}\n'.format(
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        log_file.write('Log: {}\n\n'.format(_LOG_PATH))
        log_file.flush()

        sys.stdout = _TeeWriter(sys.__stdout__, log_file)
        sys.stderr = _TeeWriter(sys.__stderr__, log_file)
    except Exception as e:
        print('[Log] Warning: Could not set up file logging: {}'.format(e))


def _read_log(path, tail_lines=200):
    """Read a log file, optionally only the last N lines."""
    try:
        if not path or not os.path.exists(path):
            return None
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        if tail_lines and len(lines) > tail_lines:
            return ''.join(lines[-tail_lines:])
        return ''.join(lines)
    except Exception:
        return None


# =====================================================================
# PART 1: PHOTO BRIDGE — iOS Photo Library Abstraction
# =====================================================================

def _parse_date(s):
    """Parse an ISO date or datetime string."""
    if not s:
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _safe_str(val, default=''):
    """Safely convert any value to string."""
    try:
        if val is None:
            return default
        return str(val)
    except Exception:
        return default


def _safe_int(val, default=0):
    """Safely convert any value to int."""
    try:
        if val is None:
            return default
        return int(val)
    except Exception:
        return default


def _safe_float(val, default=0.0):
    """Safely convert any value to float."""
    try:
        if val is None:
            return default
        return float(val)
    except Exception:
        return default


def _safe_bool(val, default=False):
    """Safely convert any value to bool."""
    try:
        if val is None:
            return default
        return bool(val)
    except Exception:
        return default


class PhotoBridge:
    """Bridge to the iOS photo library via Pythonista's photos module.

    DESIGN FOR iOS STABILITY (v1.2.0):
    - Server starts BEFORE any photo library access.
    - Enumeration runs in a background thread.
    - We cache only lightweight metadata dicts (never Asset objects).
    - Filenames, location, and media_subtypes are resolved LAZILY.
    - Every single property access is individually wrapped in try/except.
    - Progress is logged every 50 assets.
    """

    def __init__(self):
        self._meta_cache = {}           # dict: local_id -> metadata dict
        self._albums_cache = None
        self._cache_time = 0
        self._cache_ttl = 120           # seconds before re-enumerating
        self._filename_cache = {}       # local_id -> filename (populated lazily)
        self._video_temp_dir = None
        self._lock = threading.Lock()
        self._loading = False
        self._load_progress = ''        # human-readable progress string
        self._loaded = False
        self._load_error = None
        try:
            self._video_temp_dir = os.path.join(tempfile.gettempdir(), 'photo_server_videos')
            os.makedirs(self._video_temp_dir, exist_ok=True)
        except Exception as e:
            print('[Bridge] Warning: Could not create video temp dir: {}'.format(e))
            self._video_temp_dir = tempfile.gettempdir()

    # --- Permission ---

    def check_permission(self):
        """Check photo library access. Returns True if authorized."""
        if not HAS_PHOTOS:
            return False
        if not HAS_OBJC:
            # Can't check ObjC status, try a minimal photos call
            try:
                _ = photos.get_assets(media_type='image')
                return True
            except Exception:
                return False
        try:
            PHPhotoLibrary = ObjCClass('PHPhotoLibrary')
            # 0=NotDetermined, 1=Restricted, 2=Denied, 3=Authorized, 4=Limited
            status = int(PHPhotoLibrary.authorizationStatus())
            print('[Bridge] Authorization status: {}'.format(status))
            if status == 0:
                # Not determined yet — trigger the permission dialog
                print('[Bridge] Triggering permission dialog...')
                try:
                    _ = photos.get_assets(media_type='image')
                except Exception as e:
                    print('[Bridge] Permission dialog error: {}'.format(e))
                # Re-check after the dialog
                status = int(PHPhotoLibrary.authorizationStatus())
                print('[Bridge] Post-dialog status: {}'.format(status))
            return status in (3, 4)
        except Exception as e:
            print('[Bridge] Permission check error: {}'.format(e))
            return False

    # --- Background loading ---

    def start_background_load(self):
        """Start enumerating the photo library in a background thread."""
        if self._loading or self._loaded:
            return
        self._loading = True
        self._load_progress = 'Starting...'
        t = threading.Thread(target=self._background_load, daemon=True)
        t.start()

    def _background_load(self):
        """Background thread: enumerate photos and build metadata cache."""
        try:
            print('[Bridge] Background enumeration starting...')
            self._refresh_cache(force=True)
            self._loaded = True
            self._load_error = None
            print('[Bridge] Background enumeration complete.')
        except Exception as e:
            self._load_error = str(e)
            print('[Bridge] Background enumeration FAILED: {}'.format(e))
            import traceback
            traceback.print_exc()
        finally:
            self._loading = False

    # --- Cache ---

    def _refresh_cache(self, force=False):
        """Rebuild the lightweight metadata cache.

        We iterate over assets and extract only safe scalar metadata.
        We do NOT call ObjC for filenames here — that's done lazily.
        We do NOT keep references to Asset objects.
        We do NOT access .location or .media_subtypes here.
        Every property access is individually wrapped.
        """
        now = time.time()
        if not force and len(self._meta_cache) > 0 and (now - self._cache_time) < self._cache_ttl:
            return

        with self._lock:
            if not force and len(self._meta_cache) > 0 and (now - self._cache_time) < self._cache_ttl:
                return

            print('[Bridge] Enumerating photo library...')
            self._load_progress = 'Fetching asset list...'
            t0 = time.time()

            new_cache = {}

            # --- Enumerate IMAGES first, then VIDEOS ---
            # Splitting by type reduces peak memory vs fetching all at once.
            for media_type_label, media_type_arg in [('images', 'image'), ('videos', 'video')]:
                self._load_progress = 'Fetching {}...'.format(media_type_label)
                print('[Bridge] Fetching {}...'.format(media_type_label))

                try:
                    raw = photos.get_assets(media_type=media_type_arg)
                except Exception as e:
                    print('[Bridge] Error fetching {}: {}'.format(media_type_label, e))
                    continue

                count = 0
                try:
                    count = len(raw)
                except Exception:
                    # Some Pythonista versions might not support len()
                    pass

                print('[Bridge] Found {} {}, extracting metadata...'.format(count, media_type_label))

                idx = 0
                for asset in raw:
                    try:
                        info = self._extract_metadata_safe(asset, media_type_arg)
                        if info and info.get('id'):
                            new_cache[info['id']] = info
                    except Exception as e:
                        # Skip this individual asset entirely
                        print('[Bridge] Skip asset {}: {}'.format(idx, e))

                    idx += 1
                    if idx % 50 == 0:
                        self._load_progress = 'Processing {} {}/{}...'.format(
                            media_type_label, idx, count)
                        print('[Bridge] {} {}/{}...'.format(
                            media_type_label, idx, count))

                # Release the raw list explicitly — crucial for iOS memory
                try:
                    del raw
                except Exception:
                    pass

                print('[Bridge] Done with {} — {} cached so far'.format(
                    media_type_label, len(new_cache)))

            self._meta_cache = new_cache
            self._cache_time = time.time()
            self._albums_cache = None
            elapsed = time.time() - t0
            self._load_progress = 'Done ({} assets in {:.1f}s)'.format(
                len(new_cache), elapsed)
            print('[Bridge] Cached {} assets in {:.1f}s'.format(
                len(new_cache), elapsed))

    def _extract_metadata_safe(self, asset, media_type_hint):
        """Extract lightweight metadata with every property individually protected.

        NO ObjC calls. NO filename resolution. NO location. NO media_subtypes.
        These are all deferred to lazy per-asset access.
        """
        # --- local_id (critical — skip asset if this fails) ---
        try:
            local_id = asset.local_id
        except Exception:
            return None
        if not local_id:
            return None

        # --- creation_date ---
        cdate = None
        cdate_iso = None
        try:
            cdate = asset.creation_date
            if cdate:
                cdate_iso = cdate.isoformat()
        except Exception:
            pass

        # --- modification_date ---
        mdate_iso = None
        try:
            mdate = asset.modification_date
            if mdate:
                mdate_iso = mdate.isoformat()
        except Exception:
            pass

        # --- media_type ---
        media_type = media_type_hint  # Use the hint since we query by type
        try:
            mt = asset.media_type
            if mt:
                media_type = str(mt)
        except Exception:
            pass

        # --- pixel dimensions ---
        width = 0
        try:
            width = int(asset.pixel_width)
        except Exception:
            pass

        height = 0
        try:
            height = int(asset.pixel_height)
        except Exception:
            pass

        # --- duration (videos only) ---
        duration = None
        if media_type == 'video':
            try:
                d = asset.duration
                if d is not None:
                    duration = float(d)
            except Exception:
                pass

        # --- favorite ---
        favorite = False
        try:
            favorite = bool(asset.favorite)
        except Exception:
            pass

        # --- hidden ---
        hidden = False
        try:
            hidden = bool(asset.hidden)
        except Exception:
            pass

        # --- Generate a synthetic filename (NO ObjC call) ---
        filename = self._filename_cache.get(local_id)
        if filename is None:
            try:
                ext = 'HEIC' if media_type == 'image' else 'MOV'
                if cdate:
                    filename = '{}_{}.{}'.format(
                        'IMG' if media_type == 'image' else 'VID',
                        cdate.strftime('%Y%m%d_%H%M%S'), ext)
                else:
                    filename = '{}.{}'.format(str(local_id)[:8], ext)
            except Exception:
                filename = '{}.bin'.format(str(local_id)[:8])

        safe_id = str(local_id).split('/')[0]
        return {
            'id': local_id,
            'filename': filename,
            'media_type': media_type,
            'width': width,
            'height': height,
            'creation_date': cdate_iso,
            'modification_date': mdate_iso,
            'duration': duration,
            'favorite': favorite,
            'hidden': hidden,
            # NOTE: location and media_subtypes are NOT included here.
            # They are resolved lazily in get_asset_info() to avoid
            # hidden ObjC calls during enumeration.
            'urls': {
                'full': '/media/{}/full'.format(safe_id),
                'thumb': '/media/{}/thumb'.format(safe_id),
                'preview': '/media/{}/preview'.format(safe_id),
            },
        }

    def _resolve_filename(self, local_id):
        """Lazily resolve the original filename via ObjC. Cached."""
        if local_id in self._filename_cache:
            return self._filename_cache[local_id]
        if not HAS_OBJC:
            return None
        try:
            PHAssetResource = ObjCClass('PHAssetResource')
            PHAsset = ObjCClass('PHAsset')
            fetch = PHAsset.fetchAssetsWithLocalIdentifiers_options_([local_id], None)
            if fetch.count() > 0:
                objc_asset = fetch.objectAtIndex_(0)
                resources = PHAssetResource.assetResourcesForAsset_(objc_asset)
                if resources and resources.count() > 0:
                    fn = str(resources.objectAtIndex_(0).originalFilename())
                    self._filename_cache[local_id] = fn
                    if local_id in self._meta_cache:
                        self._meta_cache[local_id]['filename'] = fn
                    return fn
        except Exception as e:
            print('[Bridge] Filename resolve error for {}: {}'.format(
                str(local_id)[:12], e))
        return None

    def _resolve_location(self, local_id):
        """Lazily resolve location for a single asset."""
        try:
            asset = photos.get_asset_with_local_id(local_id)
            if asset is None:
                return None
            loc = asset.location
            if loc and isinstance(loc, dict) and 'latitude' in loc:
                return {'latitude': loc['latitude'], 'longitude': loc['longitude']}
        except Exception:
            pass
        return None

    def _get_asset(self, asset_id):
        """Fetch a fresh Asset object by ID (exact or prefix match).
        Returns (asset, info) or None."""
        self._ensure_loaded()
        # Find the full local_id
        local_id = None
        info = None
        if asset_id in self._meta_cache:
            local_id = asset_id
            info = self._meta_cache[asset_id]
        else:
            for lid, meta in self._meta_cache.items():
                if lid.startswith(asset_id):
                    local_id = lid
                    info = meta
                    break
        if local_id is None:
            return None

        # Fetch the actual Asset object on demand (not cached)
        try:
            asset = photos.get_asset_with_local_id(local_id)
            if asset is None:
                return None

            # Lazily resolve filename on first media access
            if local_id not in self._filename_cache:
                self._resolve_filename(local_id)
                if local_id in self._meta_cache:
                    info = self._meta_cache[local_id]

            return (asset, info)
        except Exception as e:
            print('[Bridge] get_asset error {}: {}'.format(
                str(asset_id)[:12], e))
            return None

    def _ensure_loaded(self):
        """Block until initial load is complete, or do a sync load."""
        if self._loaded and len(self._meta_cache) > 0:
            return
        if self._loading:
            # Wait for background load to finish
            for _ in range(600):  # up to 60 seconds
                if self._loaded or not self._loading:
                    return
                time.sleep(0.1)
            return
        # No background load running and not loaded — do sync load
        self._refresh_cache(force=True)

    # --- Listing ---

    def get_status(self):
        if self._loading:
            return {
                'photo_count': sum(1 for i in self._meta_cache.values()
                                   if i.get('media_type') == 'image'),
                'video_count': sum(1 for i in self._meta_cache.values()
                                   if i.get('media_type') == 'video'),
                'total_count': len(self._meta_cache),
                'loading': True,
                'progress': self._load_progress,
            }
        self._ensure_loaded()
        pc = sum(1 for info in self._meta_cache.values()
                 if info.get('media_type') == 'image')
        vc = sum(1 for info in self._meta_cache.values()
                 if info.get('media_type') == 'video')
        return {
            'photo_count': pc,
            'video_count': vc,
            'total_count': len(self._meta_cache),
            'loading': False,
        }

    def list_assets(self, media_type=None, album=None, after=None, before=None,
                    sort='date_desc', offset=0, limit=100, favorite=None, search=None):
        self._ensure_loaded()
        limit = min(limit, 500)

        album_ids = None
        if album:
            album_ids = self._get_album_asset_ids(album)
            if album_ids is None:
                return {'total': 0, 'offset': offset, 'limit': limit, 'assets': []}

        after_dt = _parse_date(after) if after else None
        before_dt = _parse_date(before) if before else None
        type_filter = {'photo': 'image', 'video': 'video'}.get(media_type)

        filtered = []
        for lid, info in self._meta_cache.items():
            if type_filter and info.get('media_type') != type_filter:
                continue
            if album_ids is not None and lid not in album_ids:
                continue
            if favorite is not None and info.get('favorite') != favorite:
                continue
            if search and search.lower() not in (info.get('filename') or '').lower():
                continue
            if after_dt and info.get('creation_date') and info['creation_date'] < after_dt.isoformat():
                continue
            if before_dt and info.get('creation_date') and info['creation_date'] > before_dt.isoformat():
                continue
            filtered.append(info)

        reverse = sort.endswith('_desc')
        if sort.startswith('date'):
            key = lambda x: x.get('creation_date') or ''
        elif sort.startswith('size'):
            key = lambda x: (x.get('width', 0) * x.get('height', 0))
        else:
            key = lambda x: x.get('creation_date') or ''
        filtered.sort(key=key, reverse=reverse)

        return {'total': len(filtered), 'offset': offset, 'limit': limit,
                'assets': filtered[offset:offset + limit]}

    def get_asset_info(self, asset_id):
        """Get detailed info for a single asset, including lazy-resolved fields."""
        self._ensure_loaded()
        info = self._meta_cache.get(asset_id)
        if not info:
            for lid, meta in self._meta_cache.items():
                if lid.startswith(asset_id):
                    info = meta
                    asset_id = lid
                    break
        if not info:
            return None

        # For single-asset detail, resolve lazy fields
        result = dict(info)  # copy

        # Resolve filename
        if asset_id not in self._filename_cache:
            fn = self._resolve_filename(asset_id)
            if fn:
                result['filename'] = fn

        # Resolve location (not done during enumeration)
        if 'location' not in result:
            result['location'] = self._resolve_location(asset_id)

        return result

    # --- Albums ---

    def list_albums(self):
        self._ensure_loaded()
        if self._albums_cache is not None:
            return self._albums_cache
        result = []
        try:
            for a in photos.get_albums():
                try:
                    name = 'Untitled'
                    try:
                        name = a.title or 'Untitled'
                    except Exception:
                        pass
                    count = 0
                    try:
                        count = len(a.assets)
                    except Exception:
                        pass
                    lid = ''
                    try:
                        lid = a.local_id
                    except Exception:
                        pass
                    result.append({'name': name, 'count': count,
                                   'type': 'user', 'local_id': lid})
                except Exception:
                    pass
        except Exception as e:
            print('[Bridge] Error listing albums: {}'.format(e))
        try:
            for a in photos.get_smart_albums():
                try:
                    count = 0
                    try:
                        count = len(a.assets)
                    except Exception:
                        pass
                    if count > 0:
                        name = 'Unknown'
                        try:
                            name = a.title or getattr(a, 'subtype', '') or 'Unknown'
                        except Exception:
                            pass
                        lid = ''
                        try:
                            lid = a.local_id
                        except Exception:
                            pass
                        result.append({'name': name, 'count': count,
                                       'type': 'smart', 'local_id': lid})
                except Exception:
                    pass
        except Exception as e:
            print('[Bridge] Error listing smart albums: {}'.format(e))
        self._albums_cache = result
        return result

    def _get_album_asset_ids(self, album_name):
        try:
            for a in photos.get_albums():
                try:
                    if (a.title or '') == album_name:
                        return {x.local_id for x in a.assets}
                except Exception:
                    pass
            for a in photos.get_smart_albums():
                try:
                    name = a.title or getattr(a, 'subtype', '') or ''
                    if name == album_name:
                        return {x.local_id for x in a.assets}
                except Exception:
                    pass
        except Exception:
            pass
        return None

    # --- Photo data ---

    def get_photo_data(self, asset_id, quality='full'):
        resolved = self._get_asset(asset_id)
        if not resolved:
            return None
        asset, info = resolved
        if info.get('media_type') != 'image':
            return None
        try:
            if quality in ('thumb', 'preview'):
                max_size = 300 if quality == 'thumb' else 1200
                return self._get_thumbnail(asset, info, max_size)
            data = asset.get_image_data(original=True)
            ct = 'image/jpeg'
            try:
                uti = getattr(data, 'uti', None)
                if uti and 'png' in str(uti).lower():
                    ct = 'image/png'
            except Exception:
                pass
            return (data.getvalue(), info.get('filename', 'photo.jpg'), ct)
        except Exception as e:
            print('[Bridge] Photo error {}: {}'.format(
                str(asset_id)[:12], e))
            return None

    def _get_thumbnail(self, asset, info, max_size):
        # Try get_ui_image first (uses iOS thumbnail cache, very efficient)
        try:
            w = info.get('width') or max_size
            h = info.get('height') or max_size
            scale = min(max_size / max(w, 1), max_size / max(h, 1), 1.0)
            tw, th = max(int(w * scale), 1), max(int(h * scale), 1)
            ui_img = asset.get_ui_image(size=(tw, th), crop=False)
            if ui_img:
                return (ui_img.to_png(), info.get('filename', 'thumb.png'), 'image/png')
        except Exception:
            pass
        # Fallback: PIL resize
        try:
            img = asset.get_image(original=False)
            if img:
                img.thumbnail((max_size, max_size))
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=80)
                return (buf.getvalue(), info.get('filename', 'thumb.jpg'), 'image/jpeg')
        except Exception as e:
            print('[Bridge] Thumbnail error: {}'.format(e))
        return None

    # --- Video data ---

    def get_video_data(self, asset_id):
        resolved = self._get_asset(asset_id)
        if not resolved:
            return None
        asset, info = resolved
        if info.get('media_type') != 'video':
            return None
        safe_id = str(info['id']).split('/')[0]
        temp_path = os.path.join(self._video_temp_dir, safe_id + '.mp4')
        if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
            return (temp_path, info.get('filename', 'video.mp4'),
                    'video/mp4', os.path.getsize(temp_path))
        try:
            return self._export_video(asset, info, temp_path)
        except Exception as e:
            print('[Bridge] Video export error {}: {}'.format(
                str(asset_id)[:12], e))
            return None

    def _export_video(self, asset, info, temp_path):
        """Export video to temp file via streaming PHAssetResourceManager.

        v1.3.1: Switched from writeDataForAssetResource_toFile_ (which
        buffers the entire video into memory before writing — iOS jetsams
        Pythonista on >100 MB videos) to the streaming variant
        requestDataForAssetResource_options_dataReceivedHandler_completionHandler_,
        which delivers NSData chunks to a handler we write directly to a
        file handle. Peak memory stays ~constant regardless of video size.

        Also adds a pre-flight fileSize check: videos > 500 MB return the
        sentinel _VIDEO_TOO_LARGE so the HTTP layer can send 413.
        """
        if not HAS_OBJC:
            return None

        print('[Bridge] Starting video export for {}...'.format(
            str(info.get('id', ''))[:12]))

        try:
            from objc_util import ObjCInstance
            import ctypes

            # Get ObjC PHAsset
            PHAsset = ObjCClass('PHAsset')
            fetch = PHAsset.fetchAssetsWithLocalIdentifiers_options_(
                [info['id']], None)
            if fetch.count() == 0:
                print('[Bridge] Video export: asset not found in PHAsset')
                return None
            objc_asset = fetch.objectAtIndex_(0)

            # Get asset resources and find the video resource
            PHAssetResource = ObjCClass('PHAssetResource')
            resources = PHAssetResource.assetResourcesForAsset_(objc_asset)
            if not resources or resources.count() == 0:
                print('[Bridge] Video export: no resources found')
                return None

            video_resource = None
            for i in range(resources.count()):
                r = resources.objectAtIndex_(i)
                rtype = int(r.type())
                # PHAssetResourceType: 1=photo, 2=video, 3=audio,
                # 5=pairedVideo (Live Photo), 9=adjustmentBasePairedVideo
                if rtype == 2:
                    video_resource = r
                    break

            if video_resource is None:
                # Fallback: try any resource that isn't a photo
                for i in range(resources.count()):
                    r = resources.objectAtIndex_(i)
                    if int(r.type()) != 1:
                        video_resource = r
                        break

            if video_resource is None:
                print('[Bridge] Video export: no video resource found')
                return None

            # --- v1.3.1 PRE-FLIGHT SIZE CHECK ---
            # PHAssetResource exposes 'fileSize' as a private NSNumber-valued
            # KVC key. Returns 0/None if unavailable; we only block when we
            # have a confident reading above the cap.
            fsize_pre = 0
            try:
                size_num = video_resource.valueForKey_('fileSize')
                if size_num is not None:
                    try:
                        fsize_pre = int(size_num.longLongValue())
                    except Exception:
                        try:
                            fsize_pre = int(size_num.intValue())
                        except Exception:
                            fsize_pre = 0
            except Exception:
                fsize_pre = 0
            MAX_VIDEO_BYTES = 500 * 1024 * 1024  # 500 MB
            if fsize_pre and fsize_pre > MAX_VIDEO_BYTES:
                mb = fsize_pre / (1024.0 * 1024.0)
                print('[Bridge] Video export REJECTED: {:.1f} MB exceeds '
                      '{} MB cap'.format(mb, MAX_VIDEO_BYTES // (1024 * 1024)))
                return _VIDEO_TOO_LARGE
            if fsize_pre:
                print('[Bridge] Video pre-flight size: {:.1f} MB'.format(
                    fsize_pre / (1024.0 * 1024.0)))

            # Remove temp file if it exists; we'll create fresh.
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

            # Stream video data via PHAssetResourceManager.
            PHAssetResourceManager = ObjCClass('PHAssetResourceManager')
            PHAssetResourceRequestOptions = ObjCClass(
                'PHAssetResourceRequestOptions')

            res_manager = PHAssetResourceManager.defaultManager()
            res_options = PHAssetResourceRequestOptions.alloc().init()
            res_options.setNetworkAccessAllowed_(True)

            done = threading.Event()
            error_msg = [None]
            bytes_written = [0]
            chunks_received = [0]
            last_log_mb = [0]
            write_error = [None]

            # Open file handle BEFORE issuing the request. The data handler
            # is dispatched serially per-request from a background ObjC
            # queue, so a single file handle with .write() is safe.
            try:
                fh = open(temp_path, 'wb')
            except Exception as e:
                print('[Bridge] Could not open temp file: {}'.format(e))
                return None

            def _data_received(data_ptr):
                try:
                    if not data_ptr:
                        return
                    nsdata = ObjCInstance(data_ptr)
                    length = int(nsdata.length())
                    if length <= 0:
                        return
                    # ctypes.string_at copies length bytes from the NSData
                    # backing store into a fresh Python bytes object. The
                    # NSData itself is autoreleased after the handler returns.
                    buf_ptr = nsdata.bytes()
                    chunk = ctypes.string_at(buf_ptr, length)
                    fh.write(chunk)
                    bytes_written[0] += length
                    chunks_received[0] += 1
                    # Log every 10 MB to avoid log spam
                    mb_now = bytes_written[0] // (1024 * 1024)
                    if mb_now >= last_log_mb[0] + 10:
                        last_log_mb[0] = mb_now
                        print('[Bridge] Streaming video: {} MB '
                              '({} chunks)'.format(mb_now, chunks_received[0]))
                except Exception as e:
                    write_error[0] = 'data handler: {}'.format(e)
                    # Don't set done here — let completion handler signal end.

            def _completion(err_ptr):
                try:
                    if err_ptr:
                        try:
                            err_obj = ObjCInstance(err_ptr)
                            error_msg[0] = str(err_obj.localizedDescription())
                        except Exception as e:
                            error_msg[0] = 'err parse: {}'.format(e)
                except Exception as e:
                    error_msg[0] = 'completion handler: {}'.format(e)
                finally:
                    done.set()

            # Pin BOTH callbacks for the lifetime of the request. If either
            # is GC'd while ObjC still holds the block, we crash.
            self._active_data_handler = _data_received
            self._active_completion = _completion

            print('[Bridge] Streaming video to {}...'.format(temp_path))
            try:
                res_manager.requestDataForAssetResource_options_dataReceivedHandler_completionHandler_(
                    video_resource, res_options, _data_received, _completion)
                # NOTE: if Pythonista fails to auto-bridge the data handler
                # block signature, wrap manually:
                #   from objc_util import ObjCBlock
                #   from ctypes import c_void_p
                #   blk_data = ObjCBlock(_data_received, restype=None, argtypes=[c_void_p])
                #   blk_done = ObjCBlock(_completion,    restype=None, argtypes=[c_void_p])
                #   self._active_data_handler = blk_data
                #   self._active_completion = blk_done
                #   res_manager.requestDataForAssetResource_options_dataReceivedHandler_completionHandler_(
                #       video_resource, res_options, blk_data, blk_done)

                # Wait for completion (single 300s ceiling, applied to the
                # completion event — data handler keeps the timer fed
                # implicitly since completion only fires after last chunk).
                if not done.wait(timeout=300):
                    print('[Bridge] Video stream timed out after 300s')
                    error_msg[0] = error_msg[0] or 'timeout'
            finally:
                try:
                    fh.flush()
                except Exception:
                    pass
                try:
                    fh.close()
                except Exception:
                    pass
                # Release callback pins now that ObjC is done with them.
                self._active_data_handler = None
                self._active_completion = None

            if write_error[0]:
                print('[Bridge] Video stream write error: {}'.format(
                    write_error[0]))
                return None

            if error_msg[0]:
                print('[Bridge] Video stream completion error: {}'.format(
                    error_msg[0]))
                # Best-effort cleanup of partial file
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass
                return None

            if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                fsize = os.path.getsize(temp_path)
                print('[Bridge] Video export complete: {} bytes '
                      '({} chunks, {} bytes via handler)'.format(
                          fsize, chunks_received[0], bytes_written[0]))
                return (temp_path, info.get('filename', 'video.mp4'),
                        'video/mp4', fsize)

            print('[Bridge] Video export: file not created or empty')
            return None

        except Exception as e:
            print('[Bridge] Video export exception: {}'.format(e))
            import traceback
            traceback.print_exc()
            return None

    def get_video_thumbnail(self, asset_id, max_size=300):
        resolved = self._get_asset(asset_id)
        if not resolved:
            return None
        asset, info = resolved
        if info.get('media_type') != 'video':
            return None
        try:
            w = info.get('width') or max_size
            h = info.get('height') or max_size
            scale = min(max_size / max(w, 1), max_size / max(h, 1), 1.0)
            tw, th = max(int(w * scale), 1), max(int(h * scale), 1)
            ui_img = asset.get_ui_image(size=(tw, th), crop=False)
            if ui_img:
                name = os.path.splitext(info.get('filename', 'thumb'))[0] + '_thumb.png'
                return (ui_img.to_png(), name, 'image/png')
        except Exception:
            pass
        try:
            img = asset.get_image(original=False)
            if img:
                img.thumbnail((max_size, max_size))
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=80)
                name = os.path.splitext(info.get('filename', 'thumb'))[0] + '_thumb.jpg'
                return (buf.getvalue(), name, 'image/jpeg')
        except Exception:
            pass
        return None

    # --- Lifecycle ---

    def cleanup(self):
        try:
            import shutil
            if self._video_temp_dir and os.path.exists(self._video_temp_dir):
                shutil.rmtree(self._video_temp_dir)
        except Exception:
            pass

    def invalidate_cache(self):
        with self._lock:
            self._meta_cache = {}
            self._albums_cache = None
            self._cache_time = 0
            self._loaded = False


# =====================================================================
# STUB BRIDGE — For desktop development/testing
# =====================================================================

class StubPhotoBridge:
    """Generates fake assets for testing without iOS."""

    def __init__(self, stub_dir=None):
        self._stub_dir = stub_dir or os.path.join(tempfile.gettempdir(), 'photo_server_stubs')
        os.makedirs(self._stub_dir, exist_ok=True)
        self._assets = []
        self._loading = False
        self._loaded = True
        self._load_progress = 'Stub mode'
        self._load_error = None
        self._generate()

    def _generate(self):
        try:
            from PIL import Image as PILImage
            has_pil = True
        except ImportError:
            has_pil = False

        for i in range(25):
            aid = 'STUB-{:04d}-0000-0000-000000000000'.format(i)
            is_vid = i % 5 == 0
            ext = 'mp4' if is_vid else 'jpg'
            fn = 'IMG_{:04d}.{}'.format(1000 + i, ext.upper())
            cdate = datetime(2025, 12, 1 + (i % 28), 10, i, 0)
            path = os.path.join(self._stub_dir, fn)
            if not os.path.exists(path):
                if is_vid:
                    with open(path, 'wb') as f:
                        f.write(b'\x00' * 1024)
                elif has_pil:
                    img = PILImage.new('RGB', (400, 300),
                                       color=((i*37)%256, (i*73)%256, (i*113)%256))
                    img.save(path, 'JPEG')
                else:
                    with open(path, 'wb') as f:
                        f.write(b'\xff\xd8\xff\xe0' + b'\x00' * 100)
            safe = aid.split('/')[0]
            self._assets.append({
                'id': aid, 'filename': fn,
                'media_type': 'video' if is_vid else 'image',
                'width': 1920 if is_vid else 4032, 'height': 1080 if is_vid else 3024,
                'creation_date': cdate.isoformat(), 'modification_date': cdate.isoformat(),
                'duration': 15.0 + i if is_vid else None,
                'favorite': i % 7 == 0, 'hidden': False,
                'location': {'latitude': 40.7+i*0.01, 'longitude': -74.0+i*0.01} if i%3==0 else None,
                'urls': {'full': '/media/{}/full'.format(safe),
                         'thumb': '/media/{}/thumb'.format(safe),
                         'preview': '/media/{}/preview'.format(safe)},
                '_path': path,
            })

    def check_permission(self):
        return True

    def start_background_load(self):
        pass

    def get_status(self):
        p = sum(1 for a in self._assets if a['media_type'] == 'image')
        v = sum(1 for a in self._assets if a['media_type'] == 'video')
        return {'photo_count': p, 'video_count': v, 'total_count': len(self._assets),
                'loading': False}

    def list_assets(self, media_type=None, album=None, after=None, before=None,
                    sort='date_desc', offset=0, limit=100, favorite=None, search=None):
        f = list(self._assets)
        if media_type == 'photo':
            f = [a for a in f if a['media_type'] == 'image']
        elif media_type == 'video':
            f = [a for a in f if a['media_type'] == 'video']
        if favorite is not None:
            f = [a for a in f if a['favorite'] == favorite]
        if search:
            f = [a for a in f if search.lower() in a['filename'].lower()]
        if after:
            dt = _parse_date(after)
            if dt:
                f = [a for a in f if a['creation_date'] >= dt.isoformat()]
        if before:
            dt = _parse_date(before)
            if dt:
                f = [a for a in f if a['creation_date'] <= dt.isoformat()]
        rev = sort.endswith('_desc')
        f.sort(key=lambda x: x.get('creation_date') or '', reverse=rev)
        limit = min(limit, 500)
        total = len(f)
        page = [{k: v for k, v in a.items() if not k.startswith('_')} for a in f[offset:offset+limit]]
        return {'total': total, 'offset': offset, 'limit': limit, 'assets': page}

    def get_asset_info(self, aid):
        for a in self._assets:
            if a['id'] == aid or a['id'].startswith(aid):
                return {k: v for k, v in a.items() if not k.startswith('_')}
        return None

    def list_albums(self):
        return [
            {'name': 'Camera Roll', 'count': len(self._assets), 'type': 'smart'},
            {'name': 'Favorites', 'count': sum(1 for a in self._assets if a['favorite']), 'type': 'smart'},
        ]

    def get_photo_data(self, aid, quality='full'):
        for a in self._assets:
            if (a['id'] == aid or a['id'].startswith(aid)) and a['media_type'] == 'image':
                p = a.get('_path')
                if p and os.path.exists(p):
                    with open(p, 'rb') as f:
                        data = f.read()
                    if quality in ('thumb', 'preview'):
                        try:
                            from PIL import Image as PILImage
                            ms = 300 if quality == 'thumb' else 1200
                            img = PILImage.open(io.BytesIO(data))
                            img.thumbnail((ms, ms))
                            buf = io.BytesIO()
                            img.save(buf, format='JPEG', quality=80)
                            return (buf.getvalue(), a['filename'], 'image/jpeg')
                        except ImportError:
                            pass
                    return (data, a['filename'], 'image/jpeg')
        return None

    def get_video_data(self, aid):
        for a in self._assets:
            if (a['id'] == aid or a['id'].startswith(aid)) and a['media_type'] == 'video':
                p = a.get('_path')
                if p and os.path.exists(p):
                    return (p, a['filename'], 'video/mp4', os.path.getsize(p))
        return None

    def get_video_thumbnail(self, aid, max_size=300):
        for a in self._assets:
            if (a['id'] == aid or a['id'].startswith(aid)) and a['media_type'] == 'video':
                try:
                    from PIL import Image as PILImage
                    img = PILImage.new('RGB', (max_size, int(max_size*9/16)), color=(40, 40, 60))
                    buf = io.BytesIO()
                    img.save(buf, format='JPEG', quality=80)
                    return (buf.getvalue(), a['filename'], 'image/jpeg')
                except ImportError:
                    return (b'\xff\xd8\xff\xe0' + b'\x00' * 100, a['filename'], 'image/jpeg')
        return None

    def cleanup(self):
        pass

    def invalidate_cache(self):
        pass


def create_bridge(stub=False, stub_dir=None):
    if stub or not HAS_PHOTOS:
        if not HAS_PHOTOS:
            print('[Bridge] Not on iOS/Pythonista - using stub mode')
        return StubPhotoBridge(stub_dir=stub_dir)
    return PhotoBridge()


# =====================================================================
# PART 2: HTTP SERVER — API-first photo server
# =====================================================================

_bridge = None


class PhotoRequestHandler(BaseHTTPRequestHandler):
    server_version = 'iOSPhotoServer/1.2'

    def log_message(self, fmt, *args):
        try:
            print('[HTTP] {} - {}'.format(self.client_address[0], fmt % args))
        except Exception:
            pass

    # --- Routing ---

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path).rstrip('/')
            qs = parse_qs(parsed.query)
            params = {k: v[0] if len(v) == 1 else v for k, v in qs.items()}
        except Exception as e:
            return self._send_error(400, 'Bad request: {}'.format(e))

        try:
            if path == '/api/status':
                return self._api_status()
            if path == '/api/assets':
                return self._api_assets(params)
            if path == '/api/albums':
                return self._api_albums()
            if path == '/api/search':
                return self._api_search(params)
            if path == '/api/refresh':
                return self._api_refresh()
            if path == '/api/logs':
                return self._api_logs('current')
            if path == '/api/logs/previous':
                return self._api_logs('previous')

            m = re.match(r'^/api/assets/(.+)$', path)
            if m:
                return self._api_asset_detail(m.group(1))

            m = re.match(r'^/media/([^/]+)/(full|thumb|preview)$', path)
            if m:
                return self._serve_media(m.group(1), m.group(2))

            if path in ('', '/'):
                return self._html_index(params)
            if path == '/browse':
                return self._html_browse(params)

            return self._send_error(404, 'Not Found')
        except Exception as e:
            try:
                import traceback
                traceback.print_exc()
            except Exception:
                pass
            try:
                return self._send_error(500, str(e))
            except Exception:
                pass

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()

    # --- Helpers ---

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    def _json(self, data, status=200):
        body = json.dumps(data, indent=2, default=str).encode('utf-8')
        self.send_response(status)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, msg):
        self._json({'error': msg, 'status': status}, status=status)

    def _html_resp(self, html, status=200):
        body = html.encode('utf-8')
        self.send_response(status)
        self._cors()
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- API ---

    def _api_status(self):
        s = _bridge.get_status()
        try:
            s['albums'] = len(_bridge.list_albums())
        except Exception:
            s['albums'] = 0
        s['status'] = 'loading' if s.get('loading') else 'ok'
        s['server_version'] = self.server_version
        s['server_uptime'] = int(time.time() - self.server.start_time)
        if hasattr(_bridge, '_load_progress'):
            s['progress'] = _bridge._load_progress
        if hasattr(_bridge, '_load_error') and _bridge._load_error:
            s['error'] = _bridge._load_error
        self._json(s)

    def _api_assets(self, p):
        kw = {
            'media_type': p.get('type'), 'album': p.get('album'),
            'after': p.get('after'), 'before': p.get('before'),
            'sort': p.get('sort', 'date_desc'),
            'offset': int(p.get('offset', 0)), 'limit': int(p.get('limit', 100)),
            'search': p.get('q') or p.get('search'),
        }
        fav = p.get('favorite')
        if fav is not None:
            kw['favorite'] = fav.lower() in ('true', '1', 'yes')
        self._json(_bridge.list_assets(**kw))

    def _api_asset_detail(self, aid):
        info = _bridge.get_asset_info(aid)
        if not info:
            return self._send_error(404, 'Asset not found')
        self._json(info)

    def _api_albums(self):
        self._json({'albums': _bridge.list_albums()})

    def _api_search(self, p):
        q = p.get('q', '')
        if not q:
            return self._send_error(400, 'Missing "q" parameter')
        self._json(_bridge.list_assets(
            search=q, media_type=p.get('type'),
            sort=p.get('sort', 'date_desc'),
            offset=int(p.get('offset', 0)), limit=int(p.get('limit', 100))))

    def _api_refresh(self):
        _bridge.invalidate_cache()
        _bridge.start_background_load()
        self._json({'refreshed': True, 'status': 'reloading'})

    def _api_logs(self, which):
        """Serve current or previous session log as plain text."""
        if which == 'previous':
            path = _PREV_LOG_PATH
            label = 'Previous session log'
        else:
            path = _LOG_PATH
            label = 'Current session log'

        content = _read_log(path, tail_lines=500)
        if content is None:
            return self._send_error(404, 'No {} available'.format(label.lower()))

        body = content.encode('utf-8')
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- Media ---

    def _serve_media(self, aid, quality):
        info = _bridge.get_asset_info(aid)
        if not info:
            return self._send_error(404, 'Asset not found')

        mt = info.get('media_type', '')

        if quality in ('thumb', 'preview'):
            if mt == 'image':
                result = _bridge.get_photo_data(aid, quality=quality)
            else:
                ms = 300 if quality == 'thumb' else 1200
                result = _bridge.get_video_thumbnail(aid, max_size=ms)
            if not result:
                return self._send_error(404, 'Thumbnail generation failed')
            data, fn, ct = result
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.end_headers()
            self.wfile.write(data)
            return

        if mt == 'image':
            result = _bridge.get_photo_data(aid, quality='full')
            if not result:
                return self._send_error(404, 'Photo read failed')
            data, fn, ct = result
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Disposition', 'inline; filename="{}"'.format(fn))
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.end_headers()
            self.wfile.write(data)
        elif mt == 'video':
            self._serve_video(aid)

    def _serve_video(self, aid):
        result = _bridge.get_video_data(aid)
        # v1.3.1: too-large sentinel → HTTP 413 Payload Too Large
        if result == _VIDEO_TOO_LARGE:
            return self._send_error(
                413, 'Video exceeds 500 MB limit (use thumbnail/preview)')
        if not result:
            return self._send_error(404, 'Video export failed')
        fpath, fn, ct, fsz = result

        rh = self.headers.get('Range')
        if rh:
            m = re.match(r'bytes=(\d+)-(\d*)', rh)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else fsz - 1
                end = min(end, fsz - 1)
                length = end - start + 1
                self.send_response(206)
                self._cors()
                self.send_header('Content-Type', ct)
                self.send_header('Content-Length', str(length))
                self.send_header('Content-Range', 'bytes {}-{}/{}'.format(start, end, fsz))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Disposition', 'inline; filename="{}"'.format(fn))
                self.end_headers()
                with open(fpath, 'rb') as f:
                    f.seek(start)
                    rem = length
                    while rem > 0:
                        chunk = f.read(min(65536, rem))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        rem -= len(chunk)
                return

        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(fsz))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Disposition', 'inline; filename="{}"'.format(fn))
        self.end_headers()
        with open(fpath, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # --- HTML UI ---

    def _html_index(self, params):
        s = _bridge.get_status()
        loading_banner = ''
        if s.get('loading'):
            loading_banner = '''
            <div style="background:#e65100;color:#fff;padding:12px;border-radius:8px;margin:16px 0;">
                &#9203; Loading library... {}</div>'''.format(
                    getattr(_bridge, '_load_progress', ''))

        try:
            albums = _bridge.list_albums()
        except Exception:
            albums = []
        arows = ''
        for a in albums:
            try:
                arows += '<tr><td><a href="/browse?album={name}">{name}</a></td>' \
                         '<td>{count}</td><td>{type}</td></tr>'.format(**a)
            except Exception:
                pass
        self._html_resp(HTML_TPL.format(title='iOS Photo Server', body='''
            <h1>iOS Photo Server</h1>{lb}
            <div class="stats">
                <div class="stat"><span class="num">{p}</span><br>Photos</div>
                <div class="stat"><span class="num">{v}</span><br>Videos</div>
                <div class="stat"><span class="num">{t}</span><br>Total</div>
            </div>
            <h2>Browse</h2>
            <ul><li><a href="/browse?type=photo">All Photos</a></li>
            <li><a href="/browse?type=video">All Videos</a></li>
            <li><a href="/browse">All Media</a></li></ul>
            <h2>Albums</h2>
            <table><tr><th>Name</th><th>Count</th><th>Type</th></tr>{ar}</table>
            <h2>API</h2>
            <ul class="api-links">
            <li><code><a href="/api/status">GET /api/status</a></code></li>
            <li><code><a href="/api/assets">GET /api/assets</a></code></li>
            <li><code><a href="/api/albums">GET /api/albums</a></code></li>
            <li><code><a href="/api/assets?type=photo&amp;limit=10">GET /api/assets?type=photo&amp;limit=10</a></code></li>
            </ul>'''.format(
                p=s.get('photo_count', 0), v=s.get('video_count', 0),
                t=s.get('total_count', 0), ar=arows, lb=loading_banner)))

    def _html_browse(self, params):
        r = _bridge.list_assets(
            media_type=params.get('type'), album=params.get('album'),
            after=params.get('after'), before=params.get('before'),
            sort=params.get('sort', 'date_desc'),
            offset=int(params.get('offset', 0)), limit=int(params.get('limit', 50)))
        parts = []
        if params.get('type'):
            parts.append(params['type'].title() + 's')
        if params.get('album'):
            parts.append(params['album'])
        title = ' - '.join(parts) or 'All Media'

        items = ''
        for a in r.get('assets', []):
            badge = ''
            if a.get('media_type') == 'video':
                d = a.get('duration', 0) or 0
                mi, se = divmod(int(d), 60)
                badge = '<span class="badge">&#9654; {}:{:02d}</span>'.format(mi, se)
            elif a.get('favorite'):
                badge = '<span class="badge fav">&#9733;</span>'
            urls = a.get('urls', {})
            items += '<div class="item"><a href="{}" target="_blank"><img src="{}" loading="lazy" alt="{}">{}</a><div class="caption">{}<br><small>{}</small></div></div>'.format(
                urls.get('full', '#'), urls.get('thumb', '#'),
                a.get('filename', ''), badge,
                a.get('filename', ''), (a.get('creation_date') or '')[:10])

        off = r.get('offset', 0)
        lim = r.get('limit', 50)
        tot = r.get('total', 0)
        bqs = '&'.join('{}={}'.format(k, v) for k, v in params.items() if k != 'offset')
        nav = ''
        if off > 0:
            nav += '<a href="/browse?{}&offset={}">&#8592; Prev</a> '.format(bqs, max(0, off-lim))
        nav += '<span>{}-{} of {}</span>'.format(off+1, min(off+lim, tot), tot)
        if off + lim < tot:
            nav += ' <a href="/browse?{}&offset={}">Next &#8594;</a>'.format(bqs, off+lim)

        self._html_resp(HTML_TPL.format(title=title, body='''
            <h1>{t}</h1><div class="nav">{n}</div>
            <div class="grid">{i}</div><div class="nav">{n}</div>
            '''.format(t=title, n=nav, i=items)))


HTML_TPL = '''<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 16px;
       background: #1a1a2e; color: #e0e0e0; }}
a {{ color: #64b5f6; }} h1 {{ color: #fff; margin-top: 0; }}
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
.item .badge {{ position: absolute; top: 6px; right: 6px; background: rgba(0,0,0,.7);
               color: #fff; padding: 2px 6px; border-radius: 3px; font-size: 12px; }}
.item .badge.fav {{ color: #ffd700; }}
.caption {{ padding: 4px 8px; font-size: 11px; white-space: nowrap; overflow: hidden;
           text-overflow: ellipsis; }}
.nav {{ margin: 12px 0; display: flex; gap: 16px; align-items: center; }}
.nav a {{ background: #16213e; padding: 6px 14px; border-radius: 4px; text-decoration: none; }}
</style></head><body>{body}</body></html>'''


class PhotoHTTPServer(HTTPServer):
    def __init__(self, *a, **kw):
        self.start_time = time.time()
        super().__init__(*a, **kw)


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def start_server(bridge, host='0.0.0.0', port=3691):
    global _bridge
    _bridge = bridge
    server = PhotoHTTPServer((host, port), PhotoRequestHandler)
    ip = get_local_ip()
    print()
    print('=' * 50)
    print('  iOS Photo Server v{} running'.format(__version__))
    print('  Local:   http://127.0.0.1:{}'.format(port))
    print('  Network: http://{}:{}'.format(ip, port))
    print()
    print('  API:     http://{}:{}/api/status'.format(ip, port))
    print('  Browse:  http://{}:{}/'.format(ip, port))
    print('=' * 50)
    print()
    return server


# =====================================================================
# PART 3: LAUNCHER
# =====================================================================

def print_qr(url):
    try:
        import segno
        qr = segno.make(url)
        print()
        for row in qr.matrix:
            print('  ' + ''.join('\u2588\u2588' if c else '  ' for c in row))
        print()
        return
    except ImportError:
        pass
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        print()
        return
    except ImportError:
        pass
    print()
    print('  +' + '-' * (len(url) + 2) + '+')
    print('  | {} |'.format(url))
    print('  +' + '-' * (len(url) + 2) + '+')
    print()


def main():
    # Set up logging FIRST — before anything else.
    # This ensures crash logs are captured even if startup fails.
    _setup_logging()

    print('[*] iOS Photo Server v{} starting...'.format(__version__))

    # Parse args carefully — Pythonista may pass unexpected sys.argv
    port = 3691
    host = '0.0.0.0'
    stub = False
    try:
        args = sys.argv[1:]
        i = 0
        while i < len(args):
            if args[i] == '--port' and i + 1 < len(args):
                port = int(args[i + 1])
                i += 2
            elif args[i] == '--host' and i + 1 < len(args):
                host = args[i + 1]
                i += 2
            elif args[i] == '--stub':
                stub = True
                i += 1
            else:
                i += 1
    except Exception:
        pass

    print('[*] Creating bridge...')
    bridge = create_bridge(stub=stub)

    print('[*] Checking photo library access...')
    if not bridge.check_permission():
        print()
        print('[!] Photo library access denied.')
        print('    Go to Settings > Privacy & Security > Photos > Pythonista')
        print('    and grant "Full Access", then run again.')
        print()
        return

    print('[*] Permission granted!')

    # =========================================================
    # KEY CHANGE in v1.2.0: Start server FIRST, load library AFTER.
    # This way the server is reachable even if enumeration crashes.
    # =========================================================

    print('[*] Starting HTTP server...')
    try:
        server = start_server(bridge, host=host, port=port)
    except Exception as e:
        print('[!] Failed to start server: {}'.format(e))
        return

    url = 'http://{}:{}'.format(get_local_ip(), port)
    print_qr(url)

    # Start enumeration in background
    print('[*] Server is UP. Now loading photo library in background...')
    print('[*] You can already access /api/status to check progress.')
    print()
    bridge.start_background_load()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print('[!] Server error: {}'.format(e))
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            bridge.cleanup()
        except Exception:
            pass
        print('[*] Server stopped.')


if __name__ == '__main__':
    main()
