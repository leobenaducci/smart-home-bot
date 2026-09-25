#!/usr/bin/env python3
"""
Motion Detection Debug Script
Tests motion detection with various scenarios to identify the root cause
"""

import sys
import os
import cv2
import numpy as np
import logging

# Add project root src directory to Python path
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), '../web_server'))

from ..web_server.motion_detector import MotionDetector, MotionZone

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def test_basic_motion_detection():
    """Test basic motion detection with synthetic frames"""
    logger.info("=" * 60)
    logger.info("TEST 1: Basic Motion Detection with Synthetic Frames")
    logger.info("=" * 60)
    
    # Create motion detector with default settings
    detector = MotionDetector(detector_type="frame_diff", background_update_rate=5)
    
    # Add a motion zone (smaller to test)
    zone = MotionZone(
        name="TestZone",
        x=100,
        y=100,
        width=200,
        height=200,
        sensitivity=0.5,
        min_area=500
    )
    detector.add_zone(zone)
    
    # Create two frames - one static, one with motion
    frame1 = np.zeros((480, 640, 3), dtype=np.uint8)
    frame2 = np.zeros((480, 640, 3), dtype=np.uint8)
    
    # Add a white square in frame 2 to simulate motion
    cv2.rectangle(frame2, (120, 120), (280, 280), (255, 255, 255), -1)
    
    event_counter = {}
    
    # Test frame 1 (should have no motion)
    logger.info("Testing frame 1 (static)...")
    events1 = detector.detect_motion(frame1, "test_cam", event_counter)
    logger.info(f"Frame 1 events: {len(events1)}")
    
    # Test frame 2 (should have motion)
    logger.info("Testing frame 2 (with motion)...")
    events2 = detector.detect_motion(frame2, "test_cam", event_counter)
    logger.info(f"Frame 2 events: {len(events2)}")
    
    if events2:
        for event in events2:
            logger.info(f"  - Zone: {event.zone_name}, Confidence: {event.confidence:.4f}, Motion pixels: {cv2.countNonZero(event.motion_mask)}")
    
    return len(events2) > 0


def test_zone_outside_frame():
    """Test motion detection when zone is outside frame bounds"""
    logger.info("=" * 60)
    logger.info("TEST 2: Zone Outside Frame Bounds")
    logger.info("=" * 60)
    
    detector = MotionDetector(detector_type="frame_diff", background_update_rate=5)
    
    # Add a zone that's larger than the frame
    zone = MotionZone(
        name="LargeZone",
        x=0,
        y=0,
        width=1920,  # Way larger than frame
        height=1080,
        sensitivity=0.5,
        min_area=500
    )
    detector.add_zone(zone)
    
    # Create a small frame
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    event_counter = {}
    
    logger.info("Testing with zone larger than frame...")
    events = detector.detect_motion(frame, "test_cam", event_counter)
    logger.info(f"Events detected: {len(events)}")
    
    return len(events) > 0


def test_low_confidence_threshold():
    """Test with lower confidence threshold"""
    logger.info("=" * 60)
    logger.info("TEST 3: Lower Confidence Threshold")
    logger.info("=" * 60)
    
    detector = MotionDetector(detector_type="frame_diff", background_update_rate=5)
    
    zone = MotionZone(
        name="TestZone",
        x=100,
        y=100,
        width=200,
        height=200,
        sensitivity=0.5,
        min_area=100  # Lower min_area
    )
    detector.add_zone(zone)
    
    frame1 = np.zeros((480, 640, 3), dtype=np.uint8)
    frame2 = np.zeros((480, 640, 3), dtype=np.uint8)
    
    # Small motion
    cv2.rectangle(frame2, (120, 120), (150, 150), (255, 255, 255), -1)
    
    event_counter = {}
    
    logger.info("Testing with small motion...")
    events = detector.detect_motion(frame2, "test_cam", event_counter)
    logger.info(f"Events detected: {len(events)}")
    
    if events:
        for event in events:
            motion_pixels = cv2.countNonZero(event.motion_mask)
            total_pixels = event.motion_mask.size
            raw_confidence = motion_pixels / total_pixels
            logger.info(f"  - Zone: {event.zone_name}")
            logger.info(f"  - Motion pixels: {motion_pixels}")
            logger.info(f"  - Total pixels in zone: {total_pixels}")
            logger.info(f"  - Raw confidence: {raw_confidence:.6f}")
            logger.info(f"  - Adjusted confidence: {event.confidence:.6f}")
    
    return len(events) > 0


def test_frame_difference_calculation():
    """Test frame difference calculation directly"""
    logger.info("=" * 60)
    logger.info("TEST 4: Frame Difference Calculation")
    logger.info("=" * 60)
    
    detector = MotionDetector(detector_type="frame_diff", background_update_rate=5)
    
    frame1 = np.zeros((480, 640, 3), dtype=np.uint8)
    frame2 = np.zeros((480, 640, 3), dtype=np.uint8)
    
    # Add motion
    cv2.rectangle(frame2, (100, 100), (300, 300), (255, 255, 255), -1)
    
    # Initialize background with frame1
    detector.background_frames["test_cam"] = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY)
    detector._frame_counts["test_cam"] = 0
    detector._event_counters["test_cam"] = 0
    
    logger.info("Calculating frame difference...")
    motion_mask = detector._calculate_frame_difference(frame2, "test_cam")
    
    motion_pixels = cv2.countNonZero(motion_mask)
    total_pixels = motion_mask.size
    logger.info(f"Motion pixels: {motion_pixels}")
    logger.info(f"Total pixels: {total_pixels}")
    logger.info(f"Motion percentage: {100 * motion_pixels / total_pixels:.2f}%")
    
    # Check threshold
    _, thresh = cv2.threshold(motion_mask, 25, 255, cv2.THRESH_BINARY)
    thresh_pixels = cv2.countNonZero(thresh)
    logger.info(f"Threshold pixels (threshold=25): {thresh_pixels}")
    
    return thresh_pixels > 0


def main():
    """Run all tests"""
    logger.info("Starting Motion Detection Debug Tests")
    
    results = []
    
    # Run tests
    results.append(("Basic Motion Detection", test_basic_motion_detection()))
    results.append(("Zone Outside Frame", test_zone_outside_frame()))
    results.append(("Low Confidence Threshold", test_low_confidence_threshold()))
    results.append(("Frame Difference Calculation", test_frame_difference_calculation()))
    
    # Print summary
    logger.info("=" * 60)
    logger.info("TEST SUMMARY")
    logger.info("=" * 60)
    
    for test_name, passed in results:
        status = "PASSED" if passed else "FAILED"
        logger.info(f"  {test_name}: {status}")
    
    # Identify most likely root cause
    logger.info("=" * 60)
    logger.info("ROOT CAUSE ANALYSIS")
    logger.info("=" * 60)
    
    if not results[0][1]:  # Basic test failed
        logger.info("Most likely: Motion detection algorithm not working correctly")
        logger.info("  - Check frame conversion (RGB/BGR)")
        logger.info("  - Check background frame initialization")
    elif not results[2][1]:  # Low confidence test failed
        logger.info("Most likely: Confidence threshold too high")
        logger.info("  - Current threshold: 0.3")
        logger.info("  - Consider lowering to 0.1 or adjusting sensitivity")
    else:
        logger.info("Motion detection appears to work in isolation")
        logger.info("Issue likely in integration:")
        logger.info("  - Frame resolution mismatch")
        logger.info("  - Zone configuration mismatch")
        logger.info("  - Frame queue empty (no frames being processed)")


if __name__ == '__main__':
    main()