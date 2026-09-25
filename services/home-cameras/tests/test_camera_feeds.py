#!/usr/bin/env python3
"""
Test module to display camera feeds using PyAV and OpenCV
This script creates OpenCV windows showing live feeds from all configured cameras
"""

import cv2
import json
import os
import threading
import time
import logging
from typing import Dict, Optional
import numpy as np
import av

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_DEFAULT_CONFIG_PATH = os.path.join(_PROJECT_ROOT, 'config', 'cameras.json')

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CameraFeedTester:
    """Test class to display camera feeds using PyAV and OpenCV"""
    
    def __init__(self, config_path: str = None):
        self.config_path = config_path or _DEFAULT_CONFIG_PATH
        self.cameras_config = {}
        self.capture_threads = {}
        self.frames = {}
        self.running = False
        self.load_camera_config()
    
    def load_camera_config(self) -> None:
        """Load camera configuration from JSON file"""
        try:
            with open(self.config_path, 'r') as f:
                self.cameras_config = json.load(f)
            logger.info(f"Loaded configuration for {len(self.cameras_config)} cameras")
        except Exception as e:
            logger.error(f"Failed to load camera configuration: {e}")
            raise
    
    def build_rtsp_url(self, camera_config: Dict) -> str:
        """Build RTSP URL from camera configuration"""
        ip = camera_config.get('device_ip')
        username = camera_config.get('username')
        password = camera_config.get('password')
        port = camera_config.get('port', '554')
        stream_path = camera_config.get('stream_path', '/onvif1')
        
        # Build RTSP URL
        rtsp_url = f"rtsp://{username}:{password}@{ip}:{port}{stream_path}"
        logger.info(f"Built RTSP URL for {ip}: {rtsp_url}")
        return rtsp_url
    
    def capture_frames_with_av(self, camera_id: str, rtsp_url: str) -> None:
        """Capture frames from a camera using PyAV in a separate thread"""
        logger.info(f"Starting PyAV capture thread for camera {camera_id}")
        
        try:
            # Open RTSP stream using PyAV
            logger.info(f"Attempting to connect to camera {camera_id} with URL: {rtsp_url}")
            container = av.open(rtsp_url, mode='r', timeout=10000000)  # 10 second timeout
        except Exception as e:
            logger.error(f"Failed to open RTSP stream for {camera_id} with URL: {rtsp_url}: {e}")
            return
        
        # Get video stream
        video_stream = None
        for stream in container.streams:
            if stream.type == 'video':
                video_stream = stream
                break
        
        if video_stream is None:
            logger.error(f"No video stream found in RTSP stream for {camera_id}")
            container.close()
            return
        
        logger.info(f"Video stream found for {camera_id}: {video_stream.codec_context.name}, resolution: {video_stream.width}x{video_stream.height}")
        
        frame_count = 0
        
        try:
            logger.info(f"Starting frame capture loop for {camera_id}")
            while self.running:
                try:
                    # Read packet from container with timeout
                    packet = None
                    for packet in container.demux(video_stream):
                        break
                    
                    if packet is None:
                        logger.warning(f"No packet received from {camera_id}")
                        time.sleep(0.1)
                        continue
                    
                    # Decode packet to frame
                    frames = video_stream.decode(packet)
                    
                    if not frames:
                        continue
                    
                    frame = frames[0]
                    frame_count += 1
                    
                    if frame_count % 100 == 0:
                        logger.info(f"Read {frame_count} frames from {camera_id}")
                    
                    # Convert PyAV frame to numpy array for OpenCV compatibility
                    frame_array = self._av_frame_to_numpy(frame)
                    
                    if frame_array is not None:
                        # Store the latest frame
                        self.frames[camera_id] = frame_array
                        logger.debug(f"Successfully decoded and stored frame from {camera_id}")
                    else:
                        logger.warning(f"Failed to convert frame from {camera_id}")
                    
                except Exception as e:
                    logger.error(f"Error decoding frame from {camera_id}: {e}")
                    time.sleep(1)
                    
        except Exception as e:
            logger.error(f"Error in capture loop for {camera_id}: {e}")
        finally:
            container.close()
            logger.info(f"Capture thread for camera {camera_id} stopped")
    
    def _av_frame_to_numpy(self, frame: 'av.frame.Frame') -> Optional[np.ndarray]:
        """Convert PyAV frame to numpy array for OpenCV compatibility"""
        try:
            # Convert to RGB first if needed
            if frame.format.name != 'rgb24':
                frame = frame.reformat(format='rgb24')
            
            # Get numpy array from frame
            frame_array = frame.to_ndarray(format='rgb24')
            
            # Convert RGB to BGR for OpenCV compatibility
            frame_array = cv2.cvtColor(frame_array, cv2.COLOR_RGB2BGR)
            
            return frame_array
        except Exception as e:
            logger.error(f"Error converting PyAV frame to numpy: {e}")
            return None
    
    def start_capture_threads(self) -> None:
        """Start capture threads for all cameras"""
        self.running = True
        
        for camera_id, config in self.cameras_config.items():
            rtsp_url = self.build_rtsp_url(config)
            
            # Start a thread for each camera
            thread = threading.Thread(
                target=self.capture_frames_with_av,
                args=(camera_id, rtsp_url),
                daemon=True
            )
            thread.start()
            self.capture_threads[camera_id] = thread
            logger.info(f"Started capture thread for camera {camera_id}")
    
    def stop_capture_threads(self) -> None:
        """Stop all capture threads"""
        self.running = False
        for camera_id, thread in self.capture_threads.items():
            thread.join(timeout=2)
            logger.info(f"Stopped capture thread for camera {camera_id}")
    
    def display_feeds(self) -> None:
        """Display camera feeds in OpenCV windows"""
        logger.info("Starting to display camera feeds. Press 'q' to quit.")
        
        # Create windows for each camera
        #for camera_id in self.cameras_config.keys():
        #    cv2.namedWindow(f"Camera {camera_id}", cv2.WINDOW_AUTOSIZE)
        
        try:
            while True:
                # Display frames for each camera
                for camera_id in self.cameras_config.keys():
                    if camera_id in self.frames:
                        frame = self.frames[camera_id]
                        # Add camera ID as text on frame
                        cv2.putText(frame, f"Camera {camera_id}", (10, 30), 
                                   cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                        cv2.imshow(f"Camera {camera_id}", frame)
                
                # Check for quit key
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
                    
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            # Clean up
            cv2.destroyAllWindows()
            self.stop_capture_threads()
    
    def run(self) -> None:
        """Run the camera feed tester"""
        logger.info("Starting camera feed tester...")
        
        # Start capture threads
        self.start_capture_threads()
        
        # Give threads a moment to start capturing
        time.sleep(2)
        
        # Display feeds
        self.display_feeds()

def main():
    """Main function"""
    tester = CameraFeedTester()
    tester.run()

if __name__ == "__main__":
    main()