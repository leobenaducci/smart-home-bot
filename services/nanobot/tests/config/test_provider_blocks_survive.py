"""Every provider block this deployment writes must survive parsing.

`ProvidersConfig` is a Pydantic model with the default `extra: ignore`, so a
block it does not declare is dropped in silence. `_resolve_alt_provider` then
does `getattr(config.providers, name, None)`, gets None, and builds an
OpenAICompatProvider with no api_key and no api_base -- which fills in OpenAI's
base and the literal "no-key". The turn fails with "Incorrect API key
provided: no-key", naming a provider the household never configured.

That is why FreeToken had never served a turn in this house: not the engine,
not the URL, not the wake path. The block was parsed away before anything
tried to use it.

The names here are the ones `deploy/deploy.py` maps a model prefix onto
(MODEL_PROVIDERS) plus the two it always writes, so this fails if the two
files drift apart.
"""
import pytest

from nanobot.config.schema import ProvidersConfig

# `ollama:` / `ollama-cloud:` / `together:` / `openai-compatible:` /
# `freetoken:` / `openrouter:` / `openai:` in assistant.models, plus `custom`,
# which is what a bare name (OpenCode Zen) resolves to.
DEPLOYED_BLOCKS = [
    "custom", "ollama", "ollama_cloud", "together_ai",
    "freetoken", "openrouter", "openai", "openai_compatible",
]


@pytest.mark.parametrize("name", DEPLOYED_BLOCKS)
def test_the_block_is_declared(name):
    assert name in ProvidersConfig.model_fields, (
        f"providers.{name} is written into config.json by the deployer but "
        f"not declared here, so Pydantic drops it and every turn on that "
        f"provider fails as OpenAI with no key")


@pytest.mark.parametrize("name", DEPLOYED_BLOCKS)
def test_a_configured_block_survives_a_round_trip(name):
    # The shape the deployer actually writes: a URL and a key.
    parsed = ProvidersConfig.model_validate(
        {name: {"api_key": "k", "api_base": "http://example.invalid/v1"}})
    block = getattr(parsed, name, None)
    assert block is not None, f"providers.{name} vanished on parse"
    assert block.api_key == "k"
    assert block.api_base == "http://example.invalid/v1"
