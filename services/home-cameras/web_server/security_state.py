"""Presence per camera, from tracks rather than frames, and how it reaches Home Assistant.

`homecameras/motion/{cam}` carries what YOLO saw in one frame -- every
detection at the 0.20 floor, before the tracker has had a say. That is the
right feed for an overlay and the wrong one for a security system: at that
floor an empty patio at night has produced `person` at 0.31 and a pergola as
two `bed`, and one such frame is all it took to notify somebody.

This module reads the tracker instead. A class is *present* on a camera when
a confirmed track of it (seen `confirm_frames` times) was matched within
`PRESENCE_HOLD_SECONDS`; it is *active* when that track has moved lately, and
*stationary* otherwise (a parked car, a hoodie on a chair, somebody sitting).
An *arrival* is a track reaching confirmation, a *departure* is presence
lapsing. Those are the events worth an automation; a frame is not.

Detection only runs during motion and for ten seconds after it, so a person
sitting perfectly still stops being tracked and presence lapses after the
hold. For a security system that is the right default -- an intrusion moves --
and it is the same trade Frigate makes with motion-gated detection.

Two things go out over MQTT, under the grammar in docs/mqtt-conventions.md:

    homecameras/security/{cam}/state     the whole presence picture, on change
                                         and every PRESENCE_REPUBLISH_SECONDS
    homecameras/security/{cam}/event     one line per arrival or departure

and, so Home Assistant needs no configuration at all, MQTT discovery under
`homeassistant/binary_sensor/...` -- one `occupancy` sensor per camera for
people and one for any tracked class, both reading the state topic. Discovery
is retained and re-announced periodically because the broker runs
`persistence false`: a broker restart forgets retained messages, and a sensor
HA forgot looks exactly like a quiet garden.
"""

import json
import logging
import os
import re
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PRESENCE_HOLD_SECONDS = float(os.environ.get('SECURITY_PRESENCE_HOLD', '30.0'))
PRESENCE_REPUBLISH_SECONDS = float(os.environ.get('SECURITY_REPUBLISH', '120.0'))
DISCOVERY_PREFIX = os.environ.get('HA_DISCOVERY_PREFIX', 'homeassistant')
DISCOVERY_REANNOUNCE_SECONDS = float(os.environ.get('SECURITY_DISCOVERY_REANNOUNCE', '600.0'))

# What the security picture is about. Everything else YOLO can name is a
# detection, not a presence; a `bed` on the patio never becomes a sensor.
KIND_OF_CLASS = {
    'person': 'person',
    'car': 'vehicle', 'truck': 'vehicle', 'bus': 'vehicle',
    'motorcycle': 'vehicle', 'bicycle': 'vehicle',
    'cat': 'animal', 'dog': 'animal',
}
KINDS = ('person', 'vehicle', 'animal')


