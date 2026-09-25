"""What Alfred says he is running has to be what is running.

Asked which model he was, Alfred answered from the roster in AGENTS.md — the
configuration, not the routing. The two agree only on an ordinary text turn on
the main model: a photo is answered by the vision model, a Profesión by its
own, and for the hour after an outage every turn is answered by the fallback
while the roster still names the model that is down. On 2026-08-23 that hour
was real, and for all of it the name he would have given is the one model that
was not answering.

So the model is written into the turn's own Runtime Context block, next to
`Current Time`, and read from the same place the routing decides it.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunResult
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse
from nanobot.utils.outage_store import SHARED_OUTAGES

TAG = ContextBuilder._RUNTIME_CONTEXT_TAG
END = ContextBuilder._RUNTIME_CONTEXT_END


class _Provider(LLMProvider):
    """A real provider, so the routing under test is the deployed one."""

    def __init__(self, default="gpt-5.6-luna", fallback_model=None, api_base=None):
        super().__init__(api_base=api_base)
        self._default = default
        self.generation = GenerationSettings(fallback_model=fallback_model)

    async def chat(self, *args, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return self._default


def _block(*lines: str) -> str:
    """A Runtime Context block shaped exactly like the built one."""
    return TAG + "\n" + "\n".join(lines) + "\n" + END


# --- which model is serving -------------------------------------------------

def test_an_ordinary_turn_is_served_by_the_model_it_asked_for():
    provider = _Provider(fallback_model="deepseek-v4-flash")
    assert provider.serving_model("gpt-5.6-luna") == ("gpt-5.6-luna", None)


def test_a_turn_with_no_model_named_resolves_the_default():
    """`model=None` is an ordinary chat turn, not an unknown — reporting it as
    None would put the word None in front of the family."""
    assert _Provider().serving_model(None) == ("gpt-5.6-luna", None)


def test_a_model_inside_its_outage_window_names_the_fallback_and_what_it_displaced():
    provider = _Provider(fallback_model="deepseek-v4-flash")
    provider._mark_model_down("gpt-5.6-luna")

    assert provider.serving_model("gpt-5.6-luna") == (
        "deepseek-v4-flash", "gpt-5.6-luna",
    )


def test_another_model_is_untouched_by_someone_elses_outage():
    """One dead model is one dead model: kimi-k3 answered normally through the
    whole of 2026-08-23 and a Profesión turn on it must still say so."""
    provider = _Provider(fallback_model="deepseek-v4-flash")
    provider._mark_model_down("gpt-5.6-luna")

    assert provider.serving_model("kimi-k3") == ("kimi-k3", None)


def test_the_fallback_is_never_reported_as_displaced():
    """It is never gated in the routing either — a fallback that could route
    around itself would strand every model with nothing left to answer."""
    provider = _Provider(fallback_model="deepseek-v4-flash")
    provider._mark_model_down("deepseek-v4-flash")

    assert provider.serving_model("deepseek-v4-flash") == ("deepseek-v4-flash", None)


def test_without_a_configured_fallback_nothing_is_ever_displaced():
    provider = _Provider()
    provider._mark_model_down("gpt-5.6-luna")

    assert provider.serving_model("gpt-5.6-luna") == ("gpt-5.6-luna", None)


def test_an_expired_window_goes_back_to_the_main_model():
    import time

    provider = _Provider(fallback_model="deepseek-v4-flash")
    provider._model_down_until["gpt-5.6-luna"] = time.monotonic() - 1

    assert provider.serving_model("gpt-5.6-luna") == ("gpt-5.6-luna", None)


@pytest.mark.parametrize("kw", [{"model": "gpt-5.6-luna"}, {}], ids=["named", "default"])
def test_the_wire_is_routed_the_way_the_prompt_says(kw):
    """An ordinary chat turn reaches the provider with no `model` key at all —
    the caller wants the default — so the two have to agree on that shape too,
    not only on the one where the model is spelled out."""
    provider = _Provider(fallback_model="deepseek-v4-flash")
    provider._mark_model_down("gpt-5.6-luna")

    routed, displaced, on_provider = provider._route_around_dead_model(dict(kw))

    assert routed["model"] == "deepseek-v4-flash"
    assert displaced == "gpt-5.6-luna"
    # None means "this provider", which is what a bare fallback name has always
    # meant. Only an entry that names another provider sends the call to
    # another client, and this is the case that must not start doing that.
    assert on_provider is None
    assert provider.serving_model(kw.get("model")) == ("deepseek-v4-flash", "gpt-5.6-luna")


def test_a_cross_provider_fallback_is_routed_to_that_provider():
    """The pre-routing has to move the call, not just the model name.

    While the requested model is inside its outage window every call goes
    straight to the fallback. If that fallback lives elsewhere and only
    `kw["model"]` were swapped, each of those calls would post another
    provider's model name at the provider that is already down -- a 404 on top
    of an outage, once per call, for the whole hour-long window.
    """
    provider = _Provider(fallback_model=[
        {"model": "local-moe", "provider": "freetoken"}])
    provider._mark_model_down("gpt-5.6-luna")

    routed, displaced, on_provider = provider._route_around_dead_model(
        {"model": "gpt-5.6-luna"})

    assert routed["model"] == "local-moe"
    assert displaced == "gpt-5.6-luna"
    assert on_provider == "freetoken"
    # The family is still told the truth about which model answers them.
    assert provider.serving_model("gpt-5.6-luna") == ("local-moe", "gpt-5.6-luna")


def test_one_provider_going_down_does_not_condemn_the_same_name_elsewhere():
    """The same weights are served under the same name by more than one host.

    Filing them under one health key would mark the local copy down because the
    hosted one was, and route around the rescue that was working.
    """
    provider = _Provider(fallback_model=[
        {"model": "deepseek-v4-flash", "provider": "freetoken"}])
    provider._mark_model_down("deepseek-v4-flash")

    assert provider._model_is_down("deepseek-v4-flash")
    assert not provider._model_is_down("deepseek-v4-flash", "freetoken")


# --- one outage, every route that shares the gateway ------------------------

def test_a_route_that_learns_the_outage_covers_the_others_on_that_gateway():
    """cli/commands.py builds a provider per route, and luna is the model on
    four of them here. Instance-local windows meant the notification route —
    ~300 turns a day — kept naming luna and kept paying the whole ladder for
    an hour after the main chat had already established it was down."""
    zen = "https://opencode.ai/zen/v1"
    main = _Provider(fallback_model="deepseek-v4-flash", api_base=zen)
    notificaciones = _Provider(fallback_model="deepseek-v4-flash", api_base=zen)

    main._mark_model_down("gpt-5.6-luna")

    assert notificaciones.serving_model("gpt-5.6-luna") == (
        "deepseek-v4-flash", "gpt-5.6-luna",
    )


def test_another_gateway_is_not_told_anything():
    """A model name means nothing without the host serving it: qwen3-vl being
    down on the house's Ollama says nothing about opencode, and vice versa."""
    opencode = _Provider(fallback_model="deepseek-v4-flash",
                         api_base="https://opencode.ai/zen/v1")
    ollama = _Provider(default="qwen3-vl:8b", fallback_model="deepseek-v4-flash",
                       api_base="http://ollama.home:11434/v1")

    opencode._mark_model_down("gpt-5.6-luna")

    assert ollama.serving_model("gpt-5.6-luna") == ("gpt-5.6-luna", None)


