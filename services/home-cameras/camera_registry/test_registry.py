#!/usr/bin/env python3
"""
Test script for Camera Registry Server
Run: python test_registry.py
"""
import requests
import json
import time
import sys

BASE_URL = "http://localhost:5001"

def test_ping():
    """Test registry health."""
    print("\n=== Testing Ping ===")
    try:
        response = requests.get(f"{BASE_URL}/api/ping", timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Response: {response.json()}")
        return response.status_code == 200
    except Exception as e:
        print(f"Error: {e}")
        return False

def test_list_cameras():
    """Test listing cameras."""
    print("\n=== Testing List Cameras ===")
    try:
        response = requests.get(f"{BASE_URL}/api/list", timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Response: {json.dumps(response.json(), indent=2)}")
        return response.status_code == 200
    except Exception as e:
        print(f"Error: {e}")
        return False

def test_register_camera():
    """Test registering a new camera."""
    print("\n=== Testing Register Camera ===")
    
    camera_data = {
        "camera_id": f"CAM-TEST-{int(time.time())}",
        "name": "Test Camera",
        "capabilities": ["rtsp", "mjpeg"],
        "credentials": {
            "username": "admin",
            "password": "test123"
        },
        "metadata": {
            "resolution": "1280x720",
            "fps": 30
        }
    }
    
    try:
        response = requests.post(
            f"{BASE_URL}/api/register",
            json=camera_data,
            timeout=5
        )
        print(f"Status: {response.status_code}")
        print(f"Response: {json.dumps(response.json(), indent=2)}")
        
        if response.status_code == 201:
            camera_id = camera_data["camera_id"]
            # Wait a moment, then verify
            time.sleep(1)
            verify = test_get_camera(camera_id)
            return response.status_code == 201 and verify
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False

def test_get_camera(camera_id):
    """Test getting camera details."""
    print(f"\n=== Testing Get Camera: {camera_id} ===")
    try:
        response = requests.get(f"{BASE_URL}/api/{camera_id}", timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Response: {json.dumps(response.json(), indent=2)}")
        return response.status_code == 200
    except Exception as e:
        print(f"Error: {e}")
        return False

def test_heartbeat(camera_id):
    """Test heartbeat endpoint."""
    print(f"\n=== Testing Heartbeat for {camera_id} ===")
    try:
        response = requests.get(f"{BASE_URL}/api/{camera_id}/ping", timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Response: {response.json()}")
        return response.status_code == 200
    except Exception as e:
        print(f"Error: {e}")
        return False

def test_stats():
    """Test stats endpoint."""
    print("\n=== Testing Stats ===")
    try:
        response = requests.get(f"{BASE_URL}/api/stats", timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Response: {json.dumps(response.json(), indent=2)}")
        return response.status_code == 200
    except Exception as e:
        print(f"Error: {e}")
        return False

def main():
    print("=" * 60)
    print("Camera Registry Server Test Suite")
    print("=" * 60)
    
    if not test_ping():
        print("\n!!! Registry server not running. Start it first! !!!")
        print(f"Run: docker-compose up -d")
        print(f"Or: python app.py")
        sys.exit(1)
    
    tests = [
        ("List Cameras", test_list_cameras),
        ("Register Camera", test_register_camera),
        ("Get Camera", lambda: test_get_camera("CAM-TEST-1") if False else None),  # Skip unless needed
        ("Heartbeat", lambda: test_heartbeat("CAM-TEST-1") if False else None),  # Skip
        ("Stats", test_stats),
    ]
    
    results = []
    for name, func in tests:
        result = func() if callable(func) else None
        results.append((name, result))
    
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {name}")
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\nAll tests passed! Camera registry is working correctly.")
    else:
        print("\nSome tests failed. Check the output above.")

if __name__ == "__main__":
    main()