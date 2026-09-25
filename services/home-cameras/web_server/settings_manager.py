"""
Settings Manager Module
Handles persistent storage of camera configurations and admin settings
"""

import json
import os
import hashlib
import secrets
import time
import uuid
from typing import Dict, Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class AdminConfig:
    """Admin configuration for settings access"""
    password_hash: str
    salt: str


_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

def _resolve_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(_PROJECT_ROOT, path))


class SettingsManager:
    """
    Manages persistent storage of camera configurations and admin settings
    """
    
    DEFAULT_RECORDING_DURATION = 10
    DEFAULT_PRE_MOTION_DURATION = 5

    # The named windows a camera can follow instead of the house one, so that
    # "the cameras outside" and "the cameras inside" are two settings rather
    # than a pair of hours copied onto every camera by hand — and changing
    # either changes every camera that follows it.
    #
    # Outside watches continuously. Inside is 23 to 6, which crosses midnight:
    # an end before the start means overnight, the same rule the rest of this
    # module uses. Both are editable in the settings page; these are only where
    # a house that has never touched them starts.
    DEFAULT_RECORDING_PRESETS = {
        'outside': {'start_hour': 0, 'end_hour': 24},
        'inside': {'start_hour': 23, 'end_hour': 6},
    }
    
    def __init__(self, config_dir: str = 'config'):
        self.config_dir = _resolve_path(config_dir)
        self.cameras_file = os.path.join(self.config_dir, 'cameras.json')
        self.admin_file = os.path.join(self.config_dir, 'admin.json')
        self.global_settings_file = os.path.join(self.config_dir, 'global_settings.json')
        self._ensure_config_dir()
        # TTL-based cache for camera configs to avoid per-frame disk reads
        self._cameras_cache: Optional[Dict[str, dict]] = None
        self._cameras_cache_time: float = 0.0
        self._cameras_cache_ttl: float = 10.0  # seconds
        # TTL-based cache for global settings to avoid per-frame disk reads
        self._global_cache: Optional[dict] = None
        self._global_cache_time: float = 0.0
        self._global_cache_ttl: float = 5.0  # seconds
        self._registry_cache: Optional[Dict[str, dict]] = None
        self._registry_cache_time: float = 0.0
        
    def _ensure_config_dir(self):
        """Ensure config directory exists"""
        if not os.path.exists(self.config_dir):
            os.makedirs(self.config_dir)
            logger.info(f"Created config directory: {self.config_dir}")
            
    def _load_json(self, filepath: str, default: dict) -> dict:
        """Load JSON file with default fallback"""
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Error loading {filepath}: {e}")
        return default
        
    def _save_json(self, filepath: str, data: dict) -> bool:
        """Save data to JSON file"""
        try:
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=4)
            return True
        except Exception as e:
            logger.error(f"Error saving {filepath}: {e}")
            return False
    
    def _migrate_cameras_dict(self, raw: dict, filepath: str = None) -> Dict[str, dict]:
        """Migrate old-format cameras dict (IP-keyed, no camera_id) to new format (camera_id-keyed).

        `filepath` is the file `raw` came from, and the migration is written
        back to it. It used to be hardcoded to `cameras_file`, which meant that
        migrating the *registry* overlay saved the registry's records over the
        camera list — one read of a legacy registry file and every hand-added
        camera was gone.
        """
        migrated = False
        new_cameras = {}
        for key, config in raw.items():
            if not isinstance(config, dict):
                continue
            if 'camera_id' not in config or not config['camera_id']:
                config['camera_id'] = uuid.uuid4().hex
                migrated = True
            cid = config['camera_id']
            if key != cid and cid not in new_cameras:
                migrated = True
            new_cameras[cid] = config
        if migrated:
            self._save_json(filepath or self.cameras_file, new_cameras)
        return new_cameras

    # Camera configuration methods (keyed by camera_id)
    def get_cameras(self) -> Dict[str, dict]:
        """Load all camera configurations (cached for up to 10 seconds).
        Auto-migrates old-format (IP-keyed, no camera_id) to new format on first read."""
        now = time.time()
        if self._cameras_cache is not None and (now - self._cameras_cache_time) < self._cameras_cache_ttl:
            return self._cameras_cache
        raw = self._load_json(self.cameras_file, {})
        self._cameras_cache = self._migrate_cameras_dict(raw, self.cameras_file)
        self._cameras_cache_time = now
        return self._cameras_cache

    def save_cameras(self, cameras: Dict[str, dict]) -> bool:
        """Save all camera configurations (keyed by camera_id) and invalidate cache"""
        result = self._save_json(self.cameras_file, cameras)
        self._cameras_cache = None  # Invalidate cache on write
        return result

    def _resolve_key(self, key: str, cameras: Dict[str, dict]) -> Optional[str]:
        """Resolve a camera_id from either a camera_id or device_ip key."""
        if key in cameras:
            return key
        for cid, config in cameras.items():
            if config.get('device_ip') == key:
                return cid
        return None

    def add_camera(self, key: str, config: dict) -> bool:
        """Add a camera configuration (keyed by camera_id if present, else key)"""
        cameras = self.get_cameras()
        cid = config.get('camera_id', key)
        if not cid:
            cid = key
        cameras[cid] = config
        return self.save_cameras(cameras)

    def remove_camera(self, key: str) -> bool:
        """Remove a camera configuration by camera_id or device_ip"""
        cameras = self.get_cameras()
        cid = self._resolve_key(key, cameras)
        if cid:
            del cameras[cid]
            return self.save_cameras(cameras)
        return False

    # Registry camera settings (persisted overlay for dynamically-registered cameras)
    @property
    def _registry_settings_file(self):
        return os.path.join(self.config_dir, 'registry_camera_settings.json')

    def get_registry_camera_settings(self) -> Dict[str, dict]:
        """Load persisted settings for all registry cameras (cached for up to 10 seconds).
        Auto-migrates old-format (IP-keyed) to new format on first read."""
        now = time.time()
        if self._registry_cache is not None and (now - self._registry_cache_time) < self._cameras_cache_ttl:
            return self._registry_cache
        raw = self._load_json(self._registry_settings_file, {})
        self._registry_cache = self._migrate_cameras_dict(raw, self._registry_settings_file)
        self._registry_cache_time = now
        return self._registry_cache

    def save_registry_camera_settings(self, settings: Dict[str, dict]) -> bool:
        """Save all registry camera settings and invalidate cache"""
        result = self._save_json(self._registry_settings_file, settings)
        self._registry_cache = None  # Invalidate cache on write
        return result

    def get_registry_camera_setting(self, key: str) -> dict:
        """Return saved settings for one registry camera (by camera_id or device_ip), or {} if none"""
        settings = self.get_registry_camera_settings()
        cid = self._resolve_key(key, settings)
        if cid:
            return settings[cid]
        return {}

    def get_camera_setting(self, key: str) -> dict:
        """This camera's saved settings, from whichever of the two stores holds it.

        A camera added by hand lives in `cameras.json`; a board that registered
        itself lives in the registry overlay. A reader that knows only one of
        them reports "no override" for half the house, and the symptom is the
        worst kind: the setting is accepted, echoed back, and then ignored.
        """
        cameras = self.get_cameras()
        cid = self._resolve_key(key, cameras)
        if cid:
            return cameras[cid]
        return self.get_registry_camera_setting(key)

    def update_registry_camera_setting(self, key: str, data: dict,
                                       replace: bool = False) -> bool:
        """Merge data into saved settings for one registry camera (by camera_id
        or device_ip).

        `replace=True` writes `data` as the whole record instead of merging it.
        A merge cannot express "go back to the default" — the key it would have
        to remove is exactly the one it keeps — so anything that clears a
        setting has to say so.
        """
        settings = self.get_registry_camera_settings()
        cid = self._resolve_key(key, settings)
        if not cid:
            cid = data.get('camera_id', key)
        if replace:
            # `camera_id` is the record's identity, not one of its settings:
            # `get_registry_camera_settings` re-keys any record that lacks one
            # to a freshly generated id, which orphans everything written under
            # the old key. A replace that dropped it looked like a save that
            # silently did nothing.
            record = dict(data)
            record.setdefault('camera_id', settings.get(cid, {}).get('camera_id', cid))
            settings[cid] = record
        else:
            settings[cid] = {**settings.get(cid, {}), **data}
        result = self.save_registry_camera_settings(settings)
        self._registry_cache = None  # Invalidate cache on write
        return result

    def remove_registry_camera_setting(self, key: str) -> bool:
        """Remove saved settings for one registry camera (by camera_id or device_ip)"""
        settings = self.get_registry_camera_settings()
        cid = self._resolve_key(key, settings)
        if cid:
            del settings[cid]
            result = self.save_registry_camera_settings(settings)
            self._registry_cache = None  # Invalidate cache on write
            return result
        return False

    # Admin password methods
    def get_admin_config(self) -> Optional[AdminConfig]:
        """Load admin configuration"""
        data = self._load_json(self.admin_file, {})
        if data and 'password_hash' in data and 'salt' in data:
            return AdminConfig(
                password_hash=data['password_hash'],
                salt=data['salt']
            )
        return None
    
    def set_admin_password(self, password: str) -> bool:
        """Set admin password with salted hash"""
        salt = secrets.token_hex(16)
        password_hash = hashlib.sha256((password + salt).encode()).hexdigest()
        
        config = {
            'password_hash': password_hash,
            'salt': salt
        }
        return self._save_json(self.admin_file, config)
    
    def verify_password(self, password: str) -> bool:
        """Verify admin password"""
        admin_config = self.get_admin_config()
        if not admin_config:
            return False
            
        password_hash = hashlib.sha256((password + admin_config.salt).encode()).hexdigest()
        return secrets.compare_digest(password_hash, admin_config.password_hash)
    
    def initialize_default_admin(self, password: str = 'admin') -> bool:
        """Initialize with default admin password"""
        return self.set_admin_password(password)

    # Global settings for recording behavior
    def get_global_settings(self) -> dict:
        """Load global settings (cached with TTL)"""
        now = time.time()
        if self._global_cache is not None and (now - self._global_cache_time) < self._global_cache_ttl:
            return self._global_cache
        defaults = {
            'recording_duration': self.DEFAULT_RECORDING_DURATION,
            'pre_motion_duration': self.DEFAULT_PRE_MOTION_DURATION,
            'recording_enabled': True,
            'recording_start_hour': 0,
            'recording_end_hour': 24,
            'recording_presets': {n: dict(w) for n, w
                                  in self.DEFAULT_RECORDING_PRESETS.items()},
            'mqtt_broker_host': os.environ.get('MQTT_BROKER_HOST', 'mqtt.home'),
            'mqtt_broker_port': int(os.environ.get('MQTT_BROKER_PORT', '1883')),
            'mqtt_username': os.environ.get('MQTT_USERNAME', ''),
            'mqtt_password': os.environ.get('MQTT_PASSWORD', ''),
        }
        self._global_cache = {**defaults, **self._load_json(self.global_settings_file, {})}
        self._global_cache_time = now
        return self._global_cache

    def save_global_settings(self, settings: dict) -> bool:
        self._global_cache = None  # Invalidate cache on save
        return self._save_json(self.global_settings_file, settings)

    def is_recording_enabled(self, key: str = None) -> bool:
        """Whether this camera should be recording right now.

        The window used to be one setting for the whole house, which is the
        wrong shape for it: the patio is worth recording overnight and the
        living room is not, and with a single window you either record the
        living room all night or lose the patio. A camera that sets neither
        hour falls back to the global pair, so nothing changes for a house that
        never touches this.
        """
        settings = self.get_global_settings()

        if not settings.get('recording_enabled', True):
            return False

        start_hour = settings.get('recording_start_hour', 0)
        end_hour = settings.get('recording_end_hour', 24)
        if key:
            per_camera = self.get_camera_setting(key) or {}
            if not per_camera.get('recording_enabled', True):
                return False
            # Both or neither: half a window is not a window, and silently
            # pairing one camera's start with the house's end is the kind of
            # surprise that shows up as a missing night of recordings.
            #
            # Hours the camera sets itself beat a preset, and a preset beats
            # the house. That order is the useful one: a camera can sit on
            # "inside" with the rest of them and still be given its own hours
            # for a week without being taken off the preset and forgotten.
            if (per_camera.get('recording_start_hour') is not None
                    and per_camera.get('recording_end_hour') is not None):
                start_hour = per_camera['recording_start_hour']
                end_hour = per_camera['recording_end_hour']
            elif per_camera.get('recording_preset'):
                preset = self.get_recording_presets().get(per_camera['recording_preset'])
                if preset:
                    start_hour = preset.get('start_hour', start_hour)
                    end_hour = preset.get('end_hour', end_hour)
                else:
                    # Named a preset that no longer exists. Following the house
                    # silently is how a camera ends up recording hours nobody
                    # chose, so say so.
                    logger.warning(
                        "Camera %s follows recording preset %r, which does not "
                        "exist; falling back to the house window %s-%s",
                        key, per_camera['recording_preset'], start_hour, end_hour)
        
        if start_hour == end_hour:
            return True
        
        from datetime import datetime
        from recording import get_timezone
        now = datetime.now(get_timezone())
        current_hour = now.hour
        
        if start_hour < end_hour:
            result = start_hour <= current_hour < end_hour
        else:
            result = current_hour >= start_hour or current_hour < end_hour
        logger.debug(f"Recording enabled: {result} (camera={key or 'global'}, "
                     f"hour={current_hour}, window={start_hour}-{end_hour})")
        return result

    def get_camera_recording_window(self, key: str) -> dict:
        """This camera's own window, or an empty dict when it follows the house."""
        per_camera = self.get_camera_setting(key) or {}
        return {k: per_camera[k] for k in
                ('recording_enabled', 'recording_start_hour', 'recording_end_hour',
                 'recording_preset')
                if k in per_camera}

    def get_recording_presets(self) -> dict:
        """The named windows a camera can follow instead of the house one.

        Merged per preset rather than taken wholesale: a settings file that had
        only ever customised `inside` would otherwise come back without
        `outside` at all, and a camera pointing at a preset that no longer
        exists has no window to follow — which resolves to the house window and
        looks, from the outside, exactly like a setting that was ignored.
        """
        stored = self.get_global_settings().get('recording_presets') or {}
        merged = {name: {**window, **(stored.get(name) or {})}
                  for name, window in self.DEFAULT_RECORDING_PRESETS.items()}
        # Anything the house has added by hand is kept as it is.
        for name, window in stored.items():
            if name not in merged and isinstance(window, dict):
                merged[name] = dict(window)
        return merged

    def validate_recording_preset(self, value):
        """Return `value` as a preset name, or raise ValueError saying why not.

        `None` is valid and means "do not follow a preset". Refusing an unknown
        name matters more than it looks: a camera silently pointed at a preset
        that does not exist falls back to the house window, records the wrong
        hours, and shows a setting on screen that nothing acts on.
        """
        if value is None:
            return None
        presets = self.get_recording_presets()
        if value not in presets:
            raise ValueError(
                f"«{value}» no es uno de los horarios: "
                f"{', '.join(sorted(presets))}")
        return value

    def set_recording_preset(self, name: str, start_hour: int, end_hour: int) -> bool:
        """Set the hours of one named preset, leaving the others alone."""
        self.validate_recording_hour('recording_start_hour', start_hour)
        self.validate_recording_hour('recording_end_hour', end_hour)
        settings = dict(self.get_global_settings())
        presets = self.get_recording_presets()
        presets[name] = {'start_hour': start_hour, 'end_hour': end_hour}
        settings['recording_presets'] = presets
        return self.save_global_settings(settings)

    # One place that decides what an hour is, so the HTTP route and this
    # module cannot drift into accepting different things. The message is what
    # the family reads, so it names the box on screen, not the JSON field.
    HOUR_LABELS = {'recording_start_hour': 'La hora de inicio',
                   'recording_end_hour': 'The end hour'}

    @classmethod
    def validate_recording_hour(cls, field: str, value):
        """Return `value` as an hour, or raise ValueError saying why it is not one.

        The start hour stops at 23 while the end hour reaches 24: a window
        starting at 24 can never contain the current hour, so `24 → 0` reads
        like a full night and silently means "never record".
        """
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{cls.HOUR_LABELS.get(field, field)} has to be a whole number")
        top = 23 if field == 'recording_start_hour' else 24
        if not 0 <= value <= top:
            raise ValueError(f"{cls.HOUR_LABELS.get(field, field)} tiene que estar entre 0 y {top}")
        return value

    def set_camera_recording_window(self, key: str, start_hour=None, end_hour=None,
                                    enabled=None, preset=None) -> bool:
        """Set or clear this camera's window. `None` for an hour means "follow
        the house", which is how a camera goes back to the default rather than
        being stuck with whatever it was last given.

        Writes to whichever store holds the camera. A window written to the
        other one is saved, shown back, and then never consulted.
        """
        for field, value in (('recording_start_hour', start_hour),
                             ('recording_end_hour', end_hour)):
            if value is not None:
                self.validate_recording_hour(field, value)
        self.validate_recording_preset(preset)
        return self.set_camera_setting(key, {
            'recording_start_hour': start_hour,
            'recording_end_hour': end_hour,
            'recording_enabled': enabled,
            'recording_preset': preset,
        })

    def get_recording_duration(self) -> int:
        """Get global recording duration in seconds"""
        return self.get_global_settings().get('recording_duration', self.DEFAULT_RECORDING_DURATION)

    def get_pre_motion_duration(self) -> float:
        """Get global pre-motion recording duration in seconds"""
        return self.get_global_settings().get('pre_motion_duration', self.DEFAULT_PRE_MOTION_DURATION)

    # Camera-specific recording duration override
    def get_camera_recording_duration(self, key: str) -> Optional[int]:
        """Get camera-specific recording duration override, or None to use global.

        Through `get_camera_setting`, like the window: a camera added by hand
        keeps its settings in cameras.json, and reading only the registry
        overlay meant its override was written and then never read — the camera
        quietly fell back to the house duration. The same bug the recording
        window had, in the field next to it.
        """
        camera_settings = self.get_camera_setting(key)
        return camera_settings.get('recording_duration')

    def set_camera_setting(self, key: str, values: dict) -> bool:
        """Merge `values` into this camera's settings, in whichever store holds
        it. A `None` value clears the field.

        The counterpart to `get_camera_setting`, and the reason it exists: the
        pair kept drifting a field at a time. The window's setter resolved the
        store by hand while the duration's wrote registry-only, so on a
        hand-added camera the duration was saved somewhere its own getter never
        looked — the same bug, in the field next door. Anything new should
        reach for this rather than pick a store.
        """
        cameras = self.get_cameras()
        cid = self._resolve_key(key, cameras)
        current = dict(cameras[cid] if cid else (self.get_registry_camera_setting(key) or {}))
        for field, value in values.items():
            if value is None:
                current.pop(field, None)
            else:
                current[field] = value
        if cid:
            # Whole-record, both branches: the point of a clear is the key that
            # is no longer there, and a merge would put it straight back.
            cameras[cid] = current
            return self.save_cameras(cameras)
        return self.update_registry_camera_setting(key, current, replace=True)

    def set_camera_recording_duration(self, key: str, duration: Optional[int]) -> bool:
        """Set camera-specific recording duration override (None to use global).

        Through `set_camera_setting`, so it writes where its getter reads.
        """
        return self.set_camera_setting(key, {'recording_duration': duration})