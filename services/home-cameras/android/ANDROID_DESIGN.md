# HomeCameras Android Client - Design Document

## 1. Architecture Overview

The Android client follows **MVVM (Model-View-ViewModel)** architecture with **Clean Architecture** principles:

```
┌─────────────────────────────────────────────────────────────┐
│                    Presentation Layer                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │ Activities  │  │  Fragments  │  │   Adapters  │         │
│  │   & Views   │  │             │  │             │         │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘         │
│         │                │                │                 │
│         ▼                ▼                ▼                 │
│  ┌─────────────────────────────────────────────────┐       │
│  │           ViewModels (State Management)          │       │
│  └─────────────────────┬───────────────────────────┘       │
└────────────────────────┼──────────────────────────────────┘
                         │
┌────────────────────────┼──────────────────────────────────┐
│                    Data Layer                              │
│         ┌─────────────▼────────────────┐                  │
│         │         Repositories          │                  │
│         │  (Single Source of Truth)     │                  │
│         └─────────────┬────────────────┘                  │
│                       │                                   │
│         ┌─────────────┴────────────────┐                  │
│         ▼                              ▼                  │
│  ┌─────────────┐              ┌─────────────┐            │
│  │   Remote    │              │    Local    │            │
│  │   Source    │              │   Source    │            │
│  │  (Retrofit) │              │  (Room DB)  │            │
│  └─────────────┘              └─────────────┘            │
│                                                          │
│  ┌──────────────────────────────────────────┐           │
│  │      WebSocket (Real-time Updates)        │           │
│  └──────────────────────────────────────────┘           │
└──────────────────────────────────────────────────────────┘
```

## 2. Technology Stack

### Core Framework
- **Language**: Kotlin 1.9.0
- **Min SDK**: 21 (Android 5.0)
- **Target SDK**: 34 (Android 14)
- **Build Tool**: Gradle 8.1.0

### Architecture Components
- **Lifecycle**: androidx.lifecycle.* (2.7.0)
- **ViewModel**: androidx.lifecycle.ViewModel
- **LiveData**: androidx.lifecycle.LiveData
- **Room**: androidx.room.* (2.6.1)
- **Navigation**: androidx.navigation.* (2.7.6)

### Networking
- **Retrofit**: 2.9.0 (REST API)
- **OkHttp**: 4.12.0 (HTTP client + WebSocket)
- **Gson**: 2.10.1 (JSON serialization)

### Firebase
- **FCM**: firebase-messaging-ktx (23.4.1)
- **Analytics**: firebase-analytics-ktx (21.5.1)

### UI
- **Material Design**: material 1.11.0
- **Coil**: 2.5.0 (Image loading)
- **ViewBinding**: Enabled

### Concurrency
- **Coroutines**: kotlinx-coroutines-android (1.7.3)

## 3. Key Components

### 3.1 Data Models

```kotlin
// Camera.kt - Represents an IP camera
@Entity(tableName = "cameras")
data class Camera(
    @PrimaryKey val deviceIp: String,
    val username: String,
    val password: String,
    val port: Int,
    val streamPath: String,
    val fps: Int,
    val width: Int,
    val height: Int,
    val rotation: Int,
    val state: String,
    val lastSeen: Long,
    val isFavorite: Boolean,
    val notificationEnabled: Boolean
)

// MotionEvent.kt - Represents motion detection events
@Entity(tableName = "motion_events")
data class MotionEvent(
    @PrimaryKey(autoGenerate = true) val id: Long,
    val cameraIp: String,
    val zoneName: String,
    val timestamp: Double,
    val confidence: Float,
    val detectedObjects: List<DetectedObject>,
    val objectCounts: Map<String, Int>,
    val notified: Boolean,
    val snapshotPath: String?
)
```

### 3.2 Repository Pattern

```kotlin
class CameraRepository(
    private val apiService: ApiService,
    private val cameraDao: CameraDao
) {
    fun getAllCameras(): Flow<List<Camera>>
    suspend fun syncCameras(): Result<List<Camera>>
    suspend fun addCamera(camera: Camera): Result<Unit>
    suspend fun removeCamera(cameraIp: String): Result<Unit>
}

class MotionEventRepository(
    private val apiService: ApiService,
    private val motionEventDao: MotionEventDao
) {
    fun getRecentEvents(): Flow<List<MotionEvent>>
    suspend fun syncMotionEvents(): Result<List<MotionEvent>>
    suspend fun insertEvent(event: MotionEvent): Long
}
```

### 3.3 ViewModels

```kotlin
class CameraListViewModel(
    private val repository: CameraRepository
) : ViewModel() {
    private val _cameras = MutableLiveData<UiState<List<Camera>>>()
    val cameras: LiveData<UiState<List<Camera>>> = _cameras
    
    fun loadCameras() { /* ... */ }
    fun addCamera(camera: Camera) { /* ... */ }
    fun removeCamera(cameraIp: String) { /* ... */ }
}

class StreamViewModel(
    private val cameraIp: String
) : ViewModel() {
    private val _streamState = MutableLiveData<StreamState>()
    val streamState: LiveData<StreamState> = _streamState
    
    fun connect() { /* ... */ }
    fun disconnect() { /* ... */ }
    fun takeSnapshot() { /* ... */ }
}
```

## 4. Push Notifications Flow