def test_a_provider_with_no_gateway_keeps_its_own_window():
    """"Gateway unknown" is not "same gateway" — an SDK-configured client or
    a test double keeps the old instance-local behaviour rather than sharing a
    window with every other unidentified provider in the process."""
    one = _Provider(fallback_model="deepseek-v4-flash")
    two = _Provider(fallback_model="deepseek-v4-flash")

    one._mark_model_down("gpt-5.6-luna")

    assert one.serving_model("gpt-5.6-luna")[0] == "deepseek-v4-flash"
    assert two.serving_model("gpt-5.6-luna") == ("gpt-5.6-luna", None)


def test_the_shared_window_expires_for_everyone_at_once():
    zen = "https://opencode.ai/zen/v1"
    main = _Provider(fallback_model="deepseek-v4-flash", api_base=zen)
    other = _Provider(fallback_model="deepseek-v4-flash", api_base=zen)
    main._mark_model_down("gpt-5.6-luna")

    import time

    # Both halves of the window, because there are two. `_mark_model_down`
    # writes the in-process one AND the file every container on the box reads,
    # and the file outlives the process that wrote it -- so expiring only the
    # first is not an expiry: the next reader adopts a fresh window from disk.
    # Written out rather than left implicit because the version of this test
    # that expired one half passed only where the store was unwritable, which
    # is a developer laptop and not the image the deployer gates on.
    main._model_down_until["gpt-5.6-luna"] = time.monotonic() - 1
    SHARED_OUTAGES.mark(zen, "gpt-5.6-luna", time.time() - 1)

    assert other.serving_model("gpt-5.6-luna") == ("gpt-5.6-luna", None)


