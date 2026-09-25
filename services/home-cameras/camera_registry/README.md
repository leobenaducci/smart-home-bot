# Camera Registry Server

A central registry server for ESP32 camera devices. This server allows ESP32 cameras to register themselves with the main HomeCameras server, providing discovery, stream management, and device tracking capabilities.

## Architecture

```
ESP32 Camera
    ↓ (POST /api/register)
Camera Registry Server (Port 5001)
    ↓ (HTTP API)
Main HomeCameras Web Server (Port 5000)
```

## Quick Start

### 1. Build and Run

```bash
cd camera_registry
pip install -r requirements.txt
python app.py
```

### 2. With Docker

```bash
docker compose up -d
```

The server will be available at `http://localhost:5001`

## API Endpoints

### Registration

**POST** `/api/register`

Register a new ESP32 camera using IP address:

```json
{
  "ip_address": "192.168.1.100",
  "name": "Living Room Camera",
  "capabilities": ["rtsp", "mjpeg"],
  "credentials": {
    "username": "admin",
    "password": "secret123"
  },
  "metadata": {
    "resolution": "1920x1080",
    "fps": 30
  },
  "location": "Living Room",
  "notes": "Front door camera"
}
```

### Discovery

**GET** `/api/list`

Get all registered cameras:

```bash
curl http://localhost:5001/api/list
```

Response:
```json
{
  "count": 3,
  "cameras": [
    {
      "ip_address": "192.168.1.100",
      "name": "Living Room",
      "status": "online",
      "registered_at": "2026-04-11T20:00:00",
      "last_heartbeat": "2026-04-11T20:05:00",
      "location": "Living Room",
      "capabilities": ["rtsp", "mjpeg"]
    }
  ]
}
```

### Heartbeat

**GET** `/api/<ip_address>/ping`

Keep camera alive and update last heartbeat:

```bash
curl http://localhost:5001/api/192.168.1.100/ping
```

**GET** `/api/cameras/heartbeat/batch?cameras=192.168.1.100,192.168.1.101`

Batch heartbeat update for multiple cameras.

### Camera Details

**GET** `/api/<ip_address>`

Get specific camera details:

```bash
curl http://localhost:5001/api/192.168.1.100
```

### Removal

**DELETE** `/api/<ip_address>`

Remove a camera from registry:

```bash
curl -X DELETE http://localhost:5001/api/192.168.1.100
```

## Database Schema

SQLite database located at `data/cameras.db`:

```sql
CREATE TABLE cameras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address TEXT UNIQUE NOT NULL,
    name TEXT,
    capabilities JSON DEFAULT "[]",
    credentials JSON DEFAULT "{}",
    metadata JSON DEFAULT "{}",
    status TEXT DEFAULT 'offline' CHECK(status IN ('online', 'offline', 'maintenance')),
    last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    location TEXT
);
```

## Integration with Main Server

Update your main `web_server.py` to use the registry with IP-based identification:

```python
# In src/web_server.py
from camera_registry.app import CameraRegistry

# Load cameras from registry
@app.route('/api/cameras/registry', methods=['GET'])
def list_registry_cameras():
    """List cameras registered with the registry server."""
    cameras = CameraRegistry.get_all_cameras()
    return jsonify(cameras)

# Get camera by IP
@app.route('/api/camera/<ip_address>', methods=['GET'])
def get_camera(ip_address):
    """Get camera details by IP address."""
    camera = CameraRegistry.get_camera(ip_address)
    if not camera:
        return jsonify({'error': 'Camera not found'}), 404
    return jsonify(camera)

# Resolve camera stream URL from IP
@app.route('/api/camera/<ip_address>/resolve', methods=['GET'])
def resolve_camera(ip_address):
    """Resolve camera stream URL from registry."""
    camera = CameraRegistry.get_camera(ip_address)
    if not camera:
        return jsonify({'error': 'Camera not found'}), 404
    
    return jsonify({
        'ip_address': ip_address,
        'stream_type': camera.get('stream_type', 'rtsp'),
        'stream_url': f"rtsp://{camera.get('username', 'admin')}:{camera.get('password', 'admin')}@{ip_address}:554/stream1"
    })
```

## ESP32 Integration

See the ESP32 integration code in the `esp32/camera-server` folder for examples of how to:
1. Register with the registry server at startup using IP address
2. Send periodic heartbeats using `/api/<ip_address>/ping`
3. Report camera status changes

## Troubleshooting

- **Database errors**: Check that the `data/` directory exists and is writable
- **Connection refused**: Ensure port 5001 is not blocked by firewall
- **Redis errors**: The Redis container should start successfully; check logs if it fails

## Configuration

Environment variables for production:
- `SECRET_KEY` - Flask secret key (generate a random string)
- `DATABASE_PATH` - Override default database location
