"""The recording gate's own tests.

These run without ultralytics, torch or a card: `object_tracker` is deliberately
plain Python so the rule that decides what gets written to disk can be tested on
any machine, including one with no GPU. The stub below stands in for
`DetectedObject` for the same reason -- importing the real one drags in cv2 and
torch and the test would only run where the service already runs.
"""

import sys
import unittest
from dataclasses import dataclass
from typing import Optional, Tuple

from object_tracker import ObjectTracker, iou


@dataclass
class Obj:
    """Same shape as object_detector.DetectedObject, without the imports."""
    class_name: str
    bounding_box: Tuple[int, int, int, int]
    confidence: float = 0.9
    track_id: Optional[int] = None

    @property
    def center_x(self) -> int:
        return self.bounding_box[0] + self.bounding_box[2] // 2

    @property
    def center_y(self) -> int:
        return self.bounding_box[1] + self.bounding_box[3] // 2


def person(x, y, w=40, h=80):
    return Obj('person', (x, y, w, h))


class TestIou(unittest.TestCase):
    def test_identical_boxes_overlap_fully(self):
        self.assertAlmostEqual(iou((0, 0, 10, 10), (0, 0, 10, 10)), 1.0)

    def test_disjoint_boxes_do_not_overlap(self):
        self.assertEqual(iou((0, 0, 10, 10), (50, 50, 10, 10)), 0.0)

    def test_touching_edges_do_not_overlap(self):
        self.assertEqual(iou((0, 0, 10, 10), (10, 0, 10, 10)), 0.0)

    def test_zero_area_box_is_not_a_match(self):
        self.assertEqual(iou((0, 0, 0, 0), (0, 0, 10, 10)), 0.0)


class TestConfirmation(unittest.TestCase):
    """The whole point: how many frames it takes before anything is written."""

    def test_one_frame_confirms_nothing(self):
        t = ObjectTracker(confirm_frames=3)
        self.assertEqual(t.update([person(100, 100)], 1.0), [])

    def test_two_frames_confirm_nothing(self):
        t = ObjectTracker(confirm_frames=3)
        t.update([person(100, 100)], 1.0)
        self.assertEqual(t.update([person(104, 100)], 1.2), [])

    def test_third_frame_confirms(self):
        t = ObjectTracker(confirm_frames=3)
        t.update([person(100, 100)], 1.0)
        t.update([person(104, 100)], 1.2)
        confirmed = t.update([person(108, 100)], 1.4)
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0].class_name, 'person')
        self.assertIsNotNone(confirmed[0].track_id)

    def test_it_keeps_confirming_while_the_object_stays(self):
        t = ObjectTracker(confirm_frames=3)
        for i in range(3):
            t.update([person(100 + 4 * i, 100)], 1.0 + 0.2 * i)
        for i in range(3, 8):
            self.assertEqual(len(t.update([person(100 + 4 * i, 100)], 1.0 + 0.2 * i)), 1)

    def test_confirmed_object_is_the_detection_that_was_passed_in(self):
        """Callers need YOLO's own box and confidence, not the track's copy."""
        t = ObjectTracker(confirm_frames=2)
        t.update([person(100, 100)], 1.0)
        original = person(104, 100)
        original.confidence = 0.42
        confirmed = t.update([original], 1.2)
        self.assertIs(confirmed[0], original)
        self.assertEqual(confirmed[0].confidence, 0.42)


