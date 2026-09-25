"""
Enhanced Motion Detection Engine Module - GPU Accelerated
Extends the base motion detection with YOLO object detection capabilities using GPU acceleration
"""

import cv2
import numpy as np
import time
import asyncio
from typing import List, Optional, Tuple, Dict
from dataclasses import dataclass, field
from enum import Enum
import logging

# Import base motion detector
from motion_detector import MotionDetector, MotionEvent, MotionZone

# Import object detector
try:
    from object_detector import ObjectDetector, DetectedObject
    from object_tracker import ObjectTracker
    OBJECT_DETECTION_AVAILABLE = True
except ImportError:
    logging.warning("Object detection not available. Install ultralytics package for YOLO support.")
    OBJECT_DETECTION_AVAILABLE = False
    ObjectDetector = None
    DetectedObject = None
    ObjectTracker = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Seconds to keep running object detection after the last motion event
MOTION_COOLDOWN_SECONDS = 10.0

# Night. An infrared frame is grey -- the three channels agree almost
# everywhere -- and it is the frame YOLO invents on: measured on an empty
# patio, `person` at 0.31 and a pergola as two `bed`, all under the daytime
# floors. So on a frame that reads as IR the priority classes need this much
# confidence per frame before the tracker even sees them. Daytime keeps the
# 0.20 floors, chosen for recall at the end of a lit garden.
import os
NIGHT_MIN_CONFIDENCE = float(os.environ.get('NIGHT_MIN_CONFIDENCE', '0.40'))
IR_CHANNEL_SPREAD = float(os.environ.get('IR_CHANNEL_SPREAD', '4.0'))


def is_ir_frame(frame: np.ndarray) -> bool:
    """True when the frame is (near-)monochrome, which is what IR night mode looks like."""
    if frame is None or frame.ndim != 3 or frame.shape[2] < 3:
        return False
    sample = frame[::16, ::16].astype(np.int16)
    spread = np.abs(sample[..., 0] - sample[..., 1]).mean() + np.abs(sample[..., 1] - sample[..., 2]).mean()
    return float(spread) < IR_CHANNEL_SPREAD


@dataclass
class EnhancedMotionEvent(MotionEvent):
    """Enhanced motion event with object detection data"""
    detected_objects: List[DetectedObject] = field(default_factory=list)
    object_counts: Dict[str, int] = field(default_factory=dict)
    # The subset of detected_objects the tracker has seen across enough frames
    # to call real. This is what a recording is allowed to act on;
    # detected_objects stays the full per-frame list so overlays and logs still
    # show what YOLO saw while it was making up its mind.
    confirmed_objects: List[DetectedObject] = field(default_factory=list)
    # Whether the confirmation gate was actually able to run. False means YOLO
    # is not loaded at all -- no ultralytics, or the model failed to load -- and
    # an empty confirmed_objects therefore means "nothing was asked", not
    # "nothing is there". A caller must record on motion alone in that case:
    # over-recording is the failure this change is fixing, but recording
    # *nothing* is the one it must never cause, and a camera that has silently
    # stopped writing looks exactly like a quiet garden.
    gate_applied: bool = True


@dataclass
class PeriodicDetectionResult:
    """Result from zone object detection during motion cooldown window"""
    camera_id: str
    timestamp: float
    detected_objects: List[DetectedObject] = field(default_factory=list)
    object_counts: Dict[str, int] = field(default_factory=dict)
    zone_name: str = ""
    confirmed_objects: List[DetectedObject] = field(default_factory=list)


