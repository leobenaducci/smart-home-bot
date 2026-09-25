"""A skill-invocation block may only leave by the path that executes it.

The runner strips blocks from the assistant's reply text. That is the one path
it watches, and the model does not always use it. Across 46 live probes on one
instance it pushed blocks out through two unguarded side doors:

    MessageTool   content={"skill":"geo","action":"list_places"}
    ExecTool      echo '{"skill":"file-share","action":"list_files"}'

Nine of those probes showed raw JSON to the family, and two produced no answer
at all because the block went out sideways and the turn never ran the skill.

Both tools now refuse, rather than strip. Stripping sends an empty message and
leaves the skill un-run; an error the model can read moves the block onto the
path that works.
"""
import pytest

from nanobot.agent.skill_invocation import (
    contains_skill_invocation,
    is_skill_invocation_json,
    register_skill_translation,
)
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.shell import ExecTool

# --- what counts as a block ---------------------------------------------

BLOCKS = [
    '{"skill":"geo","action":"list_places"}',
    '{"skill": "camera-feed", "action": "snapshot", "camera": "patio"}',
    '```json\n{"skill":"menu","action":"list_menu"}\n```',
    '```\n{"skill":"tasks","action":"list_chores"}\n```',
]

NOT_BLOCKS = [
    "",
    "Aquí tiene la foto del patio.",
    # names a skill in words — an instruction, not a block
    "Usa la skill de cámaras y mándame el patio",
    "La reunión es a las {hora} en la sala",
    '{"status": "ok", "count": 3}',
    # "skill" as a value rather than the key
    '{"topic": "skill", "note": "hablamos de eso"}',
]


@pytest.mark.parametrize("text", BLOCKS)
def test_detects_a_block(text):
    assert contains_skill_invocation(text)


@pytest.mark.parametrize("text", NOT_BLOCKS)
def test_leaves_ordinary_text_alone(text):
    assert not contains_skill_invocation(text)


def test_none_is_not_a_block():
    """Callers hold optional strings; a blank one once crashed a whole turn."""
    assert not contains_skill_invocation(None)
    assert not is_skill_invocation_json(None)


def test_strict_check_requires_the_whole_payload():
    """is_skill_invocation_json decides whether a shell argument IS a block, so
    it must not fire on text that merely contains one."""
    assert is_skill_invocation_json('{"skill":"geo","action":"list_places"}')
    assert not is_skill_invocation_json('mira esto: {"skill":"geo"}')
    assert not is_skill_invocation_json('{"not_json')


# --- MessageTool ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("block", BLOCKS)
async def test_message_tool_refuses_a_block(block):
    sent = []

    tool = MessageTool(send_callback=lambda m: sent.append(m))
    tool.set_context("websocket", "chat1")
    result = await tool.execute(content=block)

    assert result.startswith("Error:")
    assert not sent, "the block must not reach the channel"


@pytest.mark.asyncio
async def test_message_tool_error_says_what_to_do_instead():
    tool = MessageTool(send_callback=lambda m: None)
    tool.set_context("websocket", "chat1")
    result = await tool.execute(content='{"skill":"geo","action":"list_places"}')
    assert "plain text" in result


@pytest.mark.asyncio
async def test_message_tool_still_sends_real_messages():
    sent = []

    async def capture(msg):
        sent.append(msg)

    tool = MessageTool(send_callback=capture)
    tool.set_context("websocket", "chat1")
    result = await tool.execute(content="Aquí tiene la cámara del patio:")

    assert result.startswith("Message sent")
    assert [m.content for m in sent] == ["Aquí tiene la cámara del patio:"]


# --- ExecTool ------------------------------------------------------------


@pytest.mark.parametrize("command", [
    """echo '{"skill":"file-share","action":"list_files"}'""",
    '''echo "{\\"skill\\":\\"file-share\\",\\"action\\":\\"list_files\\"}"''',
    """printf '{"skill":"camera-feed","action":"snapshot","camera":"patio"}'""",
])
def test_exec_refuses_a_block_echoed_through_the_shell(command):
    error = ExecTool._skill_invocation_error(command)
    assert error and error.startswith("Error:")
    assert "plain text" in error


@pytest.mark.parametrize("command", [
    "ls -la",
    "echo hola",
    "curl -s http://compute.home:5000/api/cameras",
    # handling JSON that contains a skill key is fine — only echoing it is not
    """python3 -c 'import json; print(json.load(open("x.json"))["skill"])'""",
    # a real payload that merely mentions the word
    """echo '{"topic":"skill","note":"ok"}'""",
])
def test_exec_leaves_ordinary_commands_alone(command):
    assert ExecTool._skill_invocation_error(command) is None


def test_exec_does_not_choke_on_unbalanced_quotes():
    """bash -n reports those; this check must not raise first."""
    assert ExecTool._skill_invocation_error("""echo '{"skill":"geo"}""") is None


# --- reimplementing a skill by hand --------------------------------------
#
# The observed failure: asked for a PDF from Paperless, the model ignored the
# skill, wrote its own python3 -c against PAPERLESS_URL, saved the file where
# the chat could not serve it, and said "le envié el archivo" having sent
# nothing. SOUL.md and AGENTS.md both forbid this in words; words were not
# enough.


