"""Require an object to persist before it counts as activity.

YOLO answers "is there a person in this frame". A recording gate asks something
else -- "is something happening" -- and the gap between those two questions is
most of what this house has been storing. A single frame is a coin toss at the
thresholds this stack runs (0.20 for person, cat and dog, chosen for recall):
a leaf, a moth lit by the IR lamp, a patch of night grain will each be a person
for one frame out of a hundred, and one frame is all it took to write a clip.

So detections are associated across frames and a class only counts once the
*same* object has been seen `confirm_frames` times. That keeps the low
per-frame threshold -- which is what finds a person at the end of a dark garden
-- while refusing to act on any single frame's opinion. It is the same argument
`deploy/models.py` makes about the gateway: a single sample is not a verdict.

**Why not ultralytics' own tracker.** `model.track(persist=True)` keeps its
state on `model.predictor`, and `web_server` calls plain `detect_objects()` on
the same model instance for the "show all objects" overlay and for scene
description. A `predict()` call between two `track()` calls resets that
predictor, so track ids would silently restart -- and a tracker that restarts
its ids never reaches `confirm_frames`, which fails closed: no recordings at
all. Associating by IoU here costs no GPU, cannot be perturbed by another call
path, and runs in a test without a card.

Association is per class and greedy by IoU. `max_misses` lets a track survive a
frame or two where YOLO did not find it -- occlusion behind a post, a bad
exposure -- because otherwise a single missed frame would reset the count and a
real person walking behind a tree would never confirm.
"""

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Detections of the same object before it counts. At the 5 fps this stack
# processes motion at, 3 frames is 0.6s -- far below how long a person or a car
# is visible, and far above how long a false positive survives.
CONFIRM_FRAMES = int(os.environ.get('TRACK_CONFIRM_FRAMES', '3'))
# Frames a track may go unmatched before it is dropped.
MAX_MISSES = int(os.environ.get('TRACK_MAX_MISSES', '2'))
# Overlap needed to call two boxes the same object.
IOU_THRESHOLD = float(os.environ.get('TRACK_IOU', '0.3'))
# ...and the reason overlap alone will not do. Motion is processed at 5 fps, so
# a walking person moves a good part of their own width between two frames: a
# 40x80 box stepping 25px overlaps itself by IoU 0.26, under any sane
# threshold. Associating on overlap alone would refuse to follow them, the
# track would never reach CONFIRM_FRAMES, and the gate would fail *closed* on
# precisely what it exists to catch -- silently, because a recording that never
# starts logs nothing.
#
# So a pair also matches when the box centre has moved less than this many box
# lengths. At 1.0 that is 80px per frame for a standing person, 400px/s, about
# a sprint -- generous enough for 5 fps and still far too tight to join two
# people passing on opposite sides of a driveway.
CENTER_TOLERANCE = float(os.environ.get('TRACK_CENTER_TOLERANCE', '1.0'))
# YOLO only runs during motion and for 10s after, so gaps are expected. Past
# this many seconds the scene is no longer continuous and tracks are dropped
# rather than matched against whatever is there now.
MAX_GAP_SECONDS = float(os.environ.get('TRACK_MAX_GAP', '3.0'))
# A confirmed object that has not moved for this long is *stationary*: still
# there, still counted as present, but no longer the kind of thing a security
# event is about. A parked car, a chair with a hoodie on it, a person sitting
# on the patio -- all real, none of them an arrival. Movement is measured
# against an anchor that only moves when the centre travels more than a
# quarter of the box, so jitter between frames does not count as walking.
STATIONARY_SECONDS = float(os.environ.get('TRACK_STATIONARY_SECONDS', '45.0'))
STATIONARY_BOX_FRACTION = float(os.environ.get('TRACK_STATIONARY_FRACTION', '0.25'))


def iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    """Intersection over union of two (x, y, w, h) boxes."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    iw, ih = ix2 - ix1, iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0
    intersection = iw * ih
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0 else 0.0


def distance(a: Tuple[int, int], b: Tuple[int, int]) -> float:
    """Euclidean distance between two box centres."""
    dx, dy = a[0] - b[0], a[1] - b[1]
    return (dx * dx + dy * dy) ** 0.5


@dataclass
class Track:
    """One object followed across frames."""
    track_id: int
    class_name: str
    bbox: Tuple[int, int, int, int]
    center: Tuple[int, int]
    first_center: Tuple[int, int]
    first_seen: float
    last_seen: float
    hits: int = 1
    misses: int = 0
    # Where the object was last seen *moving from*, and when. See
    # STATIONARY_SECONDS.
    anchor: Optional[Tuple[int, int]] = None
    last_moved: float = 0.0
    confirmed_at: float = 0.0

    def __post_init__(self) -> None:
        if self.anchor is None:
            self.anchor = self.center
        if not self.last_moved:
            self.last_moved = self.first_seen

    def note_position(self, center: Tuple[int, int], box_len: int, now: float) -> None:
        """Record a new centre; the anchor follows it only on real movement."""
        self.center = center
        if distance(center, self.anchor) > STATIONARY_BOX_FRACTION * max(1, box_len):
            self.anchor = center
            self.last_moved = now

    def is_stationary(self, now: float) -> bool:
        return (now - self.last_moved) >= STATIONARY_SECONDS

    def distance_moved(self) -> float:
        """How far the box centre has travelled since the track opened."""
        dx = self.center[0] - self.first_center[0]
        dy = self.center[1] - self.first_center[1]
        return (dx * dx + dy * dy) ** 0.5


class ObjectTracker:
    """Follows detections across frames and reports the ones that persist.

    One instance per camera: association assumes frames arrive in order from a
    single scene, and mixing two cameras into one tracker would match a car on
    the street against a cat on the patio.
    """

    def __init__(self,
                 confirm_frames: int = CONFIRM_FRAMES,
                 max_misses: int = MAX_MISSES,
                 iou_threshold: float = IOU_THRESHOLD,
                 max_gap_seconds: float = MAX_GAP_SECONDS,
                 center_tolerance: float = CENTER_TOLERANCE):
        self.confirm_frames = max(1, confirm_frames)
        self.max_misses = max_misses
        self.iou_threshold = iou_threshold
        self.max_gap_seconds = max_gap_seconds
        self.center_tolerance = center_tolerance
        self.tracks: Dict[int, Track] = {}
        self._next_id = 1

    def reset(self) -> None:
        """Forget every track. The scene is no longer continuous."""
        self.tracks.clear()

    def update(self, objects: List, timestamp: float) -> List:
        """Match `objects` against open tracks and return the confirmed ones.

        Each returned object is the *detection* passed in, with `track_id` set,
        so callers keep the box and confidence YOLO actually reported. An
        object is returned only once its track has been seen `confirm_frames`
        times; before that it is being followed but has not earned a recording.
        """
        # A long gap means these frames do not continue the previous ones.
        if self.tracks:
            newest = max(t.last_seen for t in self.tracks.values())
            if timestamp - newest > self.max_gap_seconds:
                self.reset()

        unmatched = set(self.tracks)
        matched: List[Tuple[object, Track]] = []
        claimed = set()

        # Greedy by overlap: the best pair first, so a detection between two
        # tracks goes to the one it actually overlaps rather than to whichever
        # happened to be created first.
        pairs = []
        for track_id, track in self.tracks.items():
            for index, obj in enumerate(objects):
                if obj.class_name != track.class_name:
                    continue
                overlap = iou(track.bbox, obj.bounding_box)
                gap = distance(track.center, (obj.center_x, obj.center_y))
                # A box length, from whichever of the two is larger, so a
                # person walking towards the camera is not lost as they grow.
                reach = self.center_tolerance * max(
                    track.bbox[2], track.bbox[3],
                    obj.bounding_box[2], obj.bounding_box[3])
                if overlap >= self.iou_threshold or gap <= reach:
                    # Rank by overlap first and closeness second: where boxes
                    # do overlap that is the better evidence, and where none do
                    # the nearest centre wins.
                    pairs.append((overlap, -gap, track_id, index))
        pairs.sort(reverse=True)

        for _, _, track_id, index in pairs:
            if track_id not in unmatched or index in claimed:
                continue
            obj = objects[index]
            track = self.tracks[track_id]
            track.bbox = obj.bounding_box
            track.note_position((obj.center_x, obj.center_y),
                                max(obj.bounding_box[2], obj.bounding_box[3]), timestamp)
            track.last_seen = timestamp
            track.hits += 1
            track.misses = 0
            unmatched.discard(track_id)
            claimed.add(index)
            matched.append((obj, track))

        # Detections that matched nothing open a new track.
        for index, obj in enumerate(objects):
            if index in claimed:
                continue
            track = Track(
                track_id=self._next_id,
                class_name=obj.class_name,
                bbox=obj.bounding_box,
                center=(obj.center_x, obj.center_y),
                first_center=(obj.center_x, obj.center_y),
                first_seen=timestamp,
                last_seen=timestamp,
            )
            self.tracks[track.track_id] = track
            self._next_id += 1
            matched.append((obj, track))

        # Tracks nothing matched this frame age out.
        for track_id in unmatched:
            track = self.tracks[track_id]
            track.misses += 1
            if track.misses > self.max_misses:
                del self.tracks[track_id]

        confirmed = []
        for obj, track in matched:
            if track.hits < self.confirm_frames:
                continue
            obj.track_id = track.track_id
            confirmed.append(obj)
            if track.hits == self.confirm_frames:
                track.confirmed_at = timestamp
                logger.info(
                    "track %d confirmed: %s after %d frames, moved %.0fpx",
                    track.track_id, track.class_name, track.hits,
                    track.distance_moved())
        return confirmed

    def confirmed_tracks(self, now: float, within_seconds: float) -> List[dict]:
        """The confirmed objects seen in the last `within_seconds`, as plain dicts.

        This is the camera's *state*, as opposed to what one frame said: a
        track that reached `confirm_frames` and was last matched recently.
        `stationary` says whether it has moved lately (STATIONARY_SECONDS);
        `moved_px` how far it has travelled since it opened. Security state and
        the assistant's `detect` answer read this, so "is anyone there" is a
        fact about the last few seconds of tracking and never one frame's
        opinion -- which on a dark patio has been a pergola.
        """
        out = []
        for track in self.tracks.values():
            if track.hits < self.confirm_frames:
                continue
            if now - track.last_seen > within_seconds:
                continue
            out.append({
                'track_id': track.track_id,
                'class_name': track.class_name,
                'hits': track.hits,
                'first_seen': track.first_seen,
                'confirmed_at': track.confirmed_at or track.first_seen,
                'last_seen': track.last_seen,
                'moved_px': round(track.distance_moved(), 1),
                'stationary': track.is_stationary(now),
                'bbox': list(track.bbox),
            })
        return out