class EnhancedMotionDetector(MotionDetector):
    """
    Enhanced motion detection with GPU-accelerated YOLO object detection capabilities.
    Object detection runs only when motion is active or within MOTION_COOLDOWN_SECONDS after it.
    """

    def __init__(
        self,
        detector_type: str = "frame_diff",
        background_update_rate: int = 5,
        enable_object_detection: bool = True,
        object_detection_model: str = None,  # Auto-selects based on GPU: yolov8x.pt (GPU) or yolov8s.pt (CPU)
        object_confidence_threshold: float = 0.35,
        use_gpu: bool = True,
    ):
        super().__init__(detector_type, background_update_rate, use_gpu)

        # Keep the parent's computed value which correctly accounts for CUDA availability
        self.enable_object_detection = enable_object_detection and OBJECT_DETECTION_AVAILABLE
        self.object_detector = None

        # Track last motion time per camera for the 10s cooldown window
        self._last_motion_time: Dict[str, float] = {}
        self.last_frame_ir = False

        # One tracker per detector, and there is one detector per camera, which
        # is what keeps a car on the street from being matched against a cat on
        # the patio.
        self.tracker = ObjectTracker() if ObjectTracker is not None else None

        if self.enable_object_detection and OBJECT_DETECTION_AVAILABLE:
            try:
                self.object_detector = ObjectDetector(
                    model_name=object_detection_model,
                    confidence_threshold=object_confidence_threshold,
                    use_gpu=use_gpu
                )
                logger.info(f"Object detection enabled with YOLO (GPU: {use_gpu})")
            except Exception as e:
                logger.error(f"Failed to initialize object detector: {e}")
                self.enable_object_detection = False
                self.object_detector = None
        elif self.enable_object_detection:
            logger.warning("Object detection requested but not available")
            self.enable_object_detection = False

    def is_in_detection_window(self, camera_id: str) -> bool:
        """Return True if within MOTION_COOLDOWN_SECONDS of the last motion event."""
        last_motion = self._last_motion_time.get(camera_id, 0)
        return (time.time() - last_motion) < MOTION_COOLDOWN_SECONDS

    def detect_motion(
        self,
        frame: np.ndarray,
        camera_id: str,
        event_counter: Dict[str, int]
    ) -> Tuple[List[EnhancedMotionEvent], Optional[PeriodicDetectionResult]]:
        """
        Detect motion in frame using configured zones and classify objects.
        Object detection only runs when motion is detected or within MOTION_COOLDOWN_SECONDS
        after the last motion event. Objects are always filtered to drawn zones.

        Returns:
            Tuple of (list of enhanced motion events, optional cooldown detection result)
        """
        base_events = super().detect_motion(frame, camera_id, event_counter)
        current_time = time.time()

        # Update last motion time whenever motion is detected in any zone
        if base_events:
            self._last_motion_time[camera_id] = current_time

        # Determine whether we are inside the detection window
        last_motion = self._last_motion_time.get(camera_id, 0)
        in_detection_window = bool(base_events) or (current_time - last_motion) < MOTION_COOLDOWN_SECONDS

        # Outside detection window — skip YOLO entirely
        if not in_detection_window or not self.enable_object_detection or self.object_detector is None:
            # base_events implies in_detection_window, so reaching here with
            # events at all means object detection is unavailable rather than
            # merely idle. Say so, and let the caller fall back to motion.
            gate_applied = bool(self.enable_object_detection and self.object_detector is not None)
            enhanced_events = []
            for event in base_events:
                enhanced_event = EnhancedMotionEvent(
                    camera_id=event.camera_id,
                    zone_name=event.zone_name,
                    timestamp=event.timestamp,
                    motion_mask=event.motion_mask,
                    event_id=event.event_id,
                    confidence=event.confidence,
                    detected_objects=[],
                    object_counts={},
                    gate_applied=gate_applied
                )
                enhanced_events.append(enhanced_event)
            return enhanced_events, None

        # Run YOLO once for the frame — results are filtered per zone below
        detected_objects = self.object_detector.detect_objects(frame)
        frame_height, frame_width = frame.shape[:2]

        # At night the per-frame floor rises; see NIGHT_MIN_CONFIDENCE.
        self.last_frame_ir = is_ir_frame(frame)
        if self.last_frame_ir and detected_objects:
            before = len(detected_objects)
            detected_objects = [o for o in detected_objects if o.confidence >= NIGHT_MIN_CONFIDENCE]
            if len(detected_objects) != before:
                logger.debug(f"{camera_id}: IR frame, dropped {before - len(detected_objects)} detection(s) under {NIGHT_MIN_CONFIDENCE}")

        # Follow detections across frames. This runs on the whole frame rather
        # than per zone: an object that steps over a zone edge is still the same
        # object, and restarting its count at the boundary would mean anything
        # entering a zone had to stand inside it for three frames before it
        # counted. Zone filtering is applied to the confirmed set below, so an
        # object outside every zone still cannot trigger anything.
        if self.tracker is not None:
            confirmed_objects = self.tracker.update(detected_objects, current_time)
        else:
            confirmed_objects = detected_objects

        if base_events:
            # Active motion — attach zone-filtered objects to each motion event
            enhanced_events = []
            for event in base_events:
                zone_objects = []
                zone_confirmed = []
                for zone in self.zones:
                    if zone.name == event.zone_name:
                        zone_x, zone_y, zone_w, zone_h = self._convert_zone_to_pixels(
                            zone, frame_width, frame_height
                        )
                        zone_objects = self.object_detector.filter_objects_in_zone(
                            detected_objects, zone_x, zone_y, zone_w, zone_h
                        )
                        zone_confirmed = self.object_detector.filter_objects_in_zone(
                            confirmed_objects, zone_x, zone_y, zone_w, zone_h
                        )
                        break

                object_counts = {}
                for obj in zone_objects:
                    object_counts[obj.class_name] = object_counts.get(obj.class_name, 0) + 1

                enhanced_event = EnhancedMotionEvent(
                    camera_id=event.camera_id,
                    zone_name=event.zone_name,
                    timestamp=event.timestamp,
                    motion_mask=event.motion_mask,
                    event_id=event.event_id,
                    confidence=event.confidence,
                    detected_objects=zone_objects,
                    object_counts=object_counts,
                    confirmed_objects=zone_confirmed
                )
                enhanced_events.append(enhanced_event)
            return enhanced_events, None

        # Cooldown window (no active motion) — collect zone-filtered objects across all zones
        cooldown_result = self._run_zone_detection(
            camera_id, current_time, detected_objects, frame_width, frame_height,
            confirmed_objects
        )
        return [], cooldown_result

    def _run_zone_detection(
        self,
        camera_id: str,
        current_time: float,
        detected_objects: list,
        frame_width: int,
        frame_height: int,
        confirmed_objects: list = None
    ) -> Optional[PeriodicDetectionResult]:
        """
        Filter pre-detected objects to all configured zones during the cooldown window.
        """
        try:
            zone_objects = []
            zone_confirmed = []
            zone_name = ""
            for zone in self.zones:
                zone_x, zone_y, zone_w, zone_h = self._convert_zone_to_pixels(
                    zone, frame_width, frame_height
                )
                objects_in_zone = self.object_detector.filter_objects_in_zone(
                    detected_objects, zone_x, zone_y, zone_w, zone_h
                )
                if objects_in_zone:
                    zone_objects.extend(objects_in_zone)
                    if not zone_name:
                        zone_name = zone.name
                if confirmed_objects:
                    zone_confirmed.extend(self.object_detector.filter_objects_in_zone(
                        confirmed_objects, zone_x, zone_y, zone_w, zone_h
                    ))

            object_counts = {}
            for obj in zone_objects:
                object_counts[obj.class_name] = object_counts.get(obj.class_name, 0) + 1

            if zone_objects:
                logger.info(f"Cooldown detection for {camera_id}: {len(zone_objects)} objects in zone '{zone_name}'")

            return PeriodicDetectionResult(
                camera_id=camera_id,
                timestamp=current_time,
                detected_objects=zone_objects,
                object_counts=object_counts,
                zone_name=zone_name,
                confirmed_objects=zone_confirmed
            )

        except Exception as e:
            logger.error(f"Error during cooldown detection for {camera_id}: {e}")
            return None

    def draw_detections(self, frame: np.ndarray, objects: List[DetectedObject]) -> np.ndarray:
        """
        Draw object detections on frame (delegates to object detector)
        """
        if self.object_detector and self.enable_object_detection:
            return self.object_detector.draw_detections(frame, objects)
        return frame

    def draw_zones(self, frame: np.ndarray, events: List[EnhancedMotionEvent] = None, alpha: float = 0.15) -> np.ndarray:
        """
        Draw motion detection zones and object detections on frame
        """
        result = super().draw_zones(frame, alpha)

        # If we have enhanced events with object detections, draw them
        if events and self.enable_object_detection:
            for event in events:
                result = self.draw_detections(result, event.detected_objects)

        return result
