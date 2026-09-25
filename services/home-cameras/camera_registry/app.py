"""
Camera Registry Server - Central registry for ESP32 camera devices
Provides registration, discovery, and stream access endpoints
"""
import os
import sys
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import logging
from flask import Flask, request, jsonify, send_file, Response
from flask_socketio import SocketIO, emit

# Create Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'camera-registry-secret-key-change-in-production')
app.config['JSON_AS_ASCII'] = False  # Support Unicode device names

# SocketIO for WebSocket connections.
#
# `async_mode` is pinned to 'threading' to match how this app is actually
# served: `app.run()` at the bottom of the file, i.e. werkzeug's threaded
# server, which hands every request its own OS thread.
#
# Left to itself Flask-SocketIO picks eventlet whenever eventlet is importable.
# That combination leaks a file descriptor per request: eventlet builds its hub
# lazily, per thread, so every one of web_server's `transport=polling` requests
# landed on a fresh werkzeug thread, created a fresh hub with a fresh epoll fd,
# and dropped it unclosed when the thread exited. The process walked up to its
# 1024 soft limit over a few hours and then *every* open() failed — including
# `sqlite3.connect`, which made `get_db_connection()` return None and made the
# whole registry answer "Database unavailable" to the ESP boards. Nothing was
# ever wrong with the database.
#
# 'threading' reuses the server's own threads and never touches eventlet.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Configure logging first: resolving the data directory below can fail, and
# that failure is the single most useful line in the log when it does.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================================
# WHERE THE SQLITE FILE LIVES
# ============================================================================

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_registry_dir():
    """Choose the data directory and say where the choice came from.

    An explicit `REGISTRY_DATA_DIR` (or a `DATABASE_PATH` naming a file) wins
    outright and is never second-guessed. Falling back to some other directory
    when the configured one is unusable would bring the registry up against an
    **empty** database, and every camera in the house would read as
    never-registered — a worse outcome than not starting, and a much quieter
    one. If you asked for a specific directory, you get that one or an error.

    The default is `<app dir>/data/camera_registry`. It used to be one level
    *up*, which is right for the repo checkout — this file lives in
    `HomeCameras/camera_registry/` — but wrong inside the image: the Dockerfile
    copies the *contents* of `camera_registry/` into `/app`, flattening that
    level away, so `..` escaped to `/` and the default became
    `/data/camera_registry`, which the non-root `registry` user cannot create.
    docker-compose.yml always sets REGISTRY_DATA_DIR so production never met
    it, but a plain `docker run` without that variable died at import with a
    bare PermissionError traceback. `<app dir>` is `/app` in the image (the
    volume mount, matching compose) and the project folder in a checkout.
    """
    explicit = os.environ.get('REGISTRY_DATA_DIR')
    if explicit:
        return os.path.abspath(explicit), 'REGISTRY_DATA_DIR'

    database_path = os.environ.get('DATABASE_PATH')
    if database_path:
        return os.path.dirname(os.path.abspath(database_path)), 'DATABASE_PATH'

    return os.path.join(APP_DIR, 'data', 'camera_registry'), 'default'


def _ensure_usable(directory: str, source: str) -> None:
    """Create the data directory and prove we can actually write in it.

    `os.makedirs(exist_ok=True)` succeeds happily on a directory that exists
    and is read-only, so existence proves nothing. Without the write probe the
    first symptom is `sqlite3.connect` failing inside `get_db_connection()`,
    which returns None, which every caller reports as the famously unhelpful
    "Database unavailable" — the exact fault that hid a file-descriptor leak
    for eleven hours. Better to find out here, once, with the path in hand.
    """
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as e:
        raise SystemExit(
            f"FATAL: cannot create the registry data directory {directory!r} "
            f"(from {source}): {e}\n"
            f"Set REGISTRY_DATA_DIR to a directory this process can write to. "
            f"Under docker-compose that is the `camera_registry_data` volume, "
            f"mounted at /app/data/camera_registry."
        )

    probe = os.path.join(directory, '.write-probe')
    try:
        with open(probe, 'w') as fh:
            fh.write('ok')
        os.remove(probe)
    except OSError as e:
        raise SystemExit(
            f"FATAL: registry data directory {directory!r} (from {source}) "
            f"exists but is not writable by uid {os.getuid()}: {e}\n"
            f"SQLite needs to create the database plus its -wal/-journal "
            f"sidecars here. Fix the ownership of the mounted volume, or set "
            f"REGISTRY_DATA_DIR elsewhere."
        )


