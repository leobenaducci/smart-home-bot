"""
Recording Module - Saves snapshots and video clips on motion detection
Stores files in RECORDINGS_DIR organized by year-month subfolders (YYYY-MM)
Uses FFmpeg for H.264 video encoding (NVENC GPU acceleration when available, libx264 fallback)
"""

import cv2
import bisect
import os
import re
import time
import logging
import tempfile
import threading
import subprocess
import collections
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import numpy as np

import clip_review

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_DEFAULT_RECORDINGS_DIR = os.path.join(_PROJECT_ROOT, 'data', 'recordings')

RECORDINGS_DIR = os.environ.get('RECORDINGS_DIR', _DEFAULT_RECORDINGS_DIR)

_configured_timezone = None

def get_timezone():
    global _configured_timezone
    if _configured_timezone is not None:
        return _configured_timezone
    try:
        tz_env = os.environ.get('TZ', '')
        if tz_env:
            _configured_timezone = ZoneInfo(tz_env)
        else:
            _configured_timezone = datetime.now().astimezone().tzinfo
    except Exception:
        _configured_timezone = timezone.utc
    return _configured_timezone

def set_timezone(tz_name: str):
    global _configured_timezone
    try:
        _configured_timezone = ZoneInfo(tz_name)
    except Exception:
        _configured_timezone = timezone.utc

VIDEO_CLIP_DURATION = int(os.environ.get('RECORDING_DURATION', '10'))
VIDEO_NO_MOTION_STOP = 30

# A recording only ends when a frame arrives and finds it overdue: every call to
# `_finalize_recording` is reached from `add_frame_to_recording` or from an
# explicit `stop_recording`. So a camera that stops delivering frames leaves its
# recording open forever.
#
# Not hypothetical. Two ffmpeg processes were found holding 0-byte clips nine
# hours after a ten-second recording started, and because the slot stays in
# `_active_recorders`, every later motion event on those cameras was answered
# with "already recording" -- Patio and Front recorded nothing at all between a
# restart and the next one, with no error logged anywhere. `clip_review` then
# discarded the empty files as "empty or nearly so", which is the only trace it
# left.
#
# So the timeout cannot live only on the frame path. This runs on its own clock.
RECORDING_STALL_GRACE = int(os.environ.get('RECORDING_STALL_GRACE', '15'))
RECORDING_SWEEP_INTERVAL = int(os.environ.get('RECORDING_SWEEP_INTERVAL', '5'))
# A reservation is held for the few milliseconds it takes to spawn ffmpeg. If it
# is still there much later, the spawn died in a way that skipped the cleanup,
# and `_finalize_recording` returns early on reservations -- so nothing else
# would ever free it.
RESERVATION_TIMEOUT = 60
VIDEO_FPS = 5
SNAPSHOT_QUALITY = 90
PRE_MOTION_DURATION = 5.0
PRE_MOTION_BUFFER_FPS = VIDEO_FPS

# Burn the camera name, the time and the detector's boxes into the recorded
# video. Off, and here as a switch rather than a deletion because a burned-in
# timestamp is a reasonable thing to want on security footage — but it cannot
# be had at the same time as a vision model reviewing the clip, and the review
# is worth more.
#
# What it cost while it was on: the overlay writes the clock into every single
# frame, so *every* clip contained something that changed no matter what the
# scene did, and the reviewer kept reporting "the clock in the corner changed from
# 20:30:38 a 20:30:53" — true, and entirely our own doing. The boxes were
# worse than that. A model shown a green rectangle labelled "person: 0.87"
# cannot judge the scene independently of it; it is being handed the answer.
# And an unrecognised class draws in red, which is where verdicts like "un
# red object on the grass moves" came from: the model describing our
# annotation as a thing in the garden.
#
# Snapshots keep the full overlay — nothing reviews those, and they are what
# gets looked at to see what the detector thought.
RECORDING_OVERLAY = os.environ.get('RECORDING_OVERLAY', '0') not in ('0', 'false', '')

_recording_lock = threading.Lock()
_active_recorders: Dict[str, dict] = {}


def _safe_camera_id(camera_ip: str, camera_name: str = None) -> str:
    """Convert camera IP, MAC, or name to a filesystem-safe string.
    Uses camera_name if provided and non-empty, otherwise falls back to sanitizing the IP/MAC."""
    if camera_name:
        safe = re.sub(r'[^a-zA-Z0-9]', '-', camera_name).strip('-')
        if safe:
            return safe
    return camera_ip.replace('.', '-').replace(':', '-')


def _month_subdir(dt: datetime) -> str:
    return dt.strftime('%Y-%m')


def _recording_path(dt: datetime, filename: str) -> str:
    return os.path.join(RECORDINGS_DIR, _month_subdir(dt), filename)


def _pending_path(filename: str) -> str:
    """Where a clip is written while it is being recorded, and where it waits
    to be looked at. One flat folder: it is a queue, not an archive."""
    return os.path.join(RECORDINGS_DIR, clip_review.PENDING_DIRNAME, filename)


def month_path_for(filename: str) -> str:
    """The shelf a reviewed clip belongs on, from the timestamp in its name.

    Taken from the name rather than from the clock, so a clip reviewed after
    midnight — or after a restart, days later — still files under the month it
    was recorded in.
    """
    dt = _parse_timestamp_from_filename(filename) or datetime.now(tz=get_timezone())
    ensure_dirs(dt)
    return _recording_path(dt, filename)