def _slug(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', str(value).lower()).strip('_') or 'camera'


class CameraPresence:
    """The presence state machine for one camera."""

    def __init__(self, camera_id: str, hold_seconds: float = PRESENCE_HOLD_SECONDS):
        self.camera_id = camera_id
        self.hold = hold_seconds
        self.last_seen: Dict[str, float] = {}       # kind -> last matched
        self.since: Dict[str, float] = {}           # kind -> when presence began
        self.present: Dict[str, bool] = {k: False for k in KINDS}
        self.active: Dict[str, int] = {k: 0 for k in KINDS}
        self.stationary: Dict[str, int] = {k: 0 for k in KINDS}
        self._known_tracks: set = set()
        self.updated_at: float = 0.0

    def update(self, tracks: List[dict], now: float) -> List[dict]:
        """Fold the tracker's confirmed tracks in; return the events this caused."""
        events: List[dict] = []
        active = {k: 0 for k in KINDS}
        stationary = {k: 0 for k in KINDS}
        seen_ids = set()
        for t in tracks:
            kind = KIND_OF_CLASS.get(t.get('class_name'))
            if kind is None:
                continue
            seen_ids.add(t['track_id'])
            self.last_seen[kind] = max(self.last_seen.get(kind, 0.0), float(t.get('last_seen', now)))
            if t.get('stationary'):
                stationary[kind] += 1
            else:
                active[kind] += 1
            if t['track_id'] not in self._known_tracks:
                self._known_tracks.add(t['track_id'])
                events.append({'kind': kind, 'event': 'arrived', 'class': t.get('class_name'),
                               'track_id': t['track_id'], 'moved_px': t.get('moved_px', 0)})
        # Tracks the tracker dropped are forgotten here too, so an id reused
        # after a reset reads as a new arrival, which it is.
        self._known_tracks &= seen_ids | {i for i in self._known_tracks
                                          if any(t['track_id'] == i for t in tracks)}
        for kind in KINDS:
            was = self.present[kind]
            is_now = (now - self.last_seen.get(kind, -1e9)) <= self.hold
            if is_now and not was:
                self.since[kind] = now
            if was and not is_now:
                events.append({'kind': kind, 'event': 'left',
                               'after_seconds': round(now - self.since.get(kind, now), 1)})
                self.since.pop(kind, None)
            self.present[kind] = is_now
        self.active, self.stationary = active, stationary
        self.updated_at = now
        return events

    def payload(self, now: float) -> dict:
        kinds = {}
        for kind in KINDS:
            last = self.last_seen.get(kind)
            kinds[kind] = {
                'present': self.present[kind],
                'active': self.active[kind],
                'stationary': self.stationary[kind],
                'since': self.since.get(kind),
                'seconds_since_seen': round(now - last, 1) if last else None,
            }
        return {
            'camera': self.camera_id,
            'any_present': any(self.present.values()),
            'hold_seconds': self.hold,
            'timestamp': now,
            **kinds,
        }


class SecurityPublisher:
    """Keeps one CameraPresence per camera and tells MQTT and HA about changes."""

    def __init__(self, mqtt_getter, hold_seconds: float = PRESENCE_HOLD_SECONDS,
                 republish_seconds: float = PRESENCE_REPUBLISH_SECONDS,
                 discovery_prefix: str = DISCOVERY_PREFIX):
        self._mqtt = mqtt_getter
        self.hold = hold_seconds
        self.republish = republish_seconds
        self.discovery_prefix = discovery_prefix
        self.cameras: Dict[str, CameraPresence] = {}
        self._last_published: Dict[str, float] = {}
        self._last_payload: Dict[str, dict] = {}
        self._announced: Dict[str, float] = {}
        self.names: Dict[str, str] = {}

    def state_of(self, camera_id: str) -> Optional[CameraPresence]:
        return self.cameras.get(camera_id)

    def observe(self, camera_id: str, tracks: List[dict], now: float,
                camera_name: Optional[str] = None) -> List[dict]:
        """Called once per processed frame with the tracker's confirmed tracks."""
        if camera_name:
            self.names[camera_id] = camera_name
        state = self.cameras.get(camera_id)
        if state is None:
            state = self.cameras[camera_id] = CameraPresence(camera_id, self.hold)
            logger.info("security: tracking presence on %s (%s), hold %.0fs",
                        camera_id, self.names.get(camera_id, '?'), self.hold)
        events = state.update(tracks, now)
        self._announce_if_due(camera_id, now)
        payload = state.payload(now)
        comparable = {k: v for k, v in payload.items() if k != 'timestamp'}
        for kind in KINDS:
            comparable[kind] = dict(comparable[kind], seconds_since_seen=None)
        due = now - self._last_published.get(camera_id, 0.0) >= self.republish
        if events or due or comparable != self._last_payload.get(camera_id):
            # Retained: a sensor HA has just discovered reads the current
            # picture at once instead of `unavailable` until the next change.
            self._publish(f"security/{camera_id}/state", payload, retain=True)
            self._last_published[camera_id] = now
            self._last_payload[camera_id] = comparable
        for event in events:
            self._publish(f"security/{camera_id}/event",
                          {'camera': camera_id, 'timestamp': now, **event})
            logger.info("security %s: %s %s", camera_id, event['kind'], event['event'])
        return events

    # --- MQTT ----------------------------------------------------------------

    def _publish(self, suffix: str, payload: dict, retain: bool = False) -> None:
        client = self._mqtt() if callable(self._mqtt) else self._mqtt
        if client is None:
            return
        try:
            client.publish(suffix, payload, retain=retain)
        except TypeError:
            client.publish(suffix, payload)
        except Exception as e:  # instrumentation never takes the detector down
            logger.warning("security publish %s failed: %s", suffix, e)

    def discovery_configs(self, camera_id: str) -> List[tuple]:
        """The HA discovery messages for one camera: (topic, payload)."""
        client = self._mqtt() if callable(self._mqtt) else self._mqtt
        prefix = getattr(client, 'topic_prefix', 'homecameras')
        name = self.names.get(camera_id) or camera_id
        slug = _slug(camera_id)
        state_topic = f"{prefix}/security/{camera_id}/state"
        device = {
            'identifiers': [f"homecameras_{slug}"],
            'name': f"Camera {name}",
            'manufacturer': 'home-cameras',
            'model': 'YOLO tracker',
        }
        out = []
        for kind, label, template in (
            ('person', 'person', "{{ 'ON' if value_json.person.present else 'OFF' }}"),
            ('any', 'activity', "{{ 'ON' if value_json.any_present else 'OFF' }}"),
        ):
            uid = f"homecameras_{slug}_{kind}"
            out.append((
                f"{self.discovery_prefix}/binary_sensor/{uid}/config",
                {
                    # `has_entity_name`: HA composes "Camera Living person"
                    # from the device name, instead of "Living Living person".
                    'name': label,
                    'has_entity_name': True,
                    'unique_id': uid,
                    'state_topic': state_topic,
                    'value_template': template,
                    'device_class': 'occupancy',
                    'json_attributes_topic': state_topic,
                    # If the cameras stop publishing, the sensor goes unknown
                    # rather than staying ON forever. Twice the republish period.
                    'expire_after': int(self.republish * 2 + self.hold),
                    'device': device,
                },
            ))
        return out

    def _announce_if_due(self, camera_id: str, now: float) -> None:
        last = self._announced.get(camera_id)
        if last is not None and now - last < DISCOVERY_REANNOUNCE_SECONDS:
            return
        self._announced[camera_id] = now
        client = self._mqtt() if callable(self._mqtt) else self._mqtt
        if client is None:
            return
        for topic, payload in self.discovery_configs(camera_id):
            try:
                client.publish_raw(topic, json.dumps(payload), retain=True)
            except AttributeError:
                logger.warning("mqtt client has no publish_raw; HA discovery skipped")
                return
            except Exception as e:
                logger.warning("HA discovery for %s failed: %s", camera_id, e)
                return
        logger.info("security: HA discovery announced for %s", camera_id)
