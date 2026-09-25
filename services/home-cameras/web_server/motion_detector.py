"""
Motion Detection Engine Module - GPU Accelerated
Implements OpenCV-based motion detection with configurable zones and GPU acceleration
"""

import cv2
import numpy as np
import os
import time
import asyncio
from typing import List, Optional, Tuple, Dict
from dataclasses import dataclass, field
from enum import Enum
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Check if OpenCV CUDA is available
CV2_CUDA_AVAILABLE = False
try:
    if hasattr(cv2, 'cuda') and cv2.cuda is not None:
        CV2_CUDA_AVAILABLE = cv2.cuda.getCudaEnabledDeviceCount() > 0
        if CV2_CUDA_AVAILABLE:
            logger.info(f"OpenCV CUDA available: {cv2.cuda.getCudaEnabledDeviceCount()} device(s)")
except Exception:
    CV2_CUDA_AVAILABLE = False


@dataclass
class MotionZone:
    """Configuration for motion detection zone"""
    name: str
    x: float       # 0-1 (percentage) or pixel value
    y: float
    width: float
    height: float
    sensitivity: float = 0.5
    min_area: int = 500
    filter_light_changes: bool = False
    light_change_threshold: float = 0.35


# The zone a camera gets when nobody has drawn one, and the margin that keeps
# the clock out of it.
#
# These cameras burn their own timestamp into the picture, and it reprints every
# second: that is motion, in the same place, forever. Which edge it sits on
# depends on how the camera was mounted -- both of this house's busiest are
# rotated, so the strip runs vertically down the *left* edge on Front and the
# *right* edge on Patio, about 58px of 1296 (4.5%) in each case. A margin on one
# guessed corner would therefore be right for one camera and wrong for the
# other, so every edge is inset instead.
#
# 5% clears the widest strip measured here with a little to spare. What it costs
# is a border where an object is not counted until it steps inside; on a camera
# watching a driveway that is the instant something enters frame, not anything
# that happens in the scene. Raise it for a camera whose OSD is larger.
#
# Note this is no longer the only thing standing between the clock and a
# recording -- a confirmed person, vehicle or animal is now required as well,
# and a clock is none of those. The margin still earns its place by keeping YOLO
# from being woken once a second, every second, on a card the detectors and the
# assistant are both using.
CLOCK_SAFE_MARGIN = float(os.environ.get('MOTION_CLOCK_MARGIN', '0.05'))


def full_frame_zone() -> MotionZone:
    """Full frame less the clock margin, for a camera with no zones drawn."""
    m = CLOCK_SAFE_MARGIN
    return MotionZone(
        name='Full Frame',
        x=m, y=m, width=1.0 - 2 * m, height=1.0 - 2 * m,
        sensitivity=0.5, min_area=500
    )


@dataclass
class MotionEvent:
    """Motion event data structure"""
    camera_id: str
    zone_name: str
    timestamp: float
    motion_mask: np.ndarray
    event_id: int
    confidence: float


