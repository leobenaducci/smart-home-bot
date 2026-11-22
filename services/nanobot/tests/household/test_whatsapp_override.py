"""household: plugin ``whatsapp`` runs the Baileys bridge once the household is on."""

from nanobot.channels.registry import load_channel_plugin
from nanobot.household.whatsapp_bridge import WhatsAppChannel as BridgeChannel


def test_upstream_runtime_without_the_household(monkeypatch):
    monkeypatch.delenv("NANOBOT_HOUSEHOLD", raising=False)
    cls = load_channel_plugin("whatsapp").load_channel_class()
    assert cls is not BridgeChannel


def test_bridge_runtime_with_the_household(monkeypatch):
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    plugin = load_channel_plugin("whatsapp")
    assert plugin.load_channel_class() is BridgeChannel
    assert BridgeChannel.name == "whatsapp"  # session keys unchanged


def test_no_neonize_install_with_the_household(monkeypatch):
    from nanobot.optional_features import ensure_enabled_channel_dependencies

    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    calls = []
    failures = ensure_enabled_channel_dependencies(
        {"whatsapp"}, {"whatsapp": load_channel_plugin("whatsapp")},
        runner=lambda *a, **k: calls.append(a),
    )
    assert failures == {} and calls == []
