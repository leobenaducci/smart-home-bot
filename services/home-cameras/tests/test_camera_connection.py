#!/usr/bin/env python3
"""
Test script to check camera connections using the same configuration as the main application
"""

import sys
import os
import logging
import asyncio

# Add project root src directory to Python path
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), '../web_server'))

from ..web_server.camera_manager import CameraManager, CameraConfig
from ..web_server.settings_manager import SettingsManager

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def test_camera_connection():
    """Test camera connection using CameraManager"""
    logger.info("Starting camera connection test")
    
    # Initialize components
    settings_manager = SettingsManager()
    camera_manager = CameraManager()
    
    # Load cameras from config
    cameras = settings_manager.get_cameras()
    logger.info(f"Found {len(cameras)} cameras in config")
    
    if not cameras:
        logger.error("No cameras found in configuration")
        return
    
    # Test each camera
    for device_ip, config in cameras.items():
        try:
            logger.info(f"Testing camera {device_ip}")
            
            # Create CameraConfig object
            camera_config = CameraConfig(
                device_ip=config.get('device_ip', device_ip),
                username=config.get('username', ''),
                password=config.get('password', ''),
                port=int(config.get('port', 80)) if isinstance(config.get('port', 80), str) else config.get('port', 80),
                stream_path=config.get('stream_path', '/stream'),
                fps=int(config.get('fps', 30)) if isinstance(config.get('fps', 30), str) else config.get('fps', 30),
                width=int(config.get('width', 1920)) if isinstance(config.get('width', 1920), str) else config.get('width', 1920),
                height=int(config.get('height', 1080)) if isinstance(config.get('height', 1080), str) else config.get('height', 1080)
            )
            
            logger.info(f"Camera config: {camera_config}")
            logger.info(f"Connection string: {camera_config.connection_string}")
            
            # Try to add camera
            result = await camera_manager.add_camera(camera_config)
            logger.info(f"Camera added: {result}")
            
            # Check camera state
            state = camera_manager.get_camera_state(device_ip)
            logger.info(f"Camera state: {state}")
            
            # Try to get a frame
            logger.info("Attempting to get frame...")
            frame = await camera_manager.get_frame(device_ip, timeout=5.0)
            if frame is not None:
                logger.info(f"Successfully got frame: {frame.shape}")
            else:
                logger.warning("No frame received")
                
        except Exception as e:
            logger.error(f"Error testing camera {device_ip}: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    # Clean up
    await camera_manager.stop_all()
    logger.info("Camera connection test completed")

if __name__ == '__main__':
    asyncio.run(test_camera_connection())