# --- writing it into the block ----------------------------------------------

def test_the_model_goes_inside_the_block_and_not_after_it():
    messages = [{"role": "user", "content": _block("Current Time: 10:00") + "\n\nhola"}]

    ContextBuilder.annotate_runtime_model(messages, "gpt-5.6-luna")

    content = messages[0]["content"]
    assert "Current Time: 10:00\nModel: gpt-5.6-luna\n" + END in content
    assert content.endswith("hola"), "the user's own message must not move"


def test_the_fallback_line_names_the_model_it_is_standing_in_for():
    messages = [{"role": "user", "content": _block("Current Time: 10:00")}]

    ContextBuilder.annotate_runtime_model(
        messages, "deepseek-v4-flash", displaced="gpt-5.6-luna"
    )

    line = "Model: deepseek-v4-flash (outage fallback — gpt-5.6-luna is not answering)"
    assert line in messages[0]["content"]


def test_a_turn_carrying_an_image_is_annotated_in_its_text_block():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": _block("Current Time: 10:00") + "\n\n¿qué es esto?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]

    ContextBuilder.annotate_runtime_model(messages, "qwen3-vl:8b")

    assert "Model: qwen3-vl:8b" in messages[0]["content"][0]["text"]
    assert messages[0]["content"][1]["type"] == "image_url", "the image survives"


def test_only_this_turn_is_annotated():
    """History already carries the block each of those turns was sent with.
    Rewriting them would relabel yesterday's answers with today's model."""
    messages = [
        {"role": "user", "content": _block("Current Time: 09:00") + "\n\nayer"},
        {"role": "assistant", "content": "listo"},
        {"role": "user", "content": _block("Current Time: 10:00") + "\n\nhoy"},
    ]

    ContextBuilder.annotate_runtime_model(messages, "gpt-5.6-luna")

    assert "Model:" not in messages[0]["content"]
    assert "Model: gpt-5.6-luna" in messages[2]["content"]


def test_a_quoted_block_in_the_family_s_own_text_cannot_steal_the_line():
    """The block is followed by text this house does not control. Somebody
    pasting a log — or asking what the prompt looks like — carries a second
    closing marker, and anchoring on the last one would write the model into
    their prose and leave the real block without the one line the prompt calls
    its only source. `_save_turn` cuts at the first marker for the same
    reason."""
    quoted = _block("Current Time: 09:00") + "\n\nModel: claude-opus-4-5"
    messages = [{"role": "user", "content":
                 _block("Current Time: 10:00") + "\n\nmirá lo que me llegó:\n" + quoted}]

    ContextBuilder.annotate_runtime_model(messages, "gpt-5.6-luna")

    content = messages[0]["content"]
    real_block = content[:content.index(END) + len(END)]
    assert "Current Time: 10:00\nModel: gpt-5.6-luna\n" + END == real_block[real_block.index("Current Time"):]
    assert content.count("Model: gpt-5.6-luna") == 1, "written once, into the real block"


