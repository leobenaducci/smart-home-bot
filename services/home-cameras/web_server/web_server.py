"""
Web Server Module - Optimized for GPU acceleration and high framerate
Provides REST API and WebSocket video streaming for camera system
"""

import hashlib
import hmac
import os
import shutil
import secrets
import uuid

from flask import Flask, request, jsonify, Response, render_template, send_file
from flask_socketio import SocketIO, emit
import cv2
import threading
import asyncio
import time
import base64
from datetime import datetime
from typing import Dict, Optional, Tuple
import logging
import traceback
from collections import OrderedDict
import re
import numpy as np
import queue

# Import H.264 encoder
try:
    from h264_encoder import H264Encoder, H264EncoderConfig, H264StreamGenerator
    H264_ENCODING_AVAILABLE = True
except ImportError:
    logging.warning("H.264 encoder not available. Install PyAV for H.264 encoding support.")
    H264_ENCODING_AVAILABLE = False
    H264Encoder = None
    H264EncoderConfig = None
    H264StreamGenerator = None

# Import camera manager and motion detector
from camera_manager import CameraManager, CameraConfig, CameraState
from enhanced_motion_detector import EnhancedMotionDetector, EnhancedMotionEvent
from security_state import PRESENCE_HOLD_SECONDS, SecurityPublisher

# Presence from tracks, for the security system -- filled in beside init_mqtt.
# Declared here, at import, because the init runs before the rest of the
# module's globals and a default placed below it would reset it to None.
security_publisher: Optional[SecurityPublisher] = None
from motion_detector import MotionZone, full_frame_zone
from object_detector import DetectedObject
from settings_manager import SettingsManager
from fcm_service import get_fcm_service, init_fcm_service
import recording as rec_module
from recording import (
    add_to_ring_buffer, drain_ring_buffer, is_recording,
    start_recording, add_frame_to_recording, DetectionInfo,
)
from mqtt_client import init_mqtt, get_mqtt

# Import camera registry client
import requests
import socketio as _sio_module

# Import recording module
from recording import (
    list_recordings,
    get_recording_path,
    delete_recording,
    RECORDINGS_DIR,
    save_snapshot,
    start_recording,
    review_queue_status,
    add_frame_to_recording,
    is_recording,
    stop_recording,
    get_active_recording_cameras,
    add_to_ring_buffer,
    drain_ring_buffer,
    get_default_duration,
    purge_orphan_snapshots,
    DetectionInfo,
)

# Resolve project root (where this file's parent's parent is)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGS_DIR = os.path.join(_PROJECT_ROOT, 'logs')
os.makedirs(_LOGS_DIR, exist_ok=True)

# Set up logging to both file and console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(_LOGS_DIR, 'web_server.log'), mode='w'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__, template_folder='templates', static_folder='static')


def _load_secret_key() -> str:
    """Session signing key: env var, else a random key persisted in the config dir.

    Never hardcode this — the key signs session cookies, so a known value lets
    anyone forge an authenticated session without ever hitting /login.
    """
    env_key = os.environ.get('FLASK_SECRET_KEY')
    if env_key:
        return env_key

    key_path = os.path.join(_PROJECT_ROOT, 'config', 'secret_key')
    try:
        if os.path.exists(key_path):
            with open(key_path) as f:
                key = f.read().strip()
            if key:
                return key
        key = secrets.token_hex(32)
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        with open(key_path, 'w') as f:
            f.write(key)
        os.chmod(key_path, 0o600)
        logger.info("Generated a new session secret key")
        return key
    except Exception as e:
        # Falling back to an ephemeral key logs everyone out on restart, which is
        # far better than running with a predictable one.
        logger.error(f"Could not persist session secret key ({e}); using an ephemeral key")
        return secrets.token_hex(32)


app.config['SECRET_KEY'] = _load_secret_key()


# ---------------------------------------------------------------------------
# Trusted-proxy login (HomeCore)
# ---------------------------------------------------------------------------
# This app's own door is a single shared admin password: everyone who gets in
# is "admin", so it can never say *who* is looking. HomeCore knows, because the
# family logs in there — so when it proxies this app at /camaras/ it forwards
# the member's name alongside a token derived from a secret only it and this
# app hold: sha256("<master>:<username>"). A per-user derivation rather than
# the master itself, so what travels can only ever claim to be that one member.
#
# The same pair, and the same derivation, that finance_helper already accepts.
# Generate one with:
#   python -c "import hashlib;print(hashlib.sha256(b'<master>:<user>').hexdigest())"
PROXY_SHARED_SECRET = os.environ.get('PROXY_SHARED_SECRET', '')
if not PROXY_SHARED_SECRET:
    # Said out loud, because the symptom is otherwise a puzzle: the pages come
    # up, and the ones behind a login quietly ask for the shared admin
    # password even though HomeCore sent a perfectly good identity. That looks
    # like broken auth rather than a missing env var, and the recordings page
    # looks emptier still - its fetch gets the login page instead of JSON and
    # renders nothing at all.
    logger.warning(
        'PROXY_SHARED_SECRET is not set: HomeCore logins will not be accepted '
        'here, so pages proxied at /camaras/ will ask for the admin password. '
        'Set it to the same PROXY_SHARED_SECRET the portal holds (see '
        'web_server/docker-compose.yml).')

# Where this app is mounted when somebody reaches it through HomeCore, so the
# pages can write links that come back here instead of to HomeCore's root.
# Empty when it is reached directly, which is the whole point: the same
# templates serve both doors.
def _mount_prefix() -> str:
    prefix = (request.headers.get('X-Forwarded-Prefix') or '').rstrip('/')
    if not prefix.startswith('/') or '//' in prefix or '..' in prefix:
        return ''
    return prefix


@app.before_request
def _proxy_login():
    """Accept HomeCore's word for who this is, and log them in as themselves."""
    from flask import session
    if not PROXY_SHARED_SECRET:
        return
    user = request.headers.get('X-Proxy-User')
    secret = request.headers.get('X-Proxy-Secret')
    if not (user and secret) or any(c in user for c in ':/\\'):
        return
    expected = hashlib.sha256(f'{PROXY_SHARED_SECRET}:{user}'.encode()).hexdigest()
    if hmac.compare_digest(expected, secret):
        session['authenticated'] = True
        session['username'] = user
        session['via_proxy'] = True


@app.context_processor
def _inject_base():
    """`base` is '' on the LAN and '/camaras' through HomeCore. Every absolute
    path a template writes is prefixed with it, so one set of templates serves
    both without a build step or a rewrite pass over the HTML."""
    return {'base': _mount_prefix()}


# Initialize components
logger.info("Initializing components")
socketio = SocketIO(app, cors_allowed_origins="*")
camera_manager = CameraManager()
logger.info("CameraManager initialized")
motion_detector = EnhancedMotionDetector(
    detector_type="frame_diff",
    background_update_rate=5,
    enable_object_detection=True,
    object_detection_model=None,  # Auto-selects: yolov8x.pt (GPU) or yolov8s.pt (CPU)
    object_confidence_threshold=0.35
)
# Motion zone will be set dynamically based on actual camera frame resolution
logger.info("EnhancedMotionDetector initialized (zone will be set dynamically)")
settings_manager = SettingsManager()
logger.info("SettingsManager initialized")

# Initialize FCM service (optional - only if Firebase is configured)
fcm_service = None
FCM_SERVICE_ACCOUNT_PATH = None  # Set this to your Firebase service account JSON path

def init_fcm():
    """Initialize FCM service if configured"""
    global fcm_service
    if FCM_SERVICE_ACCOUNT_PATH:
        try:
            fcm_service = init_fcm_service(FCM_SERVICE_ACCOUNT_PATH)
            logger.info("FCM service initialized")
        except Exception as e:
            logger.error(f"Failed to initialize FCM service: {e}")
    else:
        fcm_service = get_fcm_service()
        logger.info("FCM service initialized (not configured for push notifications)")

init_fcm()

# Initialize MQTT client (broker config comes from global settings / env vars)
def init_mqtt_client():
    gs = settings_manager.get_global_settings()
    host = gs.get('mqtt_broker_host', 'mqtt.home')
    port = int(gs.get('mqtt_broker_port', 1883))
    username = gs.get('mqtt_username') or None
    password = gs.get('mqtt_password') or None
    init_mqtt(broker_host=host, broker_port=port, username=username, password=password)
    # Presence from tracks, for the security system -- see security_state.py.
    global security_publisher
    security_publisher = SecurityPublisher(get_mqtt)
    logger.info(f"MQTT client initialized (broker: {host}:{port})")

init_mqtt_client()

# Track active recording metadata (filename / tags) so we can publish MQTT when done
_camera_recording_info: dict = {}

# Cameras whose current recording was started by an explicit API trigger. Those
# are exempt from the recording-window check that otherwise stops a recording the
# moment the window closes — a manual trigger means "record now", whatever time
# it is. Entries are cleared when the recording finishes.
_manual_recordings: set = set()

# Flag to track if cameras have been loaded
cameras_loaded = False

# IPs of cameras that came from the registry (not from cameras.json)
registry_camera_ips: set = set()

def resolve_camera_id(key: str) -> Optional[str]:
    """Resolve a camera_id from a camera_id, a device_ip, or the camera's name.

    Names were added 2026-09-20: every HTTP route here takes a `camera_key`,
    and the only thing a person — or an assistant relaying one — ever says is
    "the living room one". Before this, `/api/cameras/Living/rediscover`
    answered "Camera not found" about a camera sitting right there in the list.

    Order matters. Id and IP are matched first so that a camera someone named
    after an address cannot shadow the real one.

    Ambiguity returns None rather than picking. These keys reach `remove` as
    well as `rediscover`, and "it deleted the wrong camera" is not a mistake
    worth risking to save a round trip — the caller gets "not found" and can
    list and choose. Exact name beats partial for the same reason: "Living"
    is also a substring of "Living Room 2", and typing the full name of one
    camera must never be read as a vague reference to another.
    """
    if not key:
        return None
    if key in camera_manager.cameras:
        return key
    for cid, config in camera_manager.cameras.items():
        if config.device_ip == key:
            return cid

    def _name(c):
        return (getattr(c, 'name', '') or '').strip().lower()

    want = key.strip().lower()
    exact = [cid for cid, c in camera_manager.cameras.items() if _name(c) == want]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    partial = [cid for cid, c in camera_manager.cameras.items()
               if want and want in _name(c)]
    return partial[0] if len(partial) == 1 else None

def resolve_camera_config(key: str) -> Optional[CameraConfig]:
    """Resolve a CameraConfig by camera_id or device_ip."""
    cid = resolve_camera_id(key)
    return camera_manager.cameras.get(cid) if cid else None

# SocketIO client that subscribes to push events from the camera registry
_registry_event_client = _sio_module.Client(reconnection=True, reconnection_delay=5, reconnection_attempts=0)

@_registry_event_client.on('camera_registered')
def _on_camera_registered(data):
    logger.info(f"Registry push: camera registered {data.get('ip_address')}, syncing immediately")
    try:
        sync_registry_cameras()
    except Exception as e:
        logger.error(f"Sync after camera_registered event failed: {e}")

@_registry_event_client.on('cameras_offline')
def _on_cameras_offline(data):
    logger.info(f"Registry push: cameras offline {data.get('ip_addresses')}, syncing immediately")
    try:
        sync_registry_cameras()
    except Exception as e:
        logger.error(f"Sync after cameras_offline event failed: {e}")

def load_cameras_from_config(loop: asyncio.AbstractEventLoop = None):
    """Load cameras from config file and camera registry on startup"""
    global cameras_loaded
    if cameras_loaded:
        logger.info("Cameras already loaded, skipping")
        return
    logger.info("Loading cameras from config file and registry")
    cameras_loaded = True
    
    # Use provided loop or get the running one
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running event loop found, cameras will be loaded later")
            return
    
    # Load cameras from local config first
    cameras = settings_manager.get_cameras()
    logger.info(f"Loading {len(cameras)} cameras from config")
    for cam_id, config in cameras.items():
        try:
            device_ip = config.get('device_ip', cam_id)
            stream_type = config.get('stream_type', 'rtsp')
            camera_name = config.get('name', '')
            stored_camera_id = config.get('camera_id', cam_id)
            
            camera_config = CameraConfig(
                camera_id=stored_camera_id,
                device_ip=device_ip,
                username=config.get('username', ''),
                password=config.get('password', ''),
                port=int(config.get('port', 80)) if isinstance(config.get('port', 80), str) else config.get('port', 80),
                stream_path=config.get('stream_path', '/stream'),
                fps=int(config.get('fps', 30)) if isinstance(config.get('fps', 30), str) else config.get('fps', 30),
                width=int(config.get('width', 1920)) if isinstance(config.get('width', 1920), str) else config.get('width', 1920),
                height=int(config.get('height', 1080)) if isinstance(config.get('height', 1080), str) else config.get('height', 1080),
                rotation=int(config.get('rotation', 0)) if isinstance(config.get('rotation', 0), str) else config.get('rotation', 0),
                stream_type=stream_type,
                name=camera_name
            )
            logger.info(f"Loading camera {device_ip} with config: {camera_config}")
            camera_manager.add_camera(camera_config)
            cid = camera_config.camera_id
            
            # Load stuck region config if present
            stuck_region = config.get('stuck_region')
            if stuck_region and stuck_region.get('enabled'):
                stuck_region_config[cid] = stuck_region
                logger.info(f"Loaded stuck region config for {device_ip} ({cid})")
            
            logger.info(f"Loaded camera: {device_ip}")
        except Exception as e:
            logger.error(f"Failed to load camera {cam_id}: {e}")
    
    # Load cameras from registry service
    try:
        registry_cameras = get_registry_cameras()
        logger.info(f"Found {len(registry_cameras)} cameras in registry")
        
        for camera in registry_cameras:
            device_ip = camera.get('ip_address')
            if not device_ip:
                continue

            # Only load online cameras
            if camera.get('status') != 'online':
                logger.info(f"Skipping registry camera {device_ip} (status: {camera.get('status')})")
                continue

            # Skip if already loaded (check by IP)
            already_loaded = any(cfg.device_ip == device_ip for cfg in camera_manager.cameras.values())
            if already_loaded:
                logger.info(f"Camera {device_ip} already loaded, skipping registry entry")
                continue
                
            try:
                camera_config = _build_registry_camera_config(camera)
                logger.info(f"Loading registry camera {device_ip}")
                camera_manager.add_camera(camera_config)
                cid = camera_config.camera_id
                registry_camera_ips.add(cid)
                
                # Load stuck region config from saved settings
                registry_settings = settings_manager.get_registry_camera_setting(cid)
                stuck_region = registry_settings.get('stuck_region') if registry_settings else None
                if stuck_region and stuck_region.get('enabled'):
                    stuck_region_config[cid] = stuck_region
                    logger.info(f"Loaded stuck region config for registry camera {device_ip}")
                
                logger.info(f"Loaded registry camera: {device_ip}")
            except Exception as e:
                logger.error(f"Failed to load registry camera {device_ip}: {e}")
                
    except Exception as e:
        logger.error(f"Failed to load cameras from registry: {e}")
    
    # Emit camera list to all connected clients
    try:
        cameras = camera_manager.get_all_cameras()
        socketio.emit('camera_list', {cid: {
            'camera_id': config.camera_id,
            'device_ip': config.device_ip,
            'name': config.name,
            'username': config.username,
            'port': config.port,
            'stream_path': config.stream_path,
            'fps': config.fps,
            'width': config.width,
            'height': config.height,
            'stream_type': config.stream_type,
            'state': camera_manager.get_camera_state(cid).value if camera_manager.get_camera_state(cid) else 'unknown'
        } for cid, config in cameras.items()})
        logger.info("Emitted camera list to connected clients")
    except Exception as e:
        logger.error(f"Failed to emit camera list: {e}")


def _safe_next_url(candidate: Optional[str]) -> str:
    """Constrain post-login redirects to this site.

    'next' comes straight from the query string, so an absolute or
    protocol-relative URL would turn the login page into an open redirect.
    """
    if not candidate or not candidate.startswith('/') or candidate.startswith('//'):
        return '/'
    return candidate


# Check if user is authenticated
def is_authenticated():
    """Check if user is authenticated"""
    from flask import session
    return session.get('authenticated', False)


# Require authentication decorator
def require_auth(f):
    """Decorator to require authentication"""
    from functools import wraps
    from flask import session, redirect, request
    
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_authenticated():
            # Pass the original URL as 'next' parameter. `next` stays
            # unprefixed - it is this app's own path - while the redirect
            # itself carries the mount, so logging in through HomeCore lands
            # back inside /camaras/ instead of on the portal's own login.
            next_url = request.path
            return redirect(f'{_mount_prefix()}/login?next={next_url}')
        return f(*args, **kwargs)
    
    return decorated_function


