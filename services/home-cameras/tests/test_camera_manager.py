#!/usr/bin/env python3
"""
Test script for the camera manager module
"""

import sys
import os
import asyncio

# Add project root src directory to Python path
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), '../web_server'))

from ..web_server.camera_manager import CameraManager, CameraConfig

async def test_camera_manager():
    """Test the camera manager functionality"""
    print("Testing CameraManager...")
    
    # Create camera manager
    camera_manager = CameraManager()
    print("CameraManager created successfully")
    
    # Create a test camera config
    # Using a public RTSP test stream
    config = CameraConfig(
        device_ip="test_camera",
        username="",
        password="",
        port=554,
        stream_path="/stream1",
        fps=30,
        width=1920,
        height=1080
    )
    
    print(f"Camera config created: {config}")
    print(f"Connection string: {config.connection_string}")
    
    # Test adding camera (this would normally connect to an RTSP stream)
    try:
        # Since we don't have a real camera to test with, we'll just test
        # that the methods exist and work without throwing errors
        print("Testing camera manager methods...")
        
        # Test get_all_cameras
        cameras = camera_manager.get_all_cameras()
        print(f"Initial cameras: {cameras}")
        
        # Test get_camera_state with non-existent camera
        state = camera_manager.get_camera_state("test_camera")
        print(f"State for non-existent camera: {state}")
        
        print("CameraManager tests completed successfully!")
        
    except Exception as e:
        print(f"Error during testing: {e}")
        return False
    
    return True

if __name__ == "__main__":
    # Run the async test
    result = asyncio.run(test_camera_manager())
    if result:
        print("All tests passed!")
        sys.exit(0)
    else:
        print("Tests failed!")
        sys.exit(1)