def test_the_line_goes_under_current_time_and_not_after_a_resumed_session():
    """`_build_runtime_context` appends `[Resumed Session]` and its summary
    last — the normal path for a family that messages hours apart. A `Model:`
    line after that prose reads as the closing sentence of a summary of the
    session that ended, not as a fact about this one."""
    block = ContextBuilder._build_runtime_context(
        "websocket", "homeweb:1", None, session_summary="Hablamos de las cámaras.",
    )
    messages = [{"role": "user", "content": block + "\n\n¿en qué quedamos?"}]

    ContextBuilder.annotate_runtime_model(messages, "gpt-5.6-luna")

    lines = messages[0]["content"].split("\n")
    assert lines[2] == "Model: gpt-5.6-luna", "directly under Current Time"
    assert lines[1].startswith("Current Time: ")
    assert lines.index("Model: gpt-5.6-luna") < lines.index("[Resumed Session]")


def test_a_message_list_with_no_block_is_left_exactly_as_it_is():
    """A subagent, or any caller that built its own messages, gets no invented
    block — an empty one would be a second place to read the time from."""
    messages = [{"role": "user", "content": "hola"}]

    ContextBuilder.annotate_runtime_model(messages, "gpt-5.6-luna")

    assert messages == [{"role": "user", "content": "hola"}]


def test_the_annotator_finds_the_block_build_messages_actually_produces(tmp_path):
    """The two halves are matched on the tag constants; this is the test that
    fails if either side changes its shape."""
    builder = ContextBuilder(tmp_path)
    messages = builder.build_messages(history=[], current_message="¿qué modelo sos?")

    ContextBuilder.annotate_runtime_model(messages, "gpt-5.6-luna")

    text = messages[-1]["content"]
    assert isinstance(text, str)
    assert "\nModel: gpt-5.6-luna\n" + END in text
    assert text.index("Model:") < text.index("¿qué modelo sos?")


# --- the loop puts the turn's own model there -------------------------------

def _loop(tmp_path: Path, **kw) -> AgentLoop:
    return AgentLoop(bus=MagicMock(), workspace=tmp_path, **kw)


async def _run(loop, monkeypatch, *runners, **kw) -> str:
    """Run one turn with the runners stubbed, returning the block that was sent."""
    seen: list[list[dict]] = []


    async def fake_run(spec):
        seen.append(spec.initial_messages)
        return AgentRunResult(final_content="ok", messages=[])

    for runner in runners:
        monkeypatch.setattr(runner, "run", fake_run)

    builder = ContextBuilder(loop.workspace)
    messages = builder.build_messages(history=[], current_message="¿qué modelo sos?")
    await loop._run_agent_loop(messages, **kw)
    return seen[0][-1]["content"]


@pytest.mark.asyncio
async def test_the_turns_own_model_reaches_the_prompt(tmp_path, monkeypatch):
    loop = _loop(tmp_path, provider=_Provider(), model="gpt-5.6-luna")

    sent = await _run(loop, monkeypatch, loop.runner)

    assert "Model: gpt-5.6-luna" in sent


@pytest.mark.asyncio
async def test_a_powerful_turn_says_the_powerful_model(tmp_path, monkeypatch):
    """And says it from the *powerful* provider's own health, not the main
    one's. Each route is a separate provider instance with a separate outage
    window (cli/commands.py builds one per role), so asking `self.provider`
    about a route it does not serve would answer from the wrong instance —
    and with no fallback configured on either double, every model name would
    round-trip unchanged and the mistake would not show. Hence the down mark
    here: it can only be read off the provider that owns it."""
    powerful = _Provider(default="deepseek-v4-pro", fallback_model="deepseek-v4-flash")
    powerful._mark_model_down("deepseek-v4-pro")
    loop = _loop(
        tmp_path,
        provider=_Provider(),
        model="gpt-5.6-luna",
        powerful_provider=powerful,
        powerful_model="deepseek-v4-pro",
    )

    sent = await _run(loop, monkeypatch, loop.runner, loop._powerful_runner, powerful=True)

    assert "Model: deepseek-v4-flash (outage fallback — deepseek-v4-pro is not answering)" in sent
    assert "gpt-5.6-luna" not in sent


