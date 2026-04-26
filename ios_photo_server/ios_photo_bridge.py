# ios_photo_bridge.py — Pythonista photo library abstraction
# Wraps the `photos` and `objc_util` modules to provide a clean API
# for listing, filtering, and serving iOS photo library assets.
#
# Designed to run inside Pythonista 3 on iOS.
# On non-iOS platforms, a stub mode is available for development/testing.

import os
import io
import time
import threading
import tempfile
from datetime import datetime

try:
    import photos
    from objc_util import ObjCInstance, ObjCClass, ns
    HAS_PHOTOS = True
except ImportError:
    HAS_PHOTOS = False

# ObjC classes we need for video export and file size
if HAS_PHOTOS:
    PHImageManager = ObjCClass('PHImageManager')
    PHVideoRequestOptions = ObjCClass('PHVideoRequestOptions')
    PHImageRequestOptions = ObjCClass('PHImageRequestOptions')
    NSFileManager = ObjCClass('NSFileManager')


class PhotoBridge:
    """Bridge to the iOS photo library via Pythonista's photos module."""

    def __init__(self):
        self._assets_cache = None
        self._albums_cache = None
        self._cache_time = 0
        self._cache_ttl = 30  # seconds before re-enumerating
        self._video_temp_dir = os.path.join(tempfile.gettempdir(), 'photo_server_videos')
        self._video_export_locks = {}  # local_id -> threading.Event
        self._lock = threading.Lock()
        os.makedirs(self._video_temp_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Permission
    # ------------------------------------------------------------------

    def check_permission(self):
        """Check if photo library access is available.
        On first call, iOS will show the permission dialog.
        Returns True if we can access photos, False if denied."""
        if not HAS_PHOTOS:
            return False
        try:
            # This triggers the iOS permission dialog on first call.
            # If the user denied, it returns an empty list silently.
            assets = photos.get_assets(media_type='image')
            # Heuristic: if the library is truly empty that's unusual
            # but possible. We can't distinguish "denied" from "empty"
            # without dropping to PHPhotoLibrary.authorizationStatus,
            # which we do below.
            return self._check_auth_status()
        except Exception:
            return False

    def _check_auth_status(self):
        """Use ObjC to check PHAuthorizationStatus directly."""
        if not HAS_PHOTOS:
            return False
        try:
            PHPhotoLibrary = ObjCClass('PHPhotoLibrary')
            # 0=NotDetermined, 1=Restricted, 2=Denied, 3=Authorized, 4=Limited
            status = PHPhotoLibrary.authorizationStatus()
            return status in (3, 4)  # Authorized or Limited
        except Exception:
            # Fallback: assume OK if we got here
            return True

    # ------------------------------------------------------------------
    # Asset enumeration
    # ------------------------------------------------------------------

    def _refresh_cache(self, force=False):
        """Reload asset list from the photo library if stale."""
        now = time.time()
        if not force and self._assets_cache is not None and (now - self._cache_time) < self._cache_ttl:
            return
        with self._lock:
            # Double-check after acquiring lock
            if not force and self._assets_cache is not None and (now - self._cache_time) < self._cache_ttl:
                return
            raw_assets = photos.get_assets(media_type=None, include_hidden=False)
            self._assets_cache = {}
            for asset in raw_assets:
                info = self._extract_metadata(asset)
                self._assets_cache[info['id']] = (asset, info)
            self._cache_time = time.time()
            # Invalidate albums cache too
            self._albums_cache = None

    def _extract_metadata(self, asset):
        """Extract metadata dict from a Pythonista Asset object."""
        local_id = asset.local_id

        # Get original filename via ObjC bridge
        filename = self._get_filename(asset)

        # Get creation/modification dates
        cdate = asset.creation_date
        mdate = asset.modification_date

        # Location
        loc = asset.location
        location = None
        if loc and 'latitude' in loc and 'longitude' in loc:
            location = {
                'latitude': loc['latitude'],
                'longitude': loc['longitude'],
            }
            if 'altitude' in loc:
                location['altitude'] = loc['altitude']

        info = {
            'id': local_id,
            'filename': filename,
            'media_type': asset.media_type,  # 'image' or 'video'
            'width': asset.pixel_width,
            'height': asset.pixel_height,
            'creation_date': cdate.isoformat() if cdate else None,
            'modification_date': mdate.isoformat() if mdate else None,
            'duration': asset.duration if asset.media_type == 'video' else None,
            'favorite': asset.favorite,
            'hidden': asset.hidden,
            'media_subtypes': asset.media_subtypes,
            'location': location,
        }

        # Build convenience URLs (server will use these as route patterns)
        safe_id = local_id.split('/')[0]  # Use just the UUID part
        info['urls'] = {
            'full': '/media/{}/full'.format(safe_id),
            'thumb': '/media/{}/thumb'.format(safe_id),
            'preview': '/media/{}/preview'.format(safe_id),
        }

        return info

    def _get_filename(self, asset):
        """Get original filename (e.g. IMG_1234.HEIC) via ObjC bridge."""
        try:
            objc_asset = ObjCInstance(asset)
            # PHAsset doesn't have .filename directly, but the underlying
            # resource has it. We access via valueForKey:
            resources = ObjCClass('PHAssetResource').assetResourcesForAsset_(objc_asset)
            if resources and resources.count() > 0:
                first = resources.objectAtIndex_(0)
                return str(first.originalFilename())
        except Exception:
            pass
        # Fallback: construct from type and date
        ext = 'jpg' if asset.media_type == 'image' else 'mp4'
        if asset.creation_date:
            ts = asset.creation_date.strftime('%Y%m%d_%H%M%S')
        else:
            ts = 'unknown'
        return '{}_{}.{}'.format(asset.media_type.upper(), ts, ext)

    # ------------------------------------------------------------------
    # Listing / filtering
    # ------------------------------------------------------------------

    def get_status(self):
        """Return library summary stats."""
        self._refresh_cache()
        photo_count = sum(1 for _, info in self._assets_cache.values() if info['media_type'] == 'image')
        video_count = sum(1 for _, info in self._assets_cache.values() if info['media_type'] == 'video')
        return {
            'photo_count': photo_count,
            'video_count': video_count,
            'total_count': len(self._assets_cache),
        }

    def list_assets(self, media_type=None, album=None, after=None, before=None,
                    sort='date_desc', offset=0, limit=100, favorite=None, search=None):
        """List assets with filtering, sorting, and pagination.

        Args:
            media_type: 'photo', 'video', or None for all
            album: album name to filter by
            after: ISO date string, assets created after this date
            before: ISO date string, assets created before this date
            sort: 'date_asc', 'date_desc', 'size_asc', 'size_desc'
            offset: pagination offset
            limit: page size (capped at 500)
            favorite: if True, only favorites; if False, only non-favorites; None=all
            search: filename substring search (case-insensitive)

        Returns:
            dict with 'total', 'offset', 'limit', 'assets'
        """
        self._refresh_cache()
        limit = min(limit, 500)

        # Get album asset IDs if filtering by album
        album_ids = None
        if album:
            album_ids = self._get_album_asset_ids(album)
            if album_ids is None:
                return {'total': 0, 'offset': offset, 'limit': limit, 'assets': []}

        # Parse date filters
        after_dt = _parse_date(after) if after else None
        before_dt = _parse_date(before) if before else None

        # Map 'photo'/'video' to Pythonista's type strings
        type_filter = None
        if media_type == 'photo':
            type_filter = 'image'
        elif media_type == 'video':
            type_filter = 'video'

        # Filter
        filtered = []
        for local_id, (asset, info) in self._assets_cache.items():
            if type_filter and info['media_type'] != type_filter:
                continue
            if album_ids is not None and local_id not in album_ids:
                continue
            if favorite is not None and info['favorite'] != favorite:
                continue
            if search and search.lower() not in info['filename'].lower():
                continue
            if after_dt and info['creation_date']:
                if info['creation_date'] < after_dt.isoformat():
                    continue
            if before_dt and info['creation_date']:
                if info['creation_date'] > before_dt.isoformat():
                    continue
            filtered.append(info)

        # Sort
        reverse = sort.endswith('_desc')
        if sort.startswith('date'):
            key = lambda x: x.get('creation_date') or ''
        elif sort.startswith('size'):
            key = lambda x: (x.get('width', 0) * x.get('height', 0))
        else:
            key = lambda x: x.get('creation_date') or ''
        filtered.sort(key=key, reverse=reverse)

        total = len(filtered)
        page = filtered[offset:offset + limit]

        return {
            'total': total,
            'offset': offset,
            'limit': limit,
            'assets': page,
        }

    def get_asset_info(self, asset_id):
        """Get metadata for a single asset by ID (or ID prefix).
        Returns the info dict or None if not found."""
        self._refresh_cache()
        # Try exact match first
        entry = self._assets_cache.get(asset_id)
        if entry:
            return entry[1]
        # Try prefix match (we use UUID prefix in URLs)
        for local_id, (asset, info) in self._assets_cache.items():
            if local_id.startswith(asset_id):
                return info
        return None

    def _resolve_asset(self, asset_id):
        """Resolve an asset_id (exact or prefix) to (asset, info) tuple."""
        self._refresh_cache()
        entry = self._assets_cache.get(asset_id)
        if entry:
            return entry
        for local_id, (asset, info) in self._assets_cache.items():
            if local_id.startswith(asset_id):
                return (asset, info)
        return None

    # ------------------------------------------------------------------
    # Albums
    # ------------------------------------------------------------------

    def list_albums(self):
        """List all albums with metadata."""
        self._refresh_cache()
        if self._albums_cache is not None:
            return self._albums_cache

        result = []

        # User albums
        try:
            for album in photos.get_albums():
                result.append({
                    'name': album.title,
                    'count': len(album.assets),
                    'type': 'user',
                    'local_id': album.local_id,
                })
        except Exception:
            pass

        # Smart albums
        try:
            for album in photos.get_smart_albums():
                count = len(album.assets)
                if count > 0:  # Skip empty smart albums
                    result.append({
                        'name': album.title or album.subtype or 'Unknown',
                        'count': count,
                        'type': 'smart',
                        'subtype': album.subtype,
                        'local_id': album.local_id,
                    })
        except Exception:
            pass

        self._albums_cache = result
        return result

    def _get_album_asset_ids(self, album_name):
        """Get set of asset local_ids belonging to a named album."""
        try:
            for album in photos.get_albums():
                if album.title == album_name:
                    return {a.local_id for a in album.assets}
            for album in photos.get_smart_albums():
                name = album.title or album.subtype or ''
                if name == album_name:
                    return {a.local_id for a in album.assets}
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Media data retrieval
    # ------------------------------------------------------------------

    def get_photo_data(self, asset_id, quality='full'):
        """Get photo data as JPEG bytes.

        Args:
            asset_id: local_id or prefix
            quality: 'full', 'preview' (max 1200px), or 'thumb' (max 300px)

        Returns:
            (jpeg_bytes, filename, content_type) or None if not found
        """
        resolved = self._resolve_asset(asset_id)
        if not resolved:
            return None
        asset, info = resolved

        if info['media_type'] != 'image':
            return None

        try:
            if quality == 'thumb':
                return self._get_thumbnail(asset, info, max_size=300)
            elif quality == 'preview':
                return self._get_thumbnail(asset, info, max_size=1200)
            else:
                # Full resolution
                data = asset.get_image_data(original=True)
                content_type = 'image/jpeg'
                # Check UTI for actual type
                uti = getattr(data, 'uti', None)
                if uti and 'png' in str(uti).lower():
                    content_type = 'image/png'
                return (data.getvalue(), info['filename'], content_type)
        except Exception as e:
            print('[PhotoBridge] Error getting photo {}: {}'.format(asset_id, e))
            return None

    def _get_thumbnail(self, asset, info, max_size=300):
        """Generate a thumbnail using Pythonista's get_ui_image + PIL."""
        try:
            # Use get_ui_image for efficient thumbnail generation
            # It uses iOS's built-in thumbnail cache
            w, h = info['width'], info['height']
            if w == 0 or h == 0:
                w, h = max_size, max_size

            # Calculate target size maintaining aspect ratio
            scale = min(max_size / w, max_size / h, 1.0)
            tw = int(w * scale)
            th = int(h * scale)

            ui_img = asset.get_ui_image(size=(tw, th), crop=False)
            if ui_img is None:
                # Fallback: use get_image (PIL) and resize
                return self._get_thumbnail_pil(asset, info, max_size)

            # Convert ui.Image to JPEG bytes
            from io import BytesIO
            import ui
            png_data = ui_img.to_png()
            return (png_data, info['filename'], 'image/png')
        except Exception:
            return self._get_thumbnail_pil(asset, info, max_size)

    def _get_thumbnail_pil(self, asset, info, max_size):
        """Fallback thumbnail generation using PIL."""
        try:
            img = asset.get_image(original=False)
            img.thumbnail((max_size, max_size))
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=80)
            return (buf.getvalue(), info['filename'], 'image/jpeg')
        except Exception as e:
            print('[PhotoBridge] Thumbnail fallback failed: {}'.format(e))
            return None

    def get_video_data(self, asset_id):
        """Get video file path for streaming.

        Videos must be exported to a temp file first via PHImageManager.

        Returns:
            (file_path, filename, content_type, file_size) or None
        """
        resolved = self._resolve_asset(asset_id)
        if not resolved:
            return None
        asset, info = resolved

        if info['media_type'] != 'video':
            return None

        # Check if already exported
        safe_id = info['id'].split('/')[0]
        temp_path = os.path.join(self._video_temp_dir, safe_id + '.mp4')
        if os.path.exists(temp_path):
            sz = os.path.getsize(temp_path)
            if sz > 0:
                return (temp_path, info['filename'], 'video/mp4', sz)

        # Export via ObjC
        try:
            return self._export_video(asset, info, temp_path)
        except Exception as e:
            print('[PhotoBridge] Video export failed for {}: {}'.format(asset_id, e))
            return None

    def _export_video(self, asset, info, temp_path):
        """Export video to temp file using PHImageManager."""
        export_done = threading.Event()
        result = [None]  # Mutable container for callback result

        objc_asset = ObjCInstance(asset)
        manager = PHImageManager.defaultManager()
        options = PHVideoRequestOptions.alloc().init()
        options.setVersion_(1)  # PHVideoRequestOptionsVersionCurrent
        options.setDeliveryMode_(1)  # HighQuality
        options.setNetworkAccessAllowed_(True)  # Allow iCloud download

        def _handler(_obj_export_session, _obj_info):
            try:
                if _obj_export_session is None:
                    export_done.set()
                    return

                export_session = ObjCInstance(_obj_export_session)
                output_url = ns(temp_path)
                file_url = ObjCClass('NSURL').fileURLWithPath_(output_url)
                export_session.setOutputURL_(file_url)
                export_session.setOutputFileType_(ns('com.apple.quicktime-movie'))

                # Synchronous export within the callback
                wait_event = threading.Event()

                def _export_complete():
                    wait_event.set()

                export_session.exportAsynchronouslyWithCompletionHandler_(_export_complete)
                wait_event.wait(timeout=120)  # 2 min max for large videos

                if os.path.exists(temp_path):
                    sz = os.path.getsize(temp_path)
                    result[0] = (temp_path, info['filename'], 'video/mp4', sz)
            except Exception as e:
                print('[PhotoBridge] Export handler error: {}'.format(e))
            finally:
                export_done.set()

        manager.requestExportSessionForVideo_options_exportPreset_resultHandler_(
            objc_asset, options, ns('AVAssetExportPresetPassthrough'), _handler
        )

        export_done.wait(timeout=180)  # 3 min total timeout
        return result[0]

    def get_video_thumbnail(self, asset_id, max_size=300):
        """Get a thumbnail frame from a video asset.

        Returns:
            (jpeg_bytes, filename, content_type) or None
        """
        resolved = self._resolve_asset(asset_id)
        if not resolved:
            return None
        asset, info = resolved

        if info['media_type'] != 'video':
            return None

        # For videos, get_ui_image returns a preview frame
        try:
            w, h = info['width'], info['height']
            if w == 0 or h == 0:
                w, h = max_size, max_size
            scale = min(max_size / w, max_size / h, 1.0)
            tw, th = int(w * scale), int(h * scale)

            ui_img = asset.get_ui_image(size=(tw, th), crop=False)
            if ui_img:
                png_data = ui_img.to_png()
                thumb_name = os.path.splitext(info['filename'])[0] + '_thumb.png'
                return (png_data, thumb_name, 'image/png')
        except Exception:
            pass

        # Fallback: use get_image (PIL)
        try:
            img = asset.get_image(original=False)
            if img:
                img.thumbnail((max_size, max_size))
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=80)
                thumb_name = os.path.splitext(info['filename'])[0] + '_thumb.jpg'
                return (buf.getvalue(), thumb_name, 'image/jpeg')
        except Exception as e:
            print('[PhotoBridge] Video thumbnail failed: {}'.format(e))

        return None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self):
        """Remove temporary video export files."""
        try:
            import shutil
            if os.path.exists(self._video_temp_dir):
                shutil.rmtree(self._video_temp_dir)
                print('[PhotoBridge] Cleaned up temp video files')
        except Exception as e:
            print('[PhotoBridge] Cleanup error: {}'.format(e))

    def invalidate_cache(self):
        """Force re-enumeration on next access."""
        with self._lock:
            self._assets_cache = None
            self._albums_cache = None
            self._cache_time = 0