def ensure_dirs(dt: datetime = None):
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    clip_review.ensure_dirs(RECORDINGS_DIR)
    if dt is not None:
        os.makedirs(os.path.join(RECORDINGS_DIR, _month_subdir(dt)), exist_ok=True)

ensure_dirs()


class RingBuffer:
    """Circular buffer storing recent compressed frames for pre-motion recording"""

    def __init__(self, camera_ip: str, duration: float = PRE_MOTION_DURATION,
                 fps: int = PRE_MOTION_BUFFER_FPS):
        self.camera_ip = camera_ip
        self.max_frames = int(duration * fps) + 1
        self.buffer: collections.deque = collections.deque(maxlen=self.max_frames)
        self.lock = threading.Lock()
        self.last_add_time = 0.0
        self.frame_interval = 1.0 / fps

    def add_frame(self, frame: np.ndarray, timestamp: float):
        if timestamp - self.last_add_time < self.frame_interval:
            return
        ret, encoded = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not ret:
            return
        with self.lock:
            self.buffer.append((encoded, timestamp))
        self.last_add_time = timestamp

    def drain(self) -> List[Tuple[np.ndarray, float]]:
        with self.lock:
            items = list(self.buffer)
            self.buffer.clear()
        result = []
        for encoded, ts in items:
            decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
            if decoded is not None:
                result.append((decoded, ts))
        return result


_ring_buffers: Dict[str, RingBuffer] = {}


def add_to_ring_buffer(camera_ip: str, frame: np.ndarray, timestamp: float):
    if camera_ip not in _ring_buffers:
        _ring_buffers[camera_ip] = RingBuffer(camera_ip)
    _ring_buffers[camera_ip].add_frame(frame, timestamp)


def drain_ring_buffer(camera_ip: str) -> list:
    buf = _ring_buffers.get(camera_ip)
    if buf is None:
        return []
    return buf.drain()


def _check_nvenc() -> bool:
    try:
        test_cmd = [
            'ffmpeg', '-y',
            '-f', 'lavfi', '-i', 'color=c=black:s=64x64:d=0.1:r=1',
            '-c:v', 'h264_nvenc', '-f', 'null', '-'
        ]
        result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


_NVENC_AVAILABLE = _check_nvenc()


def _get_video_codec() -> str:
    return 'h264_nvenc' if _NVENC_AVAILABLE else 'libx264'


class DetectionInfo:
    __slots__ = ('class_name', 'confidence', 'bounding_box')

    def __init__(self, class_name: str, confidence: float, bounding_box: tuple):
        self.class_name = class_name
        self.confidence = confidence
        self.bounding_box = bounding_box

    @classmethod
    def from_detected_object(cls, obj):
        return cls(obj.class_name, obj.confidence, obj.bounding_box)


