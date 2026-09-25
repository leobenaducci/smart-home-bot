# HomeCameras WebView App

A simple Android app that displays the contents of compute.home:5000 in a WebView.

## Features

- **Simple WebView**: Displays web content from compute.home:5000
- **URL Restriction**: Only allows loading the specified URL, prevents navigation to other sites
- **No Navigation Controls**: No back/forward buttons or refresh functionality
- **Basic WebView Settings**: JavaScript enabled, zoom controls available

## Installation

1. Open the project in Android Studio
2. Sync Gradle files
3. Build and run on device or emulator

## Usage

The app will automatically load http://compute.home:5000 when opened. The WebView is restricted to only this URL - users cannot navigate to other websites.

## Technical Details

- **Package Name**: com.homecameras.android
- **Minimum SDK**: 21 (Android 5.0)
- **Target SDK**: 34 (Android 14)
- **WebView Configuration**: JavaScript enabled, zoom controls available
- **URL Restriction**: Custom WebViewClient prevents navigation to any URL other than the specified one

## Notes

- The app uses a custom WebViewClient to restrict navigation to only http://compute.home:5000
- Back button is disabled to prevent navigation
- No additional UI elements are included to keep the app minimal