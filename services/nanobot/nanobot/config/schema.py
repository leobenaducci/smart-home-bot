"""Configuration schema using Pydantic."""

import re
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

from nanobot.cron.types import CronSchedule


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

class ChannelsConfig(Base):
    """Configuration for chat channels.

    Built-in and plugin channel configs are stored as extra fields (dicts).
    Each channel parses its own config in __init__.
    Per-channel "streaming": true enables streaming output (requires send_delta impl).
    """

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))
    send_max_retries: int = Field(default=3, ge=0, le=10)  # Max delivery attempts (initial send included)
    transcription_provider: str = "groq"  # Voice transcription backend: "groq" or "openai"
    transcription_language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")  # Optional ISO-639-1 hint for audio transcription


class RoutingConfig(Base):
    """Which tier a turn starts on, and when a failed cheap attempt moves up.

    ``mode``: ``off`` routes nothing (the caller's flags alone, as before
    2026-09-21); ``shadow`` classifies and records every turn but changes
    nothing -- a day of labels against real chats before trusting them;
    ``active`` routes on the label and escalates on failure.

    ``escalate_on`` are the runner's stop reasons that mean the cheap attempt
    did not finish: the strong model then continues the same turn, at most
    ``max_escalations_per_turn`` times. ``sticky_turns`` is how many turns a
    session starts on the strong tier after one escalation, because a hard
    conversation should not pay the cheap failure on every message.
    """

    mode: Literal["off", "shadow", "active"] = "shadow"
    # 3 s, not 1.5: the real prompt (~500 tokens) costs gemma4:e4b 600-700 ms
    # warm, and the first call after a restart crossed 1.5 s on 2026-09-21 --
    # which sent the request that started all this to the cheap model.
    timeout_s: float = Field(default=3.0, ge=0.2)
    long_message_chars: int = Field(default=600, ge=50)
    sticky_turns: int = Field(default=3, ge=0)
    max_escalations_per_turn: int = Field(default=1, ge=0)
    escalate_on: list[str] = Field(default_factory=lambda: [
        "bad_invocation", "error", "empty_final_response",
        "repeated_tool_calls", "max_iterations",
    ])
    subagents: bool = True  # classify a spawned task when the spawner did not say
    complex_iterations: int = Field(default=80, ge=1)  # budget once a turn is on the strong tier
    # A `long` turn works through a plan in the chat (tools/plan.py): each step
    # it sets buys this many model calls, and past this many seconds the steps
    # left go to a sub-agent where the conversation allows one.
    # 12 until 2026-09-24: every local model then ran out on the step that
    # takes one call per item (flash each of eight lights).
    plan_step_calls: int = Field(default=24, ge=1)
    # Thinking on a plan step run by the local model. "none" by default: a
    # step is one narrow job, and Qwen3.8-27B on a 3060 spent 45-75 s thinking
    # before each call at 12 tok/s (2026-09-24). A step that falls back runs
    # on the planner with the turn's own effort.
    plan_step_effort: str | None = "none"
    # A plan still running this long into a chat turn is detached: the chat
    # gets one line and the same run -- planner, step model, what is done --
    # continues in the background, its answer posted to the conversation.
    # Under the API server's request timeout (300 s), which otherwise cut the
    # turn and restarted the request from scratch on a sub-agent.
    plan_detach_seconds: int = Field(default=240, ge=30)
    plan_chat_seconds: int = Field(default=300, ge=30)
    # Which model carries out a plan's steps: "subagent" (the sub-agent's,
    # usually the house's local one -- the everyday model only plans and
    # answers) or "everyday" (it does everything). A step the local model
    # cannot finish is done again on the everyday one.
    plan_executor: Literal["subagent", "everyday"] = "subagent"
    # A cheap turn the classifier routed gets this many iterations before it
    # counts as `max_iterations` and escalates. The house's own
    # max_tool_iterations (200) stays for forced turns; an interactive turn
    # that is still calling tools after 40 rounds is not converging, and on
    # 2026-09-21 one ran 42 rounds of hand-written exec and grep on the cheap
    # model before anybody could see it.
    everyday_iterations: int = Field(default=40, ge=1)