REIMPLEMENTATIONS = [
    # the one that actually happened
    '''python3 -c "import subprocess, os; BASE=os.environ['PAPERLESS_URL']"''',
    """curl -s -H "Authorization: Token $PAPERLESS_API_TOKEN" http://paperless:8000/api/documents/""",
    """curl -s "$TASKS_API_URL/chores" """,
    """curl -X POST "$HOME_LIGHTS_API_URL/lights/on" """,
]


@pytest.mark.parametrize("command", REIMPLEMENTATIONS)
def test_refuses_hand_written_access_to_a_skills_service(command):
    error = ExecTool._skill_reimplementation_error(command)
    assert error and error.startswith("Error:")


# The host signals used to be three literals ending in a DNS suffix this house
# does not use, so they matched nothing here and the redirect never fired for a
# model curling its own camera or paperless box. They are read from the same
# URLs the deployer supplies instead -- which means the test has to supply them
# too, and asserting on a literal would put the original bug back.
@pytest.mark.parametrize("var, url, command, skill", [
    ("PAPERLESS_URL", "http://192.0.2.7:21030",
     "curl -s http://192.0.2.7:21030/api/documents/", "paperless"),
    ("CAMERA_API_URL", "http://192.0.2.7:5000",
     "curl -s http://192.0.2.7:5000/snapshot/45b0ad58 -o /tmp/x.jpg", "camera-feed"),
    ("NANOBOT_N8N_BASE_URL", "http://192.0.2.9:5678",
     "curl -s http://192.0.2.9:5678/api/v1/workflows", "n8n"),
])
def test_a_services_own_address_is_a_signal(monkeypatch, var, url, command, skill):
    monkeypatch.setenv(var, url)
    error = ExecTool._skill_reimplementation_error(command)
    assert error and error.startswith("Error:")
    assert skill in error


def test_loopback_is_not_a_signal(monkeypatch):
    """Every other service on the box shares it, so matching it would redirect
    unrelated commands into a skill that cannot serve them."""
    monkeypatch.setenv("PAPERLESS_URL", "http://127.0.0.1:21030")
    assert ExecTool._skill_reimplementation_error(
        "curl -s http://127.0.0.1:9999/health") is None


def test_a_service_with_no_address_contributes_no_signal(monkeypatch):
    monkeypatch.delenv("CAMERA_API_URL", raising=False)
    assert ExecTool._skill_reimplementation_error(
        "curl -s http://192.0.2.7:5000/snapshot/1") is None


def test_the_refusal_names_the_skill_to_use():
    """An error the model cannot act on costs the same turn twice."""
    error = ExecTool._skill_reimplementation_error(
        '''python3 -c "import os; os.environ['PAPERLESS_URL']"''')
    assert "`paperless`" in error and "invocation block" in error


def _wrapped(source: bytes) -> str:
    """The command the translator emits for *source*, byte for byte."""
    import base64
    payload = base64.b64encode(source).decode()
    return f"""python -c "import base64; exec(base64.b64decode(b'{payload}').decode('utf-8'))\""""


def test_the_skills_own_generated_code_still_runs():
    """Skills execute through this same tool. Blocking them would take out
    every JSON-invocable skill in the house."""
    command = _wrapped(b"import requests; print(1)")
    register_skill_translation(command)
    assert ExecTool._skill_reimplementation_error(command) is None


def test_a_translated_call_can_run_twice():
    """Registration is not consumed. A retry re-runs the skill's own code,
    which is what would have happened without the ledger at all."""
    command = _wrapped(b"print(1)")
    register_skill_translation(command)
    assert ExecTool._skill_reimplementation_error(command) is None
    assert ExecTool._skill_reimplementation_error(command) is None


def test_the_wrapper_alone_vouches_for_nothing():
    """The shape used to be the whole test. It is visible to the model in its
    own tool-call history, so it is not evidence of anything."""
    command = _wrapped(b"import os; print(os.environ['PAPERLESS_API_TOKEN'])")
    assert ExecTool._skill_reimplementation_error(command) is not None


def test_the_dishes_payload_is_refused():
    """2026-08-11, asked whose turn it was to wash the dishes: a hand-written
    sixteen-line copy of the tasks skill's internals, calling its private
    _curl with a path it invented, base64'd into the translator's wrapper.
    It ran, because nothing looked inside the base64."""
    source = (
        b"import subprocess, json, os\n"
        b"BASE = os.environ.get('TASKS_API_URL', 'https://hub.home:8443/tasks/api')\n"
        b"def _curl(method, path):\n"
        b"    pass\n"
        b"print(json.dumps(_curl('GET', 'list?scope=all')))\n"
    )
    error = ExecTool._skill_reimplementation_error(_wrapped(source))
    assert error and "`tasks`" in error


def test_an_unregistered_payload_is_refused_even_inline():
    """Not every hand-written payload bothers with base64."""
    command = """python3 -c "import os; print(os.environ['TASKS_API_URL'])" """
    assert ExecTool._skill_reimplementation_error(command) is not None


@pytest.mark.parametrize("command", [
    "ls -la",
    "curl -s https://api.open-meteo.com/v1/forecast?latitude=-33.45",
    "curl -s https://es.wikipedia.org/wiki/Gabriela_Mistral",
    "python3 -c 'print(2+2)'",
    # a different host on the same box is not the camera API
    "curl -s http://compute.home:9000/status",
])
def test_leaves_everything_else_alone(command):
    """Only services a skill owns. The model still needs a shell."""
    assert ExecTool._skill_reimplementation_error(command) is None
