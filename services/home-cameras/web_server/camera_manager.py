"""
Camera Manager Module - Handles camera connections and stream management
Uses PyAV for maximum compatibility with various camera RTSP implementations
"""

from typing import List, Dict, Optional
from enum import Enum
from dataclasses import dataclass
import logging
import traceback
import time
import asyncio
import uuid
import numpy as np
import cv2
import threading
import av

# Import H.264 encoder
try:
    from h264_encoder import H264Encoder, H264EncoderConfig, H264StreamGenerator
    H264_ENCODING_AVAILABLE = True
except ImportError:
    logging.warning("H.264 encoder not available. Install PyAV for H.264 encoding support.")
    H264_ENCODING_AVAILABLE = False
    H264Encoder = None
    H264EncoderConfig = None
    H264StreamGenerator = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class CameraConfig:
    """Configuration for a single camera - keyed by camera_id (UUID)"""
    camera_id: str = ""
    device_ip: str = ""
    username: str = ""
    password: str = ""
    port: int = 80
    stream_url_format: str = "rtsp://{username}:{password}@{host}:{port}/stream"
    stream_path: str = "/stream"
    video_codec: str = "h264"
    fps: int = 30
    width: int = 1920
    height: int = 1080
    rotation: int = 0
    motion_zones: list = None
    stream_type: str = "rtsp"
    name: str = ""

    def __post_init__(self):
        if not self.camera_id:
            self.camera_id = uuid.uuid4().hex
        if self.motion_zones is None:
            self.motion_zones = []

    @property
    def id(self) -> str:
        return self.camera_id

    @property
    def connection_string(self) -> str:
        if self.stream_url_format != "rtsp://{username}:{password}@{host}:{port}/stream":
            return self.stream_url_format.format(
                host=self.device_ip,
                username=self.username,
                password=self.password,
                port=self.port
            )
        return f"rtsp://{self.username}:{self.password}@{self.device_ip}:{self.port}{self.stream_path}"

    @property
    def stream_url(self) -> str:
        if self.stream_type == "mjpeg":
            return f"http://{self.device_ip}:{self.port}{self.stream_path}"
        else:
            return self.connection_string


class CameraState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    BUFFERING = "buffering"
    ERROR = "error"