### 4.1 Registration Flow

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   Android   │    │   Firebase  │    │   Backend   │
│    App      │    │     FCM     │    │   Server    │
└──────┬──────┘    └──────┬──────┘    └──────┬──────┘
       │                  │                  │
       │  Request Token   │                  │
       │─────────────────>│                  │
       │                  │                  │
       │    FCM Token     │                  │
       │<─────────────────│                  │
       │                  │                  │
       │                  │   Register Token │
       │                  │─────────────────>│
       │                  │                  │
       │                  │     Success      │
       │                  │<─────────────────│
       │                  │                  │
```

### 4.2 Notification Flow

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   Camera    │    │   Backend   │    │   Android   │
│   System    │    │   Server    │    │    App      │
└──────┬──────┘    └──────┬──────┘    └──────┬──────┘
       │                  │                  │
       │  Motion Detected │                  │
       │─────────────────>│                  │
       │                  │                  │
       │                  │  Send to FCM     │
       │                  │─────────────────>│
       │                  │                  │
       │                  │   Show Notify    │
       │                  │<─────────────────│
       │                  │                  │
```

## 5. API Integration

### 5.1 REST Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/login` | POST | Authenticate |
| `/api/cameras` | GET | Get all cameras |
| `/api/cameras/{ip}/state` | GET | Get camera state |
| `/api/motion_events` | GET | Get motion events |
| `/api/cameras/{ip}/settings` | GET | Get camera settings |
| `/api/cameras/{ip}/settings` | POST | Update camera settings |
| `/api/register_device` | POST | Register FCM device |
| `/api/unregister_device` | POST | Unregister FCM device |

### 5.2 WebSocket Events

| Event | Direction | Description |
|-------|-----------|-------------|
| `camera_list` | Server → Client | List of cameras |
| `request_camera_list` | Client → Server | Request camera list |
| `request_frame` | Client → Server | Request video frame |
| `video_frame` | Server → Client | Video frame data |
| `add_camera` | Client → Server | Add new camera |
| `remove_camera` | Client → Server | Remove camera |

## 6. Database Schema

### Cameras Table
```sql
CREATE TABLE cameras (
    deviceIp TEXT PRIMARY KEY,
    username TEXT,
    password TEXT,
    port INTEGER,
    streamPath TEXT,
    fps INTEGER,
    width INTEGER,
    height INTEGER,
    rotation INTEGER,
    state TEXT,
    lastSeen INTEGER,
    isFavorite INTEGER,
    notificationEnabled INTEGER
)
```

### Motion Events Table
```sql
CREATE TABLE motion_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cameraIp TEXT,
    zoneName TEXT,
    timestamp REAL,
    confidence REAL,
    detectedObjects TEXT,
    objectCounts TEXT,
    notified INTEGER,
    snapshotPath TEXT
)
```

## 7. Notification Channels

| Channel ID | Name | Importance | Description |
|------------|------|------------|-------------|
| `motion_alerts` | Motion Alerts | HIGH | Motion detection alerts |
| `camera_status` | Camera Status | DEFAULT | Camera online/offline |
| `app_updates` | App Updates | LOW | General app updates |

## 8. Permissions

```xml
<!-- Network -->
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />

<!-- Notifications -->
<uses-permission android:name="android.permission.POST_NOTIFICATIONS" />

<!-- Foreground Service -->
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_DATA_SYNC" />

<!-- Biometric -->
<uses-permission android:name="android.permission.USE_BIOMETRIC" />
```

## 9. Screen Designs

### 9.1 Login Screen
- Password input field
- Login button
- Biometric authentication option

### 9.2 Dashboard (Camera List)
- RecyclerView with camera cards
- Each card shows:
  - Camera name/IP
  - Connection status (green/yellow/red dot)
  - Last seen timestamp
  - Quick actions (favorite, notifications)
- FAB for adding new camera
- Pull-to-refresh

### 9.3 Stream View
- Full-screen MJPEG stream
- Pinch-to-zoom
- Control overlay:
  - Back button
  - Fullscreen toggle
  - Snapshot button
  - Settings button

### 9.4 Settings Screen
- Server URL configuration
- Notification preferences
- Quiet hours settings
- Biometric authentication toggle
- About section

## 10. Backend Integration

The backend (`src/fcm_service.py`) provides:

1. **Device Registration**: Store FCM tokens for push notifications
2. **Motion Notifications**: Send push notifications when motion is detected
3. **Status Notifications**: Notify when cameras go online/offline

### Configuration

Set `FCM_SERVICE_ACCOUNT_PATH` in `src/web_server.py` to enable push notifications:

```python
FCM_SERVICE_ACCOUNT_PATH = "path/to/firebase-service-account.json"
```

Install Firebase Admin SDK:
```bash
pip install firebase-admin
```

## 11. Build Instructions

1. **Setup Firebase**:
   - Create Firebase project
   - Download `google-services.json`
   - Place in `android/app/` directory

2. **Configure Server URL**:
   - Update `local.properties` with backend URL
   - Or configure in app settings

3. **Build**: the Gradle wrapper here is incomplete (no `gradle-wrapper.jar`,
   no Unix `gradlew`), so regenerate it once with a system Gradle first — see
   [DEPLOYMENT.md](DEPLOYMENT.md).
   ```bash
   cd android
   gradle wrapper --gradle-version 8.13
   ./gradlew assembleDebug
   ```

4. **Install**:
   ```bash
   adb install app/build/outputs/apk/debug/app-debug.apk
   ```

## 12. Future Enhancements

- **Multi-camera grid view** for simultaneous monitoring
- **Video recording** capability
- **Two-way audio** support
- **Cloud storage** integration
- **AI-powered** object classification
- **Geofencing** for location-based alerts
- **Widget** for home screen monitoring
- **Wear OS** support