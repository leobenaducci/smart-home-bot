"""
Provider Registry — single source of truth for LLM provider metadata.

Adding a new provider:
  1. Add a ProviderSpec to PROVIDERS below.
  2. Add a field to ProvidersConfig in config/schema.py.
  Done. Env vars, config matching, status display all derive from here.

Order matters — it controls match priority and fallback. Gateways first.
Every entry writes out all fields so you can copy-paste as a template.
"""

from __future__ import annotations

import functools
import re

from dataclasses import dataclass
from typing import Any

from pydantic.alias_generators import to_snake


@dataclass(frozen=True)
class ProviderSpec:
    """One LLM provider's metadata. See PROVIDERS below for real examples.

    Placeholders in env_extras values:
      {api_key}  — the user's API key
      {api_base} — api_base from config, or this spec's default_api_base
    """

    # identity
    name: str  # config field name, e.g. "dashscope"
    keywords: tuple[str, ...]  # model-name keywords for matching (lowercase)
    env_key: str  # env var for API key, e.g. "DASHSCOPE_API_KEY"
    display_name: str = ""  # shown in `nanobot status`

    # which provider implementation to use
    # "openai_compat" | "anthropic" | "azure_openai" | "openai_codex" | "github_copilot"
    backend: str = "openai_compat"

    # extra env vars, e.g. (("ZHIPUAI_API_KEY", "{api_key}"),)
    env_extras: tuple[tuple[str, str], ...] = ()

    # gateway / local detection
    is_gateway: bool = False  # routes any model (OpenRouter, AiHubMix)
    is_local: bool = False  # local deployment (vLLM, Ollama)
    detect_by_key_prefix: str = ""  # match api_key prefix, e.g. "sk-or-"
    detect_by_base_keyword: str = ""  # match substring in api_base URL
    default_api_base: str = ""  # OpenAI-compatible base URL for this provider

    # gateway behavior
    strip_model_prefix: bool = False  # strip "provider/" before sending to gateway
    supports_max_completion_tokens: bool = False

    # per-model param overrides, e.g. (("kimi-k2.5", {"temperature": 1.0}),)
    model_overrides: tuple[tuple[str, dict[str, Any]], ...] = ()

    # OAuth-based providers (e.g., OpenAI Codex) don't use API keys
    is_oauth: bool = False

    # Direct providers skip API-key validation (user supplies everything)
    is_direct: bool = False

    # Provider supports cache_control on content blocks (e.g. Anthropic prompt caching)
    supports_prompt_caching: bool = False

    # How to inject the thinking on/off toggle into extra_body.
    # ""              — no extra_body needed (default)
    # "thinking_type" — {"thinking": {"type": "enabled"/"disabled"}}
    #                   (DeepSeek, VolcEngine, BytePlus)
    # "enable_thinking" — {"enable_thinking": true/false}  (DashScope)
    # "reasoning_split" — {"reasoning_split": true/false}  (MiniMax)
    thinking_style: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.name.title()


# ---------------------------------------------------------------------------
# PROVIDERS — the registry. Order = priority. Copy any entry as template.
# ---------------------------------------------------------------------------

