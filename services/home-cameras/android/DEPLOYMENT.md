# HomeCameras Android Client - Deployment Guide

This guide provides step-by-step instructions for deploying the HomeCameras Android client application.

## Prerequisites

### Development Environment
- Android Studio Arctic Fox or newer
- Android SDK 21 (Android 5.0) or higher
- Java Development Kit (JDK) 17 or higher
- Gradle 8.1.0 or compatible version

### Firebase Setup
- Firebase project with FCM (Firebase Cloud Messaging) enabled
- Firebase service account key for backend integration

### Backend Requirements
- HomeCameras backend server running and accessible
- Firebase Admin SDK installed on backend server
- Network connectivity between Android device and backend server

## Step 1: Firebase Configuration

### 1.1 Create Firebase Project
1. Go to [Firebase Console](https://console.firebase.google.com/)
2. Click "Add project" and follow the setup wizard
3. Give your project a name (e.g., "HomeCameras")
4. Accept terms and create the project

### 1.2 Add Android App to Firebase
1. In Firebase Console, click the Android icon to add an Android app
2. Enter package name: `com.homecameras.android`
3. Enter app nickname (optional)
4. Register the app

### 1.3 Download Configuration File
1. Download the `google-services.json` file
2. Place it in the `android/app/` directory
3. This file contains Firebase project configuration

### 1.4 Enable Firebase Cloud Messaging
1. In Firebase Console, go to Project Settings
2. Navigate to Cloud Messaging tab
3. Copy the Server Key (you'll need this for backend configuration)

## Step 2: Backend Configuration

### 2.1 Install Firebase Admin SDK
```bash
pip install firebase-admin
```

### 2.2 Configure Backend for Push Notifications
1. Download Firebase service account key:
   - Go to Firebase Console → Project Settings
   - Navigate to Service Accounts tab
   - Click "Generate new private key"
   - Save the JSON file

2. Update backend configuration:
   - Place the service account key file in your backend directory
   - Set the path in `src/web_server.py`:
   ```python
   FCM_SERVICE_ACCOUNT_PATH = "path/to/firebase-service-account.json"
   ```

3. Restart the backend server to apply changes

## Step 3: Android Project Setup

### 3.1 Open Project in Android Studio
1. Launch Android Studio
2. Select "Open an existing Android Studio project"
3. Navigate to the `android/` directory
4. Select the project and wait for Gradle sync to complete

### 3.2 Configure Backend Server URL
Update the backend server URL in one of these ways:

**Option A: local.properties file**
Create or edit `android/local.properties`:
```properties
backend_url=http://your-server-ip:5000
```

**Option B: App Settings**
1. Run the app
2. Go to Settings
3. Configure the server URL in the app settings

### 3.3 Verify Dependencies
Ensure all dependencies are properly configured in `android/app/build.gradle`:
- Firebase dependencies are included
- Retrofit for API calls
- Room for local database
- Coroutines for async operations

## Step 4: Build and Run

### 4.1 Build the Application
```bash
cd android
# The Gradle wrapper is incomplete in this directory: `gradle-wrapper.jar` and
# the Unix `gradlew` were never committed, so `./gradlew` does not run here (on
# any platform -- `gradlew.bat` needs the same missing jar). Regenerate it once
# with a system Gradle 8.13+, after which `./gradlew` works normally:
gradle wrapper --gradle-version 8.13
./gradlew assembleDebug
```

### 4.2 Install on Device
```bash
# Connect Android device via USB
adb install app/build/outputs/apk/debug/app-debug.apk
```

### 4.3 Run in Android Studio
1. Connect Android device or start emulator
2. Click "Run" button in Android Studio
3. Select your device from the device list
4. Wait for installation and launch

## Step 5: Application Configuration

### 5.1 Initial Setup
1. Launch the app
2. Enter the admin password (default: `admin`)
3. Configure server URL if not set in local.properties
4. Save settings

### 5.2 Add Cameras
1. Go to the main dashboard
2. Tap the "+" button to add a camera
3. Enter camera details:
   - IP address
   - Username and password
   - Port and stream path
   - Camera settings (resolution, FPS)
4. Test connection
5. Save camera configuration

### 5.3 Configure Notifications
1. Go to Settings → Notifications
2. Enable motion detection alerts
3. Configure notification preferences:
   - Sound and vibration
   - Quiet hours
   - Notification frequency
4. Test notification functionality

## Step 6: Testing and Verification

### 6.1 Test Camera Connection
1. Add a camera to the app
2. Verify the camera appears in the dashboard
3. Check connection status (should show green)
4. Tap camera to view live stream

### 6.2 Test Motion Detection
1. Ensure motion detection is enabled on the backend
2. Trigger motion in front of the camera
3. Verify motion events appear in the app
4. Check that push notifications are received

### 6.3 Test Push Notifications
1. Configure Firebase properly on both client and server
2. Trigger motion detection
3. Verify push notification appears on device
4. Check notification content and actions

## Troubleshooting

### Common Issues

**Firebase Configuration Errors**
- Ensure `google-services.json` is in the correct location
- Verify package name matches Firebase project
- Check Firebase dependencies are up to date

**Backend Connection Issues**
- Verify backend server is running
- Check network connectivity
- Ensure correct server URL is configured
- Verify firewall allows connections on port 5000

**Push Notification Issues**
- Ensure FCM is enabled in Firebase Console
- Verify service account key is correctly configured
- Check device has internet connectivity
- Ensure notification permissions are granted

**Build Errors**
- Update Android Studio to latest version
- Ensure Gradle version is compatible
- Clean and rebuild project if needed
- Check all dependencies are available

### Debug Information

**Enable Debug Logging**
1. Go to Settings → Developer Options
2. Enable debug logging
3. Check logcat for detailed error messages

**Check Network Connectivity**
1. Verify device can reach backend server
2. Test with simple HTTP request
3. Check for firewall or network restrictions

## Production Deployment

### 1. Generate Signed APK
1. In Android Studio: Build → Generate Signed Bundle / APK
2. Select APK format
3. Create or select keystore
4. Fill in keystore information
5. Select release build type
6. Generate signed APK

### 2. App Store Submission
1. Create Google Play Developer account
2. Prepare app listing materials
3. Follow Google Play Console submission guidelines
4. Submit app for review

### 3. Production Backend Configuration
1. Use production Firebase project
2. Configure proper SSL certificates
3. Set up production server environment
4. Monitor app performance and errors

## Support and Maintenance

### Regular Updates
- Keep Firebase dependencies updated
- Monitor for security updates
- Test with new Android versions
- Update app permissions as needed

### Monitoring
- Monitor push notification delivery
- Track app crashes and errors
- Monitor backend server performance
- Check camera connectivity regularly

### User Support
- Provide clear setup instructions
- Document common issues and solutions
- Maintain up-to-date documentation
- Respond to user feedback and questions

## Additional Resources

- [Firebase Documentation](https://firebase.google.com/docs)
- [Android Developer Guide](https://developer.android.com/guide)
- [HomeCameras Backend Documentation](../AGENTS.md)
- [Android Design Document](ANDROID_DESIGN.md)