class DreamConfig(Base):
    """Dream memory consolidation configuration."""

    _HOUR_MS = 3_600_000

    interval_h: int = Field(default=2, ge=1)  # Every 2 hours by default
    cron: str | None = Field(default=None, exclude=True)  # Legacy compatibility override
    model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelOverride", "model", "model_override"),
    )  # Optional Dream-specific model override
    max_batch_size: int = Field(default=20, ge=1)  # Max history entries per run
    # Bumped from 10 to 15 in #3212 (exp002: +30% dedup, no accuracy loss; >15 plateaus).
    max_iterations: int = Field(default=15, ge=1)  # Max tool calls per Phase 2
    # Whether Dream may author skills into workspace/skills. Off by default:
    # on 2026-06-06 it wrote `direct-curl-execution` ("bypass skills with curl
    # via exec when they fail") and `spawn-tool-usage` into a member's live
    # workspace, and every prompt since then carried them -- which is where the
    # exec/curl detours measured on 2026-09-10 came from. A skill is a standing
    # instruction; a nightly job should not be able to write one unreviewed.
    write_skills: bool = Field(default=False, validation_alias=AliasChoices("writeSkills", "write_skills"))
    # Per-line git-blame age annotation in Phase 1 prompt (see #3212). Default
    # on — set to False to feed MEMORY.md raw if a specific LLM reacts poorly
    # to the `← Nd` suffix or you want deterministic, git-independent prompts.
    annotate_line_ages: bool = True

    def build_schedule(self, timezone: str) -> CronSchedule:
        """Build the runtime schedule, preferring the legacy cron override if present."""
        if self.cron:
            return CronSchedule(kind="cron", expr=self.cron, tz=timezone)
        return CronSchedule(kind="every", every_ms=self.interval_h * self._HOUR_MS)

    def describe_schedule(self) -> str:
        """Return a human-readable summary for logs and startup output."""
        if self.cron:
            return f"cron {self.cron} (legacy)"
        hours = self.interval_h
        return f"every {hours}h"