# ------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------

def _parse_date(s):
    """Parse an ISO date or datetime string."""
    if not s:
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------
# Stub for development/testing on non-iOS platforms
# ------------------------------------------------------------------

class StubPhotoBridge:
    """Stub implementation for testing on desktop.
    Generates fake assets so the server can be developed without iOS."""

    def __init__(self, stub_dir=None):
        self._stub_dir = stub_dir or '/tmp/photo_server_stubs'
        os.makedirs(self._stub_dir, exist_ok=True)
        self._generate_stubs()

    def _generate_stubs(self):
        """Create some fake image files for testing."""
        self._assets = []
        try:
            from PIL import Image
            has_pil = True
        except ImportError:
            has_pil = False

        for i in range(25):
            asset_id = 'STUB-{:04d}-0000-0000-000000000000'.format(i)
            is_video = i % 5 == 0  # Every 5th is a video
            ext = 'mp4' if is_video else 'jpg'
            filename = 'IMG_{:04d}.{}'.format(1000 + i, ext.upper())
            cdate = datetime(2025, 12, 1 + (i % 28), 10, i, 0)

            # Create a stub file
            stub_path = os.path.join(self._stub_dir, filename)
            if not os.path.exists(stub_path):
                if is_video:
                    with open(stub_path, 'wb') as f:
                        f.write(b'\x00' * 1024)  # Fake video
                elif has_pil:
                    img = Image.new('RGB', (400, 300),
                                    color=((i * 37) % 256, (i * 73) % 256, (i * 113) % 256))
                    img.save(stub_path, 'JPEG')
                else:
                    with open(stub_path, 'wb') as f:
                        f.write(b'\xff\xd8\xff\xe0' + b'\x00' * 100)  # Minimal JPEG header

            sz = os.path.getsize(stub_path)
            safe_id = asset_id.split('/')[0]
            self._assets.append({
                'id': asset_id,
                'filename': filename,
                'media_type': 'video' if is_video else 'image',
                'width': 1920 if is_video else 4032,
                'height': 1080 if is_video else 3024,
                'creation_date': cdate.isoformat(),
                'modification_date': cdate.isoformat(),
                'duration': 15.0 + i if is_video else None,
                'favorite': i % 7 == 0,
                'hidden': False,
                'media_subtypes': [],
                'location': {'latitude': 40.7 + i * 0.01, 'longitude': -74.0 + i * 0.01} if i % 3 == 0 else None,
                'urls': {
                    'full': '/media/{}/full'.format(safe_id),
                    'thumb': '/media/{}/thumb'.format(safe_id),
                    'preview': '/media/{}/preview'.format(safe_id),
                },
                '_stub_path': stub_path,
            })

    def check_permission(self):
        return True

    def get_status(self):
        photos = sum(1 for a in self._assets if a['media_type'] == 'image')
        videos = sum(1 for a in self._assets if a['media_type'] == 'video')
        return {'photo_count': photos, 'video_count': videos, 'total_count': len(self._assets)}

    def list_assets(self, media_type=None, album=None, after=None, before=None,
                    sort='date_desc', offset=0, limit=100, favorite=None, search=None):
        filtered = list(self._assets)
        if media_type == 'photo':
            filtered = [a for a in filtered if a['media_type'] == 'image']
        elif media_type == 'video':
            filtered = [a for a in filtered if a['media_type'] == 'video']
        if favorite is not None:
            filtered = [a for a in filtered if a['favorite'] == favorite]
        if search:
            filtered = [a for a in filtered if search.lower() in a['filename'].lower()]
        if after:
            after_dt = _parse_date(after)
            if after_dt:
                filtered = [a for a in filtered if a['creation_date'] >= after_dt.isoformat()]
        if before:
            before_dt = _parse_date(before)
            if before_dt:
                filtered = [a for a in filtered if a['creation_date'] <= before_dt.isoformat()]

        reverse = sort.endswith('_desc')
        if sort.startswith('date'):
            filtered.sort(key=lambda x: x.get('creation_date') or '', reverse=reverse)

        limit = min(limit, 500)
        total = len(filtered)
        page = filtered[offset:offset + limit]
        # Strip internal fields
        clean = [{k: v for k, v in a.items() if not k.startswith('_')} for a in page]
        return {'total': total, 'offset': offset, 'limit': limit, 'assets': clean}

    def get_asset_info(self, asset_id):
        for a in self._assets:
            if a['id'] == asset_id or a['id'].startswith(asset_id):
                return {k: v for k, v in a.items() if not k.startswith('_')}
        return None

    def list_albums(self):
        return [
            {'name': 'Camera Roll', 'count': len(self._assets), 'type': 'smart'},
            {'name': 'Favorites', 'count': sum(1 for a in self._assets if a['favorite']), 'type': 'smart'},
        ]

    def get_photo_data(self, asset_id, quality='full'):
        for a in self._assets:
            if (a['id'] == asset_id or a['id'].startswith(asset_id)) and a['media_type'] == 'image':
                path = a.get('_stub_path')
                if path and os.path.exists(path):
                    with open(path, 'rb') as f:
                        data = f.read()
                    if quality in ('thumb', 'preview'):
                        max_sz = 300 if quality == 'thumb' else 1200
                        try:
                            from PIL import Image
                            img = Image.open(io.BytesIO(data))
                            img.thumbnail((max_sz, max_sz))
                            buf = io.BytesIO()
                            img.save(buf, format='JPEG', quality=80)
                            return (buf.getvalue(), a['filename'], 'image/jpeg')
                        except ImportError:
                            pass
                    return (data, a['filename'], 'image/jpeg')
        return None

    def get_video_data(self, asset_id):
        for a in self._assets:
            if (a['id'] == asset_id or a['id'].startswith(asset_id)) and a['media_type'] == 'video':
                path = a.get('_stub_path')
                if path and os.path.exists(path):
                    return (path, a['filename'], 'video/mp4', os.path.getsize(path))
        return None

    def get_video_thumbnail(self, asset_id, max_size=300):
        # Return a simple colored rectangle for stubs
        for a in self._assets:
            if (a['id'] == asset_id or a['id'].startswith(asset_id)) and a['media_type'] == 'video':
                try:
                    from PIL import Image
                    img = Image.new('RGB', (max_size, int(max_size * 9 / 16)), color=(40, 40, 60))
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
    """Factory: return real PhotoBridge on iOS, StubPhotoBridge otherwise."""
    if stub or not HAS_PHOTOS:
        if not HAS_PHOTOS:
            print('[PhotoBridge] Not running on iOS/Pythonista — using stub mode')
        return StubPhotoBridge(stub_dir=stub_dir)
    return PhotoBridge()