def _format_timestamp(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tz = get_timezone()
    dt = dt.astimezone(tz)
    return dt.strftime('%Y-%m-%d_%H-%M-%S')


def _draw_overlay(frame: np.ndarray, detected_objects: list,
                  camera_ip: str, timestamp: float,
                  camera_name: str = None) -> np.ndarray:
    result = frame.copy()
    tz = get_timezone()
    dt = datetime.fromtimestamp(timestamp, tz=tz)
    ts_str = dt.strftime('%Y-%m-%d %H:%M:%S')
    display_name = camera_name or camera_ip

    cv2.putText(result, f"{display_name}  {ts_str}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 3)
    cv2.putText(result, f"{display_name}  {ts_str}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 1)

    color_map = {
        'person': (0, 255, 0),
        'cat': (255, 0, 0),
        'dog': (0, 165, 255),
    }

    tags = []
    for obj in detected_objects:
        x, y, w, h = obj.bounding_box
        color = color_map.get(obj.class_name, (0, 0, 255))
        cv2.rectangle(result, (x, y), (x + w, y + h), color, 2)
        label = f"{obj.class_name}: {obj.confidence:.2f}"
        cv2.putText(result, label, (x, y - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        tags.append(f"{obj.class_name}({obj.confidence:.0%})")

    if tags:
        tag_text = " | ".join(tags)
        cv2.putText(result, tag_text, (10, result.shape[0] - 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

    return result


def _parse_timestamp_from_filename(filename: str) -> Optional[datetime]:
    try:
        date_part = filename.split('_')[0]
        time_part = filename.split('_')[1]
        return datetime.strptime(f"{date_part}_{time_part}", '%Y-%m-%d_%H-%M-%S')
    except (ValueError, IndexError):
        return None


def save_snapshot(camera_ip: str, frame: np.ndarray,
                  detected_objects: list,
                  timestamp: float,
                  object_tags: List[str] = None,
                  camera_name: str = None) -> Optional[str]:
    tz = get_timezone()
    dt = datetime.fromtimestamp(timestamp, tz=tz)
    ensure_dirs(dt)
    try:
        ts_str = _format_timestamp(dt)

        tags_suffix = ""
        if object_tags:
            safe_tags = [t.replace(' ', '_') for t in object_tags[:3]]
            tags_suffix = "_" + "_".join(safe_tags)

        filename = f"{ts_str}_{_safe_camera_id(camera_ip, camera_name)}{tags_suffix}.jpg"
        filepath = _recording_path(dt, filename)

        overlay_frame = _draw_overlay(frame, detected_objects, camera_ip, timestamp, camera_name)

        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), SNAPSHOT_QUALITY]
        ret, buffer = cv2.imencode('.jpg', overlay_frame, encode_param)
        if not ret:
            logger.error(f"Failed to encode snapshot for {camera_ip}")
            return None

        with open(filepath, 'wb') as f:
            f.write(buffer.tobytes())

        size_kb = len(buffer) / 1024
        rel_path = os.path.join(_month_subdir(dt), filename)
        logger.info(f"Saved snapshot: {rel_path} ({size_kb:.0f}KB, {len(detected_objects)} objects)")
        return rel_path

    except Exception as e:
        logger.error(f"Error saving snapshot for {camera_ip}: {e}")
        return None


def start_recording(camera_ip: str, frame: np.ndarray,
                    detected_objects: list,
                    timestamp: float,
                    object_tags: List[str] = None,
                    pre_buffer: list = None,
                    duration: int = None,
                    camera_name: str = None) -> Optional[str]:
    """Begin a clip for this camera, or return None if one is already running.

    **One camera, one writer, enforced here.** The caller used to ask
    `is_recording()` and then call this, which is a check and an act with no
    lock between them and a good fifty milliseconds in the middle — long
    enough to drain a ring buffer and spawn an encoder. Two threads watching
    the same camera both passed the check, both got here, and the second one
    overwrote `_active_recorders[camera_ip]`.

    What that cost, seen on this house's own cameras:

    - the first encoder was **orphaned** — nothing held it any more, so its
      stdin was never closed and it never exited. Four were found stuck, one
      for eighteen minutes;
    - when both landed in the same second they built the *same filename* and
      wrote the same path. One of them finished, passed `validate_clip` and
      was filed to the month folder — and the orphan, holding a descriptor to
      that inode, kept writing into it afterwards. A recording that was
      checked and then corrupted, which is why the review could not catch it
      and why the browser refused it ("Invalid mvhd time scale");
    - when they landed a second apart the orphan eventually had its stdin
      closed by the garbage collector, finalising a clip that held nothing but
      its pre-buffer. Those are the one-second videos.

    The reservation goes in under the lock *before* anything slow happens, so
    the second caller is turned away rather than racing.
    """
    tz = get_timezone()
    dt = datetime.fromtimestamp(timestamp, tz=tz)
    ensure_dirs(dt)

    with _recording_lock:
        if camera_ip in _active_recorders:
            logger.debug("already recording %s, ignoring a second start", camera_ip)
            return None
        # A placeholder, not the real entry: it has no process to finalize, and
        # `add_frame_to_recording` and `_finalize_recording` both have to be
        # able to tell it apart from a live recorder if a frame arrives while
        # ffmpeg is still starting.
        _active_recorders[camera_ip] = {'starting': True,
                                       'reserved_at': time.time()}

    # The sweeper is what gives a camera back when frames stop arriving, so it
    # has to be running before the first recording, not after someone notices.
    start_recording_sweeper()


    # Every way out that did not install a real recorder gives the camera back.
    # One place, covering the return-None paths as well as the raising ones:
    # ffmpeg missing, every codec refused, an exception on the way. A
    # reservation left behind is permanent — nothing else clears it — so that
    # camera silently never records again until the process restarts, which is
    # a worse failure than the one being guarded against.
    started = None
    try:
        started = _start_recording_locked(camera_ip, frame, detected_objects,
                                          timestamp, dt, object_tags, pre_buffer,
                                          duration, camera_name)
        return started
    finally:
        if started is None:
            with _recording_lock:
                if _active_recorders.get(camera_ip, {}).get('starting'):
                    del _active_recorders[camera_ip]


def _start_recording_locked(camera_ip, frame, detected_objects, timestamp, dt,
                            object_tags, pre_buffer, duration, camera_name):
    try:
        ts_str = _format_timestamp(dt)

        tags_suffix = ""
        if object_tags:
            safe_tags = [t.replace(' ', '_') for t in object_tags[:3]]
            tags_suffix = "_" + "_".join(safe_tags)

        filename = f"{ts_str}_{_safe_camera_id(camera_ip, camera_name)}{tags_suffix}.mp4"
        # Written into pending/, not into the month folder. Most of what a
        # motion detector records is a cloud or a branch, so where a clip
        # *belongs* is not known until something has looked at it — see
        # clip_review. The month folder stays the shelf for recordings that
        # earned their place on it.
        filepath = _pending_path(filename)

        height, width = frame.shape[:2]

        codecs_to_try = ['h264_nvenc', 'libx264'] if _NVENC_AVAILABLE else ['libx264']

        writer = None
        used_codec = None
        used_filepath = filepath

        for codec in codecs_to_try:
            cmd = [
                'ffmpeg', '-y',
                '-f', 'rawvideo',
                '-vcodec', 'rawvideo',
                '-pix_fmt', 'bgr24',
                '-s', f'{width}x{height}',
                '-r', str(VIDEO_FPS),
                '-i', '-',
                '-c:v', codec,
                '-pix_fmt', 'yuv420p',
            ]

            if codec == 'libx264':
                cmd.extend(['-preset', 'fast', '-crf', '23', '-profile:v', 'high', '-level', '4.1'])
            else:
                cmd.extend(['-preset', 'fast', '-rc', 'vbr', '-b:v', '2M', '-profile:v', 'high', '-level', '4.1'])

            # `-nostats` because the progress line is the bulk of what ffmpeg
            # writes to stderr, and every byte of it had to go somewhere.
            cmd.extend(['-nostats', '-loglevel', 'warning'])
            cmd.extend(['-movflags', '+faststart', '-f', 'mp4', used_filepath])

            # A temp file, not `subprocess.PIPE`. Nothing reads this pipe until
            # something has already gone wrong, so ffmpeg filled the 64 KB
            # buffer, blocked forever *writing* to it, and stopped reading
            # stdin -- which blocked whoever was feeding it frames. Two
            # processes were found wedged exactly there: `wchan
            # anon_pipe_write`, `rchar` of one frame and not a byte more, on a
            # ten-second clip that had been running nine hours. The camera stays
            # marked as recording throughout, so it records nothing ever again.
            #
            # A file cannot fill, is read the same way at the three places that
            # want it, and costs nothing when the recording works -- which,
            # with `-nostats`, is a few hundred bytes.
            stderr_file = tempfile.TemporaryFile()
            try:
                process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    bufsize=width * height * 3 * 2,
                )
            except FileNotFoundError:
                stderr_file.close()
                logger.error("FFmpeg not found, cannot record video")
                return None

            try:
                if pre_buffer:
                    for buf_frame, buf_ts in pre_buffer:
                        try:
                            overlay_frame = (_draw_overlay(buf_frame, [], camera_ip, buf_ts, camera_name)
                                             if RECORDING_OVERLAY else buf_frame)
                            if overlay_frame.shape[:2] != (height, width):
                                overlay_frame = cv2.resize(overlay_frame, (width, height))
                            process.stdin.write(overlay_frame.tobytes())
                        except BrokenPipeError:
                            break
                    try:
                        process.stdin.flush()
                    except BrokenPipeError:
                        pass

                overlay_frame = (_draw_overlay(frame, detected_objects, camera_ip, timestamp, camera_name)
                                 if RECORDING_OVERLAY else frame)
                if overlay_frame.shape[:2] != (height, width):
                    overlay_frame = cv2.resize(overlay_frame, (width, height))
                process.stdin.write(overlay_frame.tobytes())
                process.stdin.flush()
            except BrokenPipeError:
                stderr_output = _read_stderr(stderr_file)
                logger.warning(f"Codec {codec} failed for {camera_ip}: {stderr_output[:200]}")
                process.wait(timeout=5)
                if os.path.exists(used_filepath):
                    try:
                        os.remove(used_filepath)
                    except OSError:
                        pass
                stderr_file.close()
                continue

            time.sleep(0.05)
            if process.poll() is not None:
                stderr_output = _read_stderr(stderr_file)
                logger.warning(f"Codec {codec} exited prematurely for {camera_ip}: {stderr_output[:200]}")
                if os.path.exists(used_filepath):
                    try:
                        os.remove(used_filepath)
                    except OSError:
                        pass
                stderr_file.close()
                continue

            writer = process
            used_codec = codec
            break

        if writer is None:
            logger.error(f"All video codecs failed for {camera_ip}, cannot record")
            return None            # the caller hands the camera back

        pre_buffer_count = len(pre_buffer) if pre_buffer else 0
        rel_path = os.path.join(_month_subdir(dt), filename)

        recording_duration = duration if duration is not None else VIDEO_CLIP_DURATION

        with _recording_lock:
            # Whatever is there is our own reservation — nobody else could have
            # taken the slot, and if that assumption ever stops holding, the
            # encoder it belongs to must be stopped rather than dropped on the
            # floor. An abandoned ffmpeg does not exit; it keeps its file open
            # and keeps writing into it.
            previous = _active_recorders.get(camera_ip)
            if previous and not previous.get('starting'):
                logger.error("a recorder for %s appeared while one was starting; "
                             "stopping the old one rather than orphaning it", camera_ip)
                _kill_writer(previous)
            _active_recorders[camera_ip] = {
                'process': writer,
                'filepath': used_filepath,
                'filename': filename,
                'rel_path': rel_path,
                'start_time': time.time(),
                'last_frame_time': time.time(),
                'last_movement_time': time.time(),
                'object_tags': object_tags or [],
                'frame_count': 1 + pre_buffer_count,
                'width': width,
                'height': height,
                'duration': recording_duration,
                'camera_name': camera_name or camera_ip,
                # Kept so the broken-pipe path can still say what ffmpeg said.
                'stderr_file': stderr_file,
            }

        logger.info(f"Started recording: {rel_path} ({width}x{height}, codec: {used_codec}, pre-buffer: {pre_buffer_count} frames)")
        return rel_path

    except Exception as e:
        logger.error(f"Error starting recording for {camera_ip}: {e}")
        return None


def _kill_writer(rec: dict) -> None:
    """Stop an encoder we are about to stop tracking.

    Closing stdin is what makes ffmpeg finish and exit; without it the process
    waits on the pipe forever, holding its output file open. Killing follows
    only if it will not go quietly — the file is unusable either way once we
    have decided not to keep this writer.
    """
    process = rec.get('process')
    if process is None:
        return
    try:
        process.stdin.close()
    except (BrokenPipeError, OSError, AttributeError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error("an abandoned encoder would not die")


def add_frame_to_recording(camera_ip: str, frame: np.ndarray,
                           timestamp: float,
                           detected_objects: list = None) -> bool:
    with _recording_lock:
        if camera_ip not in _active_recorders:
            return False

        rec = _active_recorders[camera_ip]
        # The slot is reserved but ffmpeg is not up yet. Not an error and not
        # a reason to drop the reservation — the frame simply has nowhere to
        # go for the few milliseconds it takes to spawn the encoder.
        if rec.get('starting'):
            return False
        elapsed = time.time() - rec['start_time']
        rec_duration = rec.get('duration', VIDEO_CLIP_DURATION)

        if elapsed >= rec_duration:
            _finalize_recording(camera_ip, rec)
            return False

        obj_list = detected_objects or []

        if not obj_list:
            time_without_movement = time.time() - rec.get('last_movement_time', rec['start_time'])
            if time_without_movement >= VIDEO_NO_MOTION_STOP:
                _finalize_recording(camera_ip, rec)
                return False
        else:
            rec['last_movement_time'] = time.time()

        process = rec['process']

        if process.poll() is not None:
            logger.error(f"FFmpeg process exited prematurely for {camera_ip} (code {process.returncode})")
            del _active_recorders[camera_ip]
            return False

        rec_camera_name = rec.get('camera_name', camera_ip)
        overlay_frame = (_draw_overlay(frame, obj_list, camera_ip, timestamp, rec_camera_name)
                         if RECORDING_OVERLAY else frame)
        try:
            process.stdin.write(overlay_frame.tobytes())
            process.stdin.flush()
        except BrokenPipeError:
            stderr_output = _read_stderr(rec.get('stderr_file'))
            logger.error(f"FFmpeg pipe broken for {camera_ip}: {stderr_output[:500]}")
            del _active_recorders[camera_ip]
            return False

        rec['last_frame_time'] = time.time()
        rec['frame_count'] += 1
        return True


def _read_stderr(stderr_file) -> str:
    """Whatever ffmpeg complained about, from the start of the file."""
    if stderr_file is None:
        return ''
    try:
        stderr_file.seek(0)
        return stderr_file.read().decode(errors='replace')
    except Exception:
        return ''


def _finalize_recording(camera_ip: str, rec: dict):
    if rec.get('starting'):
        return                      # a reservation, not a recording
    process = rec['process']
    filepath = rec['filepath']
    rel_path = rec.get('rel_path', rec['filename'])

    try:
        process.stdin.close()
    except BrokenPipeError:
        pass

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        logger.warning(f"FFmpeg did not exit in time for {camera_ip}, killing")
        process.kill()
        process.wait(timeout=5)

    try:
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        logger.info(f"Recording complete: {rel_path} ({rec['frame_count']} frames, {size_mb:.1f}MB)")
    except Exception:
        logger.info(f"Recording complete: {rel_path} ({rec['frame_count']} frames)")

    sf = rec.get('stderr_file')
    if sf is not None:
        # After `_read_stderr` above has had its chance: a temp file per clip
        # adds up over a process that runs for weeks.
        try:
            sf.close()
        except Exception:
            pass

    if camera_ip in _active_recorders:
        del _active_recorders[camera_ip]

    # The clip is closed; now something has to decide whether it was worth
    # recording. Handed over rather than done here: this runs on the camera's
    # own thread, and pulling frames through a vision model on it would stall
    # the stream it is reading.
    _review_queue().submit(rec['filename'])


_review_q = None
_review_q_lock = threading.Lock()


def _review_queue():
    """The single reviewer, built on first use.

    Lazily, because importing this module must not start a thread — the tests
    and the CLI tools import it to read paths, and a worker that outlives them
    is a surprise nobody asked for.
    """
    global _review_q
    with _review_q_lock:
        if _review_q is None:
            _review_q = clip_review.ClipReviewQueue(RECORDINGS_DIR, month_path_for)
            _review_q.resume()
        return _review_q


def review_queue_status() -> dict:
    """What the reviewer has been up to, for the settings page."""
    q = _review_queue()
    return {'enabled': clip_review.REVIEW_ENABLED, 'model': clip_review.REVIEW_MODEL,
            'pending': q.depth(), 'reviewed': q.reviewed, 'kept': q.kept,
            'to_review': q.to_review, 'last_error': q.last_error,
            # Deleted unwatched, so it is counted where somebody can see it.
            # A number that only exists in a log file is a number nobody
            # checks, and this is the one thing here that destroys a file.
            'deleted': q.deleted, 'delete_static': clip_review.DELETE_STATIC,
            # How often the reviewer gave the card to something else, and how
            # often it gave up waiting. Without these a queue that is politely
            # yielding all afternoon looks identical to a reviewer that has
            # stopped working, which is the one thing this box is read for.
            'gpu_waits': q.gpu_waits, 'gpu_timeouts': q.gpu_timeouts,
            'max_age_days': clip_review.REVIEW_MAX_AGE_DAYS}


def sweep_stale_recordings(now: float = None) -> list:
    """Finalize recordings that no frame has come back to. Returns their keys.

    Overdue means past the clip's own duration plus a grace, so this never races
    the normal path -- a recording being fed frames is finalized by the frame
    that finds it finished, exactly as before, and this only ever sees the ones
    nothing came back for.
    """
    now = time.time() if now is None else now
    closed = []
    with _recording_lock:
        for camera_ip, rec in list(_active_recorders.items()):
            if rec.get('starting'):
                if now - rec.get('reserved_at', now) > RESERVATION_TIMEOUT:
                    logger.warning(
                        "recording slot for %s was reserved %.0fs ago and never "
                        "started; releasing it", camera_ip,
                        now - rec.get('reserved_at', now))
                    del _active_recorders[camera_ip]
                    closed.append(camera_ip)
                continue
            duration = rec.get('duration', VIDEO_CLIP_DURATION)
            overdue = now - rec['start_time'] - duration
            if overdue > RECORDING_STALL_GRACE:
                logger.warning(
                    "no frame reached %s's recording for %.0fs past its %.0fs "
                    "duration; closing it so the camera can record again",
                    camera_ip, overdue, duration)
                _finalize_recording(camera_ip, rec)
                closed.append(camera_ip)
    return closed


_sweeper_lock = threading.Lock()
_sweeper_thread = None


def start_recording_sweeper():
    """Start the sweep loop once. Idempotent; safe to call on every recording."""
    global _sweeper_thread
    with _sweeper_lock:
        if _sweeper_thread is not None and _sweeper_thread.is_alive():
            return _sweeper_thread

        def _loop():
            while True:
                time.sleep(RECORDING_SWEEP_INTERVAL)
                try:
                    sweep_stale_recordings()
                except Exception:
                    # Never let one bad recorder kill the sweeper: it is the
                    # only thing that can free a stuck camera, and a dead
                    # sweeper looks exactly like the bug it exists to fix.
                    logger.exception("recording sweep failed")

        _sweeper_thread = threading.Thread(target=_loop, daemon=True,
                                           name='recording-sweeper')
        _sweeper_thread.start()
        return _sweeper_thread


def stop_recording(camera_ip: str):
    with _recording_lock:
        if camera_ip in _active_recorders:
            rec = _active_recorders[camera_ip]
            _finalize_recording(camera_ip, rec)


def is_recording(camera_ip: str) -> bool:
    with _recording_lock:
        return camera_ip in _active_recorders


def get_active_recording_cameras() -> list:
    with _recording_lock:
        return list(_active_recorders.keys())


# Subdirectories of RECORDINGS_DIR that hold clips which are not filed yet.
# Kept as a set here so the listing and the path resolver agree on what is
# "the shelf" and what is still in the reviewer's hands.
_STAGING_DIRNAMES = frozenset((clip_review.PENDING_DIRNAME, clip_review.REVIEW_DIRNAME))


def list_recordings(rec_type: str = None, camera_ip: str = None,
                    limit: int = 100, offset: int = 0,
                    camera_name: str = None,
                    device_ip: str = None) -> List[dict]:
    ensure_dirs()
    results = []

    with _recording_lock:
        # A reservation ({'starting': True}) has no filepath yet: it is the slot
        # start_recording() holds while ffmpeg spawns. It is not a live
        # recording, so a listing during that window must skip it, not crash.
        active_paths = {rec['filepath'] for rec in _active_recorders.values()
                        if not rec.get('starting')}

    try:
        if not os.path.exists(RECORDINGS_DIR):
            return []
        all_months = os.listdir(RECORDINGS_DIR)
    except OSError as e:
        logger.warning(f"Cannot read RECORDINGS_DIR: {e}")
        return []

    # Build set of safe IDs to match against (camera_id, name, device_ip)
    camera_safe_ids = set()
    if camera_ip:
        camera_safe_ids.add(_safe_camera_id(camera_ip))
    if camera_name:
        safe_name = _safe_camera_id(None, camera_name)
        if safe_name:
            camera_safe_ids.add(safe_name)
    if device_ip:
        camera_safe_ids.add(_safe_camera_id(device_ip))

    for month_dir in sorted(all_months, reverse=True):
        try:
            # The staging folders are not the shelf. A clip in pending/ has not
            # been judged yet and a clip in to_review/ is waiting for a person
            # to say; listing either here would show it twice on the recordings
            # page and let Select All delete the ones the tray exists to save.
            if month_dir in _STAGING_DIRNAMES:
                continue
            month_path = os.path.join(RECORDINGS_DIR, month_dir)
            if not os.path.isdir(month_path):
                continue
            for f in os.listdir(month_path):
                try:
                    is_snapshot = f.endswith('.jpg')
                    is_video = f.endswith('.mp4')

                    if rec_type == 'snapshot' and not is_snapshot:
                        continue
                    if rec_type == 'video' and not is_video:
                        continue
                    if rec_type is None and not is_snapshot and not is_video:
                        continue

                    filepath = os.path.join(month_path, f)

                    if filepath in active_paths:
                        continue

                    stat = os.stat(filepath)
                    if stat.st_size == 0:
                        continue

                    parts = f.split('_')
                    file_camera = parts[2].split('.')[0] if len(parts) > 2 else ''

                    if camera_safe_ids and file_camera not in camera_safe_ids:
                        continue

                    rel_path = os.path.join(month_dir, f)

                    rec_type_str = 'snapshot' if is_snapshot else 'video'

                    # What the reviewer made of this clip. The sidecar is
                    # written for everything it files, kept or shelved, so the
                    # judgement already existed for these — it was just never
                    # carried out to the page, and only the to_review tray
                    # could show why a clip was where it was. One small read
                    # per video; snapshots are not reviewed and are skipped.
                    verdict = clip_review.read_verdict(filepath) if is_video else None

                    results.append({
                        'filename': f,
                        'path': rel_path,
                        'type': rec_type_str,
                        'camera_ip': file_camera,
                        'size': stat.st_size,
                        'size_mb': round(stat.st_size / (1024 * 1024), 2),
                        'created': stat.st_mtime,
                        'url': f'/recordings/{rel_path}?type={rec_type_str}',
                        'thumbnail_url': f'/recordings/{rel_path}?type=snapshot' if is_snapshot else None,
                        'why': (verdict or {}).get('what') or '',
                        'certainty': (verdict or {}).get('certainty') or '',
                        # One word, so the page can be filtered by it. Empty
                        # for everything recorded before the reviewer named
                        # what it saw, which the page shows as "sin clasificar"
                        # rather than quietly folding it into some category.
                        'kind': (verdict or {}).get('kind') or '',
                        # Named, so the page can say "nobody looked at this
                        # one" rather than leaving a silent blank that reads
                        # like the model had nothing to say.
                        'review_error': (verdict or {}).get('error'),
                        'reviewed_at': (verdict or {}).get('reviewed_at'),
                        'review_model': (verdict or {}).get('model'),
                    })
                except Exception as e:
                    logger.warning(f"Error processing {f}: {e}")
                    continue
        except Exception as e:
            logger.warning(f"Error listing month {month_dir}: {e}")
            continue

    results.sort(key=lambda x: x['created'], reverse=True)
    return results[offset:offset + limit]


def _find_file(filename: str) -> Optional[str]:
    for month_dir in os.listdir(RECORDINGS_DIR):
        month_path = os.path.join(RECORDINGS_DIR, month_dir, filename)
        if os.path.exists(month_path):
            return month_path
    return None


def _resolve_path(rel_path: str) -> Optional[str]:
    full_path = os.path.join(RECORDINGS_DIR, rel_path)
    if os.path.exists(full_path):
        return full_path
    # Only a bare filename may be hunted for. If the caller named a folder and
    # the file is not in it, the file has moved on — a clip kept out of
    # to_review/ now lives in its month folder under the same name, and
    # searching by basename would resolve a stale "delete to_review/X" onto the
    # archived X the household just chose to keep.
    if os.path.dirname(rel_path):
        return None
    return _find_file(os.path.basename(rel_path))


# Directories a sweep is allowed to touch. Never `pending`, never `to_review`,
# never anything somebody adds later.
_MONTH_DIR = re.compile(r'^\d{4}-\d{2}$')

# How long after a clip started a snapshot can still belong to it.
#
# Only the *first* snapshot of a recording shares the clip's name -- both are
# written from the same motion event, with the same timestamp. After that
# `save_snapshot` fires on its own 5-second cooldown for as long as motion keeps
# being confirmed, so the rest are scattered through the clip under names of
# their own. Pairing has to be by time.
#
# The window is deliberately far wider than any clip this records, because of
# which way it errs: it decides what is *kept*, so being generous leaves a few
# stale JPEGs and being tight deletes a snapshot whose video is still on the
# shelf. Five minutes against a default clip of VIDEO_CLIP_DURATION seconds.
ORPHAN_WINDOW_S = int(os.environ.get('SNAPSHOT_ORPHAN_WINDOW', '300'))


def _name_parts(filename: str):
    """(datetime, camera) from a recording's name, or (None, None).

    `YYYY-MM-DD_HH-MM-SS_<camera>[_tag...]`, and the camera cannot contain an
    underscore -- `_safe_camera_id` turns anything that is not a letter or a
    digit into a dash -- so the first two fields are the stamp and the third is
    the camera. Same grammar `clip_review.living_tags_in` reads.

    A name this cannot parse returns (None, None), and every caller here treats
    that as "leave it alone". A sweep that deleted what it could not read would
    be a sweep that deletes anything a future rename produces.
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    parts = stem.split('_')
    if len(parts) < 3:
        return None, None
    try:
        when = datetime.strptime('_'.join(parts[:2]), '%Y-%m-%d_%H-%M-%S')
    except ValueError:
        return None, None
    return when, parts[2]


def _remove_inside(path: str) -> bool:
    """Delete `path`, but only if it really is inside the recordings tree."""
    real_dir = os.path.realpath(RECORDINGS_DIR)
    real = os.path.realpath(path)
    if real != real_dir and not real.startswith(real_dir + os.sep):
        logger.warning(f"Blocked deletion outside recordings dir: {path} -> {real}")
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def purge_orphan_snapshots(recordings_dir: str = None,
                           window_s: int = None) -> int:
    """Delete snapshots that no surviving clip can account for.

    Deleting a video used to leave its snapshots behind, and there are three to
    four of them per clip: after the household emptied the month folders on
    2026-09-03 the videos came to nothing and **31,451 JPEGs and 29 GB stayed**,
    which is as much disk as the video had been.

    `delete_recording` takes the one snapshot that shares the clip's name. This
    takes the rest, and it is written as a sweep rather than as more work on the
    delete path on purpose: it asks "is there any clip that could own this",
    which is the question that stays right when several clips overlap, when a
    clip is deleted by retention rather than by hand, and when a delete half
    finished. Deleting a clip's neighbours' snapshots is not recoverable and
    this cannot do it.

    Bounded the same way `clip_review.purge_old_recordings` is: month folders
    only, and a name it cannot parse is never touched.
    """
    root = RECORDINGS_DIR if recordings_dir is None else recordings_dir
    window = ORPHAN_WINDOW_S if window_s is None else window_s
    if window <= 0 or not os.path.isdir(root):
        return 0

    # A clip is written into pending/ and only reaches its month folder if the
    # reviewer keeps it -- but its snapshots go straight to the month folder
    # from the first frame. So a sweep that looked only at month folders would
    # delete the snapshots of the clip *being recorded right now*, and of every
    # clip sitting in to_review/ waiting for a person, within 60 seconds of
    # their being taken. The staging folders are never swept; they are read
    # here so what is still in them can still speak for its snapshots.
    staged = {}                           # camera -> when its clips started
    for tray in (clip_review.PENDING_DIRNAME, clip_review.REVIEW_DIRNAME):
        try:
            names = os.listdir(os.path.join(root, tray))
        except OSError:
            continue
        for name in names:
            if not name.endswith('.mp4'):
                continue
            when, camera = _name_parts(name)
            if when is not None:
                staged.setdefault(camera, []).append(when.timestamp())

    removed = 0
    for month in sorted(os.listdir(root)):
        folder = os.path.join(root, month)
        if not _MONTH_DIR.match(month) or not os.path.isdir(folder):
            continue
        try:
            names = os.listdir(folder)
        except OSError:
            continue

        starts = {c: list(w) for c, w in staged.items()}
        for name in names:
            if not name.endswith('.mp4'):
                continue
            when, camera = _name_parts(name)
            if when is not None:
                starts.setdefault(camera, []).append(when.timestamp())
        for whens in starts.values():
            whens.sort()

        for name in names:
            if not name.endswith('.jpg'):
                continue
            when, camera = _name_parts(name)
            if when is None:
                continue
            taken = when.timestamp()
            owned = starts.get(camera, ())
            i = bisect.bisect_right(owned, taken)
            # The clip that started most recently before this snapshot is the
            # only one that could hold it; anything earlier ended sooner.
            if i > 0 and taken - owned[i - 1] <= window:
                continue
            if _remove_inside(os.path.join(folder, name)):
                removed += 1
    if removed:
        logger.info(f"Deleted {removed} snapshot(s) whose recording is gone")
    return removed


def delete_recording(rel_path: str, rec_type: str) -> bool:
    try:
        filepath = _resolve_path(rel_path)
        if not filepath or not os.path.exists(filepath):
            return False

        # Refuse to delete anything that resolves outside the recordings tree.
        real_dir = os.path.realpath(RECORDINGS_DIR)
        real_file = os.path.realpath(filepath)
        if real_file != real_dir and not real_file.startswith(real_dir + os.sep):
            logger.warning(f"Blocked deletion outside recordings dir: {rel_path} -> {real_file}")
            return False

        os.remove(filepath)
        # The reviewer's verdict travels with the clip — api_keep_review
        # moves it, so deleting must take it too, or to_review/ silently
        # fills with sidecars for clips that are gone.
        sidecar = filepath + '.json'
        if os.path.exists(sidecar):
            real_sidecar = os.path.realpath(sidecar)
            if real_sidecar == real_dir or real_sidecar.startswith(real_dir + os.sep):
                os.remove(sidecar)
        # ...and so does the snapshot taken at the instant this clip started.
        # Both are written from the same motion event with the same timestamp,
        # so they share a name exactly, and that one is unambiguously this
        # clip's. The snapshots taken *during* the clip have names of their own
        # and are left to purge_orphan_snapshots, which can see whether some
        # other surviving clip owns them.
        if filepath.endswith('.mp4'):
            paired = filepath[:-4] + '.jpg'
            if os.path.exists(paired) and _remove_inside(paired):
                logger.info(f"Deleted the snapshot filed with it: "
                            f"{os.path.basename(paired)}")
        logger.info(f"Deleted recording: {rel_path}")
        return True
    except Exception as e:
        logger.error(f"Error deleting recording {rel_path}: {e}")
        return False


def get_recording_path(rel_path: str, rec_type: str) -> Optional[str]:
    full_path = os.path.join(RECORDINGS_DIR, rel_path)
    if os.path.exists(full_path):
        real = os.path.realpath(full_path)
        rec_dir = os.path.realpath(RECORDINGS_DIR)
        if not real.startswith(rec_dir + os.sep) and real != rec_dir:
            return None
        return full_path

    basename = os.path.basename(rel_path)
    filepath = _find_file(basename)
    if filepath:
        real = os.path.realpath(filepath)
        rec_dir = os.path.realpath(RECORDINGS_DIR)
        if not real.startswith(rec_dir + os.sep):
            return None
        return filepath
    return None


def get_default_duration() -> int:
    """Get default recording duration from constant"""
    return VIDEO_CLIP_DURATION


def get_default_pre_motion_duration() -> float:
    """Get default pre-motion duration from constant"""
    return PRE_MOTION_DURATION