class HarnessConfig(Base):
    """Long background tasks on the pi harness (nanobot/harness/pi_runner.py).

    Off by default. On, a sub-agent task from the member's own session runs on
    pi, with the model the household picked for the sub-agent (or the powerful
    sub-agent, for a complex task).
    """

    enabled: bool = False
    engine: Literal["pi"] = "pi"
    # OpenCode Go (the flat plan) for pi's models. CLAUDE.md keeps Go for a
    # person at the keyboard and says unattended traffic can get the account
    # blocked; this is the household's explicit exception, for pi only.
    allow_go: bool = False
    # Long tasks too: a turn the classifier labels `long` goes to a sub-agent
    # on pi instead of being planned in the chat. Off by default -- a plan in
    # the chat asks the person as it goes, keeps read-only steps read-only and
    # is quicker for short multi-step work; pi is for work that ends in a
    # document. `background` turns go to pi either way.
    long_tasks: bool = False
    # Optional override. Empty: pi runs the sub-agent's own model (and the
    # powerful sub-agent's, for a complex task), reached as nanobot reaches it.
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    context_window: int = 40960
    # The server takes `reasoning_effort: "none"` (Ollama does). Off for a
    # server that rejects it, which then gets no reasoning parameter at all.
    reasoning: bool = True
    thinking: str = "off"
    timeout_s: int = 900
    max_nudges: int = 3
    keep_workdirs: int = 10


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "~/.nanobot/workspace"
    model: str = "anthropic/claude-opus-4-5"
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    context_block_limit: int | None = None
    temperature: float = 0.1
    max_tool_iterations: int = 200
    max_tool_result_chars: int = 16_000
    provider_retry_mode: Literal["standard", "persistent"] = "standard"
    reasoning_effort: str | None = None  # low / medium / high / adaptive - enables LLM thinking mode
    timezone: str = "UTC"  # IANA timezone, e.g. "Asia/Shanghai", "America/New_York"
    unified_session: bool = False  # Share one session across all channels (single-user multi-device)
    disabled_skills: list[str] = Field(default_factory=list)  # Skill names to exclude from loading (e.g. ["summarize", "skill-creator"])
    session_ttl_minutes: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("idleCompactAfterMinutes", "sessionTtlMinutes"),
        serialization_alias="idleCompactAfterMinutes",
    )  # Auto-compact idle threshold in minutes (0 = disabled)
    dream: DreamConfig = Field(default_factory=DreamConfig)
    subagent_model: str | None = None   # Model for sub-agents (defaults to main model)
    harness: HarnessConfig = Field(default_factory=HarnessConfig)  # background tasks on pi
    subagent_provider: str | None = None  # Provider for sub-agents (defaults to main provider)
    subagent_model_powerful: str | None = None   # Model for complex sub-agent tasks
    subagent_provider_powerful: str | None = None  # Provider for complex sub-agent tasks
    model_powerful: str | None = None   # Model for turns the caller marks `powerful` (off if unset)
    provider_powerful: str | None = None  # Provider for that model (defaults to auto-match)
    # Role -> model, for callers that know *what kind* of turn this is rather
    # than which model they want. HomeCore names the Profession ("designer");
    # which model that means is decided here, so the roster stays in one place
    # and a client never has to be redeployed to change it.
    model_profiles: dict[str, str] = Field(default_factory=dict)
    provider_profiles: dict[str, str] = Field(default_factory=dict)
    # Thinking, per role, for the roles where it is wrong.
    #
    # `reasoning_effort` above is a property of the *provider*: set it and every
    # call that provider makes carries it. That is the right shape for a house
    # running one model, and the wrong one here, where notification triage and
    # background work can sit on a local model while the professions stay
    # hosted. A reasoning model asked to triage six notifications spends
    # thousands of tokens thinking, hits the cap and returns empty content with
    # `finish_reason: "length"` -- so the turn does not fail, it silently says
    # nothing. Measured on Qwen3.6-35B-A3B: default and "low" both produced an
    # empty answer, "none" produced clean JSON in 14s. "low" is worse than the
    # default, not better; only "none" actually turns it off.
    #
    # These ride the same per-turn route the model does, so a role that names
    # a local model can turn thinking off without touching the professions.
    # Unset means "whatever the provider is configured with", which is what
    # every role did before this existed.
    reasoning_effort_profiles: dict[str, str] = Field(default_factory=dict)
    reasoning_effort_powerful: str | None = None
    # The default turn -- ordinary chat and background subagent work, which are
    # neither a Profession nor `powerful` and so could not be reached by either
    # of the two above. Somebody is waiting on this turn, which is what makes
    # it worth setting: measured on freetoken:qwen3.6-35b-a3b with everyday
    # prompts, 43.6s with reasoning left on against 18.2s at "low" and 15.4s at
    # "none".
    #
    # The house runs "low", and the reason is not speed. `none` does not stop
    # the model reasoning -- it moves the reasoning into the visible reply, so
    # answers stay correct but arrive as a walkthrough. "low" was the most
    # concise of the three and still 2.4x faster than reasoning on. The fastest
    # setting is not the best one for a turn somebody reads.
    #
    # Per-role on purpose: "low" is right here and wrong for triage, where it
    # produced more reasoning than the default and an empty answer.
    # Unset keeps the old behaviour: whatever the provider does.
    reasoning_effort_default: str | None = None
    vision_model: str | None = None   # Model for turns containing images (off if unset)
    vision_provider: str | None = None  # Provider for the vision model (defaults to auto-match)
    # The heartbeat's decision turn: read HEARTBEAT.md, answer skip or run.
    #
    # It had no model of its own and inherited the agent's, so on this
    # household it ran on a hosted model every thirty minutes, in six
    # containers, to answer a question about a 369-byte file. That is the
    # same shape as notification triage and household events -- small,
    # unattended, nobody waiting -- and those already run locally for nothing.
    #
    # Unset inherits the agent's model, which is what every install did
    # before this key existed.
    heartbeat_model: str | None = None
    heartbeat_provider: str | None = None
    # The turn classifier -- see agent/classify.py. A small local model that
    # names a request chat / action / complex before the turn starts. Off when
    # unset: every turn then starts on the tier the caller asked for.
    classifier_model: str | None = None
    classifier_provider: str | None = None
    # Work of several steps in the chat (tools/plan.py): who plans it and
    # writes the answer, and who carries out each step. Unset, the planner is
    # the main model and the steps follow `routing.plan_executor`.
    plan_model: str | None = None
    plan_provider: str | None = None
    plan_step_model: str | None = None
    plan_step_provider: str | None = None
    classifier_reasoning_effort: str | None = "none"
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    # Its own, not the default turn's: the heartbeat does not go through the
    # agent loop at all (HeartbeatService calls chat_with_retry directly), so
    # `reasoning_effort_default` never reaches it. Measured on
    # freetoken:qwen3.6-35b-a3b against a real HEARTBEAT.md: 131-200 completion
    # tokens and ~7.4s with reasoning on, 122 and 5.3s at "low", 26 and 2.7s at
    # "none" -- and the tool call came back correct at all three, which is the
    # thing that could have broken. This fires 48 times a day in every
    # assistant to answer yes or no, so "none" is the setting and the tokens
    # saved are the whole point.
    heartbeat_reasoning_effort: str | None = None
    # Model to try when the requested one keeps failing, after the retry ladder
    # has already established that waiting does not help. Off if unset. This is
    # an *outage* fallback, not a config one: it fires on a model that is
    # answering with errors, not on a model that is missing. Once it has
    # answered in the failing model's place the swap sticks for an hour
    # (LLMProvider._MODEL_OUTAGE_COOLDOWN_S), so a multi-hour vendor outage
    # costs one exhausted ladder rather than one per call.
    #
    # There is deliberately no providerFallback. The swap happens inside one
    # provider instance, on the same key and base URL, so the fallback has to
    # be a model that provider can already route. A second provider would mean
    # building a second client mid-turn; a config key that promised it and did
    # nothing would be worse than not offering it. Routes resolved onto some
    # other provider are simply not given one - see _outage_fallback_for.
    # A name, or an ordered list of them. A single provider outage took the
    # main model *and* the one fallback out at the same moment (2026-08-30), so
    # one name is a rescue that shares its fate with the thing it rescues; a
    # list lets the next candidate come from another family.
    # A name, an ordered list of names, or an entry naming another provider:
    #   "deepseek-v4-flash"
    #   ["deepseek-v4-flash", {"model": "glm-5.3-flash", "provider": "together_ai"}]
    # A bare name means the provider serving the role, which is what every
    # config written before this said and what it has always meant. An entry
    # with a provider is routed to that provider's own client -- the only shape
    # that survives a provider being down rather than one model on it.
    model_fallback: str | list[str | dict] | dict | None = None


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str | None = None
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    # `ollama_<id>` blocks beyond the fields below: one per further instance of
    # the household's own Ollama (the stack's cloud.ollama.instances). Kept as
    # extras and turned into ProviderConfig, so `getattr(providers, name)`
    # answers for them the way it does for a named field.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    @model_validator(mode="after")
    def _local_ollama_instances(self) -> "ProvidersConfig":
        extra = self.__pydantic_extra__ or {}
        for key, value in list(extra.items()):
            if re.fullmatch(r"ollama_[a-z][a-z0-9]{0,15}", key) and isinstance(value, dict):
                extra[key] = ProviderConfig.model_validate(value)
        return self

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    # The second Ollama, for vision/OCR, on its own port. Added 2026-09-20:
    # the deployer has always rendered this block into config.json as
    # `${OLLAMA_VISION_URL}/v1`, but there was no field to receive it, so
    # pydantic dropped it on load and `getattr(providers, "ollama_vision")`
    # raised. Together with the missing registry spec that is why every vision
    # turn failed before reaching :11435.
    ollama_vision: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama vision instance
    lm_studio: ProviderConfig = Field(default_factory=ProviderConfig)  # LM Studio local models
    ovms: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenVINO Model Server (OVMS)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax_anthropic: ProviderConfig = Field(default_factory=ProviderConfig)  # MiniMax Anthropic endpoint (thinking)
    mistral: ProviderConfig = Field(default_factory=ProviderConfig)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)  # Step Fun (阶跃星辰)
    xiaomi_mimo: ProviderConfig = Field(default_factory=ProviderConfig)  # Xiaomi MIMO (小米)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus (VolcEngine international)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus Coding Plan
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # Github Copilot (OAuth)
    qianfan: ProviderConfig = Field(default_factory=ProviderConfig)  # Qianfan (百度千帆)
    together_ai: ProviderConfig = Field(default_factory=ProviderConfig)  # Together AI
    # Three this stack writes into config.json that upstream does not declare.
    # `extra` is Pydantic's default `ignore`, so the blocks were parsed away in
    # silence and `_resolve_alt_provider`'s `getattr(config.providers, name)`
    # returned None -- leaving api_key and api_base both unset, which
    # OpenAICompatProvider fills in with OpenAI's base and the literal
    # "no-key". Every turn on one of these answered "Incorrect API key
    # provided: no-key", naming a provider the household never configured.
    #
    # It is why FreeToken had never served a single turn here: not the engine,
    # not the URL, not the wake path -- the block was being dropped before
    # anything tried to use it.
    freetoken: ProviderConfig = Field(default_factory=ProviderConfig)  # local MoE engine
    ollama_cloud: ProviderConfig = Field(default_factory=ProviderConfig)  # ollama.com
    openai_compatible: ProviderConfig = Field(default_factory=ProviderConfig)  # any OpenAI-compatible URL


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    keep_recent_messages: int = 8
    channel: str = ""   # fixed delivery channel (e.g. "websocket"); auto-detect if empty
    chat_id: str = ""   # fixed delivery chat_id; auto-detect if empty