def require_socket_auth(f):
    """Decorator to require session authentication for Socket.IO events."""
    from functools import wraps

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_authenticated():
            logger.warning(f"Unauthorized Socket.IO event blocked: {f.__name__}")
            if f.__name__ == 'handle_connect':
                return False
            emit('error', {'message': 'Authentication required'})
            return None
        return f(*args, **kwargs)

    return decorated_function


# Serve recordings page
@app.route('/recordings')
@require_auth
def recordings_page():
    """Serve recordings browser page"""
    return render_template('recordings.html')


# API endpoint to list recordings
@app.route('/api/recordings')
@require_auth
def api_list_recordings():
    """List recordings with optional filters"""
    rec_type = request.args.get('type')  # 'snapshot' or 'video'
    camera_ip = request.args.get('camera')
    limit = int(request.args.get('limit', 100))
    offset = int(request.args.get('offset', 0))
    camera_name = _get_camera_name(camera_ip) if camera_ip else None
    camera_config = resolve_camera_config(camera_ip)
    device_ip = camera_config.device_ip if camera_config else None
    recordings = list_recordings(rec_type=rec_type, camera_ip=camera_ip, limit=limit, offset=offset, camera_name=camera_name, device_ip=device_ip)
    return jsonify({'recordings': recordings})


# Serve recording files
@app.route('/recordings/<path:rel_path>')
@require_auth
def serve_recording(rel_path):
    """Serve a recording file (snapshot image or video)"""
    rec_type = request.args.get('type', '')
    filepath = get_recording_path(rel_path, rec_type)
    if not filepath or not os.path.exists(filepath):
        return "Recording not found", 404
    
    if filepath.endswith('.jpg'):
        mimetype = 'image/jpeg'
    else:
        mimetype = 'video/mp4'
    
    file_size = os.path.getsize(filepath)
    
    range_header = request.headers.get('Range')
    if range_header:
        match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1
            end = min(end, file_size - 1)
            content_length = end - start + 1
            
            def generate_range():
                with open(filepath, 'rb') as f:
                    f.seek(start)
                    remaining = content_length
                    while remaining > 0:
                        chunk = min(65536, remaining)
                        data = f.read(chunk)
                        if not data:
                            break
                        remaining -= len(data)
                        yield data
            
            response = Response(generate_range(), status=206, mimetype=mimetype)
            response.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
            response.headers['Content-Length'] = content_length
            response.headers['Content-Type'] = mimetype
            response.headers['Accept-Ranges'] = 'bytes'
            response.headers['Cache-Control'] = 'no-cache'
            return response
    
    response = send_file(filepath, mimetype=mimetype)
    response.headers['Accept-Ranges'] = 'bytes'
    response.headers['Cache-Control'] = 'no-cache'
    return response


# API endpoint to delete a recording
# --- Clips waiting for a person -----------------------------------------------
# What the reviewer thought was a shadow. Kept rather than deleted, and shown
# here, because "probably nothing" is a judgement a model made about a dark
# frame and the cost of it being wrong is the one clip somebody needed.