PROVIDERS: tuple[ProviderSpec, ...] = (
    # === Custom (direct OpenAI-compatible endpoint) ========================
    ProviderSpec(
        name="custom",
        keywords=(),
        env_key="",
        display_name="Custom",
        backend="openai_compat",
        is_direct=True,
    ),

    # === Azure OpenAI (direct API calls with API version 2024-10-21) =====
    ProviderSpec(
        name="azure_openai",
        keywords=("azure", "azure-openai"),
        env_key="",
        display_name="Azure OpenAI",
        backend="azure_openai",
        is_direct=True,
    ),
    # === Gateways (detected by api_key / api_base, not model name) =========
    # Gateways can route any model, so they win in fallback.
    # OpenRouter: global gateway, keys start with "sk-or-"
    ProviderSpec(
        name="openrouter",
        keywords=("openrouter",),
        env_key="OPENROUTER_API_KEY",
        display_name="OpenRouter",
        backend="openai_compat",
        is_gateway=True,
        detect_by_key_prefix="sk-or-",
        detect_by_base_keyword="openrouter",
        default_api_base="https://openrouter.ai/api/v1",
        supports_prompt_caching=True,
    ),
    # AiHubMix: global gateway, OpenAI-compatible interface.
    # strip_model_prefix=True: doesn't understand "anthropic/claude-3",
    # strips to bare "claude-3".
    ProviderSpec(
        name="aihubmix",
        keywords=("aihubmix",),
        env_key="OPENAI_API_KEY",
        display_name="AiHubMix",
        backend="openai_compat",
        is_gateway=True,
        detect_by_base_keyword="aihubmix",
        default_api_base="https://aihubmix.com/v1",
        strip_model_prefix=True,
    ),
    # SiliconFlow (硅基流动): OpenAI-compatible gateway, model names keep org prefix
    ProviderSpec(
        name="siliconflow",
        keywords=("siliconflow",),
        env_key="OPENAI_API_KEY",
        display_name="SiliconFlow",
        backend="openai_compat",
        is_gateway=True,
        detect_by_base_keyword="siliconflow",
        default_api_base="https://api.siliconflow.cn/v1",
    ),

    # VolcEngine (火山引擎): OpenAI-compatible gateway, pay-per-use models
    ProviderSpec(
        name="volcengine",
        keywords=("volcengine", "volces", "ark"),
        env_key="OPENAI_API_KEY",
        display_name="VolcEngine",
        backend="openai_compat",
        is_gateway=True,
        detect_by_base_keyword="volces",
        default_api_base="https://ark.cn-beijing.volces.com/api/v3",
        thinking_style="thinking_type",
    ),

    # VolcEngine Coding Plan (火山引擎 Coding Plan): same key as volcengine
    ProviderSpec(
        name="volcengine_coding_plan",
        keywords=("volcengine-plan",),
        env_key="OPENAI_API_KEY",
        display_name="VolcEngine Coding Plan",
        backend="openai_compat",
        is_gateway=True,
        default_api_base="https://ark.cn-beijing.volces.com/api/coding/v3",
        strip_model_prefix=True,
        thinking_style="thinking_type",
    ),

    # BytePlus: VolcEngine international, pay-per-use models
    ProviderSpec(
        name="byteplus",
        keywords=("byteplus",),
        env_key="OPENAI_API_KEY",
        display_name="BytePlus",
        backend="openai_compat",
        is_gateway=True,
        detect_by_base_keyword="bytepluses",
        default_api_base="https://ark.ap-southeast.bytepluses.com/api/v3",
        strip_model_prefix=True,
        thinking_style="thinking_type",
    ),

    # BytePlus Coding Plan: same key as byteplus
    ProviderSpec(
        name="byteplus_coding_plan",
        keywords=("byteplus-plan",),
        env_key="OPENAI_API_KEY",
        display_name="BytePlus Coding Plan",
        backend="openai_compat",
        is_gateway=True,
        default_api_base="https://ark.ap-southeast.bytepluses.com/api/coding/v3",
        strip_model_prefix=True,
        thinking_style="thinking_type",
    ),


    # === Standard providers (matched by model-name keywords) ===============
    # Anthropic: native Anthropic SDK
    ProviderSpec(
        name="anthropic",
        keywords=("anthropic", "claude"),
        env_key="ANTHROPIC_API_KEY",
        display_name="Anthropic",
        backend="anthropic",
        supports_prompt_caching=True,
    ),
    # OpenAI: SDK default base URL (no override needed)
    ProviderSpec(
        name="openai",
        keywords=("openai", "gpt"),
        env_key="OPENAI_API_KEY",
        display_name="OpenAI",
        backend="openai_compat",
        supports_max_completion_tokens=True,
    ),
    # OpenAI Codex: OAuth-based, dedicated provider
    ProviderSpec(
        name="openai_codex",
        keywords=("openai-codex",),
        env_key="",
        display_name="OpenAI Codex",
        backend="openai_codex",
        detect_by_base_keyword="codex",
        default_api_base="https://chatgpt.com/backend-api",
        is_oauth=True,
    ),
    # GitHub Copilot: OAuth-based
    ProviderSpec(
        name="github_copilot",
        keywords=("github_copilot", "copilot"),
        env_key="",
        display_name="Github Copilot",
        backend="github_copilot",
        default_api_base="https://api.githubcopilot.com",
        strip_model_prefix=True,
        is_oauth=True,
        supports_max_completion_tokens=True,
    ),
    # DeepSeek: OpenAI-compatible at api.deepseek.com
    ProviderSpec(
        name="deepseek",
        keywords=("deepseek",),
        env_key="DEEPSEEK_API_KEY",
        display_name="DeepSeek",
        backend="openai_compat",
        default_api_base="https://api.deepseek.com",
        thinking_style="thinking_type",
    ),
    # Gemini: Google's OpenAI-compatible endpoint
    ProviderSpec(
        name="gemini",
        keywords=("gemini",),
        env_key="GEMINI_API_KEY",
        display_name="Gemini",
        backend="openai_compat",
        default_api_base="https://generativelanguage.googleapis.com/v1beta/openai/",
    ),
    # Zhipu (智谱): OpenAI-compatible at open.bigmodel.cn
    ProviderSpec(
        name="zhipu",
        keywords=("zhipu", "glm", "zai"),
        env_key="ZAI_API_KEY",
        display_name="Zhipu AI",
        backend="openai_compat",
        env_extras=(("ZHIPUAI_API_KEY", "{api_key}"),),
        default_api_base="https://open.bigmodel.cn/api/paas/v4",
    ),
    # DashScope (通义): Qwen models, OpenAI-compatible endpoint
    ProviderSpec(
        name="dashscope",
        keywords=("qwen", "dashscope"),
        env_key="DASHSCOPE_API_KEY",
        display_name="DashScope",
        backend="openai_compat",
        default_api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        thinking_style="enable_thinking",
    ),
    # Moonshot (月之暗面): Kimi K2.5 / K2.6 enforce temperature >= 1.0.
    ProviderSpec(
        name="moonshot",
        keywords=("moonshot", "kimi"),
        env_key="MOONSHOT_API_KEY",
        display_name="Moonshot",
        backend="openai_compat",
        default_api_base="https://api.moonshot.ai/v1",
        model_overrides=(
            ("kimi-k2.5", {"temperature": 1.0}),
            ("kimi-k2.6", {"temperature": 1.0}),
        ),
    ),
    # MiniMax: OpenAI-compatible API
    ProviderSpec(
        name="minimax",
        keywords=("minimax",),
        env_key="MINIMAX_API_KEY",
        display_name="MiniMax",
        backend="openai_compat",
        default_api_base="https://api.minimax.io/v1",
        thinking_style="reasoning_split",
    ),
    # MiniMax Anthropic-compatible endpoint: supports thinking mode
    ProviderSpec(
        name="minimax_anthropic",
        keywords=("minimax_anthropic",),
        env_key="MINIMAX_API_KEY",
        display_name="MiniMax (Anthropic)",
        backend="anthropic",
        default_api_base="https://api.minimax.io/anthropic",
    ),
    # Mistral AI: OpenAI-compatible API
    ProviderSpec(
        name="mistral",
        keywords=("mistral",),
        env_key="MISTRAL_API_KEY",
        display_name="Mistral",
        backend="openai_compat",
        default_api_base="https://api.mistral.ai/v1",
    ),
    # Step Fun (阶跃星辰): OpenAI-compatible API
    ProviderSpec(
        name="stepfun",
        keywords=("stepfun", "step"),
        env_key="STEPFUN_API_KEY",
        display_name="Step Fun",
        backend="openai_compat",
        default_api_base="https://api.stepfun.com/v1",
    ),
    # Xiaomi MIMO (小米): OpenAI-compatible API
    ProviderSpec(
        name="xiaomi_mimo",
        keywords=("xiaomi_mimo", "mimo"),
        env_key="XIAOMIMIMO_API_KEY",
        display_name="Xiaomi MIMO",
        backend="openai_compat",
        default_api_base="https://api.xiaomimimo.com/v1",
    ),
    # === Local deployment (matched by config key, NOT by api_base) =========
    # vLLM / any OpenAI-compatible local server
    ProviderSpec(
        name="vllm",
        keywords=("vllm",),
        env_key="HOSTED_VLLM_API_KEY",
        display_name="vLLM/Local",
        backend="openai_compat",
        is_local=True,
    ),
    # Ollama (local, OpenAI-compatible)
    ProviderSpec(
        name="ollama",
        keywords=("ollama", "nemotron"),
        env_key="OLLAMA_API_KEY",
        display_name="Ollama",
        backend="openai_compat",
        is_local=True,
        detect_by_base_keyword="11434",
        default_api_base="http://localhost:11434/v1",
        # Disable chain-of-thought for Qwen3/Qwen3.5 thinking models.
        # Ollama's OpenAI-compatible endpoint accepts options.think via extra_body.
        model_overrides=(
            ("qwen3", {"extra_body": {"options": {"think": False}}}),
        ),
    ),
    # LM Studio (local, OpenAI-compatible)
    ProviderSpec(
        name="lm_studio",
        keywords=("lm-studio", "lmstudio", "lm_studio"),
        env_key="LM_STUDIO_API_KEY",
        display_name="LM Studio",
        backend="openai_compat",
        is_local=True,
        detect_by_base_keyword="1234",
        default_api_base="http://localhost:1234/v1",
    ),
    # === OpenVINO Model Server (direct, local, OpenAI-compatible at /v3) ===
    ProviderSpec(
        name="ovms",
        keywords=("openvino", "ovms"),
        env_key="",
        display_name="OpenVINO Model Server",
        backend="openai_compat",
        is_direct=True,
        is_local=True,
        default_api_base="http://localhost:8000/v3",
    ),
    # === Auxiliary (not a primary LLM provider) ============================
    # Groq: mainly used for Whisper voice transcription, also usable for LLM
    ProviderSpec(
        name="groq",
        keywords=("groq",),
        env_key="GROQ_API_KEY",
        display_name="Groq",
        backend="openai_compat",
        default_api_base="https://api.groq.com/openai/v1",
    ),
    # Qianfan (百度千帆): OpenAI-compatible API
    ProviderSpec(
        name="qianfan",
        keywords=("qianfan", "ernie"),
        env_key="QIANFAN_API_KEY",
        display_name="Qianfan",
        backend="openai_compat",
        default_api_base="https://qianfan.baidubce.com/v2"
    ),
    # The second Ollama, for vision/OCR. A separate server from `ollama`, not a
    # flag on it, because OLLAMA_NUM_PARALLEL and OLLAMA_CONTEXT_LENGTH are
    # server-global -- the same reason deploy.py gives the `ollama-vision:`
    # prefix its own entry in MODEL_PROVIDERS.
    #
    # Added 2026-09-20, after Alfred reported "the vision model is down" while
    # the vision server was healthy, held the model, and had received no request
    # at all. `assistant.models.vision` is `ollama-vision:qwen3-vl:4b`, which
    # resolves to provider `ollama_vision` -- and this registry had no such
    # spec, so every vision turn died in `_make_provider` before a byte reached
    # :11435. Same shape of hole as `openai_compatible` below.
    #
    # No `keywords`: a model on this server is an ordinary model name with no
    # tell, and the prefix is what selects it. No `default_api_base` either --
    # the port is whatever the household runs the second server on, and a
    # guessed one is a connection refused that reads as the engine being down.
    ProviderSpec(
        name="ollama_vision",
        keywords=(),
        env_key="OLLAMA_API_KEY",
        display_name="Ollama (vision)",
        backend="openai_compat",
        is_local=True,
    ),
    # ollama.com. Not local, and not keyless: what goes to it leaves the house
    # and it bills. `is_local` is deliberately absent, so a missing key is
    # refused here the way it is for any hosted provider.
    #
    # Added 2026-09-20 alongside ollama_vision: `ollama-cloud:` was the third
    # prefix deploy.py published without a spec behind it, so a cloud model
    # could be picked on the Models page and chosen for a role, then fail at
    # the first turn.
    ProviderSpec(
        name="ollama_cloud",
        keywords=(),
        env_key="OLLAMA_CLOUD_API_KEY",
        display_name="Ollama Cloud",
        backend="openai_compat",
        detect_by_base_keyword="ollama.com",
        default_api_base="https://ollama.com/v1",
    ),
    # Anything that speaks the OpenAI API at a URL you give it: a vLLM or
    # llama.cpp server on the LAN, LM Studio, or a provider this package has
    # never heard of. The `openai-compatible:` prefix in deploy's
    # MODEL_PROVIDERS, and `providers.openai_compatible` in the config schema.
    #
    # Added 2026-09-18, because the prefix was documented, rendered into the
    # container config as `${OPENAI_COMPATIBLE_URL}/v1`, and offered by the
    # admin models page -- while this registry had no spec for it. The whole
    # chain existed except the one entry that makes it resolvable, so every
    # `openai-compatible:` model died in `_make_provider` with "No API key
    # configured": `find_by_name` is exact-match with no aliases, returns None,
    # `_match_provider` then returns (None, None), and the key check sees no
    # provider at all. The message names the wrong cause, which is what made it
    # survive -- a key was present; the provider was not.
    #
    # `keywords` is deliberately empty. This slot is "whatever you pointed it
    # at", so there is no model-name pattern that means it, and any keyword
    # here would hijack auto-matching for a model that belongs to a real
    # provider. It resolves through the explicit prefix, which sets
    # `agents.defaults.provider` and takes the `forced` path -- no keyword
    # needed. Same reason there is no detect_by_base_keyword: FreeToken can
    # look for "freetoken" in the URL, this one has no such tell.
    ProviderSpec(
        name="openai_compatible",
        keywords=(),
        env_key="OPENAI_COMPATIBLE_API_KEY",
        display_name="OpenAI-compatible",
        backend="openai_compat",
        # The load-bearing field, for the reason spelled out under FreeToken
        # below: a server on your own desk usually has no key, and without this
        # the provider refuses to construct.
        is_local=True,
    ),
    # FreeToken: a local MoE serving engine, OpenAI-compatible.
    #
    # `is_local` is the load-bearing field and not a label. Without a spec at
    # all the resolver falls back to the openai_compat backend, which is right
    # -- and then refuses to construct the provider because no API key is
    # configured. A server on your own desk usually has none, which is exactly
    # what `is_local` exempts. Ollama carries it for the same reason.
    ProviderSpec(
        name="freetoken",
        keywords=("freetoken",),
        env_key="FREETOKEN_API_KEY",
        display_name="FreeToken",
        backend="openai_compat",
        is_local=True,
        # It serves whatever the household loaded, under those models' own
        # names, so there is no name pattern to detect it by -- only the
        # address. No default_api_base: there is no conventional port to guess,
        # and a guessed one is a connection refused that reads as the engine
        # being down.
        detect_by_base_keyword="freetoken",
    ),
    # Together AI: OpenAI-compatible inference gateway
    ProviderSpec(
        name="together_ai",
        keywords=("together_ai", "together"),
        env_key="TOGETHER_API_KEY",
        display_name="Together AI",
        backend="openai_compat",
        is_gateway=True,
        detect_by_base_keyword="together.xyz",
        default_api_base="https://api.together.xyz/v1",
    ),
)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


