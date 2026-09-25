# Android Client - AI Agents Documentation & Change Log

This file tracks all changes made by AI agents specifically to the Android client project.

For changes affecting multiple projects, see the main [CHANGELOG.md](../CHANGELOG.md).

## Recent Changes

### [Date: 2026-04-10] - Android App H.264 Streaming Support
**Agent**: Cline (Anthropic)
**Task**: Update Android app to use H.264 stream with native hardware decoding

**Changes Made**:
1. Updated `android/app/build.gradle`:
   - Added ExoPlayer dependency: `implementation 'com.google.android.exoplayer:exoplayer:2.19.1'`

2. Updated `android/app/src/main/res/layout/activity_stream.xml`:
   - Replaced `TextureView` with `PlayerView` from ExoPlayer
   - Added ExoPlayer control view configuration

3. Created `android/app/src/main/res/layout/exo_player_control_view.xml`:
   - Custom ExoPlayer control layout with play/pause, rewind, fast-forward
   - Fullscreen toggle and duration/position display

4. Updated `android/app/src/main/java/com/homecameras/android/ui/StreamActivity.kt`:
   - Replaced MJPEG parsing logic with ExoPlayer integration
   - Changed stream URL from `/stream/<camera_ip>` (MJPEG) to `/h264/<camera_ip>` (H.264)
   - Implemented ExoPlayer lifecycle management (onCreate, onStart, onResume, onPause, onStop, onDestroy)
   - Added immersive full-screen mode with system UI hiding
   - Added proper error handling and state management

**Key Features**:
- Native H.264 hardware decoding using Android's MediaCodec via ExoPlayer
- Lower CPU usage compared to MJPEG parsing
- Better battery efficiency on mobile devices
- Built-in player controls (play/pause, seek, fullscreen)
- Automatic format detection and playback

**Usage**:
- Camera streams now use H.264 encoding from backend
- Stream URL: `http://compute.home:5000/h264/<camera_ip>`
- ExoPlayer handles H.264 elementary stream natively

**Requirements**:
- Android 5.0 (API 21) or higher
- Device with H.264 decoder support (virtually all modern Android devices)

**Summary**:
- Updated Android app to use H.264 streaming instead of MJPEG
- Uses ExoPlayer for native hardware-accelerated H.264 decoding
- Provides more efficient video playback with lower CPU and battery usage
- Better user experience with built-in player controls

### [Date: 2026-04-10] - Fix Android App "Source Error" with Raw H.264 Stream
**Agent**: Cline (Anthropic)
**Task**: Fix "source error" in Android app when playing H.264 stream

**Problem**:
The Android app was showing a "source error" immediately when opening the camera stream. The issue was caused by the fragmented MP4 format produced by FFmpeg not being properly compatible with ExoPlayer.

**Root Cause**:
The H.264 encoder was using FFmpeg to produce fragmented MP4 (ISOBMFF) format with `-movflags +frag_keyframe+empty_moov+default_base_moof`. This format was not properly parseable by ExoPlayer when served as a continuous HTTP stream.

**Changes Made**:
1. Updated `src/h264_encoder.py`:
   - Changed FFmpeg output format from `mp4` to `h264` (raw elementary stream)
   - Removed fragmented MP4 flags (`-movflags`)
   - Added `-preset quality` and `-rc vbr` options for NVENC configuration
   - Now outputs raw H.264 NAL units without any container format

2. Updated `src/web_server.py`:
   - Changed response mimetype from `video/mp4` to `video/h264`
   - Added `Cache-Control: no-cache` and `Connection: keep-alive` headers
   - Updated comments to reflect raw H.264 elementary stream output

3. Updated `android/app/src/main/java/com/homecameras/android/ui/StreamActivity.kt`:
   - Replaced ExoPlayer with direct MediaCodec decoder
   - Uses TextureView instead of PlayerView
   - Implements custom H.264 decoder using Android's MediaCodec API

4. Created `android/app/src/main/java/com/homecameras/android/media/H264Decoder.kt`:
   - Custom H.264 decoder using MediaCodec API
   - Hardware-accelerated decoding via Surface
   - Thread-safe decoding loop

**Technical Details**:
- Raw H.264 elementary stream contains only NAL units (no container)
- FFmpeg command changed: `-f h264` instead of `-f mp4 -movflags +frag_keyframe...`
- MediaCodec uses hardware acceleration via Surface
- Lower latency compared to fragmented MP4 approach
- NVENC hardware encoding is used when available (NVIDIA GPU with GTX 9xx series or newer)

**Summary**:
- Fixed "source error" by using raw H.264 elementary stream format
- Changed FFmpeg to output raw H.264 NAL units without MP4 container
- Updated mimetype to `video/h264` for proper stream identification
- Android app now uses MediaCodec directly for hardware-accelerated H.264 decoding

