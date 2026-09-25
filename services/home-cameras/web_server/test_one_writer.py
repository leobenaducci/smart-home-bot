#!/usr/bin/env python3
"""One camera, one encoder — under threads, which is the only way it broke.

Run: python web_server/test_one_writer.py  (needs numpy, cv2, ffmpeg)

Found live, from two complaints: "some videos won't play" and "it keeps saving
1 second videos". Both were the same bug, and the evidence was four ffmpeg
processes still running with nobody holding them, one of them eighteen minutes
old, writing into files that had already been checked and filed:

    ffmpeg ... /app/data/recordings/pending/2026-08-09_19-16-34_Front_car.mp4
    fd 3 -> /app/data/recordings/2026-08/2026-08-09_19-16-34_Front_car.mp4

Two fetcher threads existed for one camera (see `camera_fetcher_generation` in
web_server.py). Both ran `if not is_recording(cam): start_recording(cam)`,
which is a check and an act with fifty-odd milliseconds between them. Both
passed. The second overwrote `_active_recorders[cam]`, so the first encoder was
never finalised and never exited — and when the two agreed on a filename, it
went on writing into the file the other one had already had validated and
filed. That is a recording that passes review and is corrupt afterwards, which
is why the browser refused it and why nothing upstream had noticed.

What this pins down:

- **exactly one writer wins**, however many threads ask at once;
- **no encoder is left running** that nobody is tracking — the thing that
  actually corrupts the files;
- a reservation that fails does not wedge the camera for ever after.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault('RECORDINGS_DIR', tempfile.mkdtemp(prefix='cams-writer-'))

try:
    import numpy as np
except ImportError as e:
    print(f'SKIP: {e} — needs numpy.')
    raise SystemExit(0)
if not shutil.which('ffmpeg'):
    print('SKIP: no ffmpeg here.')
    raise SystemExit(0)

try:
    import recording as R
except ImportError as e:
    print(f'SKIP: {e} — run where recording.py can import.')
    raise SystemExit(0)

failures = []


def check(label, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def living_encoders():
    """Our own ffmpeg children that have not exited."""
    alive = []
    for rec in list(R._active_recorders.values()):
        p = rec.get('process')
        if p is not None and p.poll() is None:
            alive.append(p)
    return alive


FRAME = np.zeros((240, 320, 3), dtype=np.uint8)


def cleanup():
    for cam in list(R._active_recorders):
        rec = R._active_recorders.get(cam)
        if rec:
            R._kill_writer(rec)
        R._active_recorders.pop(cam, None)


# --- the race -----------------------------------------------------------------
print('eight threads ask to record the same camera at the same instant')
started = []
barrier = threading.Barrier(8)


def racer():
    barrier.wait()                     # everyone leaves the gate together
    got = R.start_recording('patio', FRAME, [], time.time(),
                            object_tags=['person'], camera_name='Patio')
    started.append(got)


threads = [threading.Thread(target=racer) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=60)

won = [s for s in started if s]
check('all eight answered', len(started) == 8, len(started))
check('exactly one of them got the camera', len(won) == 1, won)
# The heart of it. Every extra encoder here is a process nobody will ever
# finalise, writing into a file somebody else is about to have validated.
encoders = subprocess.run(
    ['pgrep', '-fc', f"rawvideo.*{R.RECORDINGS_DIR}"], capture_output=True, text=True)
running = int((encoders.stdout or '0').strip() or 0)
check('and only one encoder is running', running <= 1, f'{running} ffmpeg processes')
check('one entry in the table', len(R._active_recorders) == 1, list(R._active_recorders))
check('the loser is told no, not given a second file',
      started.count(None) == 7, started)

# All eight would have built the same name from the same timestamp — which is
# how two encoders came to share one path.
names = [s for s in won]
check('the winner named a file', names and names[0].endswith('.mp4'), names)

print('\nand it is really recording, not just holding the slot')
rec = R._active_recorders['patio']
check('the slot holds a live process', rec.get('process') is not None
      and rec['process'].poll() is None, rec.get('process'))
check('and is no longer a bare reservation', not rec.get('starting'), rec)

print('\nfinishing it releases the camera and leaves nothing running')
R.stop_recording('patio')
check('the table is empty', not R._active_recorders, list(R._active_recorders))
time.sleep(0.4)
after = subprocess.run(
    ['pgrep', '-fc', f"rawvideo.*{R.RECORDINGS_DIR}"], capture_output=True, text=True)
left = int((after.stdout or '0').strip() or 0)
check('no encoder outlives the clip', left == 0, f'{left} still running')

print('\nand the same camera can record again afterwards')
again = R.start_recording('patio', FRAME, [], time.time(), camera_name='Patio')
check('a second clip starts', bool(again), again)
cleanup()

# --- a failed start must not wedge the camera ---------------------------------
print('\na start that fails hands the camera back')
_real = R.subprocess.Popen
try:
    R.subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(OSError('no ffmpeg'))
    out = R.start_recording('garaje', FRAME, [], time.time(), camera_name='Garaje')
    check('it reports failure', out is None, out)
finally:
    R.subprocess.Popen = _real
check('and left no reservation behind',
      'garaje' not in R._active_recorders, list(R._active_recorders))
# Left behind, the reservation is permanent: nothing else ever clears it, so
# that camera would quietly never record again until a restart.
ok = R.start_recording('garaje', FRAME, [], time.time(), camera_name='Garaje')
check('so the camera still works', bool(ok), ok)
cleanup()

# --- a frame arriving mid-spawn ----------------------------------------------
print('\na frame that arrives while the encoder is still starting is dropped, '
      'not counted as a recording')
R._active_recorders['reservada'] = {'starting': True}
check('add_frame says no', R.add_frame_to_recording('reservada', FRAME, time.time()) is False)
check('and the reservation survives it', 'reservada' in R._active_recorders)
# ...and finalising one is a no-op rather than a KeyError on 'process'.
R._finalize_recording('reservada', R._active_recorders['reservada'])
check('finalising a reservation does not explode', True)
R._active_recorders.pop('reservada', None)

cleanup()
shutil.rmtree(R.RECORDINGS_DIR, ignore_errors=True)

print()
if failures:
    print(f'{len(failures)} FAILED: ' + ', '.join(failures))
    raise SystemExit(1)
print('all checks passed')
