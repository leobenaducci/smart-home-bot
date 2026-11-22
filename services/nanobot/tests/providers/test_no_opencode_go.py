"""Trial port: nothing in this stack may reach OpenCode Go."""

import pytest

from nanobot.config.schema import Config
from nanobot.providers.routing import resolve_provider_route


def _cfg(model, providers):
    return Config.model_validate({"agents": {"defaults": {"model": model}}, "providers": providers})


def _route(cfg):
    return resolve_provider_route(cfg, preset=cfg.resolve_preset())


def test_go_model_is_refused():
    cfg = _cfg("opencode-go/kimi-k2", {"opencodeGo": {"apiKey": "k"}})
    with pytest.raises(ValueError, match="OpenCode Go is not allowed"):
        _route(cfg)


def test_custom_provider_pointed_at_go_is_refused():
    cfg = _cfg("custom/kimi-k2", {"custom": {"apiKey": "k", "apiBase": "https://opencode.ai/zen/go/v1"}})
    with pytest.raises(ValueError, match="OpenCode Go is not allowed"):
        _route(cfg)


def test_zen_is_fine():
    cfg = _cfg("opencode/kimi-k2", {"opencode": {"apiKey": "k"}})
    assert _route(cfg).provider_name == "opencode"
