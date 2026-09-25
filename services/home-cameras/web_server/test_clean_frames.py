#!/usr/bin/env python3
"""What the reviewer is shown must be the scene, not our notes on it.

Run: python web_server/test_clean_frames.py   (needs numpy, cv2, ffmpeg)

Every recorded frame used to carry `_draw_overlay`: the camera name and the
wall clock in the top-left, a coloured rectangle around each thing YOLO found,
and its class and confidence beside it. Clips are reviewed by reading frames
back out of that same file, so the vision model was being shown a picture with
the answer written on it — and two failures followed, both seen in production:

- the clock is redrawn every frame, so **every** clip contained something that
  changed however still the scene was, and verdicts read "la hora en la esquina
  cambió de 20:30:38 a 20:30:53". True, and entirely our own doing;
- an unrecognised class draws in red, and verdicts came back describing "un
  objeto rojo en el césped" — the model reporting our annotation as a thing in
  the garden.

This checks the pixels that reach the model, because the bug was invisible in
the code path: `_draw_overlay` was called correctly, on the right frames, and
did exactly what it says.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('RECORDINGS_DIR', tempfile.mkdtemp(prefix='cams-clean-'))

try:
    import numpy as np
    import cv2
except ImportError as e:
    print(f'SKIP: {e} — needs numpy and cv2.')
    raise SystemExit(0)
if not shutil.which('ffmpeg'):
    print('SKIP: no ffmpeg here.')
    raise SystemExit(0)

import recording as R          # noqa: E402
import clip_review as C        # noqa: E402

failures = []


def check(label, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


class Obj:
    """A detection, shaped the way _draw_overlay reads one."""
    def __init__(self):
        self.bounding_box = (40, 40, 120, 120)
        self.class_name = 'person'
        self.confidence = 0.87


# A flat mid-grey scene: anything not grey in the output is something we drew.
GREY = 128
FRAME = np.full((240, 320, 3), GREY, dtype=np.uint8)


def record_one(camera):
    """One short clip through the real recorder, returned as frames."""
    started = R.start_recording(camera, FRAME, [Obj()], time.time(),
                                object_tags=['person'], camera_name='Patio')
    assert started, 'the recorder refused to start'
    for _ in range(6):
        R.add_frame_to_recording(camera, FRAME, time.time(), [Obj()])
    path = R._active_recorders[camera]['filepath']
    R.stop_recording(camera)
    for _ in range(50):                       # the encoder finishes on its own
        if os.path.exists(path) and os.path.getsize(path) > 2048:
            break
        time.sleep(0.1)
    # Wherever the review queue filed it.
    if not os.path.exists(path):
        for folder in os.listdir(R.RECORDINGS_DIR):
            cand = os.path.join(R.RECORDINGS_DIR, folder, os.path.basename(path))
            if os.path.exists(cand):
                path = cand
                break
    return path


def ink(path):
    """How many pixels in the clip are not the flat grey we recorded.

    Read through `_extract_frames` — the reviewer's own function — so this is
    literally what the model receives, not a re-implementation of it.
    """
    frames = C._extract_frames(path, 3)
    assert frames, 'no frames came back'
    marks = 0
    for buf in frames:
        img = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
        # JPEG is lossy, so "not grey" needs a margin; drawn ink is white,
        # black or a saturated colour and clears it by a mile.
        marks += int(np.count_nonzero(np.abs(img.astype(int) - GREY).max(axis=2) > 40))
    return marks


print('a recorded clip carries no overlay into the review')
path = record_one('patio')
check('the clip exists', os.path.exists(path), path)
clean = ink(path)
# Not zero: the encoder is lossy and a flat field still shakes a little.
check('what the model sees is the scene, not our ink', clean < 500, f'{clean} marked pixels')

print('\nand with the overlay switched back on, it is unmistakably there')
_was = R.RECORDING_OVERLAY
R.RECORDING_OVERLAY = True
try:
    dirty_path = record_one('garaje')
    dirty = ink(dirty_path)
finally:
    R.RECORDING_OVERLAY = _was
# The clock alone is hundreds of pixels; the box and its label are thousands.
# If this does not dwarf the clean run, the check above proves nothing.
check('the overlay is visible when asked for', dirty > 3000, f'{dirty} marked pixels')
check('and clean really is cleaner', clean * 5 < dirty, f'clean={clean} dirty={dirty}')

print('\nsnapshots keep it — nothing reviews those, and they show what YOLO saw')
snap = R.save_snapshot('patio', FRAME, [Obj()], time.time(),
                       object_tags=['person'], camera_name='Patio')
check('a snapshot was written', bool(snap), snap)
if snap:
    img = cv2.imread(os.path.join(R.RECORDINGS_DIR, snap))
    marked = int(np.count_nonzero(np.abs(img.astype(int) - GREY).max(axis=2) > 40))
    check('and it still carries the overlay', marked > 3000, f'{marked} marked pixels')

for cam in list(R._active_recorders):
    R._kill_writer(R._active_recorders[cam])
    R._active_recorders.pop(cam, None)
shutil.rmtree(R.RECORDINGS_DIR, ignore_errors=True)

print()
if failures:
    print(f'{len(failures)} FAILED: ' + ', '.join(failures))
    sys.exit(1)
print('all checks passed')
