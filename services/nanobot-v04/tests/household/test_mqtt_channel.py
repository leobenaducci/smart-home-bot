"""household: the inbound MQTT channel (registered only with the household on)."""

from __future__ import annotations

import json

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.registry import discover_plugins
from nanobot.household.mqtt_channel import MQTTChannel


def test_mqtt_is_a_channel_only_in_the_household(monkeypatch):
    monkeypatch.delenv("NANOBOT_HOUSEHOLD", raising=False)
    assert "mqtt" not in discover_plugins()
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    plugins = discover_plugins()
    assert "mqtt" in plugins and plugins["mqtt"].load_channel_class() is MQTTChannel
    assert "mqtt" not in discover_plugins({"email"})


@pytest.mark.asyncio
async def test_a_motion_event_becomes_an_agent_turn():
    bus = MessageBus()
    channel = MQTTChannel({"allowFrom": ["mqtt"], "chatId": "mqtt:events"}, bus, network=None)
    payload = {"camera": "patio", "zone": "gate", "confidence": 0.91,
               "has_objects": True, "object_tags": ["person"]}
    await channel._on_mqtt_message("homecameras/motion/10.0.0.7", json.dumps(payload).encode())
    msg = await bus.consume_inbound()
    assert msg.channel == "mqtt" and msg.chat_id == "mqtt:events"
    assert "Motion detected on camera patio" in msg.content and "person" in msg.content
    assert msg.metadata["mqtt_topic"] == "homecameras/motion/10.0.0.7"


@pytest.mark.asyncio
async def test_a_sender_not_allowed_is_dropped():
    bus = MessageBus()
    channel = MQTTChannel({"allowFrom": []}, bus)
    await channel._on_mqtt_message("homecameras/recording/x", b"{}")
    assert bus.inbound_size == 0 if hasattr(bus, "inbound_size") else bus.inbound.empty()