### [Date: 2026-04-09] - Fix Socket.IO WebSocket Parsing Error
**Agent**: Cline (Anthropic)
**Task**: Fix `JsonSyntaxException` in WebSocketManager when receiving Socket.IO protocol messages

**Changes Made**:
1. Updated `android/app/src/main/java/com/homecameras/android/data/api/WebSocketManager.kt`:
   - Added Socket.IO protocol handling: strip leading numeric packet type prefix before parsing JSON
   - Added check for empty messages after cleaning
   - Fixed message parsing error at line 177

**Root Cause**:
Socket.IO protocol adds a numeric packet type identifier at the beginning of every WebSocket message (like '0', '2', '3', '4'). The parser was trying to parse this directly as JSON, causing `MalformedJsonException` at column 3.

**Summary**:
- Fixed WebSocket message parsing error that appeared immediately after connection
- Now properly handles standard Socket.IO EIO=4 protocol format
- WebSocket communication now works correctly with the Python backend server

### [Date: 2026-04-09] - Fix Black Page Issue
**Agent**: Cline (Anthropic)
**Task**: Fix "black page" issue where the Android app showed only a title with nothing else

**Changes Made**:
1. Enhanced `android/app/src/main/res/layout/activity_main.xml`:
   - Added a proper Toolbar with app branding
   - Added a status card showing system status and camera count
   - Added a RecyclerView for displaying camera list
   - Added an empty state layout for when no cameras exist
   - Added a refresh button to reload camera data

2. Created `android/app/src/main/res/layout/item_camera.xml`:
   - Camera card layout with thumbnail, name, IP, and status
   - View and Settings buttons for each camera
   - Status indicator with color coding (connected=green, disconnected=red, etc.)

3. Created `android/app/src/main/res/drawable/status_indicator.xml`:
   - Oval shape drawable for camera status indicators

4. Created `android/app/src/main/java/com/homecameras/android/ui/CameraAdapter.kt`:
   - RecyclerView adapter for displaying camera list
   - Handles camera status display with proper color coding
   - Click handlers for viewing stream and opening settings

5. Updated `android/app/src/main/java/com/homecameras/android/ui/MainActivity.kt`:
   - Implemented proper UI initialization with views
   - Added camera loading from database via CameraRepository
   - Added status card updates showing online/offline camera counts
   - Added empty state handling
   - Added refresh functionality
   - Integrated with StreamingService

6. Updated `android/app/src/main/java/com/homecameras/android/ui/StreamActivity.kt`:
   - Added EXTRA_CAMERA_NAME constant for passing camera name

**Summary**:
- The "black page" issue was caused by an extremely minimal layout that only showed a title
- The app now displays a full dashboard with status information and camera list
- Users can see camera status, click to view streams, and access camera settings
- The UI follows Material Design guidelines with proper theming

### [Date: 2026-04-09] - Fix Camera Loading and Add Settings Menu
**Agent**: Cline (Anthropic)
**Task**: Fix "No cameras found" issue and add missing Settings functionality

**Changes Made**:
1. Updated `android/app/src/main/java/com/homecameras/android/ui/MainActivity.kt`:
   - Added camera sync from server before loading local database
   - Added proper error handling for camera loading
   - Added toolbar menu with Settings and Refresh options
   - Added SettingsActivity navigation
   - Enhanced camera loading logic to sync with backend API

2. Created `android/app/src/main/res/menu/menu_main.xml`:
   - Menu resource with Settings and Refresh actions
   - Toolbar integration for better UX

**Summary**:
- Fixed "No cameras found" issue by implementing server sync
- Added Settings menu to toolbar for easy access
- Enhanced camera loading to fetch from backend API
- Improved error handling and user feedback
- Added refresh functionality via menu and button

### [Date: 2026-04-09] - Fix Firebase Dependencies
**Agent**: Cline (Anthropic)
**Task**: Fix "Unresolved reference: firebase" error in FCMService.kt

**Changes Made**:
1. Uncommented Firebase dependencies in `android/app/build.gradle`:
   - `firebase-bom:32.7.0`
   - `firebase-messaging-ktx:23.4.1`
   - `firebase-analytics-ktx:21.5.1`
2. Added Google Services plugin to `android/app/build.gradle`: `id 'com.google.gms.google-services'`
3. Created placeholder `android/app/google-services.json` (needs to be replaced with real Firebase config)
4. Created `android/gradlew.bat` for Windows builds

**Important Note**: The `google-services.json` file is a placeholder. Users must:
1. Create a Firebase project at https://console.firebase.google.com/
2. Add an Android app with package name `com.homecameras.android`
3. Download the real `google-services.json` and replace the placeholder

### [Date: 2026-04-09] - Fix Missing UI Components
**Agent**: Cline (Anthropic)
**Task**: Fix compilation errors in NotificationManager.kt due to missing UI classes

