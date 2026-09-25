"""
Hardware H.264 Encoder Module - NVIDIA NVENC Accelerated
Provides hardware-accelerated H.264 encoding using NVIDIA GPU (NVENC)
with software fallback for systems without NVENC support.
"""

import cv2
import numpy as np
import threading
import time
import logging
import subprocess
import os
from typing import Optional, Tuple, Dict
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class H264EncoderConfig:
    """Configuration for H.264 encoder"""
    width: int = 1920
    height: int = 1080
    fps: int = 30
    bitrate: int = 2000000
    gop_size: int = 30
    profile: str = "high"
    preset: str = "quality"
    rc_mode: str = "vbr"


class H264Encoder:
    """Hardware-accelerated H.264 encoder using NVIDIA NVENC via FFmpeg."""
    
    def __init__(self, config: H264EncoderConfig):
        self.config = config
        self.process = None
        self.is_open = False
        self._lock = threading.Lock()
        self._use_nvenc = self._check_nvenc()
        
        if self._use_nvenc:
            logger.info("NVENC available, using hardware encoding")
        else:
            logger.warning("NVENC not available, falling back to CPU encoding")
        
        self._init_encoder()
    
    def _check_nvenc(self) -> bool:
        """Check if NVENC is available by testing h264_nvenc codec"""
        try:
            result = subprocess.run(
                ['ffmpeg', '-encoders'],
                capture_output=True,
                text=True,
                timeout=5
            )
            return 'h264_nvenc' in result.stdout
        except Exception:
            return False
    
    def _init_encoder(self) -> bool:
        """Initialize FFmpeg process for H.264 encoding"""
        try:
            # FFmpeg command for H.264 encoding
            # Uses NVENC if available, otherwise falls back to libx264
            codec = 'h264_nvenc' if self._use_nvenc else 'libx264'
            
            # Build FFmpeg command
            # Input: raw YUV420p frames from stdin
            # Output: raw H.264 elementary stream (no container)
            # Using 'h264' format for raw NAL unit output
            cmd = [
                'ffmpeg',
                '-y',  # Overwrite output file
                '-f', 'rawvideo',  # Input format
                '-vcodec', 'rawvideo',  # Input codec
                '-pix_fmt', 'yuv420p',  # Input pixel format
                '-s', f'{self.config.width}x{self.config.height}',  # Frame size
                '-r', str(self.config.fps),  # Frame rate
                '-i', '-',  # Input from stdin
                '-c:v', codec,  # Output codec
                '-pix_fmt', 'yuv420p',  # Output pixel format
                '-profile:v', self.config.profile,
                '-b:v', str(self.config.bitrate),
                '-g', str(self.config.gop_size),
                '-bf', '0',  # No B-frames
                '-preset', self.config.preset,  # NVENC preset
                '-rc', self.config.rc_mode,  # Rate control mode
                '-f', 'h264',  # Output format - raw H.264 elementary stream
                '-'  # Output to stdout
            ]
            
            # Start FFmpeg process
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=10**8  # Large buffer for streaming
            )
            
            self.is_open = True
            logger.info(f"H.264 encoder initialized: {self.config.width}x{self.config.height} @ {self.config.fps}fps using {codec}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize H.264 encoder: {e}")
            self.is_open = False
            return False
    
    def encode_frame(self, frame: np.ndarray) -> Optional[bytes]:
        """Encode a single frame as H.264 using FFmpeg"""
        if not self.is_open or self.process is None:
            return None
        
        with self._lock:
            try:
                # Convert BGR to YUV for H.264 encoding
                frame_yuv = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420)
                
                # Write raw frame data to FFmpeg stdin
                self.process.stdin.write(frame_yuv.tobytes())
                self.process.stdin.flush()
                
                # Read encoded frame from FFmpeg stdout
                # For elementary stream, we need to read available data
                # Use non-blocking read with a reasonable buffer size
                import select
                import sys
                
                # Check if data is available to read
                encoded_data = b''
                try:
                    # Try to read available data
                    while True:
                        # Read in chunks to get complete NAL units
                        chunk = self.process.stdout.read(4096)
                        if not chunk:
                            break
                        encoded_data += chunk
                        # Small delay to allow more data to accumulate
                        import time
                        time.sleep(0.001)
                except Exception:
                    pass
                
                if encoded_data:
                    logger.debug(f"Encoded frame size: {len(encoded_data)} bytes")
                    return encoded_data
                return None
                
            except Exception as e:
                logger.error(f"Error encoding frame: {e}")
                return None
    
    def flush(self) -> bytes:
        """Flush any remaining encoded data"""
        if not self.is_open or self.process is None:
            return b''
        
        with self._lock:
            try:
                # Send flush signal to FFmpeg
                self.process.stdin.write(b'\x00' * 100)  # Padding for flush
                self.process.stdin.flush()
                
                # Read any remaining data
                remaining = b''
                try:
                    while True:
                        data = self.process.stdout.read(1024)
                        if not data:
                            break
                        remaining += data
                except Exception:
                    pass
                
                return remaining
            except Exception as e:
                logger.error(f"Error flushing encoder: {e}")
                return b''
    
    def close(self):
        """Close the encoder and cleanup resources"""
        if self.is_open and self.process is not None:
            try:
                self.flush()
            except Exception as e:
                logger.error(f"Error closing encoder: {e}")
            finally:
                try:
                    if self.process.stdin:
                        self.process.stdin.close()
                    if self.process.stdout:
                        self.process.stdout.close()
                    if self.process.stderr:
                        self.process.stderr.close()
                    self.process.terminate()
                    self.process.wait(timeout=2)
                except Exception:
                    try:
                        if self.process:
                            self.process.kill()
                    except Exception:
                        pass
                finally:
                    self.is_open = False
                    self.process = None


class H264StreamGenerator:
    """Generate H.264 stream from camera frames."""
    
    def __init__(self, width: int = 1920, height: int = 1080, fps: int = 30):
        self.width = width
        self.height = height
        self.fps = fps
        self.config = H264EncoderConfig(
            width=width, height=height, fps=fps,
            bitrate=2000000, gop_size=fps, profile="high"
        )
        self.encoder = H264Encoder(self.config)
        self.frame_count = 0
        self._sps_pps = b''
        self._first_frame = True
    
    def generate_stream(self, frame: np.ndarray) -> Optional[bytes]:
        """Generate H.264 stream from a single frame"""
        if frame is None:
            return None
        
        encoded = self.encoder.encode_frame(frame)
        if encoded is None:
            return None
        
        self.frame_count += 1
        
        # For the first frame, we need to include SPS/PPS for proper decoding
        if self._first_frame:
            self._first_frame = False
            logger.info(f"First H.264 frame generated, size: {len(encoded)} bytes")
        
        return encoded
    
    def flush(self) -> bytes:
        """Flush any remaining encoded data"""
        return self.encoder.flush()
    
    def close(self):
        """Close the encoder"""
        self.encoder.close()