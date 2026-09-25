"""A recording nothing comes back to still ends.

Run: python3 web_server/test_recording_sweep.py   (needs numpy)

Every call to `_finalize_recording` used to be reached from
`add_frame_to_recording` or from an explicit `stop_recording`, so the only thing
that ended a recording was another frame arriving. A camera that stopped
delivering frames left its recording open forever: ffmpeg held a 0-byte file,
the slot stayed in `_active_recorders`, and every later motion event on that
camera was refused with "already recording".

That is not a thought experiment -- two ffmpeg processes were found nine hours
into a ten-second clip, and the cameras behind them had recorded nothing since.
Nothing logged an error; `clip_review` discarded the empty files afterwards and
that was the only trace.

These drive `sweep_stale_recordings()` directly rather than waiting on the
thread, so the checks are about the rule and not about a sleep.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import recording  # noqa: E402

FAILED = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))
    if not ok:
        FAILED.append(label)


class FakeProc:
    """Stands in for ffmpeg: records that stdin was closed and it was waited on."""

    def __init__(self):
        self.stdin = self
        self.closed = False
        self.waited = False
        self.killed = False
        self.returncode = 0
        self.stderr = None

    def close(self):
        self.closed = True

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def kill(self):
        self.killed = True

    def poll(self):
        return None


def make_recorder(camera, start_time, duration=10):
    proc = FakeProc()
    recording._active_recorders[camera] = {
        'process': proc,
        'filepath': os.devnull,
        'filename': f'{camera}.mp4',
        'rel_path': f'pending/{camera}.mp4',
        'start_time': start_time,
        'duration': duration,
        'frame_count': 0,
        'camera_name': camera,
    }
    return proc


def reset():
    recording._active_recorders.clear()


# The review queue writes to disk and spawns work; the sweep's job is to close
# the clip and free the camera, and that is what these are about.
recording._review_queue = lambda: type('Q', (), {'submit': staticmethod(lambda *a: None)})()

print("a recording no frame comes back to is closed on the sweep's own clock")
reset()
now = time.time()
proc = make_recorder('cam-a', now - 600, duration=10)   # ten seconds, ten minutes ago
closed = recording.sweep_stale_recordings(now=now)
check("the stalled recording is closed", closed == ['cam-a'], closed)
check("  ffmpeg's stdin was closed", proc.closed, "stdin left open -> the process leaks")
check("  and it was waited on", proc.waited, "not reaped")
check("  the camera is free to record again",
      not recording.is_recording('cam-a'), "slot still held")

print("\na recording still inside its duration is left alone")
reset()
proc = make_recorder('cam-b', now - 2, duration=10)
closed = recording.sweep_stale_recordings(now=now)
check("nothing is closed", closed == [], closed)
check("  and it is still recording", recording.is_recording('cam-b'), "closed too early")

print("\nnor is one that is merely overdue by less than the grace")
reset()
# Past its duration but inside the grace: the frame path finalizes these itself,
# and racing it here would cut clips short on a camera that is working fine.
proc = make_recorder('cam-c', now - (10 + recording.RECORDING_STALL_GRACE - 2), duration=10)
closed = recording.sweep_stale_recordings(now=now)
check("still left alone inside the grace", closed == [], closed)
check("  the normal path keeps its clip", recording.is_recording('cam-c'), "cut short")

print("\nand a reservation that never became a recording is released")
reset()
# `_finalize_recording` returns early on a reservation, so if the spawn dies in a
# way that skips the cleanup in start_recording, nothing else would ever free it
# and that camera never records again.
recording._active_recorders['cam-d'] = {
    'starting': True, 'reserved_at': now - (recording.RESERVATION_TIMEOUT + 5)}
closed = recording.sweep_stale_recordings(now=now)
check("the stuck reservation is released", closed == ['cam-d'], closed)
check("  the camera is free", not recording.is_recording('cam-d'), "slot still held")

print("\na reservation a few milliseconds old is not")
reset()
recording._active_recorders['cam-e'] = {'starting': True, 'reserved_at': now - 0.05}
closed = recording.sweep_stale_recordings(now=now)
check("a fresh reservation survives", closed == [], closed)
check("  ffmpeg still gets to finish spawning",
      recording.is_recording('cam-e'), "reservation dropped mid-spawn")

print("\nthe sweeper thread starts once and stays alive")
reset()
t1 = recording.start_recording_sweeper()
t2 = recording.start_recording_sweeper()
check("the same thread is reused", t1 is t2, f"{t1} vs {t2}")
check("  it is a daemon, so it never holds the process open", t1.daemon, "not a daemon")
check("  and it is running", t1.is_alive(), "died on start")

print("\nand ffmpeg's stderr never goes to a pipe nobody drains")
# This is what actually wedged the cameras. `stderr=subprocess.PIPE` is only
# read after something has gone wrong, so ffmpeg filled the 64 KB buffer,
# blocked writing to it, and stopped reading stdin -- which blocked the thread
# feeding it frames, with the camera still marked as recording. Found in the
# wild as `wchan: anon_pipe_write` and an `rchar` of exactly one frame, nine
# hours into a ten-second clip.
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recording.py')).read()
spawn = src[src.index('process = subprocess.Popen('):]
spawn = spawn[:spawn.index(')')]
check("stderr is not an undrained pipe",
      'stderr=subprocess.PIPE' not in spawn, spawn.replace(chr(10), ' '))
check("  it goes to a file instead", 'stderr=stderr_file' in spawn, spawn.replace(chr(10), ' '))
check("  and ffmpeg is told not to write a progress line",
      "'-nostats'" in src, "-nostats missing; stderr fills far faster")

# The handle has to reach the broken-pipe reader, or the diagnostics the file
# exists to preserve are gone.
check("the recorder carries the handle", "'stderr_file': stderr_file" in src, "not stored")
check("  and closes it when the clip ends", "sf.close()" in src, "a temp file per clip, forever")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed")
