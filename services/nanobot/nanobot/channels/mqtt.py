"""MQTT channel — subscribes to MQTT topics and injects events into the agent bus."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel

try:
    import paho.mqtt.client as mqtt_lib
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False


class MQTTChannel(BaseChannel):
    """MQTT channel that subscribes to topics and triggers the agent on each event.

    Inbound-only: incoming MQTT messages become agent prompts. Outbound messages
    (agent replies) are silently dropped — responses go via ntfy/Telegram/etc.

    Config keys:
        brokerHost    MQTT broker hostname        (default: "localhost")
        brokerPort    MQTT broker port            (default: 1883)
        username      Optional broker username
        password      Optional broker password
        clientId      MQTT client identifier      (default: "nanobot-mqtt")
        topics        List of topic patterns      (default: ["homecameras/#"])
        chatId        Chat id for the bus session (default: "mqtt:events")
        allowFrom     Must include "mqtt"         (checked by BaseChannel)
    """

    name = "mqtt"
    display_name = "MQTT"

    def __init__(self, config: Any, bus: MessageBus):
        super().__init__(config, bus)
        self._client = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default=None):
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    # ------------------------------------------------------------------
    # BaseChannel interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not MQTT_AVAILABLE:
            logger.error("MQTT channel: paho-mqtt is not installed — run: pip install paho-mqtt")
            return

        self._running = True
        self._loop = asyncio.get_event_loop()
        self._stop_event = asyncio.Event()

        broker_host: str = self._cfg("brokerHost", "mqtt.home")
        broker_port: int = int(self._cfg("brokerPort", 1883))
        username: str | None = self._cfg("username") or None
        password: str | None = self._cfg("password") or None
        client_id: str = self._cfg("clientId", "nanobot-mqtt")
        # "motion/+" not "motion/#": the "#" form also matches
        # homecameras/motion/<cam>/objects, which fired the agent a second time
        # with a raw JSON dump of the same detection.
        topics: list[str] = self._cfg("topics", ["homecameras/motion/+", "homecameras/recording/+"])

        client = mqtt_lib.Client(client_id=client_id, protocol=mqtt_lib.MQTTv311)
        if username and password:
            client.username_pw_set(username, password)

        def on_connect(c, userdata, flags, rc):
            if rc == 0:
                logger.info("MQTT channel: connected to {}:{}", broker_host, broker_port)
                for topic in topics:
                    c.subscribe(topic, qos=1)
                    logger.info("MQTT channel: subscribed to {}", topic)
            else:
                logger.error("MQTT channel: connection failed rc={}", rc)

        def on_disconnect(c, userdata, rc):
            if rc != 0:
                logger.warning("MQTT channel: unexpected disconnect rc={}, will reconnect", rc)

        def on_message(c, userdata, message):
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._on_mqtt_message(message.topic, message.payload),
                    self._loop,
                )

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.connect_async(broker_host, broker_port, keepalive=60)
        client.loop_start()
        self._client = client

        logger.info("MQTT channel: connecting to {}:{}", broker_host, broker_port)
        await self._stop_event.wait()

        client.loop_stop()
        client.disconnect()
        logger.info("MQTT channel: stopped")

    async def stop(self) -> None:
        self._running = False
        if self._stop_event:
            self._stop_event.set()

    async def send(self, msg: OutboundMessage) -> None:
        # MQTT channel is inbound-only; agent responses go via other channels (ntfy, Telegram…)
        pass

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    async def _on_mqtt_message(self, topic: str, payload_bytes: bytes) -> None:
        try:
            payload = json.loads(payload_bytes.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = payload_bytes.decode(errors="replace")

        content = self._format_event(topic, payload)
        chat_id: str = self._cfg("chatId", "mqtt:events")

        await self._handle_message(
            sender_id="mqtt",
            chat_id=chat_id,
            content=content,
            metadata={"mqtt_topic": topic, "mqtt_payload": payload},
        )

    def _format_event(self, topic: str, payload: Any) -> str:
        """Convert a raw MQTT payload into a natural-language prompt for the agent."""
        parts = topic.split("/")

        if not isinstance(payload, dict):
            return f"MQTT event on `{topic}`:\n{payload}"

        camera = payload.get("camera", "unknown")

        # homecameras/motion/<ip>
        if len(parts) >= 2 and parts[1] == "motion" and parts[-1] != "objects":
            zone = payload.get("zone", "")
            confidence = payload.get("confidence", 0)
            object_tags = payload.get("object_tags", [])
            has_objects = payload.get("has_objects", False)
            lines = [f"Motion detected on camera {camera}"]
            if zone:
                lines.append(f"Zone: {zone}")
            lines.append(f"Confidence: {confidence:.1%}")
            if has_objects and object_tags:
                lines.append(f"Objects detected: {', '.join(object_tags)}")
            return "\n".join(lines)

        # homecameras/recording/<ip>
        if len(parts) >= 2 and parts[1] == "recording":
            event_type = payload.get("event_type", "")
            filename = payload.get("filename", "")
            object_tags = payload.get("object_tags", [])
            lines = [f"Recording {event_type} on camera {camera}"]
            if filename:
                lines.append(f"File: {filename}")
            if object_tags:
                lines.append(f"Objects: {', '.join(object_tags)}")
            return "\n".join(lines)

        return f"MQTT event on `{topic}`:\n{json.dumps(payload, indent=2)}"
