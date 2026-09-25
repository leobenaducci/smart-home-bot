"""The zone a camera gets when nobody has drawn one.

Run: python3 web_server/test_motion_zone.py  (needs cv2, so: in the container)

These cameras burn a timestamp into the picture and it reprints every second.
The old default zone inset the frame by 1% -- 13px of 1296 -- and the clock
strip is four times that, so the clock sat inside the zone and every camera
without hand-drawn zones was watching its own clock tick, forever.

The numbers below are measured off two real frames from this house on
2026-09-03, both 1296x2304 and both mounted rotated, which is why the strip runs
vertically down an edge and why it is a *different* edge on each.
"""

import unittest

from motion_detector import MotionZone, full_frame_zone

FRAME_W, FRAME_H = 1296, 2304

# Measured: Front's clock runs down the left edge, Patio's down the right.
FRONT_CLOCK = (0, 46)          # x range, left edge
PATIO_CLOCK = (1238, 1296)     # x range, right edge


def bounds(zone: MotionZone, w=FRAME_W, h=FRAME_H):
    """Same percentage-to-pixel rule MotionDetector._convert_zone_to_pixels uses."""
    x, y = int(zone.x * w), int(zone.y * h)
    return x, y, x + int(zone.width * w), y + int(zone.height * h)


class TestFullFrameZone(unittest.TestCase):
    def test_it_is_expressed_as_percentages(self):
        """Pixel values would be wrong the moment a camera changed resolution."""
        z = full_frame_zone()
        for value in (z.x, z.y, z.width, z.height):
            self.assertTrue(0 <= value <= 1, value)

    def test_it_excludes_the_clock_on_the_left_edge(self):
        left = bounds(full_frame_zone())[0]
        self.assertGreater(left, FRONT_CLOCK[1],
                           "zone starts inside Front's clock strip")

    def test_it_excludes_the_clock_on_the_right_edge(self):
        right = bounds(full_frame_zone())[2]
        self.assertLess(right, PATIO_CLOCK[0],
                        "zone ends inside Patio's clock strip")

    def test_the_old_one_percent_inset_would_not_have(self):
        """Pins why this changed, so nobody quietly puts 0.01 back."""
        old = MotionZone(name='Full Frame', x=0.01, y=0.01,
                         width=0.98, height=0.98)
        self.assertLess(bounds(old)[0], FRONT_CLOCK[1])
        self.assertGreater(bounds(old)[2], PATIO_CLOCK[0])

    def test_it_still_covers_the_middle_of_the_scene(self):
        x1, y1, x2, y2 = bounds(full_frame_zone())
        self.assertLess(x1, FRAME_W * 0.1)
        self.assertGreater(x2, FRAME_W * 0.9)
        self.assertLess(y1, FRAME_H * 0.1)
        self.assertGreater(y2, FRAME_H * 0.9)

    def test_it_keeps_most_of_the_frame(self):
        x1, y1, x2, y2 = bounds(full_frame_zone())
        covered = (x2 - x1) * (y2 - y1) / (FRAME_W * FRAME_H)
        self.assertGreater(covered, 0.75, "margin is eating the scene")

    def test_the_margin_is_symmetric(self):
        x1, y1, x2, y2 = bounds(full_frame_zone())
        self.assertAlmostEqual(x1, FRAME_W - x2, delta=2)
        self.assertAlmostEqual(y1, FRAME_H - y2, delta=2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
