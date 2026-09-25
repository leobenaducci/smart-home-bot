# Camera Video Analysis System

This project provides a Python application that connects to IP cameras using RTSP streams, analyzes video feed using OpenCV for motion detection, and streams detected video via a web server with configurable motion detection zones.

## Features

- Connects to IP cameras using RTSP streams
- Real-time motion detection using OpenCV
- Configurable motion detection zones
- Web-based dashboard for monitoring cameras
- Real-time video streaming via MJPEG
- WebSocket-based communication for low-latency updates
- REST API for camera management and motion event retrieval

## Getting Started

### Prerequisites

- Python 3.7 or higher
- pip package manager

### Installation

1. Clone the repository:
   ```
   git clone <repository-url>
   cd HomeCameras
   ```

2. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```

### Configuration

1. Add your cameras. **Not in this directory** — the deployer pushes this tree
   with `rsync --delete`, so a `config/cameras.json` written here is invisible
   to the running service and gone on the next deploy. The live file is
   `{paths.config}/home-cameras/cameras.json` on the host (the `CAMERAS_CONFIG_DIR`
   bind mount), by default `/var/lib/home-stack/config/home-cameras/`. Edit it
   there, or add cameras from the camera wall. Its format:
   ```json
   {
     "camera_ip_address": {
       "device_ip": "camera_ip_address",
       "username": "your_username",
       "password": "your_password",
       "port": "554",
       "stream_path": "/stream1",
       "fps": "30",
       "width": "1920",
       "height": "1080"
     }
   }
   ```

### Running the Application

1. Start the server:
   ```
   python main.py
   ```

2. Access the web interface at `http://localhost:5000`

3. The default admin password for settings access is `admin`

## API Endpoints

- `/` - Main dashboard
- `/settings` - Camera configuration interface
- `/stream/<camera_ip>` - MJPEG video stream for a specific camera
- `/api/cameras` - Get list of configured cameras
- `/api/cameras/<camera_ip>/state` - Get state of a specific camera
- `/api/motion_events` - Get recent motion events

## Android Client

The project includes a comprehensive Android client for mobile monitoring:

### Features
- **Live Camera Streaming**: View real-time MJPEG streams from your IP cameras
- **Motion Detection Alerts**: Receive instant notifications when motion is detected
- **Push Notifications**: Get alerts via Firebase Cloud Messaging even when app is backgrounded
- **Multi-Camera Support**: Manage and monitor multiple cameras simultaneously
- **Object Detection**: See detected objects (person, car, pet, etc.) in camera feeds
- **Motion Zones**: Configure specific areas for motion detection
- **Secure Authentication**: Password-protected access with biometric support

### Deployment
For detailed Android client deployment instructions, see:
- [Android Deployment Guide](../android/DEPLOYMENT.md) - Complete setup and deployment guide
- [Android Design Document](../android/ANDROID_DESIGN.md) - Technical architecture and design

### Prerequisites
- Android Studio Arctic Fox or newer
- Android SDK 21 or higher
- Firebase project with FCM enabled
- HomeCameras backend server running

### Quick Start
1. Set up Firebase project and download `google-services.json`
2. Configure backend server with Firebase Admin SDK
3. Open Android project in Android Studio
4. Update server URL in `local.properties` or app settings
5. Build and install on Android device

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