@pytest.mark.asyncio
async def test_a_question_drained_mid_turn_is_labelled_too(tmp_path, monkeypatch):
    """Somebody asking "¿qué modelo sos?" while a turn is already running has
    it drained into that turn, and the block it arrives under is the *last*
    one the model reads. Unlabelled, that is the one place where following the
    prompt's "read the Model line" finds nothing and falls back to the roster.
    """
    import asyncio

    from nanobot.bus.events import InboundMessage

    provider = _Provider(fallback_model="deepseek-v4-flash")
    provider._mark_model_down("gpt-5.6-luna")
    loop = _loop(tmp_path, provider=provider, model="gpt-5.6-luna")

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(InboundMessage(
        channel="websocket", sender_id="user1", chat_id="homeweb:1",
        content="¿qué modelo sos?",
    ))
    drained: list[list[dict]] = []


    async def fake_run(spec):
        drained.append(await spec.injection_callback())
        return AgentRunResult(final_content="ok", messages=[])

    monkeypatch.setattr(loop.runner, "run", fake_run)
    builder = ContextBuilder(loop.workspace)
    messages = builder.build_messages(history=[], current_message="hola")
    await loop._run_agent_loop(messages, pending_queue=queue)

    assert "Model: deepseek-v4-flash (outage fallback — gpt-5.6-luna is not answering)" \
        in drained[0][0]["content"]


@pytest.mark.asyncio
async def test_the_turn_is_billed_to_the_model_that_spent_the_tokens(tmp_path, monkeypatch):
    """The cost ledger is exactly the kind of consumer that must not read the
    roster: during the window the fallback spends the tokens, and filing them
    against the model that did not answer bills a dead model at the wrong price
    and shows the working one at zero."""
    provider = _Provider(fallback_model="deepseek-v4-flash")
    provider._mark_model_down("gpt-5.6-luna")
    loop = _loop(tmp_path, provider=provider, model="gpt-5.6-luna")
    billed: list[str | None] = []
    monkeypatch.setattr(
        "nanobot.agent.loop.report_usage",
        lambda key, model, usage, tools, **kw: billed.append(model),
    )

    await _run(loop, monkeypatch, loop.runner)

    assert billed == ["deepseek-v4-flash"]


@pytest.mark.asyncio
async def test_a_fallback_that_fires_mid_turn_is_still_billed_correctly(tmp_path, monkeypatch):
    """The turn the label cannot catch. The prompt went out naming the model
    that was asked for, because the model was still alive when it was built —
    but `served_by_model` comes back from the provider afterwards and says who
    actually replied, so the ledger has no excuse. The window this opens makes
    the *label* right from the next turn on."""
    loop = _loop(tmp_path, provider=_Provider(), model="gpt-5.6-luna")
    billed: list[str | None] = []
    monkeypatch.setattr(
        "nanobot.agent.loop.report_usage",
        lambda key, model, usage, tools, **kw: billed.append(model),
    )

    async def fake_run(spec):
        return AgentRunResult(
            final_content="ok", messages=[], served_by_model="deepseek-v4-flash",
        )

    monkeypatch.setattr(loop.runner, "run", fake_run)
    builder = ContextBuilder(loop.workspace)
    messages = builder.build_messages(history=[], current_message="hola")
    await loop._run_agent_loop(messages)

    assert "Model: gpt-5.6-luna" in messages[-1]["content"], "labelled with what was asked"
    assert billed == ["deepseek-v4-flash"], "billed to what answered"


@pytest.mark.asyncio
async def test_during_an_outage_the_prompt_names_the_fallback(tmp_path, monkeypatch):
    """The turn this house was missing: the roster still says luna, and for the
    next hour luna is precisely the model that is not answering."""
    provider = _Provider(fallback_model="deepseek-v4-flash")
    provider._mark_model_down("gpt-5.6-luna")
    loop = _loop(tmp_path, provider=provider, model="gpt-5.6-luna")

    sent = await _run(loop, monkeypatch, loop.runner)

    assert "Model: deepseek-v4-flash (outage fallback — gpt-5.6-luna is not answering)" in sent