**Changes Made**:
1. Created `android/app/src/main/java/com/homecameras/android/ui/MainActivity.kt` - Main dashboard activity
2. Created `android/app/src/main/java/com/homecameras/android/ui/StreamActivity.kt` - Camera stream viewing activity
3. Created `android/app/src/main/res/layout/activity_main.xml` - Layout for main activity
4. Created `android/app/src/main/res/layout/activity_stream.xml` - Layout for stream activity

**Summary**:
- Fixed "Unresolved reference" errors for MainActivity and StreamActivity
- Added basic UI structure for the application
- Created placeholder implementations that can be expanded with actual functionality
- All activities are properly declared in AndroidManifest.xml

### [Date: 2026-04-09] - Fix Missing StreamingService Class
**Agent**: Cline (Anthropic)
**Task**: Fix compilation errors due to missing StreamingService class referenced by BootReceiver

**Changes Made**:
1. Created `android/app/src/main/java/com/homecameras/android/service/StreamingService.kt` - Foreground service for maintaining WebSocket connection and handling streaming

**Summary**:
- Fixed compilation errors: "Unresolved reference: StreamingService"
- Created a foreground service that maintains WebSocket connections to the backend server
- Service handles camera monitoring and streaming functionality
- Includes proper notification channel setup for Android O+ compatibility

### [Date: 2026-04-09] - Fix Missing BootReceiver and NotificationReceiver Classes
**Agent**: Cline (Anthropic)
**Task**: Fix crash caused by missing BootReceiver class referenced in AndroidManifest.xml

**Changes Made**:
1. Created `android/app/src/main/java/com/homecameras/android/receiver/BootReceiver.kt` - Handles BOOT_COMPLETED broadcast to restart streaming service after device reboot
2. Created `android/app/src/main/java/com/homecameras/android/receiver/NotificationReceiver.kt` - Handles notification actions (view camera, dismiss)

**Summary**:
- Fixed fatal crash: `java.lang.ClassNotFoundException: Didn't find class "com.homecameras.android.receiver.BootReceiver"`
- Both receivers were declared in AndroidManifest.xml but the classes were missing
- BootReceiver starts the StreamingService when the device boots up
- NotificationReceiver handles user interactions with notifications (viewing cameras, dismissing alerts)

### [Date: 2026-04-09] - Update Server URL Configuration
**Agent**: Cline (Anthropic)
**Task**: Configure Android app to point to server at compute.home:5000

**Changes Made**:
1. Updated `BACKEND_URL` in `android/app/build.gradle` from `http://localhost:5000` to `http://compute.home:5000`

**Summary**:
- The Android app will now connect to the correct server IP address by default
- This affects both REST API calls (via Retrofit) and WebSocket connections

### [Date: 2026-04-08] - Android Deployment Documentation
**Agent**: Cline (Anthropic)
**Task**: Create comprehensive Android client deployment documentation

**Changes Made**:
1. Created `android/DEPLOYMENT.md` - Comprehensive deployment guide for Android client
2. Updated `README.md` - Added Android client section and deployment overview
3. Updated `android/README.md` - Enhanced with detailed setup instructions and troubleshooting
4. Created this `AGENTS.md` file for tracking future changes

**Summary**:
- Added complete step-by-step Android deployment instructions
- Included Firebase setup requirements
- Added backend configuration for push notifications
- Provided build and installation commands
- Added troubleshooting section for common issues

**Files Modified**:
- `README.md` (added Android client section)
- `android/README.md` (enhanced with deployment details)

---

## Guidelines for Future Agents

When making changes to the Android client:

1. **Update this AGENTS.md file** with a summary of changes
2. **Follow existing UI patterns** and Material Design guidelines
3. **Test on multiple devices** with varying Android versions
4. **Update relevant documentation** to reflect changes
5. **Handle edge cases** like poor network conditions and device orientation changes

## Android Project Structure

```
android/
├── AGENTS.md                  # This file - Android change tracking
├── DEPLOYMENT.md              # Deployment guide
├── README.md                  # Android documentation
├── ANDROID_DESIGN.md          # Technical design
├── build.gradle              # Build configuration with Firebase
├── gradlew.bat               # Windows build script
└── app/
    ├── src/main/java/
    │   ├── com/homecameras/android/
    │   │   ├── data/
    │   │   │   └── api/
    │   │   │       └── WebSocketManager.kt    # Socket.IO handling
    │   │   ├── ui/
    │   │   │   ├── MainActivity.kt
    │   │   │   ├── StreamActivity.kt
    │   │   │   ├── CameraAdapter.kt
    │   │   │   └── SettingsActivity.kt (placeholder)
    │   │   ├── service/
    │   │   │   └── StreamingService.kt
    │   │   └── receiver/
    │   │       ├── BootReceiver.kt
    │   │       └── NotificationReceiver.kt
    │   └── media/
    │       └── H264Decoder.kt
```

## Contact & Support

For questions about Android client contributions or to report issues, please refer to the project maintainers.