class MorningGreetingConfig(Base):
    """The daily good-morning message (``agent/morning_greeting.py``).

    Off by default: it writes into somebody's chat unprompted, so it has to be
    asked for. It also needs ``HOMECORE_USER_ID`` to know whose chat that is —
    without one the job is registered disabled rather than firing into nothing.
    The wording is not here; it lives in the workspace's ``MORNING.md``, which
    can be edited without a restart.
    """

    enabled: bool = False
    cron: str = "0 7 * * *"   # local time, in agents.defaults.timezone


class ApiConfig(Base):
    """OpenAI-compatible API server configuration."""

    host: str = "127.0.0.1"  # Safer default: local-only bind.
    port: int = 8900
    timeout: float = 120.0  # Per-request timeout in seconds.


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "127.0.0.1"  # Safer default: local-only bind.
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    morning_greeting: MorningGreetingConfig = Field(default_factory=MorningGreetingConfig)


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: str = "duckduckgo"  # brave, tavily, duckduckgo, searxng, jina, kagi
    api_key: str = ""
    base_url: str = ""  # SearXNG base URL
    max_results: int = 5
    timeout: int = 30  # Wall-clock timeout (seconds) for search operations


class WebToolsConfig(Base):
    """Web tools configuration."""

    enable: bool = True
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    enable: bool = True
    timeout: int = 60
    path_append: str = ""
    sandbox: str = ""  # sandbox backend: "" (none) or "bwrap"
    allowed_env_keys: list[str] = Field(default_factory=list)  # Env var names to pass through to subprocess (e.g. ["GOPATH", "JAVA_HOME"])