# Any further instance of the household's own Ollama: `ollama_<id>`, one per
# entry in the stack's cloud.ollama.instances (deploy/ollama_instances.py).
# `ollama` and `ollama_vision` have specs of their own above; the rest are the
# same kind of server on another port, so they share this shape rather than
# each needing a line here.
LOCAL_OLLAMA_RE = re.compile(r"ollama_([a-z][a-z0-9]{0,15})")


@functools.lru_cache(maxsize=64)
def _local_ollama_spec(name: str) -> ProviderSpec:
    return ProviderSpec(
        name=name,
        keywords=(),
        env_key="OLLAMA_API_KEY",
        display_name=f"Ollama ({name[len('ollama_'):]})",
        backend="openai_compat",
        is_local=True,
    )


def find_by_name(name: str) -> ProviderSpec | None:
    """Find a provider spec by config field name, e.g. "dashscope"."""
    normalized = to_snake(name.replace("-", "_"))
    for spec in PROVIDERS:
        if spec.name == normalized:
            return spec
    # Matched on the name as given: to_snake splits a digit off (`gpu0` ->
    # `gpu_0`), and the stack's instance ids end in digits often.
    raw = (name or "").replace("-", "_").lower()
    m = LOCAL_OLLAMA_RE.fullmatch(raw)
    if m and m.group(1) != "cloud":
        return _local_ollama_spec(raw)
    return None
