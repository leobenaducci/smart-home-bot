"""The daily greeting, and the four ways the hand-made version of it failed.

It kept posting into the day the job was created; it ended its turn with a
receipt that the delivery gate then suppressed as routine; it ran in a session
of its own, so replying to it talked to an agent that had never heard of it;
and it lived on one instance, which can only write into one person's history.
Each test below is one of those.
"""

import json
from pathlib import Path

import pytest

from nanobot.agent.morning_greeting import CHANNEL, MorningGreeting

TZ = "Etc/UTC"
# 2026-08-03 07:00:00 -04:00 — a Monday, the hour the house is greeted.
FIRE_MS = 1785754800000


class _Response:
    def __init__(self, content):
        self.content = content


class _Agent:
    """Records how the turn was run, and answers with a fixed text."""

    def __init__(self, answer="¡Buenos días Alex! Lunes 3 de agosto. …"):
        self.answer = answer
        self.calls = []

    async def run_turn(self, prompt, *, session_key, channel, chat_id):
        self.calls.append({"prompt": prompt, "session_key": session_key,
                           "channel": channel, "chat_id": chat_id})
        return _Response(self.answer)


class _Bus:
    def __init__(self):
        self.published = []

    async def publish(self, chat_id, text):
        self.published.append((chat_id, text))


def _greeting(tmp_path, wording="Escribe el saludo.", user_id="user1",
              agent=None, bus=None):
    if wording is not None:
        (tmp_path / "MORNING.md").write_text(wording, encoding="utf-8")
    agent = agent or _Agent()
    bus = bus or _Bus()
    g = MorningGreeting(tmp_path, TZ, user_id, agent.run_turn, bus.publish)
    return g, agent, bus


@pytest.mark.asyncio
async def test_it_delivers_into_todays_chat(tmp_path):
    """The target is built at fire time, so there is no stored day to go stale
    — the failure that made the first version invisible for weeks."""
    g, agent, bus = _greeting(tmp_path)
    await g.run(now_ms=FIRE_MS)
    chat_id, text = bus.published[0]
    assert chat_id == f"homeweb:user1:2026-08-03:{FIRE_MS}"
    assert text.startswith("¡Buenos días Alex!")


@pytest.mark.asyncio
async def test_each_firing_opens_its_own_conversation(tmp_path):
    g, _, bus = _greeting(tmp_path)
    await g.run(now_ms=FIRE_MS)
    await g.run(now_ms=FIRE_MS + 86_400_000)
    first, second = (c for c, _ in bus.published)
    assert first != second


@pytest.mark.asyncio
async def test_the_turn_runs_in_the_conversation_the_reply_will_land_in(tmp_path):
    """HomeCore sends channel "websocket" and session_id = chat_id, so the key
    for a reply is websocket:<chat_id>. Run anywhere else and "gracias, ¿qué
    tengo hoy?" reaches an agent that never said good morning."""
    g, agent, bus = _greeting(tmp_path)
    await g.run(now_ms=FIRE_MS)
    call = agent.calls[0]
    assert call["channel"] == CHANNEL == "websocket"
    assert call["session_key"] == f"websocket:{call['chat_id']}"
    assert call["chat_id"] == bus.published[0][0]


@pytest.mark.asyncio
async def test_the_wording_comes_from_the_file(tmp_path):
    """MORNING.md is mounted, not baked in: rewording the greeting must not
    need a rebuild."""
    g, agent, _ = _greeting(tmp_path, wording="Salúdala en gallego.")
    await g.run(now_ms=FIRE_MS)
    assert agent.calls[0]["prompt"] == "Salúdala en gallego."


@pytest.mark.asyncio
async def test_no_wording_file_means_no_greeting(tmp_path):
    """Rather than inventing a default and putting words in Alfred's mouth
    that nobody wrote."""
    g, agent, bus = _greeting(tmp_path, wording=None)
    assert await g.run(now_ms=FIRE_MS) is None
    assert agent.calls == [] and bus.published == []


@pytest.mark.asyncio
async def test_an_empty_wording_file_means_no_greeting(tmp_path):
    g, agent, bus = _greeting(tmp_path, wording="   \n\n")
    assert await g.run(now_ms=FIRE_MS) is None
    assert agent.calls == [] and bus.published == []


@pytest.mark.asyncio
async def test_an_instance_with_nobody_to_greet_stays_quiet(tmp_path):
    """No HOMECORE_USER_ID means no chat to write into. Silence beats a message
    addressed to a chat_id nobody owns."""
    g, agent, bus = _greeting(tmp_path, user_id="")
    assert g.addressable is False
    assert await g.run(now_ms=FIRE_MS) is None
    assert agent.calls == [] and bus.published == []


@pytest.mark.asyncio
async def test_an_empty_answer_is_not_published(tmp_path):
    g, _, bus = _greeting(tmp_path, agent=_Agent(answer="   "))
    assert await g.run(now_ms=FIRE_MS) is None
    assert bus.published == []


@pytest.mark.asyncio
async def test_the_answer_is_delivered_verbatim(tmp_path):
    """No gate in front of it. The previous version's last line was a receipt
    ("saludos enviados a todos"), which evaluate_response suppressed as a
    routine status — the message existed and nobody ever saw it."""
    text = "¡Buenos días Kai! 🌅 Lunes 3 de agosto.\n…\n🌤️ 6°/17°, despejado."
    g, _, bus = _greeting(tmp_path, agent=_Agent(answer=text))
    assert await g.run(now_ms=FIRE_MS) == text
    assert bus.published == [(f"homeweb:user1:2026-08-03:{FIRE_MS}", text)]


@pytest.mark.asyncio
async def test_an_unknown_timezone_still_greets(tmp_path):
    """tzdata missing in a slim image must not cost the family its greeting."""
    g = MorningGreeting(tmp_path, "Mars/Olympus", "user1",
                        _Agent().run_turn, _Bus().publish)
    (tmp_path / "MORNING.md").write_text("hola", encoding="utf-8")
    out = await g.run(now_ms=FIRE_MS)
    assert out


# --- how it is wired up --------------------------------------------------


def test_it_is_off_until_asked_for():
    """It writes into somebody's chat unprompted, so stock nanobot must not do
    it by default."""
    from nanobot.config.schema import GatewayConfig
    assert GatewayConfig().morning_greeting.enabled is False


def test_the_house_config_turns_it_on_for_everyone():
    """One line, deployed to every per-member instance — the point of building
    it in rather than creating a job per person by hand."""
    cfg = json.loads(
        (Path(__file__).resolve().parents[2] / "config" / "config.json")
        .read_text(encoding="utf-8")
    )
    greeting = cfg["gateway"]["morningGreeting"]
    assert greeting["enabled"] is True
    assert greeting["cron"] == "0 7 * * *"


def test_the_wording_file_is_symlinked_into_every_workspace():
    """MORNING.md is only readable if the entrypoint links it in, and the job
    goes quiet without it — a silence that would be blamed on the schedule."""
    entrypoint = (Path(__file__).resolve().parents[2] / "entrypoint.sh")
    line = next(ln for ln in entrypoint.read_text(encoding="utf-8").splitlines()
                if ln.startswith("for f in "))
    assert "MORNING.md" in line
