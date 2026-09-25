"""
MQTT Client Module - Publishes motion and object detection events via MQTT
"""

import json
import logging
import os
import socket
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

MQTT_AVAILABLE = False
mqtt_client = None

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
    logger.info("paho-mqtt available for MQTT publishing")
except ImportError:
    logger.info("paho-mqtt not available - MQTT publishing disabled")


class MQTTClient:
    def __init__(self, broker_host: str = 'mqtt.home', broker_port: int = 1883,
                 username: Optional[str] = None, password: Optional[str] = None,
                 topic_prefix: str = 'homecameras'):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.username = username
        self.password = password
        self.topic_prefix = topic_prefix
        self.client = None
        self.connected = False
        self._lock = threading.Lock()
        self._connect()

    def _connect(self):
        if not MQTT_AVAILABLE:
            logger.warning("MQTT not available, skipping connection")
            return
        try:
            # The id must be unique per instance. A fixed one means any second
            # deployment (a stale Jenkins deploy on another agent, a test run)
            # kicks the real server off the broker on every connect, and the two
            # flap against each other forever with rc=7.
            client_id = os.environ.get('MQTT_CLIENT_ID') or f"homecameras-{socket.gethostname()}"
            self.client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
            if self.username and self.password:
                self.client.username_pw_set(self.username, self.password)
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.connect_async(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()
            logger.info(f"MQTT client '{client_id}' connecting to {self.broker_host}:{self.broker_port}")
        except Exception as e:
            logger.error(f"Failed to connect MQTT client: {e}")

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            logger.info(f"MQTT connected to {self.broker_host}:{self.broker_port}")
        else:
            logger.error(f"MQTT connection failed with code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            logger.warning(f"MQTT unexpected disconnect (rc={rc}), will auto-reconnect")

    def publish(self, topic_suffix: str, payload: dict, retain: bool = False):
        if not MQTT_AVAILABLE or self.client is None:
            return
        self.publish_raw(f"{self.topic_prefix}/{topic_suffix}", json.dumps(payload), retain=retain)

    def publish_raw(self, topic: str, payload: str, retain: bool = False):
        """A full topic, outside the prefix: Home Assistant discovery lives
        under `homeassistant/...` and is retained so a sensor survives HA
        restarting (the broker forgetting it is handled by re-announcing)."""
        if not MQTT_AVAILABLE or self.client is None:
            return
        try:
            with self._lock:
                self.client.publish(topic, payload, qos=1, retain=retain)
            logger.debug(f"MQTT published to {topic}")
        except Exception as e:
            logger.error(f"MQTT publish error: {e}")

    def publish_motion(self, camera_ip: str, zone_name: str, confidence: float,
                       detected_objects: list, object_counts: dict):
        objects_tags = list(set(obj['class_name'] for obj in detected_objects)) if detected_objects else []
        payload = {
            'camera': camera_ip,
            'zone': zone_name,
            'confidence': round(confidence, 4),
            'objects': detected_objects,
            'object_counts': object_counts,
            'object_tags': objects_tags,
            'has_objects': len(detected_objects) > 0,
            'timestamp': time.time()
        }
        self.publish(f"motion/{camera_ip}", payload)
        if detected_objects:
            self.publish(f"motion/{camera_ip}/objects", {
                'camera': camera_ip,
                'tags': objects_tags,
                'counts': object_counts,
                'timestamp': time.time()
            })

    def publish_recording(self, camera_ip: str, filename: str, event_type: str,
                          detected_objects: list, object_tags: list):
        payload = {
            'camera': camera_ip,
            'filename': filename,
            'event_type': event_type,
            'object_tags': object_tags,
            'has_objects': len(detected_objects) > 0,
            'timestamp': time.time()
        }
        self.publish(f"recording/{camera_ip}", payload)

    def disconnect(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False


_mqtt_instance: Optional[MQTTClient] = None


def init_mqtt(broker_host: str = 'mqtt.home', broker_port: int = 1883,
              username: Optional[str] = None, password: Optional[str] = None,
              topic_prefix: str = 'homecameras'):
    global _mqtt_instance
    if MQTT_AVAILABLE:
        _mqtt_instance = MQTTClient(broker_host, broker_port, username, password, topic_prefix)
    else:
        logger.info("MQTT not available - skipping initialization")
    return _mqtt_instance


def get_mqtt() -> Optional[MQTTClient]:
    return _mqtt_instance