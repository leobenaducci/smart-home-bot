"""The gate between motion and the disk.

Run: python3 web_server/test_recording_gate.py  (needs cv2/numpy: in the container)

`test_object_tracker.py` covers the counting rule on its own. What is pinned
here is the wiring around it, and above all the direction it fails in.

Over-recording is what this gate was built to stop -- 54% of six days of clips
had no object in them. But recording *nothing* is a worse failure and a silent
one: a camera that has quietly stopped writing looks exactly like a quiet
garden. So when YOLO is not loaded at all, the gate must stand aside rather
than reject everything it was never able to ask about.
"""

import unittest

import numpy as np

from enhanced_motion_detector import EnhancedMotionDetector, EnhancedMotionEvent
from motion_detector import full_frame_zone


def frames():
    """A dark scene, then the same scene with something bright in the middle.

    The block has to be big: a zone's confidence is the fraction of it that
    changed times its sensitivity (0.5), against a 0.05 threshold, so anything
    under about a tenth of the zone does not register as motion at all. 240x240
    of a 576x432 zone is ~23%.
    """
    before = np.zeros((480, 640, 3), dtype=np.uint8)
    after = before.copy()
    after[120:360, 200:440] = 255
    return before, after


def gate_would_record(event) -> bool:
    """The exact condition web_server.process_frame applies."""
    confirmed = getattr(event, 'confirmed_objects', None) or []
    return not (getattr(event, 'gate_applied', True) and not confirmed)


class TestEventContract(unittest.TestCase):
    def test_an_event_is_gated_by_default(self):
        """A bare event must not be treated as pre-approved."""
        self.assertTrue(EnhancedMotionEvent(
            camera_id='c', zone_name='z', timestamp=0.0,
            motion_mask=None, event_id=1, confidence=1.0).gate_applied)

    def test_a_gated_event_with_nothing_confirmed_is_not_recorded(self):
        self.assertFalse(gate_would_record(EnhancedMotionEvent(
            camera_id='c', zone_name='z', timestamp=0.0, motion_mask=None,
            event_id=1, confidence=1.0, detected_objects=['a per-frame guess'])))

    def test_a_gated_event_with_something_confirmed_is_recorded(self):
        self.assertTrue(gate_would_record(EnhancedMotionEvent(
            camera_id='c', zone_name='z', timestamp=0.0, motion_mask=None,
            event_id=1, confidence=1.0, confirmed_objects=['a tracked person'])))

    def test_an_ungated_event_is_recorded_even_with_nothing_confirmed(self):
        """YOLO never ran. Empty means 'not asked', not 'nothing there'."""
        self.assertTrue(gate_would_record(EnhancedMotionEvent(
            camera_id='c', zone_name='z', timestamp=0.0, motion_mask=None,
            event_id=1, confidence=1.0, gate_applied=False)))


class TestDegradesOpen(unittest.TestCase):
    """With no object detector, the house must keep recording on motion."""

    def test_motion_without_a_detector_still_reaches_the_disk(self):
        d = EnhancedMotionDetector(enable_object_detection=False)
        d.add_zone(full_frame_zone())
        before, after = frames()
        counter = {}
        d.detect_motion(before, 'cam', counter)
        events, _ = d.detect_motion(after, 'cam', counter)

        self.assertTrue(events, "no motion detected; the fixture is wrong")
        for event in events:
            self.assertFalse(event.gate_applied)
            self.assertEqual(event.confirmed_objects, [])
            self.assertTrue(gate_would_record(event),
                            "a camera with no YOLO would record nothing at all")


if __name__ == '__main__':
    unittest.main(verbosity=2)