class MotionDetector:
    """
    OpenCV-based motion detection with zone support and GPU acceleration
    """
    
    def __init__(
        self,
        detector_type: str = "frame_diff",
        background_update_rate: int = 5,
        use_gpu: bool = True
    ):
        self.detector_type = detector_type
        self.background_update_rate = background_update_rate
        self.use_gpu = use_gpu and CV2_CUDA_AVAILABLE
        self.zones: List[MotionZone] = []
        self._lock = asyncio.Lock()
        
        # Motion detection state per camera
        self.background_frames: Dict[str, np.ndarray] = {}
        # GPU-specific state
        self.gpu_background_frames: Dict[str, 'cv2.cuda_GpuMat'] = {}
        self._frame_counts: Dict[str, int] = {}
        self._event_counters: Dict[str, int] = {}
        
    def add_zone(self, zone: MotionZone) -> None:
        """Add a motion detection zone"""
        self.zones.append(zone)
        logger.info(f"Added motion zone: {zone.name} at ({zone.x}, {zone.y})")
        
    def detect_motion(
        self,
        frame: np.ndarray,
        camera_id: str,
        event_counter: Dict[str, int]
    ) -> List[MotionEvent]:
        """
        Detect motion in frame using configured zones
        Returns list of motion events detected
        Uses GPU acceleration if available and enabled
        """
        # Use GPU-accelerated path if available
        if self.use_gpu:
            return self._detect_motion_gpu(frame, camera_id, event_counter)
        
        # CPU path
        return self._detect_motion_cpu(frame, camera_id, event_counter)
    
    def _detect_motion_cpu(
        self,
        frame: np.ndarray,
        camera_id: str,
        event_counter: Dict[str, int]
    ) -> List[MotionEvent]:
        """
        CPU-based motion detection implementation.
        Called directly when GPU is not available or not enabled.
        """
        events = []
        
        # Update background model if needed
        if camera_id not in self.background_frames:
            # Convert to grayscale for background storage
            if len(frame.shape) == 3:
                self.background_frames[camera_id] = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            else:
                self.background_frames[camera_id] = frame.copy()
            self._frame_counts[camera_id] = 0
            self._event_counters[camera_id] = 0
        elif self._frame_counts[camera_id] >= self.background_update_rate:
            # Update background with current frame (convert to grayscale first)
            if len(frame.shape) == 3:
                gray_for_update = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            else:
                gray_for_update = frame
            self.background_frames[camera_id] = cv2.addWeighted(
                gray_for_update, 0.7,
                self.background_frames[camera_id], 0.3, 0
            )
            self._frame_counts[camera_id] = 0
            
        self._frame_counts[camera_id] += 1
        
        # Perform motion detection based on type
        if self.detector_type == "frame_diff":
            motion_mask = self._calculate_frame_difference(frame, camera_id)
        elif self.detector_type == "gaussian_mixture":
            motion_mask = self._gaussian_background_sub(frame, camera_id)
        else:
            motion_mask = self._calculate_frame_difference(frame, camera_id)
            
        has_light_filter = any(z.filter_light_changes for z in self.zones)
        bg_frame = self.background_frames.get(camera_id) if has_light_filter else None
        
        for zone in self.zones:
            motion_event = self._check_zone_motion(
                motion_mask, zone, camera_id, event_counter,
                current_frame=frame if zone.filter_light_changes else None,
                background_frame=bg_frame
            )
            if motion_event:
                events.append(motion_event)
                
        return events
    
    def _detect_motion_gpu(
        self,
        frame: np.ndarray,
        camera_id: str,
        event_counter: Dict[str, int]
    ) -> List[MotionEvent]:
        """
        GPU-accelerated motion detection using OpenCV CUDA.
        Falls back to CPU detection if CUDA is not available.
        """
        if not CV2_CUDA_AVAILABLE:
            logger.warning("GPU acceleration requested but CUDA not available, falling back to CPU")
            # Directly call CPU implementation to avoid infinite recursion
            return self._detect_motion_cpu(frame, camera_id, event_counter)
        
        events = []
        
        # Convert to grayscale and upload to GPU
        if len(frame.shape) == 3:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        else:
            gray = frame
        
        # Initialize GPU background frame if needed
        if camera_id not in self.gpu_background_frames:
            self.gpu_background_frames[camera_id] = cv2.cuda_GpuMat()
            self.gpu_background_frames[camera_id].upload(gray)
            self._frame_counts[camera_id] = 0
        elif self._frame_counts.get(camera_id, 0) >= self.background_update_rate:
            # Update background with weighted average on GPU
            current_gpu = cv2.cuda_GpuMat()
            current_gpu.upload(gray)
            
            # Use cv2.cuda.addWeighted for GPU-accelerated blending
            bg_gpu = self.gpu_background_frames[camera_id]
            cv2.cuda.addWeighted(current_gpu, 0.7, bg_gpu, 0.3, 0, bg_gpu)
            self._frame_counts[camera_id] = 0
        
        self._frame_counts[camera_id] = self._frame_counts.get(camera_id, 0) + 1
        
        # Perform GPU-accelerated motion detection
        if self.detector_type == "frame_diff":
            motion_mask = self._calculate_frame_difference_gpu(gray, camera_id)
        elif self.detector_type == "gaussian_mixture":
            motion_mask = self._gaussian_background_sub_gpu(frame, camera_id)
        else:
            motion_mask = self._calculate_frame_difference_gpu(gray, camera_id)
        
        has_light_filter = any(z.filter_light_changes for z in self.zones)
        bg_frame_cpu = None
        if has_light_filter and camera_id in self.gpu_background_frames:
            try:
                bg_frame_cpu = self.gpu_background_frames[camera_id].download()
            except Exception:
                bg_frame_cpu = None
        
        for zone in self.zones:
            motion_event = self._check_zone_motion(
                motion_mask, zone, camera_id, event_counter,
                current_frame=frame if zone.filter_light_changes else None,
                background_frame=bg_frame_cpu
            )
            if motion_event:
                events.append(motion_event)
        
        return events
    
    def _calculate_frame_difference_gpu(self, gray: np.ndarray, camera_id: str) -> np.ndarray:
        """GPU-accelerated frame difference calculation"""
        if not CV2_CUDA_AVAILABLE:
            raise RuntimeError("CUDA not available for GPU-accelerated frame difference")
        
        # Upload current frame to GPU
        current_gpu = cv2.cuda_GpuMat()
        current_gpu.upload(gray)
        
        # Get background frame (already on GPU)
        bg_gpu = self.gpu_background_frames.get(camera_id)
        if bg_gpu is None:
            # Create background from current frame
            self.gpu_background_frames[camera_id] = cv2.cuda_GpuMat()
            self.gpu_background_frames[camera_id].upload(gray)
            bg_gpu = self.gpu_background_frames[camera_id]
        
        # Calculate absolute difference on GPU
        diff_gpu = cv2.cuda_GpuMat()
        cv2.cuda.absdiff(current_gpu, bg_gpu, diff_gpu)
        
        # Apply threshold on GPU
        thresh_gpu = cv2.cuda_GpuMat()
        cv2.cuda.threshold(diff_gpu, 25, 255, cv2.THRESH_BINARY, thresh_gpu)
        
        # Morphological operations on GPU
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        
        # Close operation on GPU
        cv2.cuda.morphologyEx(thresh_gpu, cv2.MORPH_CLOSE, kernel, thresh_gpu)
        # Open operation on GPU
        cv2.cuda.morphologyEx(thresh_gpu, cv2.MORPH_OPEN, kernel, thresh_gpu)
        # Dilate on GPU
        cv2.cuda.dilate(thresh_gpu, kernel, thresh_gpu, iterations=2)
        
        # Download result to CPU
        thresh = thresh_gpu.download()
        return thresh
    
    def _gaussian_background_sub_gpu(self, frame: np.ndarray, camera_id: str) -> np.ndarray:
        """GPU-accelerated Gaussian Mixture background subtraction"""
        if not CV2_CUDA_AVAILABLE:
            raise RuntimeError("CUDA not available for GPU-accelerated background subtraction")
        
        # Convert to grayscale if not already
        if len(frame.shape) == 3:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        else:
            gray = frame
        
        key = self._get_camera_key(camera_id)
        
        # Create or get GPU background subtractor
        if key not in self.background_frames:
            # Use createBackgroundSubtractorMOG2 (works with GPU mats)
            self.background_frames[key] = cv2.createBackgroundSubtractorMOG2(
                history=200,  # Reduced history for GPU
                varThreshold=16,
                detectShadows=False  # Disable shadow detection for performance
            )
        
        subtractor = self.background_frames[key]
        
        # Upload frame to GPU and apply subtractor
        gpu_frame = cv2.cuda_GpuMat()
        gpu_frame.upload(gray)
        
        fg_mask_gpu = cv2.cuda_GpuMat()
        subtractor.apply(gpu_frame, fg_mask_gpu, 0.01)  # Learning rate
        
        # Apply threshold on GPU
        thresh_gpu = cv2.cuda_GpuMat()
        cv2.cuda.threshold(fg_mask_gpu, 200, 255, cv2.THRESH_BINARY, thresh_gpu)
        
        # Morphological operations on GPU
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        cv2.cuda.morphologyEx(thresh_gpu, cv2.MORPH_CLOSE, kernel, thresh_gpu)
        cv2.cuda.morphologyEx(thresh_gpu, cv2.MORPH_OPEN, kernel, thresh_gpu)
        
        # Download result
        thresh = thresh_gpu.download()
        return thresh
        
    def _calculate_frame_difference(self, frame: np.ndarray, camera_id: str) -> np.ndarray:
        """Calculate frame difference between current and background"""
        # Convert to grayscale if not already
        if len(frame.shape) == 3:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        else:
            gray = frame
            
        # Get background frame
        bg_frame = self.background_frames.get(camera_id)
        
        # If background frame doesn't exist, create one from current frame
        if bg_frame is None:
            bg_frame = gray.copy()
        # If background frame is color (3D), convert to grayscale
        elif len(bg_frame.shape) == 3:
            bg_frame = cv2.cvtColor(bg_frame, cv2.COLOR_BGR2GRAY)
        
        # Calculate absolute difference
        diff = cv2.absdiff(gray, bg_frame)
        
        # Apply threshold
        _, thresh = cv2.threshold(
            diff,
            25,  # Threshold for frame difference
            255, cv2.THRESH_BINARY
        )
        
        # Apply morphological operations to clean up noise
        kernel = np.ones((5, 5), np.uint8)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
        
        # Dilate to expand detected motion
        thresh = cv2.dilate(thresh, kernel, iterations=2)
        
        # Return motion mask
        return thresh
        
    def _gaussian_background_sub(self, frame: np.ndarray, camera_id: str) -> np.ndarray:
        """Use Gaussian Mixture-based background subtraction"""
        # Convert to grayscale if not already
        if len(frame.shape) == 3:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        else:
            gray = frame
            
        # Create or get background subtractor
        key = self._get_camera_key(camera_id)
        if key not in self.background_frames:
            self.background_frames[key] = cv2.createBackgroundSubtractorMOG2(
                history=500,
                varThreshold=16,
                detectShadows=True
            )
            
        subtractor = self.background_frames[key]
        mask = subtractor.apply(gray)
        
        # Apply threshold
        _, thresh = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
        
        # Morphological operations
        kernel = np.ones((5, 5), np.uint8)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
        
        return thresh
        
    def _check_zone_motion(
        self,
        motion_mask: np.ndarray,
        zone: MotionZone,
        camera_id: str,
        event_counter: Dict[str, int],
        current_frame: np.ndarray = None,
        background_frame: np.ndarray = None
    ) -> Optional[MotionEvent]:
        """Check if motion is detected within zone boundaries"""
        frame_height, frame_width = motion_mask.shape[:2]
        
        x, y, w, h = self._convert_zone_to_pixels(zone, frame_width, frame_height)
        
        motion_in_zone = motion_mask[y:y+h, x:x+w]
        
        total_pixels = motion_in_zone.size
        motion_pixels = cv2.countNonZero(motion_in_zone)
        
        motion_percentage = motion_pixels / total_pixels if total_pixels > 0 else 0
        confidence = motion_percentage * zone.sensitivity
        
        logger.debug(f"Motion check - Zone: {zone.name}, Camera: {camera_id}, "
                    f"Motion pixels: {motion_pixels}/{total_pixels} ({100*motion_percentage:.2f}%), "
                    f"Confidence: {confidence:.4f}, Threshold: 0.05")
        
        if motion_pixels > zone.min_area and confidence > 0.05:
            # Light-change filter: reject uniform brightness shifts that lack edge structure
            if zone.filter_light_changes and current_frame is not None and background_frame is not None:
                if self._is_light_change(current_frame, background_frame, zone, frame_width, frame_height):
                    logger.debug(f"Light change filtered out in zone '{zone.name}' for {camera_id}")
                    return None

            key = f"{camera_id}_{zone.name}"
            event_counter[key] = event_counter.get(key, 0) + 1
            
            return MotionEvent(
                camera_id=camera_id,
                zone_name=zone.name,
                timestamp=time.time(),
                motion_mask=motion_in_zone,
                event_id=event_counter[key],
                confidence=confidence
            )
            
        return None
    
    def _is_light_change(
        self,
        current_frame: np.ndarray,
        background_frame: np.ndarray,
        zone: MotionZone,
        frame_width: int,
        frame_height: int
    ) -> bool:
        """
        Detect whether a motion event is caused by a lighting change rather than real motion.
        Uses edge contour analysis: real objects have structured edges with high perimeter-to-area
        ratios, while lighting shifts produce diffuse regions with low perimeter-to-area ratios.
        """
        try:
            x, y, w, h = self._convert_zone_to_pixels(zone, frame_width, frame_height)
            
            cur_gray = cv2.cvtColor(current_frame, cv2.COLOR_BGR2GRAY) if len(current_frame.shape) == 3 else current_frame
            bg_gray = cv2.cvtColor(background_frame, cv2.COLOR_BGR2GRAY) if len(background_frame.shape) == 3 else background_frame
            
            diff = cv2.absdiff(cur_gray, bg_gray)
            _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
            
            zone_thresh = thresh[y:y+h, x:x+w]
            
            contours, _ = cv2.findContours(zone_thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not contours:
                return False
            
            total_area = 0
            total_perimeter = 0
            large_contours = 0
            for c in contours:
                area = cv2.contourArea(c)
                if area < 100:
                    continue
                total_area += area
                total_perimeter += cv2.arcLength(c, True)
                large_contours += 1
            
            if large_contours == 0 or total_area == 0:
                return False
            
            circularity = (4 * 3.14159 * total_area) / (total_perimeter * total_perimeter) if total_perimeter > 0 else 0
            compactness = total_perimeter / total_area if total_area > 0 else 0
            
            threshold = zone.light_change_threshold
            
            if circularity > 0.7 and compactness < (1.0 - threshold) * 0.05:
                return True
            
            if compactness < 0.01 and circularity > 0.5:
                return True
            
            return False
        except Exception as e:
            logger.debug(f"Light change detection error: {e}")
            return False
        
    def _get_camera_key(self, camera_id: str = "default_camera") -> str:
        """Get the current camera key for background frame storage"""
        return camera_id
        
    def _convert_zone_to_pixels(self, zone: MotionZone, frame_width: int, frame_height: int) -> Tuple[int, int, int, int]:
        """Convert zone coordinates from percentages to pixel coordinates if needed"""
        x, y, w, h = zone.x, zone.y, zone.width, zone.height
        
        # Check if coordinates are percentages (values between 0 and 1)
        # We use a threshold of 0.99 to detect percentages since pixel coords are typically > 1
        if 0 <= x <= 1 and 0 <= y <= 1 and 0 <= w <= 1 and 0 <= h <= 1:
            # Convert percentages to pixel coordinates
            x = int(x * frame_width)
            y = int(y * frame_height)
            w = int(w * frame_width)
            h = int(h * frame_height)
        
        return x, y, w, h
    
    def draw_zones(self, frame: np.ndarray, alpha: float = 0.15) -> np.ndarray:
        """Draw motion detection zones on frame with semi-transparent overlay"""
        result = frame.copy()
        frame_height, frame_width = frame.shape[:2]
        
        for zone in self.zones:
            # Convert zone coordinates from percentages to pixels if needed
            x, y, w, h = self._convert_zone_to_pixels(zone, frame_width, frame_height)
            
            # Create semi-transparent filled rectangle (green with low opacity)
            overlay = result.copy()
            cv2.rectangle(overlay, (x, y), (x + w, y + h), (0, 255, 0), -1)
            cv2.addWeighted(overlay, alpha, result, 1 - alpha, 0, result)
            # Draw border with slightly higher opacity for visibility
            cv2.rectangle(result, (x, y), (x + w, y + h), (0, 255, 0), 1)
            # Draw zone name with semi-transparent background
            (text_width, text_height), baseline = cv2.getTextSize(
                zone.name, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            # Background for text
            cv2.rectangle(result, (x, y - text_height - baseline - 2),
                         (x + text_width, y - 2), (0, 0, 0), -1)
            cv2.addWeighted(result, 0.7, frame, 0.3, 0, result)
            # Text
            cv2.putText(result, zone.name, (x, y - 5),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        return result