class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])  # Only register these tools; accepts raw MCP names or wrapped mcp_<server>_<tool> names; ["*"] = all tools; [] = no tools
    # Never register these, whatever enabled_tools says; same two name forms.
    # The denylist is for a server whose tool set grows on its own -- Home
    # Assistant adds an intent tool for every new kind of device -- where an
    # allowlist would hide each new one without a word.
    disabled_tools: list[str] = Field(default_factory=list)

class MyToolConfig(Base):
    """Self-inspection tool configuration."""

    enable: bool = True  # register the `my` tool (agent runtime state inspection)
    allow_set: bool = False  # let `my` modify loop state (read-only if False)


class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    my: MyToolConfig = Field(default_factory=MyToolConfig)
    restrict_to_workspace: bool = False  # restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    # Built-in tools this instance does not offer, by name (e.g. "glob",
    # "notebook_edit"): every tool's schema is in every prompt, used or not.
    disabled: list[str] = Field(default_factory=list)
    ssrf_whitelist: list[str] = Field(default_factory=list)  # CIDR ranges to exempt from SSRF blocking (e.g. ["100.64.0.0/10"] for Tailscale)


class N8nConfig(Base):
    """n8n workflow automation integration configuration."""

    api_key: str | None = None
    base_url: str = "http://localhost:5678"
    workflows_endpoint: str = "/api/v1/workflows/"

    model_config = ConfigDict(extra="allow")


class ProfilingConfig(Base):
    """Where turns spend their time and tokens — /v1/debug/profile.

    On by default. It costs a `perf_counter()` and a dict append per LLM call
    and per tool, against a network round trip, and everything it keeps is a
    bounded ring buffer — while a profiler you have to turn on first is one
    that is off during the incident you needed it for. The switch exists for
    the deployment that wants nothing recorded at all.
    """

    enabled: bool = True


class Config(BaseSettings):
    """Root configuration for nanobot."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    n8n: N8nConfig = Field(default_factory=N8nConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    profiling: ProfilingConfig = Field(default_factory=ProfilingConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def _match_provider(
        self, model: str | None = None
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from nanobot.providers.registry import PROVIDERS, find_by_name

        forced = self.agents.defaults.provider
        if forced != "auto":
            spec = find_by_name(forced)
            if spec:
                p = getattr(self.providers, spec.name, None)
                return (p, spec.name) if p else (None, None)
            return None, None

        model_lower = (model or self.agents.defaults.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        # Prefer providers whose detect_by_base_keyword matches the configured api_base
        # (e.g. Ollama's "11434" in "http://localhost:11434") over plain registry order.
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model)
        return p

    def get_provider_name(self, model: str | None = None) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model)
        return name

    def get_api_key(self, model: str | None = None) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model)
        return p.api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        """Get API base URL for the given model, falling back to the provider default when present."""
        from nanobot.providers.registry import find_by_name

        p, name = self._match_provider(model)
        if p and p.api_base:
            return p.api_base
        if name:
            spec = find_by_name(name)
            if spec and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = ConfigDict(env_prefix="NANOBOT_", env_nested_delimiter="__")