class CameraManager:
    MAX_CAPTURE_FPS = 15

    def __init__(self):
        self.cameras: Dict[str, CameraConfig] = {}  # keyed by camera_id
        self.stream_threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self.frame_queues: Dict[str, asyncio.Queue] = {}
        self.latest_frames: Dict[str, np.ndarray] = {}
        self.latest_frames_small: Dict[str, np.ndarray] = {}
        self.frame_timestamps: Dict[str, float] = {}
        self.camera_states: Dict[str, CameraState] = {}
        self._running: Dict[str, bool] = {}
        self.processing_scale = 0.5
        self._containers: Dict[str, object] = {}  # camera_id -> av container
        self._caps: Dict[str, object] = {}  # camera_id -> cv2.VideoCapture

    def add_camera(self, config: CameraConfig) -> CameraConfig:
        if isinstance(config.port, str):
            config.port = int(config.port)
        if isinstance(config.fps, str):
            config.fps = int(config.fps)
        if isinstance(config.width, str):
            config.width = int(config.width)
        if isinstance(config.height, str):
            config.height = int(config.height)

        cam_id = config.camera_id
        with self._lock:
            self.cameras[cam_id] = config
            self.camera_states[cam_id] = CameraState.BUFFERING
            self._running[cam_id] = True

            try:
                self.frame_queues[cam_id] = asyncio.Queue(maxsize=30)

                stream_thread = threading.Thread(
                    target=self._process_stream_thread,
                    args=(config,),
                    daemon=True
                )
                stream_thread.start()
                self.stream_threads[cam_id] = stream_thread

                logger.info(f"Camera {cam_id} ({config.device_ip}) starting connection process with URL: {config.connection_string}")
                return config
            except Exception as e:
                logger.error(f"Failed to connect camera {cam_id}: {e}")
                self.camera_states[cam_id] = CameraState.ERROR
                return config

    def _process_stream_thread(self, config: CameraConfig) -> None:
        """Process video stream in a separate thread using PyAV with FPS limiting and auto-reconnect"""
        key = config.camera_id
        logger.info(f"Starting stream processing for {key} ({config.device_ip}), type: {config.stream_type}")

        container = None
        cap = None
        frame_count = 0
        last_log_time = time.time()
        consecutive_errors = 0
        max_consecutive_errors = 10
        reconnect_delay = 5
        max_reconnect_delay = 60
        min_frame_interval = 1.0 / self.MAX_CAPTURE_FPS

        while self._running.get(key, False):
            try:
                consecutive_errors = 0
                frame_count = 0
                last_log_time = time.time()

                stream_url = config.stream_url
                logger.info(f"Attempting to connect to camera {key} ({config.device_ip}) with URL: {stream_url}")

                if config.stream_type == "mjpeg":
                    cap = cv2.VideoCapture(stream_url)
                    if not cap.isOpened():
                        logger.error(f"Failed to open MJPEG stream for {key}")
                        self.camera_states[key] = CameraState.ERROR
                        time.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
                        continue

                    self._caps[key] = cap
                    logger.info(f"MJPEG stream opened for {key}")
                    self.camera_states[key] = CameraState.CONNECTED

                    while self._running.get(key, False):
                        loop_start = time.time()
                        try:
                            ret, frame = cap.read()
                            if not ret:
                                consecutive_errors += 1
                                if consecutive_errors >= max_consecutive_errors:
                                    logger.error(f"Too many consecutive errors for camera {key}, marking as disconnected")
                                    self.camera_states[key] = CameraState.DISCONNECTED
                                    break
                                time.sleep(0.1)
                                continue

                            consecutive_errors = 0
                            frame_count += 1

                            if config.rotation != 0:
                                frame = self._rotate_frame(frame, config.rotation)

                            self.latest_frames[key] = frame
                            self.frame_timestamps[key] = time.time()

                            height, width = frame.shape[:2]
                            new_size = (int(width * self.processing_scale), int(height * self.processing_scale))
                            self.latest_frames_small[key] = cv2.resize(
                                frame, new_size, interpolation=cv2.INTER_AREA
                            )

                            logger.debug(f"Frame stored for camera {key}")

                            if frame_count % 100 == 0:
                                current_time = time.time()
                                elapsed_time = current_time - last_log_time
                                fps = 100 / elapsed_time if elapsed_time > 0 else 0
                                logger.info(f"Camera {key} processed {frame_count} frames. Current FPS: {fps:.2f}")
                                last_log_time = current_time

                            if frame_count % 100 == 0:
                                self.camera_states[key] = CameraState.CONNECTED

                        except Exception as e:
                            consecutive_errors += 1
                            if consecutive_errors >= max_consecutive_errors:
                                logger.error(f"Too many consecutive errors for camera {key}, marking as disconnected")
                                self.camera_states[key] = CameraState.DISCONNECTED
                                break
                            time.sleep(0.1)
                            continue

                        elapsed = time.time() - loop_start
                        if elapsed < min_frame_interval:
                            time.sleep(max(0, min_frame_interval - elapsed))

                    if cap:
                        cap.release()
                        cap = None

                else:
                    try:
                        container = av.open(stream_url, mode='r', timeout=10000000)

                        self._containers[key] = container

                        video_stream = None
                        for stream in container.streams:
                            if stream.type == 'video':
                                video_stream = stream
                                break

                        if video_stream is None:
                            logger.error(f"No video stream found in RTSP stream for {key}")
                            self.camera_states[key] = CameraState.ERROR
                            if container:
                                container.close()
                            self._containers.pop(key, None)
                            time.sleep(reconnect_delay)
                            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
                            continue

                        logger.info(f"Video stream found for {key}: {video_stream.codec_context.name}, resolution: {video_stream.width}x{video_stream.height}")
                        logger.info(f"Starting frame capture loop for {key} (max {self.MAX_CAPTURE_FPS} FPS)")

                        self.camera_states[key] = CameraState.CONNECTED

                        while self._running.get(key, False):
                            loop_start = time.time()
                            try:
                                packet = None
                                for packet in container.demux(video_stream):
                                    break

                                if packet is None:
                                    consecutive_errors += 1
                                    if consecutive_errors >= max_consecutive_errors:
                                        logger.error(f"Too many consecutive errors for camera {key}, marking as disconnected")
                                        self.camera_states[key] = CameraState.DISCONNECTED
                                        break
                                    time.sleep(0.1)
                                    continue

                                frames = video_stream.decode(packet)

                                if not frames:
                                    continue

                                frame = frames[0]
                                consecutive_errors = 0
                                frame_count += 1

                                frame_array = self._av_frame_to_numpy(frame)

                                if frame_array is not None:
                                    if config.rotation != 0:
                                        frame_array = self._rotate_frame(frame_array, config.rotation)

                                    self.latest_frames[key] = frame_array
                                    self.frame_timestamps[key] = time.time()

                                    height, width = frame_array.shape[:2]
                                    new_size = (int(width * self.processing_scale), int(height * self.processing_scale))
                                    self.latest_frames_small[key] = cv2.resize(
                                        frame_array, new_size, interpolation=cv2.INTER_AREA
                                    )

                                    logger.debug(f"Frame stored for camera {key}")

                                    if frame_count % 100 == 0:
                                        current_time = time.time()
                                        elapsed_time = current_time - last_log_time
                                        fps = 100 / elapsed_time if elapsed_time > 0 else 0
                                        logger.info(f"Camera {key} processed {frame_count} frames. Current FPS: {fps:.2f}")
                                        last_log_time = current_time

                                    if frame_count % 100 == 0:
                                        self.camera_states[key] = CameraState.CONNECTED
                                else:
                                    logger.warning(f"Failed to convert frame from {key}")

                            except av.OSError as e:
                                logger.error(f"AV error decoding frame from {key}: {e}")
                                consecutive_errors += 1
                                if consecutive_errors >= max_consecutive_errors:
                                    self.camera_states[key] = CameraState.DISCONNECTED
                                    break
                                time.sleep(1)
                            except Exception as e:
                                logger.error(f"Error in frame capture for {key}: {e}")
                                consecutive_errors += 1
                                if consecutive_errors >= max_consecutive_errors:
                                    self.camera_states[key] = CameraState.DISCONNECTED
                                    break
                                time.sleep(1)

                            elapsed = time.time() - loop_start
                            if elapsed < min_frame_interval:
                                time.sleep(max(0, min_frame_interval - elapsed))

                    except Exception as e:
                        logger.error(f"Error opening RTSP stream for {key}: {e}")
                        self.camera_states[key] = CameraState.ERROR
                        if container:
                            container.close()
                        time.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
                        continue

            except Exception as e:
                logger.error(f"Error in stream processing for {key}: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                self.camera_states[key] = CameraState.ERROR

            finally:
                if container:
                    container.close()
                    self._containers.pop(key, None)
                if cap:
                    cap.release()
                    self._caps.pop(key, None)
                self.camera_states[key] = CameraState.DISCONNECTED
                logger.info(f"Stream processing stopped for {key}")

    def get_frame(self, key: str) -> Optional[np.ndarray]:
        """Get the latest frame from a camera"""
        return self.latest_frames.get(key)

    def get_processing_frame(self, key: str) -> Optional[np.ndarray]:
        """Get the latest processing frame (scaled down) from a camera"""
        return self.latest_frames_small.get(key)

    def get_camera_state(self, key: str) -> Optional[CameraState]:
        """Get the current state of a camera"""
        return self.camera_states.get(key)

    def get_all_cameras(self) -> Dict[str, CameraConfig]:
        """Get all configured cameras, ensuring all are CameraConfig objects"""
        to_convert = []
        for key, config in self.cameras.items():
            if isinstance(config, dict):
                to_convert.append((key, config))

        for key, config in to_convert:
            try:
                cam_config = CameraConfig(
                    camera_id=config.get('camera_id', key),
                    device_ip=config.get('device_ip', key),
                    username=config.get('username', ''),
                    password=config.get('password', ''),
                    port=int(config.get('port', 554)),
                    stream_path=config.get('stream_path', '/stream'),
                    fps=int(config.get('fps', 30)),
                    width=int(config.get('width', 1920)),
                    height=int(config.get('height', 1080)),
                    rotation=int(config.get('rotation', 0)),
                    stream_type=config.get('stream_type', 'rtsp'),
                    name=config.get('name', '')
                )
                self.cameras[cam_config.camera_id] = cam_config
                if key != cam_config.camera_id:
                    self.cameras.pop(key, None)
            except Exception as e:
                logger.error(f"Failed to convert camera {key} to CameraConfig: {e}")

        return self.cameras

    def get_camera_by_ip(self, ip: str) -> Optional[CameraConfig]:
        """Get camera config by IP address"""
        for config in self.cameras.values():
            if config.device_ip == ip:
                return config
        return None

    def _av_frame_to_numpy(self, frame: av.VideoFrame) -> Optional[np.ndarray]:
        """Convert PyAV VideoFrame to numpy array"""
        try:
            frame = frame.reformat(format='bgr24')
            return frame.to_ndarray()
        except Exception as e:
            logger.error(f"Error converting frame to numpy: {e}")
            return None

    def _rotate_frame(self, frame: np.ndarray, rotation: int) -> np.ndarray:
        """Rotate a frame by the specified degrees"""
        if rotation == 0:
            return frame
        elif rotation == 90:
            return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif rotation == 180:
            return cv2.rotate(frame, cv2.ROTATE_180)
        elif rotation == 270:
            return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return frame