REGISTRY_DIR, _REGISTRY_DIR_SOURCE = _resolve_registry_dir()
DATABASE_PATH = os.environ.get('DATABASE_PATH', os.path.join(REGISTRY_DIR, 'cameras.db'))
_ensure_usable(REGISTRY_DIR, _REGISTRY_DIR_SOURCE)
logger.info(f"Registry data directory: {REGISTRY_DIR} (from {_REGISTRY_DIR_SOURCE})")
logger.info(f"Database: {DATABASE_PATH}")


def get_db_connection():
    """Get database connection with proper error handling."""
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logger.error(f"Database error: {e}")
        return None


def init_database():
    """Initialize database tables."""
    conn = get_db_connection()
    if not conn:
        return
    
    try:
        cursor = conn.cursor()
        
        # Cameras table - using ip_address as unique identifier
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cameras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address TEXT UNIQUE NOT NULL,
                name TEXT,
                port INTEGER DEFAULT 80,
                stream_url TEXT DEFAULT '/stream',
                stream_type TEXT DEFAULT 'rtsp' CHECK(stream_type IN ('rtsp', 'mjpeg')),
                capabilities JSON DEFAULT "[]",
                credentials JSON DEFAULT "{}",
                metadata JSON DEFAULT "{}",
                status TEXT DEFAULT 'offline' CHECK(status IN ('online', 'offline', 'maintenance')),
                last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notes TEXT,
                location TEXT
            )
        ''')
        
        # Database migrations - add missing columns if they don't exist
        def column_exists(table, column):
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in cursor.fetchall()]
            return column in columns
        
        # Add port column if missing (fix for existing databases)
        if not column_exists('cameras', 'port'):
            cursor.execute("ALTER TABLE cameras ADD COLUMN port INTEGER DEFAULT 80")
            logger.info("Added missing port column to cameras table")
        
        # Add stream_url column if missing
        if not column_exists('cameras', 'stream_url'):
            cursor.execute("ALTER TABLE cameras ADD COLUMN stream_url TEXT DEFAULT '/stream'")
            logger.info("Added missing stream_url column to cameras table")
            
        # Add capabilities column if missing
        if not column_exists('cameras', 'capabilities'):
            cursor.execute("ALTER TABLE cameras ADD COLUMN capabilities JSON DEFAULT \"[]\"")
            logger.info("Added missing capabilities column to cameras table")
            
        # Add credentials column if missing
        if not column_exists('cameras', 'credentials'):
            cursor.execute("ALTER TABLE cameras ADD COLUMN credentials JSON DEFAULT \"{}\"")
            logger.info("Added missing credentials column to cameras table")
            
        # Add metadata column if missing
        if not column_exists('cameras', 'metadata'):
            cursor.execute("ALTER TABLE cameras ADD COLUMN metadata JSON DEFAULT \"{}\"")
            logger.info("Added missing metadata column to cameras table")

        # Add stream_type column if missing
        if not column_exists('cameras', 'stream_type'):
            cursor.execute("ALTER TABLE cameras ADD COLUMN stream_type TEXT DEFAULT 'rtsp'")
            logger.info("Added missing stream_type column to cameras table")

        # Add location column if missing
        if not column_exists('cameras', 'location'):
            cursor.execute("ALTER TABLE cameras ADD COLUMN location TEXT")
            logger.info("Added missing location column to cameras table")

        # Add notes column if missing
        if not column_exists('cameras', 'notes'):
            cursor.execute("ALTER TABLE cameras ADD COLUMN notes TEXT")
            logger.info("Added missing notes column to cameras table")
        
        # Stream preferences (cached stream URLs)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stream_preferences (
                camera_id TEXT PRIMARY KEY,
                preferred_format TEXT DEFAULT 'rtsp',
                resolution TEXT,
                stream_host TEXT,
                last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        
        conn.commit()
        logger.info("Database initialized successfully")
        
    except sqlite3.Error as e:
        logger.error(f"Database initialization error: {e}")
        conn.rollback()
    finally:
        conn.close()


# Initialize database on startup
init_database()


def _emit_registry_event(event: str, data: dict):
    """Push a SocketIO event to all connected clients (e.g. web_server)."""
    try:
        socketio.emit(event, data)
    except Exception:
        pass


def _stale_camera_cleanup_worker():
    """Background thread: mark cameras with no heartbeat for 15 minutes as offline."""
    while True:
        time.sleep(60)  # check every minute
        stale = CameraRegistry.cleanup_stale_cameras(timeout_minutes=15)
        if stale:
            _emit_registry_event('cameras_offline', {
                'ip_addresses': stale,
                'timestamp': datetime.now().isoformat()
            })


_cleanup_thread = threading.Thread(target=_stale_camera_cleanup_worker, daemon=True)
_cleanup_thread.start()


# ============================================================================
# CORE REGISTRY LOGIC
# ============================================================================

class CameraRegistry:
    """Core registry operations."""
    
    @staticmethod
    def register_camera(ip_address: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Register a new camera or update existing one by IP address.
        
        Args:
            ip_address: Camera IP address (e.g., "192.168.1.100")
            data: Camera data dict with name, capabilities, etc.
            
        Returns:
            Registration result with status and camera info
        """
        conn = get_db_connection()
        if not conn:
            return {'success': False, 'error': 'Database unavailable'}
        
        try:
            cursor = conn.cursor()
            
            # Check if camera with this IP already exists
            cursor.execute("SELECT id FROM cameras WHERE ip_address = ?", (ip_address,))
            existing = cursor.fetchone()
            
            if existing:
                # Update existing camera
                cursor.execute('''
                    UPDATE cameras SET 
                        name = ?,
                        port = ?,
                        stream_url = ?,
                        stream_type = ?,
                        capabilities = ?,
                        credentials = ?,
                        metadata = ?,
                        location = ?,
                        notes = ?,
                        status = 'online'
                    WHERE ip_address = ?
                ''', (
                    data.get('name', ''),
                    data.get('port', 80),
                    data.get('stream_url', '/stream'),
                    data.get('stream_type', 'mjpeg'),
                    json.dumps(data.get('capabilities', [])),
                    json.dumps(data.get('credentials', {})),
                    json.dumps(data.get('metadata', {})),
                    data.get('location'),
                    data.get('notes'),
                    ip_address
                ))
                is_new = False
            else:
                # Insert new camera
                cursor.execute('''
                    INSERT INTO cameras (ip_address, name, port, stream_url, stream_type, capabilities, credentials, metadata, location, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    ip_address,
                    data.get('name', ''),
                    data.get('port', 80),
                    data.get('stream_url', '/stream'),
                    data.get('stream_type', 'mjpeg'),
                    json.dumps(data.get('capabilities', [])),
                    json.dumps(data.get('credentials', {})),
                    json.dumps(data.get('metadata', {})),
                    data.get('location'),
                    data.get('notes')
                ))
                is_new = True
            
            camera_id = cursor.lastrowid if is_new else existing['id']
            
            # Set initial heartbeat
            cursor.execute("UPDATE cameras SET last_heartbeat = CURRENT_TIMESTAMP, status = 'online' WHERE id = ?", (camera_id,))
            
            conn.commit()
            
            result = {
                'success': True,
                'camera_id': camera_id,
                'is_new': is_new,
                'ip_address': ip_address,
                'data': dict(data),
                'message': f"Camera {'registered' if is_new else 'updated'}: {ip_address}"
            }
            
            logger.info(f"{'Registered' if is_new else 'Updated'} camera: {ip_address}")
            return result
            
        except sqlite3.Error as e:
            logger.error(f"Registration error: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            conn.close()
    
    @staticmethod
    def get_camera(ip_address: str) -> Optional[Dict[str, Any]]:
        """Get camera data by IP address."""
        conn = get_db_connection()
        if not conn:
            return None
        
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM cameras WHERE ip_address = ?', (ip_address,))
            row = cursor.fetchone()
            
            if row:
                return dict(row)
            return None
            
        finally:
            conn.close()
    
    @staticmethod
    def get_all_cameras() -> List[Dict[str, Any]]:
        """Get all registered cameras."""
        conn = get_db_connection()
        if not conn:
            return []
        
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT ip_address, name, port, stream_url, stream_type, status, registered_at, last_heartbeat, location, capabilities FROM cameras ORDER BY registered_at DESC')
            rows = cursor.fetchall()
            
            cameras = []
            for row in rows:
                camera = dict(row)
                # Parse JSON fields
                if 'capabilities' in camera and camera['capabilities']:
                    try:
                        camera['capabilities'] = json.loads(camera['capabilities'])
                    except:
                        camera['capabilities'] = []
                cameras.append(camera)
            
            return cameras
            
        finally:
            conn.close()
    
    @staticmethod
    def update_heartbeat(ip_address: str) -> bool:
        """Update last heartbeat timestamp."""
        conn = get_db_connection()
        if not conn:
            return False
        
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE cameras 
                SET last_heartbeat = CURRENT_TIMESTAMP, 
                    status = 'online'
                WHERE ip_address = ?
            ''', (ip_address,))
            
            conn.commit()
            rows_affected = cursor.rowcount
            logger.info(f"Heartbeat updated for camera: {ip_address}")
            return rows_affected > 0
            
        except sqlite3.Error as e:
            logger.error(f"Heartbeat update error: {e}")
            return False
        finally:
            conn.close()
    
    @staticmethod
    def cleanup_stale_cameras(timeout_minutes: int = 15) -> List[str]:
        """Mark cameras offline that haven't responded within timeout_minutes."""
        conn = get_db_connection()
        if not conn:
            return []

        try:
            cursor = conn.cursor()
            cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)
            cursor.execute(
                "SELECT ip_address FROM cameras WHERE last_heartbeat < ? AND status = 'online'",
                (cutoff.strftime('%Y-%m-%d %H:%M:%S'),)
            )
            stale = [row[0] for row in cursor.fetchall()]

            if stale:
                cursor.execute(
                    "UPDATE cameras SET status = 'offline' WHERE last_heartbeat < ? AND status = 'online'",
                    (cutoff.strftime('%Y-%m-%d %H:%M:%S'),)
                )
                conn.commit()
                for ip in stale:
                    logger.info(f"Marked camera offline (no heartbeat for {timeout_minutes}m): {ip}")

            return stale

        except sqlite3.Error as e:
            logger.error(f"Stale camera cleanup error: {e}")
            return []
        finally:
            conn.close()

    @staticmethod
    def remove_camera(ip_address: str) -> bool:
        """Remove a camera from registry."""
        conn = get_db_connection()
        if not conn:
            return False
        
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM cameras WHERE ip_address = ?', (ip_address,))
            camera = cursor.fetchone()
            
            if not camera:
                return False
            
            cursor.execute('DELETE FROM cameras WHERE ip_address = ?', (ip_address,))
            conn.commit()
            
            logger.info(f"Removed camera: {ip_address}")
            return True
            
        except sqlite3.Error as e:
            logger.error(f"Removal error: {e}")
            return False
        finally:
            conn.close()


# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/', methods=['GET'])
def root():
    """What this server is, and what it currently knows.

    It had no root at all, so anything asking for `/` — a browser checking
    whether the registry is up, a camera probing before it registers — got a
    bare 404, which reads as "there is nothing here" rather than "you want a
    different path". That is exactly how this looked when a camera went
    missing and somebody came to see whether the registry was even alive.
    """
    try:
        cameras = CameraRegistry.get_all_cameras()
    except Exception:
        cameras = []
    online = sum(1 for c in cameras if (c.get('status') if isinstance(c, dict) else None) == 'online')
    return jsonify({
        'service': 'camera-registry',
        'status': 'ok',
        'cameras': len(cameras),
        'online': online,
        # Named here so a person who lands on this page can see where to go
        # next without reading the source.
        'endpoints': {
            'register': 'POST /api/register  (ip_address required, top level)',
            'list': 'GET /api/list',
            'one': 'GET /api/<ip_address>',
            'heartbeat': 'GET /api/<ip_address>/ping',
            'remove': 'DELETE /api/remove/<ip_address>',
            'health': 'GET /api/ping',
        },
    })


@app.route('/api/register', methods=['POST'])
def register_camera():
    """
    Register a new ESP32 camera.
    
    Expected JSON payload:
    {
        "ip_address": "192.168.1.100",
        "name": "Living Room",
        "capabilities": ["rtsp", "mjpeg"],
        "credentials": {"username": "admin", "password": "secret123"},
        "metadata": {"resolution": "1920x1080", "fps": 30}
    }
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'No JSON data provided'}), 400
        
        ip_address = data.get('ip_address')
        if not ip_address:
            return jsonify({'error': 'ip_address is required'}), 400
        
        # Validate data
        required_fields = ['ip_address']
        missing = [field for field in required_fields if field not in data]
        if missing:
            return jsonify({'error': f'Missing required fields: {missing}'}), 400
        
        # Register camera
        result = CameraRegistry.register_camera(ip_address, data)
        
        if result['success']:
            _emit_registry_event('camera_registered', {
                'ip_address': ip_address,
                'is_new': result['is_new'],
                'timestamp': datetime.now().isoformat()
            })
            return jsonify(result), 201
        else:
            return jsonify(result), 500

    except Exception as e:
        logger.error(f"Registration exception: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/list', methods=['GET'])
def list_cameras():
    """Get list of all registered cameras."""
    try:
        cameras = CameraRegistry.get_all_cameras()

        response = []
        for camera in cameras:
            camera_entry = {
                'ip_address': camera['ip_address'],
                'name': camera.get('name'),
                'port': camera.get('port', 80),
                'stream_url': camera.get('stream_url', '/stream'),
                'stream_type': camera.get('stream_type', 'rtsp'),
                'status': camera.get('status', 'offline'),
                'registered_at': camera.get('registered_at'),
                'last_heartbeat': camera.get('last_heartbeat'),
                'location': camera.get('location'),
                'capabilities': camera.get('capabilities', [])
            }
            response.append(camera_entry)

        return jsonify({
            'count': len(response),
            'cameras': response
        })
    except Exception as e:
        logger.error(f"Error listing cameras: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/<ip_address>', methods=['GET'])
def get_camera(ip_address: str):
    """Get specific camera details by IP address."""
    camera = CameraRegistry.get_camera(ip_address)
    
    if not camera:
        return jsonify({'error': f'Camera {ip_address} not found'}), 404
    
    # Remove credentials from response for security
    if 'credentials' in camera:
        del camera['credentials']
        
    return jsonify(camera)


@app.route('/api/<ip_address>/ping', methods=['GET'])
def camera_ping(ip_address: str):
    """Heartbeat endpoint for camera keep-alive.

    404 when nothing was updated, rather than a cheerful "ok" regardless.

    `update_heartbeat` already reports whether it matched a row and this threw
    the answer away, so a camera calling the wrong path — the sample firmware
    used to ping `/api/<camera_id>/ping`, and the registry keys everything on
    the address — was told it was fine every single time while its heartbeat
    was never written. It then aged quietly to "offline" in a registry that
    had answered 200 to every request it ever made. A camera that cannot be
    seen must be able to find that out from the thing it is talking to.
    """
    if not CameraRegistry.update_heartbeat(ip_address):
        return jsonify({
            'status': 'unknown',
            'ip_address': ip_address,
            'error': f'{ip_address} is not registered — register it first, '
                     f'and ping the address it registered with',
        }), 404
    return jsonify({
        'status': 'ok',
        'ip_address': ip_address,
        'timestamp': datetime.now().isoformat()
    })


@app.route('/api/remove/<ip_address>', methods=['DELETE'])
def remove_camera_api(ip_address: str):
    """Remove a camera from registry."""
    success = CameraRegistry.remove_camera(ip_address)
    
    if success:
        return jsonify({
            'success': True,
            'message': f'Camera {ip_address} removed',
            'timestamp': datetime.now().isoformat()
        })
    else:
        return jsonify({
            'success': False,
            'error': f'Camera {ip_address} not found or removal failed',
            'timestamp': datetime.now().isoformat()
        }), 404


@app.route('/api/ping', methods=['GET'])
def registry_ping():
    """Registry server health check.

    503 when the database cannot be reached, rather than `healthy` with the
    reason tucked into a field nobody reads.

    This endpoint is what the Docker HEALTHCHECK and the `Verify Camera
    Registry` stage of the Jenkinsfile both test, and it used to answer
    `{"status": "healthy"}` with HTTP 200 no matter what the database did. So
    the registry spent eleven hours telling every ESP board "Database
    unavailable" while docker ps said `(healthy)` and the last deploy went
    green. Same failure as `/api/<ip>/ping` answering `ok` to cameras that
    were not registered: a health check that cannot report ill is not a health
    check.
    """
    healthy = True
    try:
        conn = get_db_connection()
        if not conn:
            raise RuntimeError(
                'get_db_connection() returned None — see the logged Database '
                'error; an fd/permission problem opening the file, not a '
                'schema one'
            )
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) as count FROM cameras')
            camera_count = cursor.fetchone()[0]
        finally:
            conn.close()
        db_status = f'operational ({camera_count} cameras)'
    except Exception as e:
        db_status = f'error: {str(e)}'
        healthy = False

    return jsonify({
        'service': 'camera-registry',
        'status': 'healthy' if healthy else 'degraded',
        'timestamp': datetime.now().isoformat(),
        'database': db_status
    }), (200 if healthy else 503)


@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get registry statistics."""
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database unavailable'}), 500
    
    try:
        cursor = conn.cursor()
        
        # Total cameras
        cursor.execute('SELECT COUNT(*) as total FROM cameras')
        total = cursor.fetchone()[0]
        
        # Status breakdown
        cursor.execute("SELECT status, COUNT(*) as count FROM cameras GROUP BY status")
        status_counts = dict(cursor.fetchall())
        
        # Last heartbeat time
        cursor.execute('SELECT MAX(last_heartbeat) as last_heartbeat FROM cameras')
        last_heartbeat = cursor.fetchone()[0]
        
        conn.close()
        
        return jsonify({
            'total_cameras': total,
            'by_status': status_counts,
            'last_activity': last_heartbeat,
            'database': DATABASE_PATH
        })
        
    finally:
        conn.close()


# ============================================================================
# SOCKET.IO WEBHOOKS
# ============================================================================

@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    logger.info(f"Client connected: {request.sid}")
    emit('connected', {
        'message': 'Connected to camera registry',
        'timestamp': datetime.now().isoformat()
    })


@socketio.on('camera_update')
def handle_camera_update(data):
    """
    Handle real-time camera state updates from ESP32.
    
    Event payload:
    {
        "camera_id": "CAM-001",
        "status": "online",
        "motion_detected": true,
        "detection_count": 5
    }
    """
    camera_id = data.get('camera_id')
    
    if camera_id:
        # Update heartbeat
        CameraRegistry.update_heartbeat(camera_id)
        
        # Emit update to other clients
        emit('camera_state', {
            'camera_id': camera_id,
            'status': data.get('status', 'online'),
            'message': 'Camera state updated',
            'timestamp': datetime.now().isoformat()
        }, broadcast=True)
        
        logger.info(f"Real-time update for camera: {camera_id}")


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    logger.info(f"Client disconnected: {request.sid}")


@socketio.on('register_success_ack')
def handle_register_ack(data):
    """Acknowledge successful camera registration."""
    camera_id = data.get('camera_id')
    if camera_id:
        emit('registration_complete', {
            'camera_id': camera_id,
            'message': 'Registration acknowledged',
            'timestamp': datetime.now().isoformat()
        })


# ============================================================================
# DIRECT STREAM ACCESS
# ============================================================================

@app.route('/api/stream/<ip_address>', methods=['GET'])
def get_stream_url(ip_address: str):
    """
    Return stream URL for camera.
    Returns either:
    1. Direct URL if available
    2. RTSP proxy URL
    """
    camera = CameraRegistry.get_camera(ip_address)
    
    if not camera:
        return jsonify({'error': f'Camera {ip_address} not found'}), 404
    
    # Return direct stream URL from IP
    return jsonify({
        'stream_url': f"http://{ip_address}/stream",
        'ip_address': ip_address,
        'type': 'direct',
        'message': f'Stream available at http://{ip_address}/stream'
    })


@app.route('/api/rtsp-proxy/<ip_address>', methods=['GET'])
def rtsp_proxy(ip_address: str):
    """
    Proxy RTSP stream to avoid RTSP protocol issues.
    This acts as a proxy endpoint that forwards to the camera's RTSP URL.
    """
    camera = CameraRegistry.get_camera(ip_address)
    
    if not camera:
        return jsonify({'error': f'Camera {ip_address} not found'}), 404
    
    # Return RTSP stream URL directly
    return jsonify({
        'rtsp_url': f"rtsp://{ip_address}/stream1",
        'ip_address': ip_address,
        'message': f'Ready to proxy RTSP stream from {ip_address}'
    })


# ============================================================================
# STREAM SERVER INTEGRATION
# ============================================================================

@app.route('/api/<ip_address>/settings', methods=['GET'])
def get_camera_settings(ip_address: str):
    """Get camera settings for integration."""
    camera = CameraRegistry.get_camera(ip_address)
    
    if not camera:
        return jsonify({'error': f'Camera {ip_address} not found'}), 404
    
    return jsonify({
        'ip_address': ip_address,
        'settings': camera,
        'metadata': {
            'supports_direct_stream': True,
            'supports_mjpeg': False,  # Add actual capability detection
            'requires_proxy': camera.get('proxy_required', False)
        }
    })


if __name__ == '__main__':
    print("Camera Registry Server starting. . .")
    # The database path is already logged at import, with the source of the
    # choice attached — no need to print a second, less informative copy.

    # Run in production mode by default
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=5001, debug=debug_mode)