class TestFalsePositives(unittest.TestCase):
    """What this gate exists to stop."""

    def test_a_detection_that_jumps_around_never_confirms(self):
        """Night grain reading as a person somewhere different every frame."""
        t = ObjectTracker(confirm_frames=3)
        spots = [(10, 10), (400, 300), (50, 600), (900, 90), (120, 450)]
        for i, (x, y) in enumerate(spots):
            self.assertEqual(t.update([person(x, y)], 1.0 + 0.2 * i), [])

    def test_a_single_flash_confirms_nothing_and_is_forgotten(self):
        t = ObjectTracker(confirm_frames=3, max_misses=2)
        self.assertEqual(t.update([person(100, 100)], 1.0), [])
        for i in range(1, 5):
            self.assertEqual(t.update([], 1.0 + 0.2 * i), [])
        self.assertEqual(t.tracks, {})

    def test_a_track_that_dies_restarts_its_count(self):
        """Two separate two-frame appearances are not one three-frame object."""
        t = ObjectTracker(confirm_frames=3, max_misses=2)
        t.update([person(100, 100)], 1.0)
        t.update([person(104, 100)], 1.2)
        for i in range(2, 8):                      # long enough to drop it
            t.update([], 1.0 + 0.2 * i)
        t.update([person(100, 100)], 2.4)
        self.assertEqual(t.update([person(104, 100)], 2.6), [])


class TestGaps(unittest.TestCase):
    """A real object that YOLO loses for a frame must still confirm."""

    def test_one_missed_frame_does_not_reset_the_count(self):
        t = ObjectTracker(confirm_frames=3, max_misses=2)
        t.update([person(100, 100)], 1.0)
        t.update([], 1.2)                          # behind a post
        confirmed = t.update([person(108, 100)], 1.4)
        self.assertEqual(confirmed, [])            # two hits, not three
        self.assertEqual(len(t.update([person(112, 100)], 1.6)), 1)

    def test_a_long_pause_starts_a_new_scene(self):
        """YOLO only runs during motion, so gaps are expected and untrustworthy."""
        t = ObjectTracker(confirm_frames=3, max_gap_seconds=3.0)
        t.update([person(100, 100)], 1.0)
        t.update([person(104, 100)], 1.2)
        self.assertEqual(t.update([person(108, 100)], 60.0), [])
        self.assertEqual(len(t.tracks), 1)


class TestAssociation(unittest.TestCase):
    def test_classes_do_not_match_each_other(self):
        t = ObjectTracker(confirm_frames=2)
        t.update([Obj('person', (100, 100, 40, 80))], 1.0)
        self.assertEqual(t.update([Obj('car', (100, 100, 40, 80))], 1.2), [])

    def test_two_objects_keep_separate_identities(self):
        t = ObjectTracker(confirm_frames=2)
        t.update([person(100, 100), person(500, 100)], 1.0)
        confirmed = t.update([person(104, 100), person(504, 100)], 1.2)
        self.assertEqual(len({o.track_id for o in confirmed}), 2)

    def test_a_detection_goes_to_the_track_it_overlaps_most(self):
        """Greedy by overlap, not by whichever track was made first."""
        t = ObjectTracker(confirm_frames=2, iou_threshold=0.1)
        t.update([person(100, 100), person(150, 100)], 1.0)
        confirmed = t.update([person(150, 100)], 1.2)
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0].bounding_box[0], 150)

    def test_a_car_crossing_the_frame_stays_one_track(self):
        t = ObjectTracker(confirm_frames=3, iou_threshold=0.3)
        ids = set()
        for i in range(12):
            for obj in t.update([Obj('car', (100 + i * 20, 200, 160, 90))], 1.0 + 0.2 * i):
                ids.add(obj.track_id)
        self.assertEqual(len(ids), 1)