@app.route('/api/recordings/review')
@require_auth
def api_list_review():
    """The to_review tray, newest first, each with why it landed there."""
    import clip_review
    folder = clip_review.review_dir(RECORDINGS_DIR)
    # Same rule purge_old_reviews uses: a cutoff of zero is never delete.
    purges = clip_review.REVIEW_MAX_AGE_DAYS > 0
    items = []
    try:
        names = [n for n in os.listdir(folder) if n.endswith('.mp4')]
    except OSError:
        names = []
    for name in names:
        path = os.path.join(folder, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        verdict = clip_review.read_verdict(path) or {}
        # A max age of zero means purge_old_reviews never runs, so there is no
        # expiry to report. Say that with null rather than 0 — zero days reads
        # as "today", which is the opposite of "never".
        expires = None
        if purges:
            expires = max(0, clip_review.REVIEW_MAX_AGE_DAYS
                          - int((time.time() - stat.st_mtime) // 86400))
        items.append({
            'filename': name,
            'rel_path': f'{clip_review.REVIEW_DIRNAME}/{name}',
            # Bytes, like /api/recordings, so the page formats both lists with
            # the one helper instead of printing "0.0 MB" next to "812.3 KB".
            'size': stat.st_size,
            'size_mb': round(stat.st_size / (1024 * 1024), 1),
            'modified': stat.st_mtime,
            'why': verdict.get('what') or '',
            'certainty': verdict.get('certainty') or '',
            'kind': verdict.get('kind') or '',
            'error': verdict.get('error'),
            # So the page can say "3 days left" rather than making somebody
            # work out when a clip is going to disappear on its own.
            'expires_in_days': expires,
        })
    items.sort(key=lambda i: i['modified'], reverse=True)
    return jsonify({'recordings': items,
                    'max_age_days': clip_review.REVIEW_MAX_AGE_DAYS if purges else None,
                    'review': review_queue_status()})


@app.route('/api/recordings/<path:rel_path>/review', methods=['POST'])
@require_auth
def api_rereview_recording(rel_path: str):
    """Look at this clip again and rewrite what it says.

    A verdict is written once and kept, so a clip reviewed while something was
    broken carries that failure for as long as it exists — every clip judged
    before the frames were capped still reads "ollama 400" no matter how well
    the reviewer works now. This is how those get a second opinion.

    It deliberately does *not* move the file. The clip has already been filed
    and may well have been looked at; changing where it lives underneath
    somebody is a bigger thing than correcting a sentence, and the shelf is one
    click away in the list either way.
    """
    import clip_review
    # The same resolver the download and delete routes use — it is the one
    # that refuses to walk out of the recordings tree.
    path = get_recording_path(rel_path, 'video')
    if not path or not os.path.exists(path):
        return jsonify({'error': 'that recording is gone'}), 404
    verdict = clip_review.review_clip(path)
    clip_review._write_verdict(path, verdict)
    return jsonify({
        'success': True,
        'why': verdict.get('what') or '',
        'certainty': verdict.get('certainty') or '',
        'kind': verdict.get('kind') or '',
        'review_error': verdict.get('error'),
        # What it *would* have decided, so the answer is not just prettier
        # words: a clip it now calls boring is one worth deleting by hand.
        'keep': verdict.get('keep', True),
    })


@app.route('/api/recordings/review/<path:filename>/keep', methods=['POST'])
@require_auth
def api_keep_review(filename):
    """Move one clip out of the tray and onto the shelf, where nothing deletes
    it on a timer."""
    import clip_review
    from recording import month_path_for
    safe = os.path.basename(filename)
    src = os.path.join(clip_review.review_dir(RECORDINGS_DIR), safe)
    if not os.path.isfile(src):
        return jsonify({'error': 'that recording is gone'}), 404
    dest = month_path_for(safe)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(src, dest)
        if os.path.exists(src + '.json'):
            shutil.move(src + '.json', dest + '.json')
    except OSError as e:
        return jsonify({'error': f'no pude moverla: {e}'}), 500
    return jsonify({'success': True, 'rel_path': os.path.relpath(dest, RECORDINGS_DIR)})


@app.route('/api/recordings/review/purge', methods=['POST'])
@require_auth
def api_purge_review():
    """Empty the tray now, rather than waiting for it to age out.

    Everything here is already on a fifteen day timer; this is the button for
    when somebody has looked at the pile and does not want to scroll past it
    for another two weeks. It touches only `to_review/` — the month folders
    are the house's recordings and nothing in this app reaches them in bulk.
    """
    import clip_review
    folder = clip_review.review_dir(RECORDINGS_DIR)
    deleted = 0
    try:
        names = os.listdir(folder)
    except OSError as e:
        return jsonify({'error': f'no pude leer la bandeja: {e}'}), 500
    for name in names:
        if not name.endswith('.mp4'):
            continue                    # sidecars go with their clip, below
        path = os.path.join(folder, name)
        try:
            os.remove(path)
            deleted += 1
        except OSError as e:
            logger.warning("could not delete %s from the tray: %s", name, e)
            continue
        try:
            os.remove(path + '.json')
        except OSError:
            pass                        # a missing sidecar costs an explanation
    logger.info("review tray emptied by hand: %d clip(s)", deleted)
    return jsonify({'success': True, 'deleted': deleted})


@app.route('/api/recordings/<path:rel_path>', methods=['DELETE'])
@require_auth
def api_delete_recording(rel_path):
    """Delete a recording file"""
    rec_type = request.args.get('type', '')
    success = delete_recording(rel_path, rec_type)
    if success:
        return jsonify({'success': True, 'message': 'Recording deleted'})
    return jsonify({'error': 'Failed to delete recording'}), 500


@app.route('/api/recordings/delete_batch', methods=['POST'])
@require_auth
def api_delete_recordings_batch():
    """Delete multiple recording files"""
    data = request.get_json()
    recordings = data.get('recordings', [])
    deleted = 0
    for rec in recordings:
        path = rec.get('path', '')
        rec_type = rec.get('type', 'video')
        if path and delete_recording(path, rec_type):
            deleted += 1
    return jsonify({'deleted': deleted, 'total': len(recordings)})





# Store active camera frames with LRU cache to limit memory usage
active_frames: OrderedDict[str, np.ndarray] = OrderedDict()
motion_events: Dict[str, EnhancedMotionEvent] = {}
periodic_detections: Dict[str, 'PeriodicDetectionResult'] = {}  # Store periodic detection results
web_frames: Dict[str, np.ndarray] = {}
event_counter: Dict[str, int] = {}
# Store actual camera resolutions (from actual frames, not config)
camera_resolutions: Dict[str, Tuple[int, int]] = {}
# Store per-camera motion detectors with their zones
camera_motion_detectors: Dict[str, EnhancedMotionDetector] = {}
# Store per-camera show_all_objects setting
camera_show_all_objects: Dict[str, bool] = {}
# Pre-computed objects to draw as overlay — updated by process_frame(), read by snapshot/stream
display_overlays: Dict[str, list] = {}
# Store per-camera frame fetcher threads
camera_fetcher_threads: Dict[str, threading.Thread] = {}
# Which fetcher thread is the live one for each camera. A thread carries the
# number it was born with and exits the moment it stops matching.
#
# The old exit condition was "my camera is no longer in camera_manager.cameras",
# which restart_camera_connection arranges — and then puts the camera back two
# seconds later. Any thread that did not happen to reach its check inside that
# window never exited at all, and there is no window wide enough: a restart is
# triggered *because* a camera has stopped producing frames, which is exactly
# when its fetcher is blocked and slowest to notice anything.
#
# So the house ran two fetchers per camera, each calling process_frame, and both
# raced the "start a recording unless one is running" check. See start_recording
# in recording.py for what that cost.
camera_fetcher_generation: Dict[str, int] = {}
_fetcher_gen_lock = threading.Lock()
# Flag to control frame fetcher threads
frame_fetchers_running = False
# Track last error notification time per camera to prevent spam
last_error_notification: Dict[str, float] = {}
ERROR_NOTIFICATION_COOLDOWN = 10.0  # Only notify every 10 seconds max
# Track last snapshot time per camera to prevent spam
snapshot_cooldowns: Dict[str, float] = {}
# Lock for thread-safe access to web_frames
web_frames_lock = threading.Lock()

# Stuck camera region monitoring - detect if camera is stuck on an image
stuck_region_config: Dict[str, dict] = {}  # camera_ip -> region config (percentages 0-1)
stuck_region_state: Dict[str, dict] = {}  # camera_ip -> state (last_change_time, prev_frame_hash)
STUCK_REGION_TIMEOUT = 30  # Seconds before restarting camera if region unchanged

# Performance optimization settings
MAX_CACHED_FRAMES = 10  # Limit cached frames to prevent memory bloat
JPEG_QUALITY = 80  # Reduced from 85 for faster encoding (lower quality = less CPU)
FRAME_SKIP_RATIO = 3  # Process every Nth frame for motion detection (increased from 2)
TARGET_FPS = 24  # Target framerate for streaming (reduced from 30)
MAX_STREAM_FPS = 24  # Cap for MJPEG streaming
STALE_FRAME_THRESHOLD = 5.0  # Seconds before a frame is considered stale (camera disconnected)

# Check for GPU-accelerated encoding support
CV2_CUDA_AVAILABLE = False
NVJPEG_ENCODER = None
try:
    if hasattr(cv2, 'cuda') and cv2.cuda is not None:
        CV2_CUDA_AVAILABLE = cv2.cuda.getCudaEnabledDeviceCount() > 0
        if CV2_CUDA_AVAILABLE:
            logger.info(f"OpenCV CUDA available for image processing: {cv2.cuda.getCudaEnabledDeviceCount()} device(s)")
            # Initialize NVJPEG hardware encoder if available
            if hasattr(cv2.cuda, 'NvJPEGEncoder'):
                NVJPEG_ENCODER = cv2.cuda.NvJPEGEncoder()
                logger.info("NVIDIA NVJPEG hardware encoder initialized")
except Exception:
    CV2_CUDA_AVAILABLE = False
    NVJPEG_ENCODER = None

HARDWARE_JPEG_ENCODING_AVAILABLE = CV2_CUDA_AVAILABLE and NVJPEG_ENCODER is not None

# Cleanup old motion events to prevent memory leaks
def cleanup_old_events():
    """Clean up old motion events to prevent memory leaks"""
    global motion_events
    current_time = time.time()
    # Keep only events from the last 5 minutes
    motion_events = {k: v for k, v in motion_events.items() if current_time - v.timestamp < 300}

# Cleanup old periodic detections to prevent memory leaks
def cleanup_old_periodic_detections():
    """Clean up old periodic detection results to prevent memory leaks"""
    global periodic_detections
    current_time = time.time()
    # Keep only detections from the last 5 minutes
    periodic_detections = {k: v for k, v in periodic_detections.items() if current_time - v.timestamp < 300}

# Thread for processing
# Frame counter per camera for frame skipping
frame_counters: Dict[str, int] = {}

def _scale_objects(objects: list, scale: float) -> list:
    """Scale detected object coordinates from processing resolution to display resolution."""
    if scale == 1.0:
        return objects
    result = []
    for obj in objects:
        x, y, w, h = obj.bounding_box
        result.append(DetectedObject(
            class_name=obj.class_name,
            confidence=obj.confidence,
            bounding_box=(int(x * scale), int(y * scale), int(w * scale), int(h * scale)),
            center_x=int(obj.center_x * scale),
            center_y=int(obj.center_y * scale),
        ))
    return result


def _publish_recording_mqtt(camera_key: str):
    """Publish a recording-complete MQTT event and clean up tracking state."""
    _manual_recordings.discard(camera_key)
    info = _camera_recording_info.pop(camera_key, None)
    if info:
        mqtt_c = get_mqtt()
        if mqtt_c:
            mqtt_c.publish_recording(camera_key, info['rel_path'], 'completed',
                                     [], info['object_tags'])
        logger.info(f"Recording completed for {camera_key}: {info['rel_path']}")


def _get_camera_name(camera_key: str) -> Optional[str]:
    """Look up the configured camera name by camera_id or IP."""
    cameras = settings_manager.get_cameras()
    cid = settings_manager._resolve_key(camera_key, cameras)
    if cid:
        config = cameras.get(cid, {})
        if config.get('name'):
            return config['name']
    reg = settings_manager.get_registry_camera_setting(camera_key)
    if reg and reg.get('name'):
        return reg['name']
    return None


def process_frame(camera_key: str, processing_frame: np.ndarray, full_frame: np.ndarray = None):
    """Process video frame in background thread with optimized frame skipping.
    camera_key is the camera_id (UUID)."""

    # Use full_frame for recording if provided, otherwise fall back to processing_frame
    frame_for_recording = full_frame if full_frame is not None else processing_frame

    # Implement frame skipping to reduce CPU/GPU load
    frame_counters[camera_key] = frame_counters.get(camera_key, 0) + 1
    if frame_counters[camera_key] % FRAME_SKIP_RATIO != 0:
        return  # Skip this frame for motion detection
    
    try:
        # Update motion zone based on actual frame resolution
        frame_height, frame_width = processing_frame.shape[:2]
        actual_resolution = (frame_width, frame_height)
        
        # Load config first so model name is available before creating the detector
        cameras = settings_manager.get_cameras()
        cid = settings_manager._resolve_key(camera_key, cameras) or camera_key
        camera_cfg = cameras.get(cid) or settings_manager.get_registry_camera_setting(camera_key)
        configured_model = camera_cfg.get('object_detection_model', None) if camera_cfg else None

        # Check if resolution or model changed and recreate the detector
        if (camera_key not in camera_resolutions or camera_resolutions[camera_key] != actual_resolution
                or camera_resolutions.get(f'{camera_key}_model') != configured_model):
            camera_resolutions[camera_key] = actual_resolution
            camera_resolutions[f'{camera_key}_model'] = configured_model
            # Create per-camera motion detector with configured or auto-selected model
            camera_motion_detector = EnhancedMotionDetector(
                detector_type="frame_diff",
                background_update_rate=5,
                enable_object_detection=True,
                object_detection_model=configured_model,
                object_confidence_threshold=0.35
            )
            motion_zones = camera_cfg.get('motion_zones', []) if camera_cfg else []
            
            if motion_zones:
                # Add saved zones (percentages are preserved as floats, pixels as ints)
                for zone_data in motion_zones:
                    # Keep values as floats if they're percentages (0-1), otherwise convert to int
                    x_val = zone_data.get('x', 0)
                    y_val = zone_data.get('y', 0)
                    w_val = zone_data.get('width', 100)
                    h_val = zone_data.get('height', 100)
                    
                    # Check if values are percentages (between 0 and 1)
                    x = x_val if 0 <= x_val <= 1 else int(x_val)
                    y = y_val if 0 <= y_val <= 1 else int(y_val)
                    w = w_val if 0 <= w_val <= 1 else int(w_val)
                    h = h_val if 0 <= h_val <= 1 else int(h_val)
                    
                    zone = MotionZone(
                        name=zone_data.get('name', 'Zone'),
                        x=x,
                        y=y,
                        width=w,
                        height=h,
                        sensitivity=float(zone_data.get('sensitivity', 0.5)),
                        min_area=int(zone_data.get('min_area', 500)),
                        filter_light_changes=bool(zone_data.get('filter_light_changes', False)),
                        light_change_threshold=float(zone_data.get('light_change_threshold', 0.35))
                    )
                    camera_motion_detector.add_zone(zone)
                logger.info(f"Loaded {len(motion_zones)} zones for {camera_key}")
            else:
                # No zones configured - use Full Frame zone so detection still works
                camera_motion_detector.add_zone(full_frame_zone())
                logger.info(f"Using default Full Frame zone for {camera_key}")
            
            camera_motion_detectors[camera_key] = camera_motion_detector
            
            # Load show_all_objects setting
            show_all_objects = camera_cfg.get('show_all_objects', False) if camera_cfg else False
            camera_show_all_objects[camera_key] = show_all_objects

            # Apply per-camera enabled object classes
            enabled_classes = camera_cfg.get('enabled_object_classes', None) if camera_cfg else None
            if camera_motion_detector.object_detector is not None:
                camera_motion_detector.object_detector.enabled_classes = (
                    set(enabled_classes) if enabled_classes is not None else None
                )

            logger.info(f"Created motion detector for {camera_key} with frame resolution: {frame_width}x{frame_height}")
        
        # Get or create motion detector for this camera
        camera_motion_detector = camera_motion_detectors.get(camera_key)
        if camera_motion_detector is None:
            camera_motion_detector = EnhancedMotionDetector(
                detector_type="frame_diff",
                background_update_rate=5,
                enable_object_detection=True,
                object_detection_model=None,
                object_confidence_threshold=0.35
            )
            camera_motion_detector.add_zone(full_frame_zone())
            logger.info(f"Created motion detector for {camera_key} with default Full Frame zone")
            camera_motion_detectors[camera_key] = camera_motion_detector
        
        # Detect motion using per-camera detector
        # Now returns a tuple: (events, periodic_result)
        result = camera_motion_detector.detect_motion(
            processing_frame,
            camera_key,
            event_counter=event_counter
        )
        
        # Handle both old single return and new tuple return for compatibility
        if isinstance(result, tuple):
            events, periodic_result = result
        else:
            events = result
            periodic_result = None
        
        # Log frame processing stats
        logger.debug(f"Frame processed for {camera_key}, events detected: {len(events)}")

        # Presence, from the tracker rather than this frame: what the security
        # topics and the assistant's `detect` answer read. Runs every processed
        # frame so a departure is noticed even when nothing moves.
        try:
            tracker = getattr(camera_motion_detector, 'tracker', None)
            if security_publisher is not None and tracker is not None:
                now_ts = time.time()
                security_publisher.observe(
                    camera_key,
                    tracker.confirmed_tracks(now_ts, PRESENCE_HOLD_SECONDS),
                    now_ts,
                    camera_name=_get_camera_name(camera_key),
                )
        except Exception as e:
            logger.error(f"Security state update failed for {camera_key}: {e}")

        # Process motion events
        for event in events:
            try:
                # Store motion event
                event_key = f"{camera_key}_{event.zone_name}_{int(event.timestamp)}"
                motion_events[event_key] = event
                logger.info(f"Motion detected in {camera_key} - {event.zone_name} with {len(event.detected_objects)} objects detected (confidence: {event.confidence:.4f})")
                
                # Log detected objects
                if event.detected_objects:
                    object_summary = ", ".join([f"{obj.class_name}({obj.confidence:.2f})" for obj in event.detected_objects])
                    logger.info(f"Objects detected in {camera_key} - {event.zone_name}: {object_summary}")

                camera_name = _get_camera_name(camera_key)

                # Nothing is written until the tracker has confirmed a target
                # class. Motion on its own is not evidence: measured over
                # 2026-08-29..09-03, 4,706 of the 8,650 stored clips -- 54% --
                # carried no object tag at all. Shadows, a cloud crossing the
                # sun, headlight sweep, rain on the lens, moths at the IR lamp.
                # Every one was written because pixels changed, and the whole
                # YOLO layer only ever got to name the file afterwards.
                #
                # Confirming costs no footage. The ring buffer keeps filling
                # for as long as this camera is not recording (5s at
                # PRE_MOTION_DURATION), so the frames from before the object
                # was confirmed are still in it and drain_ring_buffer() hands
                # them to the recording below.
                #
                # This gate covers snapshots too, and deliberately: they were
                # 31,445 files and 28.5 GB over the same six days -- as much
                # disk as the video -- saved every 5s of motion through the
                # same missing check, and the reviewer never looks at them.
                confirmed = getattr(event, 'confirmed_objects', None) or []
                if getattr(event, 'gate_applied', True) and not confirmed:
                    continue

                # Trigger recording on motion detection
                if not is_recording(camera_key) and settings_manager.is_recording_enabled(camera_key):
                    object_tags = [obj.class_name for obj in confirmed[:3]]
                    # Drain pre-motion ring buffer
                    pre_buffer = drain_ring_buffer(camera_key)
                    # Start recording with pre-buffer and configured duration
                    rec_duration = settings_manager.get_camera_recording_duration(camera_key)
                    if rec_duration is None:
                        rec_duration = settings_manager.get_recording_duration()
                    rec_path = start_recording(
                        camera_key, frame_for_recording,
                        confirmed,
                        event.timestamp,
                        object_tags=object_tags,
                        pre_buffer=pre_buffer,
                        duration=rec_duration,
                        camera_name=camera_name
                    )
                    if rec_path:
                        logger.info(f"Started recording for {camera_key}: {rec_path}")
                        socketio.emit('recording_status', {'camera_key': camera_key, 'recording': True})
                        _camera_recording_info[camera_key] = {
                            'rel_path': rec_path,
                            'object_tags': object_tags,
                        }
                
                # Save snapshot with overlay (rate-limited to avoid spam)
                # Only save snapshots within the configured recording time window
                if settings_manager.is_recording_enabled(camera_key):
                    snapshot_key = f"{camera_key}_last_snapshot"
                    current_time = time.time()
                    if current_time - snapshot_cooldowns.get(snapshot_key, 0) > 5.0:
                        object_tags = [obj.class_name for obj in confirmed[:3]]
                        snap_path = save_snapshot(
                            camera_key, frame_for_recording,
                            confirmed,
                            event.timestamp,
                            object_tags=object_tags,
                            camera_name=camera_name
                        )
                        if snap_path:
                            logger.info(f"Saved snapshot for {camera_key}: {snap_path}")
                        snapshot_cooldowns[snapshot_key] = current_time
                    
            except Exception as e:
                logging.error(f"Error processing motion event for {camera_key}: {e}")
                logging.error(f"Event details: {event}")
        
        # Process cooldown detection result (runs up to 10 seconds after last motion)
        if periodic_result is not None:
            try:
                periodic_key = f"{camera_key}_periodic"
                periodic_detections[periodic_key] = periodic_result

                if periodic_result.detected_objects:
                    object_summary = ", ".join([f"{obj.class_name}({obj.confidence:.2f})" for obj in periodic_result.detected_objects])
                    logger.info(f"Cooldown detection for {camera_key}: {len(periodic_result.detected_objects)} objects in zone '{periodic_result.zone_name}': {object_summary}")
                else:
                    logger.debug(f"Cooldown detection for {camera_key}: No objects detected in zones")
            except Exception as e:
                logging.error(f"Error processing periodic detection for {camera_key}: {e}")
                logging.error(f"Periodic result: {periodic_result}")

        # Update display_overlays so both /snapshot/ and /stream/ show the same overlay
        try:
            show_all_objects = camera_show_all_objects.get(camera_key, False)
            coord_scale = 1.0 / camera_manager.processing_scale
            if show_all_objects and camera_motion_detector and camera_motion_detector.object_detector:
                # Full-frame detection only within motion detection window
                if camera_motion_detector.is_in_detection_window(camera_key):
                    raw = camera_motion_detector.object_detector.detect_objects(processing_frame)
                    display_overlays[camera_key] = _scale_objects(raw, coord_scale)
            else:
                # Collect zone-filtered objects from current frame's events and periodic detection only
                objects: list = []
                for evt in events:
                    if evt.detected_objects:
                        objects.extend(evt.detected_objects)
                if periodic_result and periodic_result.detected_objects:
                    objects.extend(periodic_result.detected_objects)
                display_overlays[camera_key] = _scale_objects(objects, coord_scale)
        except Exception as e:
            logger.error(f"Error updating display_overlays for {camera_key}: {e}")

        # --- MQTT motion publishing ---
        if events:
            try:
                mqtt_c = get_mqtt()
                if mqtt_c:
                    for event in events:
                        obj_counts: dict = {}
                        for obj in event.detected_objects:
                            obj_counts[obj.class_name] = obj_counts.get(obj.class_name, 0) + 1
                        mqtt_c.publish_motion(
                            camera_key,
                            event.zone_name,
                            event.confidence,
                            [{'class_name': o.class_name, 'confidence': o.confidence,
                              'bounding_box': list(o.bounding_box)} for o in event.detected_objects],
                            obj_counts,
                        )
            except Exception as e:
                logger.error(f"MQTT motion publish error for {camera_key}: {e}")

    except Exception as e:
        logging.error(f"Error processing frame {camera_key}: {e}")
        import traceback
        logging.error(f"Traceback: {traceback.format_exc()}")


STUCK_REGION_PIXEL_CHANGE_THRESHOLD = 5  # Min white-pixel-count change to consider timestamp updated

def _binarize_timestamp(region: np.ndarray) -> np.ndarray:
    """Convert a timestamp region to pure black/white, isolating the clock text from background."""
    if len(region.shape) == 3:
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    else:
        gray = region
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


def compute_region_hash(frame: np.ndarray, region: dict) -> int:
    """Count white (text) pixels in the timestamp region after binarization.
    
    When the camera's on-screen clock ticks, the seconds digit changes, which
    measurably shifts the white pixel count. Binarization eliminates sensor noise,
    making the count very stable between frames with the same timestamp.
    """
    try:
        h, w = frame.shape[:2]
        if region['x'] <= 1 and region['y'] <= 1:
            x = int(region['x'] * w)
            y = int(region['y'] * h)
            rw = int(region['width'] * w)
            rh = int(region['height'] * h)
        else:
            x, y, rw, rh = int(region['x']), int(region['y']), int(region['width']), int(region['height'])
        
        x = max(0, min(x, w - 1))
        y = max(0, min(y, h - 1))
        rw = min(rw, w - x)
        rh = min(rh, h - y)
        
        if rw <= 0 or rh <= 0:
            return 0
        
        region_frame = frame[y:y+rh, x:x+rw]
        binary = _binarize_timestamp(region_frame)
        return int(np.sum(binary > 0))
    except Exception as e:
        logger.error(f"Error computing region hash: {e}")
        return 0


STUCK_REGION_RETRY_COOLDOWN = 300  # Seconds before retrying a stuck restart (5 minutes)

def check_stuck_region(camera_key: str, frame: Optional[np.ndarray] = None):
    """Check if the camera is stuck; restart if region unchanged for 30s or no fresh frames for 30s"""
    if camera_key not in stuck_region_config:
        return
    
    region = stuck_region_config[camera_key]
    if not region or not region.get('enabled', False):
        return
    
    current_time = time.time()
    
    if camera_key not in stuck_region_state:
        stuck_region_state[camera_key] = {
            'last_change_time': current_time,
            'prev_white_pixels': 0,
            'restart_scheduled': False,
            'last_restart_time': 0,
            'last_fresh_frame_time': current_time if frame is not None else 0
        }
        return
    
    state = stuck_region_state[camera_key]
    
    if 'last_restart_time' not in state:
        state['last_restart_time'] = 0
    if 'last_fresh_frame_time' not in state:
        state['last_fresh_frame_time'] = current_time if frame is not None else 0
    if 'prev_white_pixels' not in state:
        state['prev_white_pixels'] = 0
    
    time_since_restart = current_time - state['last_restart_time']
    can_retry = time_since_restart > STUCK_REGION_RETRY_COOLDOWN
    
    if frame is not None:
        state['last_fresh_frame_time'] = current_time
        current_white_pixels = compute_region_hash(frame, region)
        pixel_diff = abs(current_white_pixels - state['prev_white_pixels'])
        
        if state['prev_white_pixels'] != 0 and pixel_diff <= STUCK_REGION_PIXEL_CHANGE_THRESHOLD:
            elapsed = current_time - state['last_change_time']
            if elapsed > STUCK_REGION_TIMEOUT and (not state['restart_scheduled'] or can_retry):
                logger.warning(f"Camera {camera_key} stuck region unchanged for {elapsed:.1f}s, restarting connection")
                state['restart_scheduled'] = True
                state['last_restart_time'] = current_time
                restart_camera_connection(camera_key)
        else:
            state['last_change_time'] = current_time
            state['prev_white_pixels'] = current_white_pixels
            state['restart_scheduled'] = False
    else:
        # A camera that has never delivered a frame must not be judged by
        # `current_time - 0`: that age is fifty years, and the watchdog
        # restarted every camera seconds after boot on top of whichever one
        # genuinely stalled -- measured, three at once, two of them healthy.
        # A fresh start gets its full timeout before a restart is considered.
        if state['last_fresh_frame_time'] == 0:
            state['last_fresh_frame_time'] = current_time
        time_since_fresh = current_time - state['last_fresh_frame_time']
        if time_since_fresh > STUCK_REGION_TIMEOUT and (not state['restart_scheduled'] or can_retry):
            logger.warning(f"Camera {camera_key} no fresh frame for {time_since_fresh:.1f}s, restarting connection")
            state['restart_scheduled'] = True
            state['last_restart_time'] = current_time
            restart_camera_connection(camera_key)


def restart_camera_connection(camera_key: str):
    """Restart a camera connection by tearing down and re-adding with full cleanup"""
    try:
        config = camera_manager.cameras.get(camera_key)
        if not config:
            logger.error(f"Cannot restart {camera_key}: not found in camera configs")
            return
        
        logger.info(f"Restarting camera connection for {camera_key}")
        
        # Stop the old stream thread
        camera_manager._running[camera_key] = False
        
        # Kept, not popped: the watchdog restarts any camera whose thread is
        # missing, so dropping the reference here invited a second fetcher
        # before this function had even finished making its own.
        old_thread = camera_fetcher_threads.get(camera_key)
        
        # Remove camera from the cameras dict so the old fetcher thread
        # sees "camera_key not in camera_manager.cameras" and breaks its loop
        camera_manager.cameras.pop(camera_key, None)
        camera_manager.camera_states.pop(camera_key, None)
        camera_manager.stream_threads.pop(camera_key, None)
        camera_manager.frame_queues.pop(camera_key, None)
        
        # Close any open containers/captures in camera_manager
        container = camera_manager._containers.pop(camera_key, None)
        if container:
            try:
                container.close()
            except Exception:
                pass
        cap = camera_manager._caps.pop(camera_key, None)
        if cap:
            try:
                cap.release()
            except Exception:
                pass
        
        # Clean up stale state
        camera_motion_detectors.pop(camera_key, None)
        camera_resolutions.pop(camera_key, None)
        camera_resolutions.pop(f'{camera_key}_model', None)
        display_overlays.pop(camera_key, None)
        for k in [k for k in motion_events if hasattr(motion_events[k], 'camera_id') and motion_events[k].camera_id == camera_key]:
            del motion_events[k]
        periodic_detections.pop(f'{camera_key}_periodic', None)
        frame_counters.pop(camera_key, None)
        encoded_frame_cache.pop(camera_key, None)
        web_frames.pop(camera_key, None)
        active_frames.pop(camera_key, None)
        last_error_notification.pop(camera_key, None)
        snapshot_cooldowns.pop(camera_key, None)
        
        # Stop any active recording for this camera
        if is_recording(camera_key):
            stop_recording(camera_key)
        
        # Wait for old threads to exit (old fetcher should notice camera is gone and break)
        time.sleep(2)
        
        if old_thread and old_thread.is_alive():
            logger.warning(f"Old fetcher thread for {camera_key} still alive after 2s")
        
        # Re-add the camera under the original camera_key and start a new stream thread
        with camera_manager._lock:
            camera_manager.cameras[camera_key] = config
            camera_manager.camera_states[camera_key] = CameraState.BUFFERING
            camera_manager._running[camera_key] = True
            try:
                camera_manager.frame_queues[camera_key] = asyncio.Queue(maxsize=30)
            except RuntimeError:
                camera_manager.frame_queues[camera_key] = queue.Queue(maxsize=30)
            stream_thread = threading.Thread(
                target=camera_manager._process_stream_thread,
                args=(config,),
                daemon=True
            )
            stream_thread.start()
            camera_manager.stream_threads[camera_key] = stream_thread
            logger.info(f"New stream thread started for {camera_key}")
        
        # Start a new fetcher thread, retiring whatever is still running. The
        # sleep(2) above hopes the old one has gone; this makes sure it goes.
        start_new_camera_fetcher(camera_key, replace=True)
        
        # Reset stuck region state
        stuck_region_state[camera_key] = {
            'last_change_time': time.time(),
            'prev_white_pixels': 0,
            'restart_scheduled': False,
            'last_restart_time': time.time(),
            'last_fresh_frame_time': time.time()
        }
        
        logger.info(f"Camera {camera_key} restarted successfully")
    except Exception as e:
        logger.error(f"Error restarting camera {camera_key}: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")


def fetch_camera_frame(camera_key: str, generation: int = 0):
    """Fetch frames from a single camera in its own thread - optimized for CPU efficiency"""
    logger.info(f"Starting frame fetcher thread for camera {camera_key} (thread: {threading.current_thread().name}, gen {generation})")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    frame_interval = 1.0 / TARGET_FPS
    consecutive_failures = 0
    max_consecutive_failures = 30

    last_motion_process_time = 0
    motion_process_interval = 1.0 / 5

    # Mark camera as active on startup so the first snapshot request isn't needed to prime it
    last_snapshot_time[camera_key] = time.time()
    # Track the timestamp of the last frame we processed to skip duplicates
    last_processed_ts = 0.0

    while frame_fetchers_running:
        loop_start = time.time()
        try:
            if camera_fetcher_generation.get(camera_key) != generation:
                logger.info(f"Fetcher for {camera_key} gen {generation} superseded, stopping")
                break
            if camera_key not in camera_manager.cameras:
                logger.debug(f"Camera {camera_key} removed, stopping fetcher thread")
                break

            # If no snapshot has been requested recently, reduce framerate but keep processing
            # for motion detection and recording (don't stop entirely)
            is_active = (time.time() - last_snapshot_time.get(camera_key, 0)) < SNAPSHOT_IDLE_THRESHOLD
            if not is_active:
                # Still process frames for motion/recording, but at lower rate
                time.sleep(0.2)

            try:
                frame = camera_manager.get_frame(camera_key)
                if frame is not None:
                    ts = camera_manager.frame_timestamps.get(camera_key, 0)
                    if time.time() - ts > STALE_FRAME_THRESHOLD:
                        logger.debug(f"Stale frame for {camera_key} ({time.time() - ts:.1f}s old), skipping")
                        frame = None
                    elif ts == last_processed_ts:
                        check_stuck_region(camera_key)
                        time.sleep(frame_interval)
                        continue
                    else:
                        last_processed_ts = ts
                if frame is not None:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    check_stuck_region(camera_key)
                    logger.debug(f"No frame available for camera {camera_key} (consecutive: {consecutive_failures})")
                    time.sleep(0.05)
                    continue
            except Exception as e:
                consecutive_failures += 1
                logger.debug(f"Error getting frame for camera {camera_key}: {e} (consecutive: {consecutive_failures})")
                state = camera_manager.get_camera_state(camera_key)
                if state == CameraState.DISCONNECTED or state == CameraState.ERROR:
                    current_time = time.time()
                    last_notification = last_error_notification.get(camera_key, 0)
                    if current_time - last_notification > ERROR_NOTIFICATION_COOLDOWN:
                        last_error_notification[camera_key] = current_time
                        logger.warning(f"Camera {camera_key} is disconnected, notifying clients")
                        try:
                            cameras = camera_manager.get_all_cameras()
                            if camera_key in cameras:
                                config = cameras[camera_key]
                                socketio.emit('camera_list', {camera_key: {
                                    'device_ip': config.device_ip,
                                    'name': config.name,
                                    'username': config.username,
                                    'port': config.port,
                                    'stream_path': config.stream_path,
                                    'fps': config.fps,
                                    'width': config.width,
                                    'height': config.height,
                                    'state': state.value if state else 'unknown'
                                }})
                        except Exception as emit_error:
                            logger.error(f"Error emitting camera state update: {emit_error}")
                check_stuck_region(camera_key)
                time.sleep(0.5)
                continue

            if frame is not None:
                active_frames[camera_key] = frame

                current_time = time.time()
                processing_frame = None
                if current_time - last_motion_process_time >= motion_process_interval:
                    processing_frame = camera_manager.get_processing_frame(camera_key)
                    if processing_frame is not None:
                        # Pass both processing frame (for detection) and full frame (for recording)
                        process_frame(camera_key, processing_frame, full_frame=frame)
                    last_motion_process_time = current_time

                check_stuck_region(camera_key, frame)

                # Feed full-resolution frame to ring buffer (for pre-motion recording)
                # Use processing frame for ring buffer to save memory
                if not is_recording(camera_key) and settings_manager.is_recording_enabled(camera_key):
                    if processing_frame is None:
                        processing_frame = camera_manager.get_processing_frame(camera_key)
                    if processing_frame is not None:
                        add_to_ring_buffer(camera_key, processing_frame, current_time)

                # Feed frame to active recording if recording is in progress
                if is_recording(camera_key):
                    # Stop recording if time window has closed, unless this
                    # recording was triggered on demand through the API.
                    if (not settings_manager.is_recording_enabled(camera_key)
                            and camera_key not in _manual_recordings):
                        stop_recording(camera_key)
                        socketio.emit('recording_status', {'camera_key': camera_key, 'recording': False})
                        _publish_recording_mqtt(camera_key)
                    else:
                        objects_to_draw = display_overlays.get(camera_key, [])
                        still_recording = add_frame_to_recording(camera_key, frame, current_time, detected_objects=objects_to_draw)
                        if not still_recording:
                            socketio.emit('recording_status', {'camera_key': camera_key, 'recording': False})
                            _publish_recording_mqtt(camera_key)

                with web_frames_lock:
                    web_frames[camera_key] = frame

                logger.debug(f"Frame stored for camera {camera_key} (shape: {frame.shape})")
            else:
                consecutive_failures += 1
                check_stuck_region(camera_key)
                if consecutive_failures >= max_consecutive_failures:
                    logger.warning(f"Camera {camera_key}: {consecutive_failures} consecutive None frames. Camera state: {camera_manager.get_camera_state(camera_key)}. Queue exists: {camera_key in camera_manager.frame_queues}")
                    consecutive_failures = 0
                time.sleep(0.05)

        except Exception as e:
            logger.error(f"Error in frame fetcher for camera {camera_key}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            time.sleep(0.1)

        elapsed = time.time() - loop_start
        if elapsed < frame_interval:
            time.sleep(max(0, frame_interval - elapsed))

    loop.close()
    logger.info(f"Frame fetcher thread for camera {camera_key} stopped")


def start_camera_fetchers():
    """Start a separate frame fetcher thread for each camera"""
    global frame_fetchers_running
    frame_fetchers_running = True
    
    # Load cameras from config first
    try:
        loop = asyncio.new_event_loop()
        load_cameras_from_config(loop)
        loop.close()
    except Exception as e:
        logger.error(f"Error loading cameras: {e}")
    
    logger.info(f"Starting fetcher threads for {len(camera_manager.cameras)} cameras: {list(camera_manager.cameras.keys())}")
    
    # Start a fetcher thread for each camera
    # Through the same door as every other start, so a generation is stamped
    # from the very first thread. Two ways to create a fetcher was how one of
    # them came to have no generation at all.
    for cam_id in list(camera_manager.cameras.keys()):
        start_new_camera_fetcher(cam_id)

    # Start background registry sync (fallback polling — SocketIO push is primary)
    def _registry_sync_worker():
        while True:
            time.sleep(120)
            try:
                sync_registry_cameras()
            except Exception as e:
                logger.error(f"Registry sync error: {e}")

    threading.Thread(target=_registry_sync_worker, daemon=True, name="registry-sync").start()
    logger.info("Started registry sync background thread (interval: 120s, SocketIO push is primary)")

    # Start SocketIO client to receive push events from the camera registry
    def _start_registry_event_client():
        registry_url = os.environ.get('CAMERA_REGISTRY_URL', 'http://cameras.home:5001')
        while True:
            try:
                logger.info(f"Connecting to registry SocketIO at {registry_url}")
                _registry_event_client.connect(registry_url)
                _registry_event_client.wait()
            except Exception as e:
                logger.error(f"Registry SocketIO client disconnected: {e}, retrying in 30s")
            time.sleep(30)

    threading.Thread(target=_start_registry_event_client, daemon=True, name="registry-sio-client").start()
    logger.info("Started registry SocketIO client thread")

    # Start background memory cleanup
    def _memory_cleanup_worker():
        while frame_fetchers_running:
            time.sleep(60)
            try:
                cleanup_old_events()
                cleanup_old_periodic_detections()
                # Clips nobody came back for, out of the to_review tray.
                import clip_review
                clip_review.purge_old_reviews(RECORDINGS_DIR)
                # And kept recordings past the household's retention, out of
                # the month folders. Separate call and separate setting from
                # the line above, because they are different acts: that one
                # bins clips nobody looked at, this one deletes recordings the
                # reviewer judged worth keeping. Off unless
                # CLIP_RECORDING_MAX_AGE_DAYS says otherwise, and off is what
                # this package ships -- see the note on RECORDING_MAX_AGE_DAYS
                # about these being the only copy.
                clip_review.purge_old_recordings(RECORDINGS_DIR)
                # Snapshots whose recording is gone, however it went -- deleted
                # from the page, binned by the reviewer, or aged out above.
                # There are three or four per clip and only one of them shares
                # the clip's name, so without this the month folders keep the
                # rest forever: emptying the videos on 2026-09-03 left 31,451
                # JPEGs and 29 GB behind, as much disk as the video had been.
                purge_orphan_snapshots(RECORDINGS_DIR)
            except Exception as e:
                logger.error(f"Memory cleanup error: {e}")

    threading.Thread(target=_memory_cleanup_worker, daemon=True, name="memory-cleanup").start()
    logger.info("Started memory cleanup background thread (interval: 60s)")


def start_new_camera_fetcher(camera_key: str, replace: bool = False):
    """Start a fetcher thread for a camera, and make it the only one.

    `replace=True` is for a restart, where an older thread may still be alive
    and will not necessarily have noticed: bumping the generation retires it at
    its next loop, whether or not it is anywhere near its check right now.
    Without that there were two fetchers per camera for the rest of the
    process's life.
    """
    global frame_fetchers_running
    if not frame_fetchers_running:
        frame_fetchers_running = True

    with _fetcher_gen_lock:
        existing = camera_fetcher_threads.get(camera_key)
        if not replace and existing is not None and existing.is_alive():
            return
        generation = camera_fetcher_generation.get(camera_key, 0) + 1
        camera_fetcher_generation[camera_key] = generation
        thread = threading.Thread(
            target=fetch_camera_frame,
            args=(camera_key, generation),
            daemon=True,
            name=f"fetcher-{camera_key}-{generation}",
        )
        thread.start()
        camera_fetcher_threads[camera_key] = thread
    logger.info(f"Started frame fetcher thread for camera {camera_key} (gen {generation})")


def watchdog_fetcher_threads():
    """Periodically check that each camera has a live fetcher thread and restart dead ones"""
    while frame_fetchers_running:
        time.sleep(10)
        if not frame_fetchers_running:
            break
        for cam_id in list(camera_manager.cameras.keys()):
            thread = camera_fetcher_threads.get(cam_id)
            if thread is None or not thread.is_alive():
                logger.warning(f"Fetcher thread for {cam_id} is dead, restarting")
                start_new_camera_fetcher(cam_id)


def stop_camera_fetchers():
    """Stop all camera fetcher threads"""
    global frame_fetchers_running
    frame_fetchers_running = False
    camera_fetcher_threads.clear()


# Start background frame fetcher threads (one per camera) and watchdog
logger.info("Starting camera frame fetcher threads")
fetcher_starter_thread = threading.Thread(target=start_camera_fetchers, daemon=True)
fetcher_starter_thread.start()
logger.info("Camera frame fetcher threads started")
watchdog_thread = threading.Thread(target=watchdog_fetcher_threads, daemon=True, name="fetcher-watchdog")
watchdog_thread.start()
logger.info("Frame fetcher watchdog thread started")


def get_camera_rotation(camera_key: str) -> int:
    """Get the rotation setting for a camera"""
    try:
        cameras = settings_manager.get_cameras()
        if camera_key in cameras:
            return cameras[camera_key].get('rotation', 0)
        # Check registry settings
        reg = settings_manager.get_registry_camera_setting(camera_key)
        if reg:
            return int(reg.get('rotation', 0))
    except Exception:
        pass
    return 0


def rotate_frame(frame: cv2.Mat, rotation: int) -> cv2.Mat:
    """Rotate a frame by the specified degrees"""
    if rotation == 0:
        return frame
    elif rotation == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    elif rotation == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    elif rotation == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return frame


# Cache for encoded frames to reduce re-encoding
encoded_frame_cache: Dict[str, Tuple[bytes, float]] = {}
FRAME_CACHE_TTL = 0.033  # Cache frames for 33ms (30 FPS)

# Stop events: when a new connection arrives for a camera, the previous generator is signalled to exit
mjpeg_stop_events: Dict[str, threading.Event] = {}
mjpeg_stop_events_lock = threading.Lock()

def generate_mjpeg(camera_key: str):
    """Generate MJPEG stream from camera frame with optimized encoding"""
    with mjpeg_stop_events_lock:
        old_event = mjpeg_stop_events.get(camera_key)
        if old_event:
            old_event.set()
        stop_event = threading.Event()
        mjpeg_stop_events[camera_key] = stop_event

    logger.info(f"Starting MJPEG stream for camera {camera_key}")
    frame_count = 0
    no_frame_count = 0

    try:
        while not stop_event.is_set():
            current_time = time.time()
            logger.debug(f"Starting MJPEG frame generation for camera {camera_key}")

            with web_frames_lock:
                has_frame = camera_key in web_frames
                current_keys = list(web_frames.keys())

            if not has_frame:
                no_frame_count += 1
                if no_frame_count <= 10 or no_frame_count % 100 == 1:
                    logger.info(f"MJPEG: No frame for {camera_key} (check #{no_frame_count}). web_frames keys: {current_keys}")
                time.sleep(0.1)
                continue

            if no_frame_count > 0:
                logger.info(f"Camera {camera_key} frame restored after {no_frame_count} empty checks")
            no_frame_count = 0

            with web_frames_lock:
                frame = web_frames[camera_key].copy()
            frame_count += 1
            logger.debug(f"Serving frame {frame_count} for camera {camera_key}")

            has_overlay = False
            try:
                objects_to_draw = display_overlays.get(camera_key, [])
                camera_motion_detector = camera_motion_detectors.get(camera_key)
                if objects_to_draw and camera_motion_detector:
                    frame = camera_motion_detector.draw_detections(frame, objects_to_draw)
                    has_overlay = True
            except Exception as e:
                logger.error(f"Error drawing detections for camera {camera_key}: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")

            try:
                jpeg_bytes = None
                if not has_overlay:
                    cached = encoded_frame_cache.get(camera_key)
                    if cached and (current_time - cached[1]) < FRAME_CACHE_TTL:
                        jpeg_bytes = cached[0]

                if jpeg_bytes is None:
                    if HARDWARE_JPEG_ENCODING_AVAILABLE:
                        gpu_frame = cv2.cuda_GpuMat()
                        gpu_frame.upload(frame)
                        _, buffer = NVJPEG_ENCODER.encode(gpu_frame, JPEG_QUALITY)
                        jpeg_bytes = buffer.tobytes()
                    else:
                        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY,
                                       int(cv2.IMWRITE_JPEG_OPTIMIZE), 1]
                        ret, buffer = cv2.imencode('.jpg', frame, encode_param)
                        if not ret:
                            logger.error(f"Failed to encode frame {frame_count} for camera {camera_key}")
                            continue
                        jpeg_bytes = buffer.tobytes()
                    if not has_overlay:
                        encoded_frame_cache[camera_key] = (jpeg_bytes, current_time)

                logger.debug(f"Frame {frame_count} encoded successfully for camera {camera_key}")
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n'
                       b'Content-Length: ' + str(len(jpeg_bytes)).encode() + b'\r\n\r\n' +
                       jpeg_bytes + b'\r\n')
                logger.debug(f"Frame {frame_count} yielded for camera {camera_key}")
            except Exception as e:
                logger.error(f"Error encoding frame {frame_count} for camera {camera_key}: {e}")

            logger.debug(f"Finished MJPEG frame generation for camera {camera_key}")

            elapsed = time.time() - current_time
            if elapsed < 1.0 / TARGET_FPS:
                time.sleep(max(0, 1.0 / TARGET_FPS - elapsed))

    except GeneratorExit:
        logger.info(f"MJPEG stream for {camera_key} closed by client")
    finally:
        with mjpeg_stop_events_lock:
            if mjpeg_stop_events.get(camera_key) is stop_event:
                del mjpeg_stop_events[camera_key]


# Stop events for H.264 streams (mirrors MJPEG pattern)
h264_stop_events: Dict[str, threading.Event] = {}
h264_stop_events_lock = threading.Lock()

# H.264 encoder cache per camera
h264_encoders: Dict[str, H264StreamGenerator] = {}

def get_h264_encoder(camera_key: str, width: int, height: int, fps: int) -> H264StreamGenerator:
    """Get or create H.264 encoder for a camera"""
    key = f"{camera_key}_{width}x{height}_{fps}"
    if key not in h264_encoders:
        if H264StreamGenerator:
            h264_encoders[key] = H264StreamGenerator(width=width, height=height, fps=fps)
            logger.info(f"Created H.264 encoder for {camera_key}: {width}x{height} @ {fps}fps")
        else:
            logger.warning(f"H.264 encoder not available for {camera_key}")
            return None
    return h264_encoders[key]

def generate_h264(camera_key: str):
    """Generate H.264 stream from camera frame"""
    with h264_stop_events_lock:
        old_event = h264_stop_events.get(camera_key)
        if old_event:
            old_event.set()
        stop_event = threading.Event()
        h264_stop_events[camera_key] = stop_event

    logger.info(f"Starting H.264 stream for camera {camera_key}")
    frame_count = 0
    no_frame_count = 0

    cameras = camera_manager.get_all_cameras()
    config = cameras.get(camera_key)
    width = config.width if config else 1920
    height = config.height if config else 1080
    fps = config.fps if config else 30

    encoder = get_h264_encoder(camera_key, width, height, fps)

    try:
        while not stop_event.is_set():
            current_time = time.time()
            logger.debug(f"Starting H.264 frame generation for camera {camera_key}")
            
            with web_frames_lock:
                has_frame = camera_key in web_frames
                current_keys = list(web_frames.keys())
            
            if not has_frame:
                no_frame_count += 1
                if no_frame_count <= 10 or no_frame_count % 100 == 1:
                    logger.info(f"H.264: No frame for {camera_key} (check #{no_frame_count}). web_frames keys: {current_keys}")
                time.sleep(0.1)
                continue
            
            if no_frame_count > 0:
                logger.info(f"Camera {camera_key} frame restored after {no_frame_count} empty checks")
            no_frame_count = 0
            
            with web_frames_lock:
                frame = web_frames[camera_key].copy()
            frame_count += 1
            logger.debug(f"Serving H.264 frame {frame_count} for camera {camera_key}")
            
            try:
                if encoder:
                    h264_data = encoder.generate_stream(frame)
                    if h264_data:
                        logger.debug(f"Frame {frame_count} H.264 encoded successfully for camera {camera_key}")
                        yield h264_data
                    else:
                        logger.debug(f"Frame {frame_count} encoding returned None for camera {camera_key}")
                else:
                    logger.warning(f"H.264 encoder not available for {camera_key}")
            except Exception as e:
                logger.error(f"Error encoding H.264 frame {frame_count} for camera {camera_key}: {e}")
            
            logger.debug(f"Finished H.264 frame generation for camera {camera_key}")
            
            elapsed = time.time() - current_time
            if elapsed < 1.0 / TARGET_FPS:
                time.sleep(max(0, 1.0 / TARGET_FPS - elapsed))
        
    except GeneratorExit:
        logger.info(f"H.264 stream for {camera_key} closed by client")
    finally:
        with h264_stop_events_lock:
            if h264_stop_events.get(camera_key) is stop_event:
                del h264_stop_events[camera_key]
        logger.info(f"H.264 stream generator for {camera_key} stopped. Total frames: {frame_count}")


# Track when each camera was last snapshot-requested (used to idle inactive cameras)
last_snapshot_time: Dict[str, float] = {}
SNAPSHOT_IDLE_THRESHOLD = 10.0  # seconds without a request before fetch loop backs off

# Serve a single JPEG snapshot (used by the dashboard's JS poller)
#
# Deliberately not @require_auth, and that is load-bearing rather than an
# oversight. Three callers reach this with no session and no way to get one:
# the ESP32 wall panels (services/proxy/esp32/control/README.md -- "both plain
# HTTP and neither asking for any credential", and the `?w=`/`?q=` parameters
# below exist for them), HomeCore's tile poller, and Alfred's camera-feed
# skill. `require_auth` answers a **302 to the login page**, so none of the
# three would see an error: the panels go blank, and the skill's
# `raise_for_status()` passes on the login page's own 200 and writes HTML into
# a .jpg. Putting these behind a session needs a credential those callers can
# carry first -- the decorator on its own is a silent outage.
@app.route('/snapshot/<camera_key>')
def snapshot(camera_key: str):
    """Return the latest JPEG frame as a single image response.

    Optional `?w=` (target width) and `?q=` (JPEG quality) shrink the response
    for callers that cannot afford a full-resolution frame. The ESP32 wall panel
    is the reason they exist: a full frame off these cameras measures 130-175 KB,
    which is most of a microcontroller's heap before it has decoded anything.
    Measured against the patio camera: `w=640&q=60` gives 40 KB, `w=800&q=60`
    gives 56 KB, `w=480&q=60` gives 25 KB.

    Note the served frame is the *rotated* one, so a camera with `rotation: 90`
    hands back a portrait image whose aspect ratio does not match the `width`
    and `height` in `/api/cameras`. Callers laying out tiles must letterbox to
    what actually arrives rather than to what the registry advertises.

    Both are ignored when absent, so the dashboard's poller is byte-for-byte
    unaffected.
    """
    cid = resolve_camera_id(camera_key)
    if not cid:
        return Response(status=404)

    # Clamped rather than rejected: these come from a device on the LAN, and a
    # panel asking for something silly should get a picture, not a 400.
    want_w = request.args.get('w', type=int)
    if want_w is not None:
        want_w = max(160, min(1920, want_w))
    want_q = request.args.get('q', type=int)
    if want_q is not None:
        want_q = max(10, min(95, want_q))

    last_snapshot_time[cid] = time.time()
    try:
        with web_frames_lock:
            if cid not in web_frames:
                return Response(status=503)
            frame = web_frames[cid].copy()

        has_overlay = False
        objects_to_draw = display_overlays.get(cid, [])
        if objects_to_draw:
            detector = camera_motion_detectors.get(cid)
            if detector:
                try:
                    frame = detector.draw_detections(frame, objects_to_draw)
                    has_overlay = True
                except Exception as e:
                    logger.error(f"Error drawing overlay for snapshot {cid}: {e}")

        # Downscale only — asking for a width larger than the sensor gives back a
        # blurrier, bigger file, which is never what the caller wanted.
        resized = False
        if want_w is not None and frame.shape[1] > want_w:
            height = max(1, round(frame.shape[0] * want_w / frame.shape[1]))
            # INTER_AREA is the right filter for shrinking; the defaults ring on
            # the high-contrast edges these cameras produce at night.
            frame = cv2.resize(frame, (want_w, height), interpolation=cv2.INTER_AREA)
            resized = True

        quality = JPEG_QUALITY if want_q is None else want_q
        # `encoded_frame_cache` is SHARED with generate_mjpeg (it is keyed on the
        # camera alone, with no notion of size or quality), so a resized or
        # requality'd frame must never be read from or written to it — one panel
        # asking for 800px would otherwise hand 800px frames to every dashboard
        # viewer for the next 33 ms. Custom requests are a slow poll from one
        # device; paying a re-encode for them is the cheap side of this trade.
        cacheable = not has_overlay and not resized and want_q is None

        current_time = time.time()
        jpeg_bytes = None
        if cacheable:
            cached = encoded_frame_cache.get(cid)
            if cached and (current_time - cached[1]) < FRAME_CACHE_TTL:
                jpeg_bytes = cached[0]

        if jpeg_bytes is None:
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality,
                            int(cv2.IMWRITE_JPEG_OPTIMIZE), 1]
            ret, buffer = cv2.imencode('.jpg', frame, encode_param)
            if not ret:
                return Response(status=500)
            jpeg_bytes = buffer.tobytes()
            if cacheable:
                encoded_frame_cache[cid] = (jpeg_bytes, current_time)

        response = Response(jpeg_bytes, mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'no-store'
        return response
    except Exception as e:
        logger.error(f"Error serving snapshot for {camera_key}: {e}")
        return Response(status=500)


# ---------------------------------------------------------------------------
# Scene detection for the assistant.
#
# "Is anybody in the patio?" used to be answered by sending the JPEG to a
# vision-language model. Measured on real night frames from these cameras, the
# small VLMs invented "una persona con chaqueta oscura" in an empty patio on
# half their runs, and the large one took 10-40s to say the same thing a
# detector says in milliseconds with a confidence score attached.
#
# So the detector answers instead. This endpoint returns the raw findings —
# every class, its confidence, where it sits in the frame and how much of the
# frame it fills — and the assistant phrases them. Deliberately no prose is
# generated here: the model on the other end writes better Spanish than a
# format string, and it knows who is asking and why.
#
# Separate from the per-camera motion detectors on purpose. Those run
# continuously on every camera and stay on the medium model with the
# alert-worthy class filter; this one is on-demand, uses the largest weights,
# and reports everything COCO knows.
# ---------------------------------------------------------------------------
SCENE_MODEL = os.environ.get('SCENE_DETECT_MODEL', 'models/yolo26x.pt')
# Floor for reporting anything at all. Below this the detector is guessing at
# shapes in the dark, and a guess phrased by a fluent assistant reads as fact.
SCENE_MIN_CONFIDENCE = float(os.environ.get('SCENE_MIN_CONFIDENCE', '0.30'))
_scene_detector = None
_scene_detector_lock = threading.Lock()


def get_scene_detector():
    """The largest model, loaded on first use and kept warm afterwards.

    Lazy because it is ~118MB of weights on a box that is also serving an LLM;
    a household that never asks the cameras a question should not pay for it.
    """
    global _scene_detector
    if _scene_detector is None:
        with _scene_detector_lock:
            if _scene_detector is None:
                from object_detector import ObjectDetector
                logger.info(f"Loading scene detection model: {SCENE_MODEL}")
                _scene_detector = ObjectDetector(
                    model_name=SCENE_MODEL,
                    confidence_threshold=SCENE_MIN_CONFIDENCE,
                    detect_all_classes=True,
                )
                # The motion detector floors person/cat/dog at 0.20 on purpose:
                # for an alert, a false alarm costs a glance and a miss costs
                # the thing the camera is for. Answering a question inverts
                # that. Measured on an empty night patio, this model reported
                # `person` at 0.35 and two `bed` at 0.35-0.38 (the pergola) —
                # and Alfred stating "there's somebody in the patio" is worse than
                # the vision model's hedged prose we are replacing. One uniform
                # floor here, with the nuance carried by the certainty bands.
                _scene_detector.class_confidence_thresholds = {}
                _scene_detector.default_class_threshold = SCENE_MIN_CONFIDENCE
    return _scene_detector


def _certainty(confidence: float) -> str:
    """A word for how much weight to put on a detection.

    The assistant sees this, not just the number: a bare 0.35 invites a
    confident sentence, and the whole point of moving off the vision model was
    to stop the family being told about people who are not there.
    """
    if confidence >= 0.70:
        return 'high'
    if confidence >= 0.45:
        return 'medium'
    return 'low'


def _frame_position(center_x: int, center_y: int, width: int, height: int) -> str:
    """Where in the frame something sits, in words the assistant can reuse."""
    horizontal = ('left' if center_x < width / 3
                  else 'right' if center_x > 2 * width / 3
                  else 'centre')
    vertical = ('top' if center_y < height / 3
                else 'bottom' if center_y > 2 * height / 3
                else 'middle')
    return f"{vertical}-{horizontal}"


@app.route('/api/detect/<camera_key>')
def detect_scene(camera_key: str):
    """What the camera can see right now, as data for the assistant to phrase."""
    cid = resolve_camera_id(camera_key)
    if not cid:
        return jsonify({'error': 'camera not found'}), 404

    config = camera_manager.cameras.get(cid)
    with web_frames_lock:
        if cid not in web_frames:
            return jsonify({'error': 'no frame available'}), 503
        frame = web_frames[cid].copy()

    try:
        detector = get_scene_detector()
    except Exception as e:
        logger.error(f"Scene detector unavailable: {e}")
        return jsonify({'error': f'detector unavailable: {e}'}), 503

    started = time.time()
    try:
        objects = detector.detect_objects(frame)
    except Exception as e:
        logger.error(f"Scene detection failed for {cid}: {e}")
        return jsonify({'error': f'detection failed: {e}'}), 500
    elapsed = time.time() - started

    height, width = frame.shape[:2]
    frame_area = float(width * height) or 1.0

    detections = []
    for obj in sorted(objects, key=lambda o: o.confidence, reverse=True):
        x, y, w, h = obj.bounding_box
        detections.append({
            'label': obj.class_name,
            'confidence': round(obj.confidence, 3),
            'certainty': _certainty(obj.confidence),
            'position': _frame_position(obj.center_x, obj.center_y, width, height),
            # Share of the frame it fills — lets the assistant tell a car parked
            # in the driveway from one out on the street.
            'area_pct': round(100.0 * (w * h) / frame_area, 1),
            'box': {'x': x, 'y': y, 'width': w, 'height': h},
        })

    counts: Dict[str, int] = {}
    for d in detections:
        counts[d['label']] = counts.get(d['label'], 0) + 1

    # The camera's *state*, from the tracker: what has been confirmed across
    # frames in the last PRESENCE_HOLD_SECONDS. This is the fact for "is
    # anyone there"; the single-frame list above is what is in view right now
    # and carries one frame's uncertainty.
    tracked = None
    presence = security_publisher.state_of(cid) if security_publisher is not None else None
    if presence is not None:
        tracked = presence.payload(time.time())
        for key in ('camera', 'timestamp', 'hold_seconds'):
            tracked.pop(key, None)
    return jsonify({
        'camera': config.name if config else cid,
        'camera_id': cid,
        'captured_at': datetime.now().isoformat(),
        'model': detector.model_name,
        'inference_seconds': round(elapsed, 3),
        'frame': {'width': width, 'height': height},
        'counts': counts,
        'detections': detections,
        # Said explicitly rather than left to be inferred from an empty list:
        # "nothing detected" is a real answer about a dark patio, and it is not
        # the same claim as "the camera is broken".
        'nothing_detected': not detections,
        'tracked': tracked,
        'note': (
            'Detections come from a YOLO object detector, not a description '
            'model. It reports objects it recognises, with confidence; it says '
            'nothing about lighting, weather or anything outside its classes. '
            'An empty list means it recognised nothing, not that the frame is '
            'unreadable. Report certainty="low" items as possibilities, never '
            'as fact — on a dark frame this detector has reported a person and '
            'two beds in an empty patio at that level. certainty="high" can be '
            'stated plainly. For "is anyone there", `tracked` is the answer: '
            'it is what the tracker confirmed across several frames in the '
            'last half minute (present / active / stationary per kind), and '
            'it beats any single detection above. `tracked` null means the '
            'camera has no tracker state yet.'
        ),
    })


# Serve live video stream (MJPEG)
# Not @require_auth -- see the note above `snapshot()`. Alfred's camera-feed
# skill hands this URL out directly.
@app.route('/stream/<camera_key>')
def stream_video(camera_key: str):
    """Stream video from camera via MJPEG"""
    try:
        cid = resolve_camera_id(camera_key)
        if not cid:
            return "Camera not found", 404
        logger.info(f"Stream request for camera {camera_key} (resolved: {cid})")
        response = Response(
            generate_mjpeg(cid),
            mimetype='multipart/x-mixed-replace; boundary=frame'
        )
        return response
        
    except Exception as e:
        logging.error(f"Error streaming video {camera_key}: {e}")
        logging.error(f"Traceback: {traceback.format_exc()}")
        return "Failed to stream video", 500


# Serve H.264 video stream
# Not @require_auth -- see the note above `snapshot()`. The Android app reads
# this one (android/CHANGELOG.md) and holds no web session.
@app.route('/h264/<camera_key>')
def stream_h264(camera_key: str):
    """Stream video from camera via H.264 (hardware accelerated)"""
    try:
        cid = resolve_camera_id(camera_key)
        if not cid:
            return "Camera not found", 404
        logger.info(f"H.264 stream request for camera {camera_key} (resolved: {cid})")
        
        if not H264_ENCODING_AVAILABLE:
            logger.warning(f"H.264 encoding not available for {cid}")
            return "H.264 encoding not available", 503
        cameras = camera_manager.get_all_cameras()
        config = cameras.get(cid)
        width = config.width if config else 1920
        height = config.height if config else 1080
        fps = config.fps if config else 30
        
        encoder = get_h264_encoder(cid, width, height, fps)
        if not encoder:
            return "Failed to create H.264 encoder", 500
        
        response = Response(
            generate_h264(cid),
            mimetype='video/h264'
        )
        return response
        
    except Exception as e:
        logging.error(f"Error streaming H.264 video {camera_key}: {e}")
        logging.error(f"Traceback: {traceback.format_exc()}")
        return "Failed to stream H.264 video", 500


@app.route('/api/ping')
def ping():
    """Liveness, in the same shape the camera registry answers.

    The wall had no health endpoint of any kind, so the deploy asserted /health
    and got a 404 for as long as that check existed. Everything else it serves
    is either a rendered page — whose text is translated, and would make the
    check fail on a language change — or /api/cameras, which answers {} on a
    fresh install and cannot tell a working wall from a broken one.

    `service` is in the payload on purpose: a bare 200 on this port has already
    meant a different container in this stack.
    """
    cameras = camera_manager.get_all_cameras()
    return jsonify({
        "status": "healthy",
        "service": "camera-wall",
        "cameras": len(cameras),
    })


# Serve camera dashboard
@app.route('/')
def dashboard():
    """Serve main dashboard"""
    cameras = camera_manager.get_all_cameras()
    cameras_dict = {}
    for cid, config in cameras.items():
        cameras_dict[cid] = {
            'camera_id': config.camera_id,
            'device_ip': config.device_ip,
            'name': config.name,
            'username': config.username,
            'port': config.port,
            'stream_path': config.stream_path,
            'fps': config.fps,
            'width': config.width,
            'height': config.height,
            'rotation': config.rotation
        }
    return render_template('dashboard.html', cameras=cameras_dict)

# Serve login page and handle login POST
@app.route('/login', methods=['GET', 'POST'])
def login():
    """Serve login page and handle login requests.

    Reached *through* HomeCore it cannot work, and saying so is the only useful
    thing to do. HomeCore strips `Set-Cookie` on the way back — it must, or this
    app's session cookie would land on HomeCore's own origin under the same name
    and log the member out of the house — so a login here would succeed, set
    nothing, and bounce straight back to this page forever.

    That only happens when PROXY_SHARED_SECRET is missing here, which is also
    what the warning at startup is about: with it set, the member is already
    authenticated and never sees this page at all.
    """
    from flask import request, session, redirect, jsonify
    if _mount_prefix() and not PROXY_SHARED_SECRET and not session.get('authenticated'):
        message = ('This page was opened from the house, and the local password '
                   'is no use here. PROXY_SHARED_SECRET is missing on this server: '
                   'without it HomeCore cannot say who you are. In the meantime, '
                   'entra directo por http://cameras.home:5000.')
        if request.method == 'POST':
            return jsonify({'error': message}), 503
        return render_template('login.html', next_url='/', error=message), 503
    
    if request.method == 'POST':
        next_url = '/'
        # Handle login form submission
        try:
            if request.is_json:
                data = request.get_json() or {}
                username = data.get('username', 'admin')
                password = data.get('password', '')
                next_url = _safe_next_url(data.get('next'))
            else:
                username = request.form.get('username', 'admin')
                password = request.form.get('password', '')
                next_url = _safe_next_url(request.form.get('next'))

            # There is a single admin account, so the password alone decides.
            if not settings_manager.verify_password(password):
                logger.warning(f"Failed login attempt from {request.remote_addr}")
                if request.is_json:
                    return jsonify({'error': 'Invalid password'}), 401
                else:
                    return render_template('login.html', next_url=next_url,
                                           error='Invalid password'), 401

            session['authenticated'] = True
            session['username'] = username

            logger.info(f"User logged in: {username} from {request.remote_addr}")

            if request.is_json:
                # The browser follows this one itself, so the proxy never sees
                # it as a redirect and cannot fix it up - it has to leave here
                # already carrying the mount.
                return jsonify({'success': True,
                                'redirect': f'{_mount_prefix()}{next_url}'})
            else:
                return redirect(next_url)

        except Exception as e:
            logger.error(f"Login error: {e}")
            if request.is_json:
                return jsonify({'error': 'Login failed'}), 500
            else:
                return render_template('login.html', next_url=next_url, error='Login failed')
    
    # Handle GET request - show login form
    next_url = _safe_next_url(request.args.get('next'))
    return render_template('login.html', next_url=next_url)


@app.route('/api/change_password', methods=['POST'])
@require_auth
def change_password():
    """Change the admin password (requires the current one)."""
    from flask import session

    data = request.get_json() or {}
    current = data.get('current_password', '')
    new = data.get('new_password', '')

    if not settings_manager.verify_password(current):
        logger.warning(f"Failed password change attempt from {request.remote_addr}")
        return jsonify({'error': 'Current password is incorrect'}), 401

    if len(new) < 8:
        return jsonify({'error': 'New password must be at least 8 characters'}), 400

    if not settings_manager.set_admin_password(new):
        return jsonify({'error': 'Failed to save new password'}), 500

    logger.info("Admin password changed")
    # Force a fresh login with the new password.
    session.clear()
    return jsonify({'success': True})


# Serve camera settings page (global settings)
@app.route('/settings')
@require_auth
def global_settings():
    """Serve global settings page for adding cameras"""
    # Load cameras from settings manager
    cameras = settings_manager.get_cameras()

    # Get cameras from camera_registry via HTTP request
    registry_cameras_list = get_registry_cameras()
    return render_template('settings.html',
                         camera_ip='',
                         cameras=cameras,
                         registry_cameras=registry_cameras_list,
                         recording_presets=settings_manager.get_recording_presets())

def get_registry_cameras():
    """Fetch cameras from camera_registry API via HTTP request"""
    try:
        # Get the registry server URL (default to local network registry at cameras.home:5001)
        registry_url = os.environ.get('CAMERA_REGISTRY_URL', 'http://cameras.home:5001')
        
        # Make HTTP GET request to /api/list
        response = requests.get(f"{registry_url}/api/list", timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            cameras = data.get('cameras', [])
            return cameras
        else:
            logger.warning(f"Camera registry API returned status {response.status_code}, returning empty list")
            return []
    except Exception as e:
        logger.error(f"Error fetching cameras from registry API: {e}")
        logger.error(f"Registry URL: {os.environ.get('CAMERA_REGISTRY_URL', 'http://cameras.home:5001')}")
        return []


@app.route('/api/cameras')
def get_cameras():
    """Return list of all cameras with state and configuration"""
    cameras = camera_manager.get_all_cameras()
    result = {}
    for cid, config in cameras.items():
        result[cid] = {
            'camera_id': config.camera_id,
            'device_ip': config.device_ip,
            'name': config.name,
            'port': config.port,
            'stream_path': config.stream_path,
            'fps': config.fps,
            'width': config.width,
            'height': config.height,
            'rotation': config.rotation,
            'stream_type': config.stream_type,
            'state': camera_manager.get_camera_state(cid).value if camera_manager.get_camera_state(cid) else 'unknown'
        }
    return jsonify(result)


def _normalise_stream_path(path) -> str:
    """A stream path always starts with '/'.

    Cameras show it both ways in their own UIs — the one added on 2026-09-20
    displayed "Source path: live0", which was stored verbatim and produced
    `404 Stream Not Found` on every frame. The 404 then masked a second error
    (a mistyped username), so fixing one symptom still left the camera dark.
    Normalising here means neither a person nor an assistant can reintroduce it.
    """
    s = str(path or '').strip()
    if not s:
        return s
    return s if s.startswith('/') else '/' + s


def _conn_fingerprint(cfg) -> tuple:
    """The fields that decide which stream a camera dials.

    Anything here changing means the live fetcher is now connecting to the
    wrong thing, so the stream has to be rebuilt. Rotation, zones and recording
    rules are deliberately absent: they change what is done with the frames,
    not where the frames come from.
    """
    return (str(cfg.get('device_ip', '')), str(cfg.get('username', '')),
            str(cfg.get('password', '')), str(cfg.get('port', '')),
            str(cfg.get('stream_path', '')), str(cfg.get('stream_type', '')))


def _restart_camera_stream(cid) -> bool:
    """Rebuild a camera's live stream from whatever is on disk right now.

    Config is saved by one code path and *used* by another: settings_manager
    writes cameras.json, while camera_manager holds the CameraConfig the
    fetcher thread actually dials. Saving alone changes nothing a camera does —
    which is why correcting a password through the settings page looked like it
    had not worked at all.
    """
    cfg = dict((settings_manager.get_cameras() or {}).get(cid) or {})
    if not cfg:
        return False
    _teardown_camera(cid)
    camera_manager.add_camera(CameraConfig(
        camera_id=cid,
        device_ip=cfg.get('device_ip', ''),
        name=cfg.get('name', ''),
        username=cfg.get('username', ''),
        password=cfg.get('password', ''),
        port=int(cfg.get('port', 554) or 554),
        stream_path=cfg.get('stream_path', '/live0'),
        fps=int(cfg.get('fps', 30) or 30),
        width=int(cfg.get('width', 1920) or 1920),
        height=int(cfg.get('height', 1080) or 1080),
        rotation=int(cfg.get('rotation', 0) or 0),
        stream_type=cfg.get('stream_type', 'rtsp'),
    ))
    start_new_camera_fetcher(cid)
    logger.info(f"Camera {cid} stream restarted")
    return True


# --- finding a camera that moved -------------------------------------------
#
# Every camera here is pinned to an IP, and a router that reshuffles its DHCP
# pool breaks all of them at once: on 2026-09-20 all four read `error` while two
# were online and streaming, three hops away at addresses nothing here knew.
#
# The identifier this uses is the camera's own RTSP credential, not its MAC.
# A MAC is the better name for "this physical camera", but this process cannot
# resolve one: the wall runs on a docker bridge (172.29.0.0/16) and reaches the
# cameras routed, so its ARP table is empty and always will be. The credential
# is per-camera, already stored, and only the right camera answers to it — which
# is exactly the property wanted. `mac` is still recorded on the config when it
# is known, because it is what a person reads when deciding what to reserve on
# the router; nothing here resolves by it.
import base64 as _b64
import hashlib as _hashlib
import re as _re
import socket as _socket
from concurrent.futures import ThreadPoolExecutor as _Pool


def _rtsp_describe(ip, path, port=554, auth=None, cseq=1, timeout=4):
    """One RTSP DESCRIBE. Returns the status line, or '' if it could not ask."""
    url = f"rtsp://{ip}:{port}{path}"
    head = f"DESCRIBE {url} RTSP/1.0\r\nCSeq: {cseq}\r\nAccept: application/sdp\r\n"
    if auth:
        head += f"Authorization: {auth}\r\n"
    head += "User-Agent: home-cameras\r\n\r\n"
    try:
        s = _socket.create_connection((ip, port), timeout)
        s.settimeout(timeout)
        s.sendall(head.encode())
        data = s.recv(4096).decode("utf-8", "replace")
        s.close()
        return data
    except Exception:
        return ""


def _rtsp_authenticates(ip, path, user, password, port=554):
    """True when these credentials open this stream on this address.

    Digest first, Basic second, because that is the order the cameras here
    offer. A 200 is the only yes: a 401 means the path exists but this is not
    the camera we are looking for, and a 404 means it is not even that model.
    """
    first = _rtsp_describe(ip, path, port)
    if not first:
        return False
    if first.startswith("RTSP/1.0 200"):
        return True
    m = _re.search(r'WWW-Authenticate:\s*Digest\s+(.*)', first)
    if m:
        f = dict(_re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        realm, nonce = f.get("realm", ""), f.get("nonce", "")
        ha1 = _hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
        ha2 = _hashlib.md5(f"DESCRIBE:rtsp://{ip}:{port}{path}".encode()).hexdigest()
        resp = _hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        hdr = (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
               f'uri="rtsp://{ip}:{port}{path}", response="{resp}"')
        return _rtsp_describe(ip, path, port, hdr, 2).startswith("RTSP/1.0 200")
    if "Basic" in first:
        b = _b64.b64encode(f"{user}:{password}".encode()).decode()
        return _rtsp_describe(ip, path, port, f"Basic {b}", 2).startswith("RTSP/1.0 200")
    return False


def _subnet_of(ip):
    """The /24 an address sits in, as a list of hosts. '' when it is not one."""
    parts = (ip or "").split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return []
    return [f"{parts[0]}.{parts[1]}.{parts[2]}.{n}" for n in range(1, 255)]


def _rtsp_hosts(candidates, port=554, workers=64):
    """Which of these addresses have an RTSP port open."""
    def probe(ip):
        try:
            s = _socket.create_connection((ip, port), 1)
            s.close()
            return ip
        except Exception:
            return None
    with _Pool(max_workers=workers) as pool:
        return [ip for ip in pool.map(probe, candidates) if ip]


def rediscover_camera_ip(cid):
    """Find where a configured camera actually is now. Returns (ip, payload).

    Scans the /24 its last known address sat in, then asks each open RTSP port
    whether this camera's credentials open it. Returns the first address that
    says yes — there can only be one, because the credential is per camera.
    """
    cameras = settings_manager.get_cameras()
    cfg = cameras.get(cid)
    if not cfg:
        return None, {"status": "error", "message": "Camera not found"}
    user, password = cfg.get("username", ""), cfg.get("password", "")
    path = cfg.get("stream_path", "/live0")
    port = int(cfg.get("port", 554) or 554)
    if not (user and password):
        return None, {"status": "error",
                      "message": "This camera has no stored credentials, so it "
                                 "cannot be identified by them"}
    hosts = _rtsp_hosts(_subnet_of(cfg.get("device_ip", "")), port)
    logger.info(f"rediscover {cid}: {len(hosts)} host(s) with {port} open")
    for ip in hosts:
        if _rtsp_authenticates(ip, path, user, password, port):
            return ip, {"status": "success", "device_ip": ip,
                        "was": cfg.get("device_ip"), "scanned": len(hosts)}
    return None, {"status": "error", "scanned": len(hosts),
                  "message": "No camera on this subnet answered to these "
                             "credentials — it is off, or on another network"}


def repoint_camera(cid, new_ip, mac=None):
    """Move a camera to a new address, keeping its id and everything on it.

    Deliberately not remove-and-re-add: that mints a fresh camera_id, and the
    motion zones, rotation and recording rules hang off the old one. A camera
    that moved is the same camera.
    """
    cameras = settings_manager.get_cameras()
    cfg = dict(cameras.get(cid) or {})
    if not cfg:
        return False, {"status": "error", "message": "Camera not found"}
    cfg["device_ip"] = new_ip
    if mac:
        cfg["mac"] = mac
    cameras[cid] = cfg
    if not settings_manager.save_cameras(cameras):
        return False, {"status": "error", "message": "Could not save the new address"}

    _teardown_camera(cid)
    config = CameraConfig(
        camera_id=cid,
        device_ip=new_ip,
        name=cfg.get("name", ""),
        username=cfg.get("username", ""),
        password=cfg.get("password", ""),
        port=int(cfg.get("port", 554) or 554),
        stream_path=cfg.get("stream_path", "/live0"),
        fps=int(cfg.get("fps", 30) or 30),
        width=int(cfg.get("width", 1920) or 1920),
        height=int(cfg.get("height", 1080) or 1080),
        rotation=int(cfg.get("rotation", 0) or 0),
        stream_type=cfg.get("stream_type", "rtsp"),
    )
    camera_manager.add_camera(config)
    start_new_camera_fetcher(cid)
    logger.info(f"Camera {cid} repointed to {new_ip}")
    return True, {"status": "success", "camera_id": cid, "device_ip": new_ip}


@app.route('/api/cameras/<camera_key>/rediscover', methods=['POST'])
@require_auth
def api_rediscover_camera(camera_key: str):
    """Find this camera's current address and move it there.

    `?dry=1` reports where it is without changing anything.
    """
    try:
        cid = resolve_camera_id(camera_key)
        if not cid:
            return jsonify({'status': 'error', 'message': 'Camera not found'}), 404
        ip, payload = rediscover_camera_ip(cid)
        if not ip:
            return jsonify(payload), 404
        if request.args.get('dry'):
            payload['dry_run'] = True
            return jsonify(payload), 200
        body = request.get_json(silent=True) or {}
        ok, moved = repoint_camera(cid, ip, mac=body.get('mac'))
        moved.update({k: payload[k] for k in ('was', 'scanned') if k in payload})
        return jsonify(moved), (200 if ok else 500)
    except Exception as e:
        logger.error(f"Error rediscovering {camera_key}: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# Adding and removing a camera over HTTP, so something that is not a browser
# can do it. Until 2026-09-20 both lived only as Socket.IO events behind
# `require_socket_auth`, which a `requests`-based caller cannot reach — the
# assistant could list cameras and take snapshots but not manage them.
#
# `@require_auth` on purpose, matching every other mutating route here
# (`/api/cameras/<key>/settings`, `/api/global/settings`, `/api/import_config`).
# The listing route above is deliberately open and these are deliberately not:
# reading which cameras exist is not the same as deleting one. A caller reaches
# these through HomeCore, which authenticates the member and re-signs the hop
# with X-Proxy-User / X-Proxy-Secret — so no caller needs the master secret.
@app.route('/api/cameras', methods=['POST'])
@require_auth
def api_add_camera():
    """Add a camera. Body is the same dict the `add_camera` socket event takes."""
    try:
        data = request.get_json(silent=True) or {}
        ok, payload = _add_camera_config(data)
        return jsonify(payload), (201 if ok else 400)
    except Exception as e:
        logger.error(f"Error adding camera over HTTP: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/cameras/<camera_key>', methods=['DELETE'])
@require_auth
def api_remove_camera(camera_key: str):
    """Remove a camera by name, id or device IP — whatever `resolve_camera_config` takes."""
    try:
        ok, payload = _remove_camera_key(camera_key)
        # 404 rather than 400: the key named nothing this wall knows, and a
        # caller retrying a delete that already happened should not read as a
        # malformed request.
        return jsonify(payload), (200 if ok else 404)
    except Exception as e:
        logger.error(f"Error removing camera over HTTP: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/export_config')
@require_auth
def export_config():
    """Export all camera configurations, admin settings, and global settings as a downloadable JSON file"""
    cameras = settings_manager.get_cameras()
    admin = settings_manager.get_admin_config()
    global_settings = settings_manager.get_global_settings()
    export_data = {
        'version': 3,
        'admin': {
            'password_hash': admin.password_hash if admin else '',
            'salt': admin.salt if admin else ''
        } if admin else {},
        'global_settings': global_settings,
        'cameras': cameras,
        # Everything set on a camera the *registry* owns — the recording
        # window among it. AGENTS.md points at this file as the remedy for the
        # config loss of build 104, and a backup that silently drops half the
        # per-camera settings is worse than one that admits it has none: a
        # restore would put every camera back on the house schedule and start
        # any camera set to "never record" recording again.
        'registry_camera_settings': settings_manager.get_registry_camera_settings(),
    }
    response = jsonify(export_data)
    response.headers['Content-Disposition'] = 'attachment; filename=cameras_config.json'
    return response


# Serve add camera page
@app.route('/add_camera')
@require_auth
def add_camera_page():
    """Serve add camera page"""
    return render_template('add_camera.html')


# Serve camera edit settings page
@app.route('/cameras/<camera_key>/edit_settings')
@require_auth
def camera_edit_settings(camera_key: str):
    """Serve edit settings page for specific camera"""
    # The presets go in so the options can be labelled with the hours they
    # actually mean. "Outside cameras" on its own tells you nothing about when
    # the camera will record, and the answer lives on a different page.
    return render_template('edit_settings.html', camera_id=camera_key,
                           recording_presets=settings_manager.get_recording_presets())

# API endpoint to get camera settings
@app.route('/api/cameras/<camera_key>/settings')
@require_auth
def get_camera_settings(camera_key: str):
    """Get settings for a specific camera (by camera_id or device_ip)"""
    cameras = settings_manager.get_cameras()
    cid = settings_manager._resolve_key(camera_key, cameras)
    
    # Check local config first
    if cid:
        camera_config = cameras[cid]
        return jsonify({
            'camera_id': cid,
            'device_ip': camera_config.get('device_ip', camera_key),
            'name': camera_config.get('name', ''),
            'username': camera_config.get('username', ''),
            'port': camera_config.get('port', 80),
            'stream_path': camera_config.get('stream_path', '/stream'),
            'stream_type': camera_config.get('stream_type', 'rtsp'),
            'rotation': camera_config.get('rotation', 0),
            'show_all_objects': camera_config.get('show_all_objects', False),
            'motion_zones': camera_config.get('motion_zones', []),
            'enabled_object_classes': camera_config.get('enabled_object_classes', None),
            'object_detection_model': camera_config.get('object_detection_model', None),
            'stuck_region': camera_config.get('stuck_region', None),
            'recording_duration': camera_config.get('recording_duration', None),
            'fps': camera_config.get('fps', 30),
            'width': camera_config.get('width', 1920),
            'height': camera_config.get('height', 1080),
            # The window this camera records in. Absent means it follows the
            # house — `is_recording_enabled(camera_key)` has honoured this
            # since it was written; there was simply no way to set it.
            **settings_manager.get_camera_recording_window(cid),
        })

    # Try registry camera
    config = resolve_camera_config(camera_key)
    if config:
        cid = config.camera_id
        saved = settings_manager.get_registry_camera_setting(cid)
        return jsonify({
            'camera_id': cid,
            'device_ip': config.device_ip,
            'name': saved.get('name', ''),
            'username': config.username,
            'port': config.port,
            'stream_path': config.stream_path,
            'stream_type': config.stream_type,
            'rotation': config.rotation,
            'show_all_objects': saved.get('show_all_objects', False),
            'motion_zones': saved.get('motion_zones', []),
            'enabled_object_classes': saved.get('enabled_object_classes', None),
            'object_detection_model': saved.get('object_detection_model', None),
            'stuck_region': saved.get('stuck_region', None),
            'recording_duration': saved.get('recording_duration', None),
            'fps': config.fps,
            'width': config.width,
            'height': config.height,
            # Same window as the branch above. Left out of one of these two
            # near-identical dicts, the page reports "follows the house" for a
            # camera that does not, and the next save acts on what it read.
            **settings_manager.get_camera_recording_window(cid),
        })

    return jsonify({'error': 'Camera not found'}), 404


# API endpoint to update camera settings
@app.route('/api/cameras/<camera_key>/settings', methods=['POST'])
@require_auth
def update_camera_settings(camera_key: str):
    """Update settings for a specific camera (by camera_id or device_ip)"""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    cameras = settings_manager.get_cameras()
    cid = settings_manager._resolve_key(camera_key, cameras)
    is_registry_camera = cid is None

    if is_registry_camera:
        config = resolve_camera_config(camera_key)
        if not config:
            return jsonify({'error': 'Camera not found'}), 404
        camera_config = dict(settings_manager.get_registry_camera_setting(config.camera_id))
    else:
        # A copy, not the record itself: `get_cameras()` hands back its own
        # cache, so editing it in place means a request that is rejected below
        # still leaves its changes where every other reader can see them — and
        # the next successful save on any camera writes them to disk.
        camera_config = dict(cameras[cid])

    # What it was dialling before this request touched anything.
    _conn_before = _conn_fingerprint(camera_config)

    if 'name' in data:
        camera_config['name'] = data['name']
    if 'username' in data:
        camera_config['username'] = data['username']
    # Password and address, added 2026-09-20. Without them the settings page
    # could not fix a camera whose credentials or address were wrong — which is
    # every camera added with a typo, and every camera the router moved. The
    # only route that could change an address was remove-and-re-add, which
    # mints a new camera_id and loses the zones and recording rules with it.
    if 'password' in data:
        camera_config['password'] = data['password']
    if 'port' in data:
        camera_config['port'] = int(data['port'])
    if 'stream_path' in data:
        camera_config['stream_path'] = _normalise_stream_path(data['stream_path'])
    if 'stream_type' in data:
        camera_config['stream_type'] = data['stream_type']
    if 'rotation' in data:
        camera_config['rotation'] = int(data['rotation'])
    if 'recording_duration' in data:
        camera_config['recording_duration'] = data['recording_duration']
    if 'show_all_objects' in data:
        camera_config['show_all_objects'] = bool(data['show_all_objects'])
    if 'motion_zones' in data:
        camera_config['motion_zones'] = data['motion_zones']
    if 'enabled_object_classes' in data:
        camera_config['enabled_object_classes'] = data['enabled_object_classes'] or None
    if 'object_detection_model' in data:
        camera_config['object_detection_model'] = data['object_detection_model'] or None
    # Recording window. `null` for a field means "follow the house", which is
    # how a camera goes back to the default instead of being stuck with
    # whatever it was last given. Both hours or neither: half a window silently
    # pairs one camera's start with the house's end, and that shows up as a
    # missing night of recordings. What an hour *is* stays in settings_manager,
    # so this route and `set_camera_recording_window` cannot drift apart.
    if 'recording_start_hour' in data or 'recording_end_hour' in data:
        start = data.get('recording_start_hour')
        end = data.get('recording_end_hour')
        if (start is None) != (end is None):
            return jsonify({'error': 'Pon las dos horas o ninguna: media ventana '
                                     'no es una ventana'}), 400
        for field, value in (('recording_start_hour', start), ('recording_end_hour', end)):
            if value is None:
                camera_config.pop(field, None)
            else:
                try:
                    camera_config[field] = SettingsManager.validate_recording_hour(field, value)
                except ValueError as e:
                    return jsonify({'error': str(e)}), 400
    # Which named window this camera follows, if any. `null` or "" means it
    # follows none — either its own hours above, or the house. What a preset
    # *is* stays in settings_manager for the same reason the hours do.
    if 'recording_preset' in data:
        preset = data['recording_preset'] or None
        if preset is None:
            camera_config.pop('recording_preset', None)
        else:
            try:
                camera_config['recording_preset'] = \
                    settings_manager.validate_recording_preset(preset)
            except ValueError as e:
                return jsonify({'error': str(e)}), 400
    if 'recording_enabled' in data:
        if data['recording_enabled'] is None:
            camera_config.pop('recording_enabled', None)
        elif not isinstance(data['recording_enabled'], bool):
            # `bool("false")` is True, so a client that sends the string wins
            # the opposite of what it asked for.
            return jsonify({'error': 'recording_enabled tiene que ser true, false o null'}), 400
        else:
            camera_config['recording_enabled'] = data['recording_enabled']
    if 'stuck_region' in data:
        camera_config['stuck_region'] = data['stuck_region']
        if camera_key in stuck_region_config:
            if data['stuck_region'] and data['stuck_region'].get('enabled'):
                stuck_region_config[camera_key] = data['stuck_region']
            else:
                stuck_region_config.pop(camera_key, None)
                stuck_region_state.pop(camera_key, None)
        elif data['stuck_region'] and data['stuck_region'].get('enabled'):
            stuck_region_config[camera_key] = data['stuck_region']

    # Handle device_ip change (just a field update — no key migration needed)
    if 'device_ip' in data:
        camera_config['device_ip'] = data['device_ip']
    # Ensure camera_id is set
    if is_registry_camera:
        save_cid = config.camera_id if config else camera_key
    else:
        save_cid = cid
    camera_config['camera_id'] = save_cid

    # Persist to the appropriate store. `camera_config` started as a copy of
    # the whole stored record, so it *is* the record — and it has to be written
    # as one. A merge would quietly restore every key this request removed,
    # which is exactly how "follow the house" and undoing "never record" would
    # report success and change nothing.
    if is_registry_camera:
        save_ok = settings_manager.update_registry_camera_setting(save_cid, camera_config,
                                                                  replace=True)
    else:
        cameras[save_cid] = camera_config
        save_ok = settings_manager.save_cameras(cameras)

    if save_ok:
        if save_cid in camera_manager.cameras:
            cm_config = camera_manager.cameras[save_cid]
            if 'device_ip' in camera_config:
                cm_config.device_ip = camera_config['device_ip']
            if 'username' in camera_config:
                cm_config.username = camera_config['username']
            if 'port' in camera_config:
                cm_config.port = camera_config['port']
            if 'stream_path' in camera_config:
                cm_config.stream_path = camera_config['stream_path']
            # The password was missing from this list, so a corrected one was
            # written to disk and never reached the object doing the dialling.
            if 'password' in camera_config:
                cm_config.password = camera_config['password']
            cm_config.rotation = camera_config.get('rotation', cm_config.rotation)
            cm_config.motion_zones = camera_config.get('motion_zones', cm_config.motion_zones or [])
        
        # Saving is not applying. Mutating the CameraConfig above does not
        # disturb the fetcher thread already talking to the old address with
        # the old credentials, so a corrected password reported success and
        # changed nothing until something else happened to restart the camera.
        if _conn_fingerprint(camera_config) != _conn_before:
            logger.info(f"Connection settings changed for {save_cid}; restarting its stream")
            _restart_camera_stream(save_cid)

        if 'show_all_objects' in data:
            camera_show_all_objects[save_cid] = bool(data['show_all_objects'])
            logger.info(f"Updated show_all_objects for {save_cid}: {camera_show_all_objects[save_cid]}")
            keys_to_remove = [k for k, v in motion_events.items() if v.camera_id == save_cid]
            for k in keys_to_remove:
                del motion_events[k]
            encoded_frame_cache.pop(save_cid, None)
        
        if save_cid in camera_motion_detectors:
            camera_motion_detector = camera_motion_detectors[save_cid]
            camera_motion_detector.zones = []
            motion_zones = camera_config.get('motion_zones', [])
            if motion_zones:
                for zone_data in motion_zones:
                    x_val = zone_data.get('x', 0)
                    y_val = zone_data.get('y', 0)
                    w_val = zone_data.get('width', 100)
                    h_val = zone_data.get('height', 100)
                    
                    x = x_val if 0 <= x_val <= 1 else int(x_val)
                    y = y_val if 0 <= y_val <= 1 else int(y_val)
                    w = w_val if 0 <= w_val <= 1 else int(w_val)
                    h = h_val if 0 <= h_val <= 1 else int(h_val)
                    
                    zone = MotionZone(
                        name=zone_data.get('name', 'Zone'),
                        x=x,
                        y=y,
                        width=w,
                        height=h,
                        sensitivity=float(zone_data.get('sensitivity', 0.5)),
                        min_area=int(zone_data.get('min_area', 500)),
                        filter_light_changes=bool(zone_data.get('filter_light_changes', False)),
                        light_change_threshold=float(zone_data.get('light_change_threshold', 0.35))
                    )
                    camera_motion_detector.add_zone(zone)
                logger.info(f"Updated {len(motion_zones)} zones for {save_cid}")
            else:
                logger.info(f"No motion zones configured for {save_cid} - detection disabled")

            if 'enabled_object_classes' in data and camera_motion_detector.object_detector:
                enabled = data['enabled_object_classes']
                camera_motion_detector.object_detector.enabled_classes = (
                    set(enabled) if enabled else None
                )
                display_overlays.pop(save_cid, None)

            if 'object_detection_model' in data:
                camera_resolutions.pop(f'{save_cid}_model', None)
                camera_motion_detectors.pop(save_cid, None)
                display_overlays.pop(save_cid, None)

        return jsonify({'success': True, 'message': 'Settings updated', 'camera_id': save_cid, 'device_ip': camera_config.get('device_ip', '')})
    else:
        return jsonify({'error': 'Failed to save settings'}), 500


# API endpoint to start a recording on demand
@app.route('/api/cameras/<camera_key>/record', methods=['POST'])
@require_auth
def trigger_recording(camera_key: str):
    """Start a recording now, ignoring the configured recording window.

    Motion-triggered recording is gated by settings_manager.is_recording_enabled(camera_key)
    (23:00-06:00 by default). An explicit API trigger is a deliberate act, so the
    window does not apply to it — only an already-running recording blocks it.

    Body (optional): {"duration": <seconds>}, defaulting to the camera's
    configured duration and then the global one.
    """
    camera_id = resolve_camera_id(camera_key)
    if not camera_id:
        return jsonify({'success': False, 'error': f'Unknown camera: {camera_key}'}), 404

    if is_recording(camera_id):
        return jsonify({'success': False, 'error': 'Recording already in progress',
                        'camera_id': camera_id}), 409

    frame = camera_manager.get_frame(camera_id)
    if frame is None:
        return jsonify({'success': False, 'error': 'No frame available yet',
                        'camera_id': camera_id}), 503

    data = request.get_json(silent=True) or {}
    duration = data.get('duration')
    if duration is None:
        duration = settings_manager.get_camera_recording_duration(camera_id)
    if duration is None:
        duration = settings_manager.get_recording_duration()
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'duration must be an integer'}), 400
    if duration <= 0:
        return jsonify({'success': False, 'error': 'duration must be positive'}), 400

    timestamp = time.time()
    object_tags = ['manual']
    rec_path = start_recording(
        camera_id, frame, [], timestamp,
        object_tags=object_tags,
        pre_buffer=drain_ring_buffer(camera_id),
        duration=duration,
        camera_name=_get_camera_name(camera_id)
    )
    if not rec_path:
        return jsonify({'success': False, 'error': 'Failed to start recording',
                        'camera_id': camera_id}), 500

    logger.info(f"Manual recording triggered for {camera_id}: {rec_path} ({duration}s)")
    socketio.emit('recording_status', {'camera_key': camera_id, 'recording': True})
    _camera_recording_info[camera_id] = {'rel_path': rec_path, 'object_tags': object_tags}
    # Exempt this recording from the window check so the frame loop keeps
    # feeding it instead of stopping it on the next tick.
    _manual_recordings.add(camera_id)

    mqtt_c = get_mqtt()
    if mqtt_c:
        mqtt_c.publish_recording(camera_id, rec_path, 'started_manual', [], object_tags)

    return jsonify({'success': True, 'camera_id': camera_id,
                    'path': rec_path, 'duration': duration})


# API endpoint to get global settings
@app.route('/api/global/settings')
@require_auth
def get_global_settings():
    """Get global application settings"""
    settings = settings_manager.get_global_settings()
    return jsonify(settings)


# API endpoint to update global settings
@app.route('/api/global/settings', methods=['POST'])
@require_auth
def update_global_settings():
    """Update global application settings"""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # What an hour is lives in settings_manager, so this route cannot drift
    # from the per-camera one. This used to merge whatever arrived straight in,
    # so the house window would happily accept hour 99 — the per-camera route
    # has refused that since the window was split per camera, and the global
    # one never did.
    for field in ('recording_start_hour', 'recording_end_hour'):
        if field in data:
            try:
                data[field] = SettingsManager.validate_recording_hour(field, data[field])
            except ValueError as e:
                return jsonify({'error': str(e)}), 400

    # Presets are a name and a window each. An unchecked one is worse than a
    # bad house window: every camera following it moves at once, and the hours
    # it moves them to are never shown on the camera's own page.
    if 'recording_presets' in data:
        presets = data['recording_presets']
        if not isinstance(presets, dict):
            return jsonify({'error': 'Los horarios tienen que venir como un objeto'}), 400
        clean = {}
        for name, window in presets.items():
            if not isinstance(window, dict):
                return jsonify({'error': f'El horario «{name}» tiene que ser un objeto'}), 400
            try:
                clean[name] = {
                    'start_hour': SettingsManager.validate_recording_hour(
                        'recording_start_hour', window.get('start_hour')),
                    'end_hour': SettingsManager.validate_recording_hour(
                        'recording_end_hour', window.get('end_hour')),
                }
            except ValueError as e:
                return jsonify({'error': f'{name}: {e}'}), 400
        data['recording_presets'] = clean

    # Load current settings and update with new values
    current = settings_manager.get_global_settings()
    current.update(data)

    if settings_manager.save_global_settings(current):
        return jsonify({'success': True, 'message': 'Global settings updated'})
    else:
        return jsonify({'error': 'Failed to save global settings'}), 500


# API endpoint to import camera config
@app.route('/api/import_config', methods=['POST'])
@require_auth
def import_config():
    """Import camera configuration from JSON"""
    try:
        data = request.get_json()
        if not data or 'config' not in data:
            return jsonify({'error': 'No config provided'}), 400
        
        imported = data['config']
        merge = data.get('merge', False)
        
        # Get current cameras
        current_cameras = settings_manager.get_cameras()
        
        # If not merging, clear existing cameras
        if not merge:
            current_cameras = {}
        
        # Import cameras from the config
        cameras_loaded = 0
        raw_cameras = {}
        if 'cameras' in imported:
            raw_cameras = imported['cameras']
        else:
            for k, v in imported.items():
                if k not in ['admin', 'global_settings', 'registry_camera_settings', 'version'] and isinstance(v, dict):
                    raw_cameras[k] = v
        
        # Run through migration to ensure all entries have camera_id
        migrated = settings_manager._migrate_cameras_dict(raw_cameras,
                                                          settings_manager.cameras_file)
        current_cameras.update(migrated)
        cameras_loaded = len(migrated)
        
        # Restore admin config if present
        if 'admin' in imported and isinstance(imported['admin'], dict):
            admin_data = imported['admin']
            if admin_data.get('password_hash') and admin_data.get('salt'):
                settings_manager._save_json(settings_manager.admin_file, {
                    'password_hash': admin_data['password_hash'],
                    'salt': admin_data['salt']
                })

        # Restore global settings if present
        if 'global_settings' in imported and isinstance(imported['global_settings'], dict):
            settings_manager.save_global_settings(imported['global_settings'])

        # And the per-camera settings the registry owns — the recording window
        # among them. Written one camera at a time rather than wholesale so a
        # merge stays a merge: a restore should not delete the settings of a
        # camera the backup had never heard of. Older exports (version 2) have
        # no such key and simply leave these alone.
        registry_settings = imported.get('registry_camera_settings')
        if isinstance(registry_settings, dict):
            for cam_key, values in registry_settings.items():
                if isinstance(values, dict):
                    settings_manager.update_registry_camera_setting(
                        cam_key, values, replace=not merge)

        # Save the merged config
        if settings_manager.save_cameras(current_cameras):
            # Reload cameras in camera_manager
            for key, config in current_cameras.items():
                if key not in camera_manager.cameras:
                    try:
                        stored_id = config.get('camera_id', key)
                        device_ip = config.get('device_ip', key)
                        camera_config = CameraConfig(
                            camera_id=stored_id,
                            device_ip=device_ip,
                            username=config.get('username', ''),
                            password=config.get('password', ''),
                            port=int(config.get('port', 80)),
                            stream_path=config.get('stream_path', '/stream'),
                            fps=int(config.get('fps', 30)),
                            width=int(config.get('width', 1920)),
                            height=int(config.get('height', 1080)),
                            rotation=int(config.get('rotation', 0)),
                            stream_type=config.get('stream_type', 'rtsp'),
                            name=config.get('name', '')
                        )
                        camera_manager.add_camera(camera_config)
                        start_new_camera_fetcher(camera_config.camera_id)
                    except Exception as e:
                        logger.error(f"Error loading imported camera {key}: {e}")
            
            _emit_camera_list()
            return jsonify({'success': True, 'cameras_loaded': cameras_loaded})
        else:
            return jsonify({'error': 'Failed to save config'}), 500
        
    except Exception as e:
        logger.error(f"Error importing config: {e}")
        return jsonify({'error': str(e)}), 500


# WebSocket endpoint for real-time updates
@socketio.on('connect')
@require_socket_auth
def handle_connect():
    """Handle client connection"""
    logger.info('Client connected')
    cameras = camera_manager.get_all_cameras()
    emit('camera_list', {cid: {
        'camera_id': config.camera_id,
        'device_ip': config.device_ip,
        'name': config.name,
        'username': config.username,
        'port': config.port,
        'stream_path': config.stream_path,
        'fps': config.fps,
        'width': config.width,
        'height': config.height,
        'rotation': config.rotation,
        'stream_type': config.stream_type,
        'state': camera_manager.get_camera_state(cid).value if camera_manager.get_camera_state(cid) else 'unknown'
    } for cid, config in cameras.items()})


def _add_camera_config(data: dict):
    """Create a camera from a dict of fields. Returns (ok, payload).

    Extracted from the Socket.IO handler on 2026-09-20 so that the wall's own
    UI and an HTTP caller add a camera by exactly the same path. It must not
    `emit`: an HTTP caller has no socket, and a helper that can only answer on
    one transport is how the two drift apart.
    """
    device_ip = data.get('device_ip', '')
    if not device_ip:
        return False, {'status': 'error', 'message': 'Device IP is required'}

    name = data.get('name', '')

    # Get existing cameras to check for duplicates
    cameras = settings_manager.get_cameras()
    for cfg in cameras.values():
        if cfg.get('device_ip') == device_ip:
            return False, {'status': 'error',
                           'message': 'Camera with this IP already exists'}

    stream_type = data.get('stream_type', 'rtsp')

    new_id = uuid.uuid4().hex
    camera_config = {
        'camera_id': new_id,
        'device_ip': device_ip,
        'name': name,
        'username': data.get('username', ''),
        'password': data.get('password', ''),
        'port': int(data.get('port', 80)),
        'stream_path': _normalise_stream_path(data.get('stream_path', '/stream')),
        'fps': int(data.get('fps', 30)),
        'width': int(data.get('width', 1920)),
        'height': int(data.get('height', 1080)),
        'rotation': int(data.get('rotation', 0)),
        'stream_type': stream_type,
        'motion_zones': [],
        'show_all_objects': False
    }

    if not settings_manager.add_camera(new_id, camera_config):
        return False, {'status': 'error',
                       'message': 'Failed to save camera configuration'}

    config = CameraConfig(
        camera_id=new_id,
        device_ip=device_ip,
        name=name,
        username=camera_config['username'],
        password=camera_config['password'],
        port=camera_config['port'],
        stream_path=camera_config['stream_path'],
        fps=camera_config['fps'],
        width=camera_config['width'],
        height=camera_config['height'],
        rotation=camera_config['rotation'],
        stream_type=stream_type
    )

    camera_manager.add_camera(config)
    start_new_camera_fetcher(config.camera_id)
    logger.info(f"Camera {device_ip} added successfully with id {new_id}")
    return True, {'status': 'success', 'camera_id': new_id}


@socketio.on('add_camera')
@require_socket_auth
def handle_add_camera(data):
    """Handle adding a new camera"""
    logger.info(f"Received add_camera request: {data}")

    try:
        _, payload = _add_camera_config(data or {})
        emit('camera_added', payload)
    except Exception as e:
        logger.error(f"Error adding camera: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        emit('camera_added', {'status': 'error', 'message': str(e)})


def _teardown_camera(camera_key: str):
    """Remove a camera from all in-memory structures and stop its threads.
    camera_key should be the camera_id."""
    camera_manager._running[camera_key] = False
    for d in [camera_manager.cameras, camera_manager.camera_states,
              camera_manager._running, camera_manager.stream_threads,
              camera_manager.frame_queues, camera_manager.latest_frames,
              camera_manager.latest_frames_small, camera_manager.frame_timestamps]:
        d.pop(camera_key, None)
    if camera_key in camera_fetcher_threads:
        camera_fetcher_threads[camera_key].join(timeout=1)
        camera_fetcher_threads.pop(camera_key, None)
    camera_motion_detectors.pop(camera_key, None)
    with web_frames_lock:
        web_frames.pop(camera_key, None)


def _emit_camera_list():
    """Broadcast the current camera list to all connected WebSocket clients."""
    cameras = camera_manager.get_all_cameras()
    socketio.emit('camera_list', {cid: {
        'camera_id': config.camera_id,
        'device_ip': config.device_ip,
        'name': config.name,
        'username': config.username,
        'port': config.port,
        'stream_path': config.stream_path,
        'fps': config.fps,
        'width': config.width,
        'height': config.height,
        'stream_type': config.stream_type,
        'state': camera_manager.get_camera_state(cid).value if camera_manager.get_camera_state(cid) else 'unknown'
    } for cid, config in cameras.items()})


def _build_registry_camera_config(camera: dict) -> CameraConfig:
    """Build a CameraConfig from a registry camera dict, applying any saved overlay settings."""
    device_ip = camera['ip_address']
    port = int(camera.get('port', 80))
    stream_path = camera.get('stream_url', '/stream')
    stream_type = camera.get('stream_type', 'rtsp')
    name = camera.get('name', '')

    saved = settings_manager.get_registry_camera_setting(device_ip)
    port = int(saved.get('port', port))
    stream_path = saved.get('stream_path', stream_path)
    stream_type = saved.get('stream_type', stream_type)
    name = saved.get('name', name)
    saved_camera_id = saved.get('camera_id', '')

    stream_url_format = "http://{host}:{port}{stream_path}" if stream_type == 'mjpeg' else "rtsp://{host}:{port}{stream_path}"

    return CameraConfig(
        camera_id=saved_camera_id,
        device_ip=device_ip,
        name=name,
        username=saved.get('username', ''),
        password=saved.get('password', ''),
        port=port,
        stream_url_format=stream_url_format,
        stream_path=stream_path,
        fps=int(saved.get('fps', 15)),
        width=int(saved.get('width', 640)),
        height=int(saved.get('height', 480)),
        rotation=int(saved.get('rotation', 0)),
        stream_type=stream_type
    )


def sync_registry_cameras():
    """Sync camera_manager with the registry: add online cameras, remove offline ones."""
    global registry_camera_ips
    registry_list = get_registry_cameras()
    online_ips = {c['ip_address'] for c in registry_list if c.get('status') == 'online' and c.get('ip_address')}
    changed = False

    # Remove cameras that went offline
    for cid in list(registry_camera_ips):
        config = camera_manager.cameras.get(cid)
        if config and config.device_ip not in online_ips:
            logger.info(f"Registry sync: camera {config.device_ip} went offline, removing from display")
            _teardown_camera(cid)
            registry_camera_ips.discard(cid)
            changed = True

    # Add cameras that came online
    for camera in registry_list:
        device_ip = camera.get('ip_address')
        if not device_ip or camera.get('status') != 'online':
            continue
        if any(cfg.device_ip == device_ip for cfg in camera_manager.cameras.values()):
            continue
        try:
            camera_config = _build_registry_camera_config(camera)
            camera_manager.add_camera(camera_config)
            start_new_camera_fetcher(camera_config.camera_id)
            registry_camera_ips.add(camera_config.camera_id)
            logger.info(f"Registry sync: added new online camera {camera_config.device_ip} ({device_ip})")
            changed = True
        except Exception as e:
            logger.error(f"Registry sync: failed to add camera {device_ip}: {e}")

    if changed:
        _emit_camera_list()


def _remove_camera_key(camera_key: str):
    """Remove a camera by name, id or IP. Returns (ok, payload).

    Extracted alongside `_add_camera_config` on 2026-09-20, and for the same
    reason: one implementation, two transports. Every comment below is the
    original's, because each records a bug this sequence already fixed.
    """
    # Resolve camera_key to camera_id
    config = resolve_camera_config(camera_key)
    cid = config.camera_id if config else camera_key
    is_registry_camera = cid in registry_camera_ips
    if not (is_registry_camera or settings_manager.remove_camera(camera_key)):
        return False, {'status': 'error', 'message': 'Failed to remove camera'}

    _teardown_camera(cid)
    registry_camera_ips.discard(cid)
    # And its settings, or they outlive it. A board that re-registers
    # gets the same camera_id back, so a camera removed while set to
    # "never record" came back still refusing to record — and
    # delete-and-re-add, which is what anybody would try, reproduced
    # the state instead of clearing it.
    try:
        # Only what makes it refuse to record. Dropping the whole
        # record took its name, motion zones, rotation and window with
        # it — and its camera_id, so a still-online board reappeared
        # within two minutes under a fresh uuid with everything
        # forgotten. "Remove" was quietly "reset".
        stored = dict(settings_manager.get_registry_camera_setting(cid) or {})
        for gone in ('recording_enabled', 'recording_start_hour',
                     'recording_end_hour'):
            stored.pop(gone, None)
        settings_manager.update_registry_camera_setting(cid, stored, replace=True)
    except Exception as e:
        logger.warning(f"could not clear the recording rules for {cid}: {e}")
    logger.info(f"Camera {camera_key} removed successfully")
    return True, {'status': 'success'}


@socketio.on('remove_camera')
@require_socket_auth
def handle_remove_camera(data):
    """Handle removing a camera"""
    camera_key = data.get('camera_key', data.get('camera_ip', ''))
    logger.info(f"Received remove_camera request for {camera_key}")

    try:
        _, payload = _remove_camera_key(camera_key)
        emit('camera_removed', payload)
    except Exception as e:
        logger.error(f"Error removing camera: {e}")
        emit('camera_removed', {'status': 'error', 'message': str(e)})


@socketio.on('request_camera_list')
@require_socket_auth
def handle_request_camera_list():
    """Request camera list from server"""
    logger.info('Client requested camera list')
    cameras = camera_manager.get_all_cameras()
    emit('camera_list', {cid: {
        'camera_id': config.camera_id,
        'device_ip': config.device_ip,
        'name': config.name,
        'username': config.username,
        'port': config.port,
        'stream_path': config.stream_path,
        'fps': config.fps,
        'width': config.width,
        'height': config.height,
        'rotation': config.rotation,
        'stream_type': config.stream_type,
        'state': camera_manager.get_camera_state(cid).value if camera_manager.get_camera_state(cid) else 'unknown'
    } for cid, config in cameras.items()})
