"""
Object Detection Module
Implements YOLO-based object detection with GPU acceleration for identifying humans, cats, animals, cars, and other objects
"""

import cv2
import numpy as np
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from ultralytics import YOLO
import logging
import time
import torch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class DetectedObject:
    """Detected object data structure"""
    class_name: str
    confidence: float
    bounding_box: Tuple[int, int, int, int]  # x, y, width, height
    center_x: int
    center_y: int
    # Set by ObjectTracker once this detection has been matched across frames.
    # None means nothing has followed it yet, not that it is a new object.
    track_id: Optional[int] = None

class ObjectDetector:
    """
    YOLO-based object detection for identifying specific object types
    """
    
    def __init__(self, model_name: str = None, confidence_threshold: float = 0.35, use_gpu: bool = True,
                 detect_all_classes: bool = False):
        """
        Initialize the object detector
        
        Args:
            model_name: YOLO model to use (e.g., 'yolov8n.pt', 'yolov8s.pt'). 
                       If None, automatically selects based on GPU availability:
                       - GPU: 'yolov8x.pt' (extra-large) for best accuracy
                       - CPU: 'yolov8s.pt' (small) for faster processing
            confidence_threshold: Minimum confidence threshold for detections
            use_gpu: Whether to use GPU acceleration if available
            detect_all_classes: Report every COCO class instead of only
                       target_classes. The target set exists to keep motion
                       alerts to things worth waking someone for; a question
                       like "what is in the patio?" wants the chairs, the
                       potted plant and the umbrella too.
        """
        # Auto-select model based on GPU availability if not specified
        gpu_available = use_gpu and torch.cuda.is_available()
        if model_name is None:
            if gpu_available:
                model_name = 'models/yolo26m.pt'
            else:
                model_name = 'models/yolo26s.pt'
        
        self.model_name = model_name
        self.confidence_threshold = confidence_threshold
        self.use_gpu = use_gpu
        self.detect_all_classes = detect_all_classes
        self.model = None
        self.class_names = []
        self.device = None
        self.batch_size = 4  # Process multiple frames in batch for efficiency
        # None = detect all target_classes; a set of class names restricts detection to those
        self.enabled_classes: Optional[set] = None

        # Object classes we're interested in
        self.target_classes = {
            'person': 'human',
            'cat': 'cat',
            'dog': 'dog',
            'horse': 'animal',
            'sheep': 'animal',
            'cow': 'animal',
            'elephant': 'animal',
            'bear': 'animal',
            'zebra': 'animal',
            'giraffe': 'animal',
            'car': 'car',
            'truck': 'car',
            'bus': 'car',
            'motorcycle': 'car',
            'bicycle': 'car'
        }
        
        # Class-specific confidence thresholds (lower = more sensitive)
        # Priority classes (humans, cats, dogs) get lower thresholds for better detection
        self.class_confidence_thresholds = {
            'person': 0.20,   # Humans - very low threshold for maximum sensitivity
            'cat': 0.20,      # Cats - very low threshold for maximum sensitivity
            'dog': 0.20,      # Dogs - very low threshold for maximum sensitivity
        }
        self.priority_class_threshold = 0.20  # Default for priority classes not explicitly listed
        self.default_class_threshold = confidence_threshold  # Default for other classes
        
        self._initialize_model()
        
    def _initialize_model(self):
        """Initialize the YOLO model with GPU acceleration if available"""
        try:
            # Determine device for GPU acceleration
            if self.use_gpu and torch.cuda.is_available():
                self.device = 'cuda'
                # Enable TF32 for faster computation on Ampere+ GPUs
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
                logger.info(f"Using GPU: {torch.cuda.get_device_name(0)}")
            else:
                self.device = 'cpu'
                if self.use_gpu and torch.cuda.is_available():
                    logger.info(f"Using GPU: {torch.cuda.get_device_name(0)}")
                elif self.use_gpu:
                    logger.warning("GPU requested but not available, falling back to CPU")
                else:
                    logger.info("Using CPU for object detection")
            
            logger.info(f"Loading YOLO model: {self.model_name} on {self.device}")
            self.model = YOLO(self.model_name)
            self.class_names = self.model.names
            
            # Configure YOLO for optimal performance
            # Warm up the model with a dummy inference
            if self.device == 'cuda':
                dummy_input = np.zeros((320, 320, 3), dtype=np.uint8)
                self.model(dummy_input, verbose=False, device=self.device)
                logger.info("YOLO model warmed up on GPU")
            
            logger.info(f"YOLO model loaded successfully with {len(self.class_names)} classes")
        except Exception as e:
            logger.error(f"Failed to load YOLO model: {e}")
            raise
    
    def detect_objects(self, frame: np.ndarray) -> List[DetectedObject]:
        """
        Detect objects in a frame using YOLO with GPU acceleration
        
        Args:
            frame: Input frame (BGR format)
            
        Returns:
            List of detected objects
        """
        if self.model is None:
            logger.warning("Model not initialized, returning empty detections")
            return []
        
        try:
            # Run object detection with GPU acceleration
            start_time = time.time()
            
            # Use GPU device if available
            results = self.model(frame, verbose=False, device=self.device, half=self.device != 'cpu')
            
            inference_time = time.time() - start_time
            logger.debug(f"YOLO inference time: {inference_time:.3f}s on {self.device}")
            
            detected_objects = []
            
            # Process detection results
            for result in results:
                boxes = result.boxes
                if boxes is not None:
                    # Move to CPU for post-processing if on GPU
                    if self.device != 'cpu':
                        xyxy = boxes.xyxy.cpu().numpy()
                        conf = boxes.conf.cpu().numpy()
                        cls = boxes.cls.cpu().numpy()
                    else:
                        xyxy = boxes.xyxy.numpy()
                        conf = boxes.conf.numpy()
                        cls = boxes.cls.numpy()
                    
                    for i, box_xyxy in enumerate(xyxy):
                        x1, y1, x2, y2 = map(int, box_xyxy)
                        width = x2 - x1
                        height = y2 - y1
                        center_x = (x1 + x2) // 2
                        center_y = (y1 + y2) // 2
                        
                        # Get confidence and class
                        confidence = float(conf[i])
                        class_id = int(cls[i])
                        class_name = self.class_names[class_id]

                        # Skip classes not in our target set. Bypassed when the
                        # caller asked to describe a scene rather than decide
                        # whether to raise a motion alert.
                        if not self.detect_all_classes and class_name not in self.target_classes:
                            continue

                        # Skip classes disabled for this camera
                        if self.enabled_classes is not None and class_name not in self.enabled_classes:
                            continue

                        # Class-specific confidence threshold
                        class_threshold = self.class_confidence_thresholds.get(
                            class_name, self.default_class_threshold
                        )

                        # Filter by confidence threshold
                        if confidence >= class_threshold:
                            detected_object = DetectedObject(
                                class_name=class_name,
                                confidence=confidence,
                                bounding_box=(x1, y1, width, height),
                                center_x=center_x,
                                center_y=center_y
                            )
                            detected_objects.append(detected_object)
            
            logger.debug(f"Detected {len(detected_objects)} objects")
            return detected_objects
            
        except Exception as e:
            logger.error(f"Error during object detection: {e}")
            return []
    
    def filter_objects_in_zone(self, objects: List[DetectedObject], zone_x: int, zone_y: int, zone_width: int, zone_height: int) -> List[DetectedObject]:
        """
        Filter objects that are within a specific zone
        
        Args:
            objects: List of detected objects
            zone_x: Zone x coordinate
            zone_y: Zone y coordinate
            zone_width: Zone width
            zone_height: Zone height
            
        Returns:
            List of objects within the zone
        """
        filtered_objects = []
        zone_right = zone_x + zone_width
        zone_bottom = zone_y + zone_height
        
        for obj in objects:
            x1, y1, width, height = obj.bounding_box
            obj_center_x = obj.center_x
            obj_center_y = obj.center_y
            
            # Check if object center is within zone
            if (zone_x <= obj_center_x <= zone_right and 
                zone_y <= obj_center_y <= zone_bottom):
                filtered_objects.append(obj)
                
        return filtered_objects
    
    def draw_detections(self, frame: np.ndarray, objects: List[DetectedObject]) -> np.ndarray:
        """
        Draw bounding boxes and labels for detected objects on frame
        
        Args:
            frame: Input frame
            objects: List of detected objects
            
        Returns:
            Frame with drawn detections
        """
        result_frame = frame.copy()
        
        # Color mapping for different categories
        color_map = {
            'human': (0, 255, 0),    # Green
            'cat': (255, 0, 0),      # Blue
            'dog': (0, 165, 255),    # Orange (distinct from cat)
            'animal': (0, 0, 255),   # Red
            'car': (255, 255, 0),    # Cyan
            'other': (128, 128, 128) # Gray
        }
        
        for obj in objects:
            x, y, w, h = obj.bounding_box
            category = self.target_classes.get(obj.class_name, 'other')
            color = color_map.get(category, (128, 128, 128))
            
            # Draw bounding box
            cv2.rectangle(result_frame, (x, y), (x + w, y + h), color, 2)
            
            # Draw label
            label = f"{obj.class_name}: {obj.confidence:.2f}"
            cv2.putText(result_frame, label, (x, y - 10),
                       cv2.FONT_HERSHEY_SIMPLEX, 2, color, 4)
        
        return result_frame