class TestRealisticPaces(unittest.TestCase):
    """The gate must not fail closed.

    Overlap alone cannot follow an object at the 5 fps this stack processes
    motion at: a 40x80 person stepping 25px overlaps themselves by IoU 0.26.
    An early version of this tracker matched on overlap only, so a person
    walking past never reached CONFIRM_FRAMES and no recording was ever
    started -- the worst failure available to a security camera, and a silent
    one, because a clip that is never written logs nothing.
    """

    def _walk(self, step, frames=6, w=40, h=80, **kw):
        t = ObjectTracker(confirm_frames=3, **kw)
        confirmed_frames, ids = 0, set()
        for i in range(frames):
            got = t.update([Obj('person', (100 + i * step, 300, w, h))], 1.0 + 0.2 * i)
            if got:
                confirmed_frames += 1
                ids.update(o.track_id for o in got)
        return confirmed_frames, ids

    def test_a_person_walking_confirms_and_stays_one_track(self):
        """~1.4 m/s at 5 fps on a box this size is about 25px per frame."""
        confirmed_frames, ids = self._walk(25)
        self.assertEqual(confirmed_frames, 4)      # frames 3..6
        self.assertEqual(len(ids), 1)

    def test_a_person_hurrying_still_confirms(self):
        confirmed_frames, ids = self._walk(45)
        self.assertEqual(confirmed_frames, 4)
        self.assertEqual(len(ids), 1)

    def test_a_car_crossing_fast_still_confirms(self):
        t = ObjectTracker(confirm_frames=3)
        ids = set()
        for i in range(8):                          # 120px per frame, 160px box
            for obj in t.update([Obj('car', (50 + i * 120, 200, 160, 90))], 1.0 + 0.2 * i):
                ids.add(obj.track_id)
        self.assertEqual(len(ids), 1)

    def test_overlap_alone_would_not_have_managed_it(self):
        """Pins the reason: with the centre rule off, the walker is lost."""
        confirmed_frames, _ = self._walk(25, center_tolerance=0.0)
        self.assertEqual(confirmed_frames, 0)


class TestMovement(unittest.TestCase):
    def test_distance_is_measured_from_where_the_track_opened(self):
        """Walked across, not teleported: a jump wide enough to break the
        overlap is a different object, which is the tracker working."""
        t = ObjectTracker(confirm_frames=1)
        for i in range(11):
            t.update([person(100 + i * 30, 100)], 1.0 + 0.2 * i)
        self.assertEqual(len(t.tracks), 1)
        track = next(iter(t.tracks.values()))
        self.assertAlmostEqual(track.distance_moved(), 300.0, places=0)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestStationaryAndState(unittest.TestCase):
    """A confirmed object that stops moving is still present and no longer news."""

    def test_a_walking_person_is_active_and_a_parked_one_is_stationary(self):
        import object_tracker as ot
        t = ObjectTracker(confirm_frames=3)
        now = 1000.0
        for i in range(4):                       # walks 30px a frame
            t.update([person(100 + 30 * i, 100)], now + 0.2 * i)
        state = t.confirmed_tracks(now + 0.8, within_seconds=30)
        self.assertEqual(len(state), 1)
        self.assertFalse(state[0]['stationary'])
        self.assertGreater(state[0]['moved_px'], 60)
        # ...then stands still for longer than STATIONARY_SECONDS
        for i in range(5):
            t.update([person(190 + (i % 2), 100)], now + 1.0 + 15.0 * i)   # 1px jitter
        state = t.confirmed_tracks(now + 1.0 + 60.0, within_seconds=30)
        self.assertEqual(len(state), 1)
        self.assertTrue(state[0]['stationary'])
        self.assertEqual(state[0]['track_id'], 1)   # same object throughout

    def test_state_only_names_confirmed_and_recent_tracks(self):
        t = ObjectTracker(confirm_frames=3)
        now = 2000.0
        t.update([person(10, 10)], now)                         # one frame: not confirmed
        self.assertEqual(t.confirmed_tracks(now, 30), [])
        for i in range(3):
            t.update([person(10 + 5 * i, 10)], now + 0.2 * (i + 1))
        self.assertEqual(len(t.confirmed_tracks(now + 1, 30)), 1)
        self.assertEqual(t.confirmed_tracks(now + 100, 30), [])    # too long ago

    def test_state_carries_the_class_and_when_it_was_confirmed(self):
        t = ObjectTracker(confirm_frames=2)
        now = 3000.0
        t.update([Obj('car', (0, 0, 200, 100))], now)
        t.update([Obj('car', (5, 0, 200, 100))], now + 0.2)
        (state,) = t.confirmed_tracks(now + 0.3, 30)
        self.assertEqual(state['class_name'], 'car')
        self.assertEqual(state['confirmed_at'], now + 0.2)
        self.assertEqual(state['hits'], 2)
