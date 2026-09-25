"""Further instances of the household's own Ollama: `ollama_<id>` providers.

The stack's cloud.ollama.instances names any number of local servers, each on
its own port with its own window and slots; the deployer writes a provider
block for each and points roles at them by name.
"""
from nanobot.config.schema import Config
from nanobot.providers.registry import find_by_name


def test_any_ollama_instance_has_a_local_spec():
    for name in ("ollama_tasks", "ollama_gpu0", "ollama-gpu0"):
        spec = find_by_name(name)
        assert spec is not None and spec.is_local, name
        assert spec.name == name.replace("-", "_")
    # The named ones keep their own specs; ollama.com is not local.
    assert find_by_name("ollama_vision").display_name == "Ollama (vision)"
    assert not find_by_name("ollama_cloud").is_local
    assert find_by_name("ollama_") is None and find_by_name("ollama_Bad!") is None


def test_an_instance_block_is_a_provider_config():
    c = Config.model_validate({
        "providers": {"ollama_gpu0": {"apiKey": "ollama", "apiBase": "http://h:11437/v1"},
                      "_comment_ollama_gpu0": "a note"},
        "agents": {"defaults": {"model": "gemma4:e4b", "provider": "ollama_gpu0"}}})
    block = getattr(c.providers, "ollama_gpu0")
    assert block.api_base == "http://h:11437/v1"
    p, name = c._match_provider()
    assert name == "ollama_gpu0" and p is block
