#!/usr/bin/env python3
"""
Test script to verify the camera manager fix for PyAV invalid data errors
"""

import sys
import os
import asyncio
import logging

# Add project root src directory to Python path
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), '../web_server'))

from ..web_server.camera_manager import CameraManager, CameraConfig

# Configure logging to see detailed output
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

async def test_camera_manager_fix():
    """Test the camera manager fix for invalid data errors"""
    print("Testing CameraManager fix for PyAV invalid data errors...")
    
    # Create camera manager
    camera_manager = CameraManager()
    print("CameraManager created successfully")
    
    # Create a test camera config with an invalid IP to simulate connection issues
    config = CameraConfig(
        device_ip="192.0.2.199",  # This matches the IP from the error log
        username="admin",
        password="Gabi2557",
        port=554,
        stream_path="/onvif1",
        fps=30,
        width=1920,
        height=1080
    )
    
    print(f"Camera config created: {config}")
    print(f"Connection string: {config.connection_string}")
    
    # Test adding camera - this will trigger the error handling
    try:
        print("Testing camera manager with error handling...")
        result = await camera_manager.add_camera(config)
        print(f"Camera added with result: {result}")
        
        # Check camera state
        state = camera_manager.get_camera_state(config.device_ip)
        print(f"Camera state: {state}")
        
        print("CameraManager test completed successfully!")
        return True
        
    except Exception as e:
        print(f"Error during testing: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    # Run the async test
    result = asyncio.run(test_camera_manager_fix())
    if result:
        print("Test passed!")
        sys.exit(0)
    else:
        print("Test failed!")
        sys.exit(1)