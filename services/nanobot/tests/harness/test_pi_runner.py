"""The harness around pi: what it lets through, what it asks for, what it refuses.

A fake `pi` stands in for the real one: it prints pi's JSON event stream for a
scripted run, one script per round, and records the arguments and environment
it was started with. What is under test is the harness, not pi or a model.
"""
import asyncio
import json
import os
import signal
import stat
import sys
from pathlib import Path

import pytest

from nanobot.harness import pi_runner as H

FAKE = r'''#!{python}
import json, os, sys
state = {state!r}
rounds = json.load(open(state))
n = sum(1 for _ in open(state + ".log")) if os.path.exists(state + ".log") else 0
with open(state + ".log", "a") as fh:
    skill_env = json.load(open(os.environ["ALFRED_SKILL_ENV_FILE"])) if os.environ.get("ALFRED_SKILL_ENV_FILE") else {{}}
    fh.write(json.dumps({{"argv": sys.argv[1:], "env": {{k: os.environ.get(k) for k in
        ("OPENCODE_API_KEY", "OPENROUTER_API_KEY", "PI_TELEMETRY", "PI_OFFLINE",
         "ALFRED_HARNESS_BENCH", "ALFRED_HARNESS_API_KEY", "HOME_LIGHTS_API_URL",
         "ALFRED_HARNESS_HEADER_0")}},
        "skill_env": {{k: skill_env.get(k) for k in ("HOME_LIGHTS_API_URL", "OPENCODE_API_KEY")}},
        "skill_env_mode": oct(os.stat(os.environ["ALFRED_SKILL_ENV_FILE"]).st_mode & 0o777)}}) + "\n")
script = rounds[min(n, len(rounds) - 1)]
if not script.get("exit"):
    sd = sys.argv[sys.argv.index("--session-dir") + 1]
    os.makedirs(sd, exist_ok=True)
    open(os.path.join(sd, "session.jsonl"), "a").close()
if script.get("sleep"):
    import time; time.sleep(script["sleep"])
if script.get("exit"):
    sys.stderr.write("boom: connection refused\n"); sys.exit(script["exit"])
def emit(e): print(json.dumps(e), flush=True)
emit({{"type": "agent_start"}})
print('{{"type":"message_update","partial":"' + "x" * 5000 + '"}}', flush=True)
for i, step in enumerate(script.get("tools", [])):
    emit({{"type": "tool_execution_start", "toolCallId": str(i), "toolName": step["name"], "args": step.get("args", {{}})}})
    emit({{"type": "tool_execution_end", "toolCallId": str(i), "toolName": step["name"],
           "isError": step.get("error", False),
           "result": {{"content": [{{"type": "text", "text": step.get("result", "")}}], "details": {{}}}}}})
if script.get("note"):
    emit({{"type": "message_end", "message": {{"role": "assistant", "content": [{{"type": "text", "text": script["note"]}}, {{"type": "toolCall", "name": "web"}}]}}}})
emit({{"type": "message_end", "message": {{"role": "assistant", "content": [{{"type": "text", "text": script.get("text", "")}}]}}}})
emit({{"type": "agent_end"}})
'''

DOC_RESULT = json.dumps({"format": "pdf", "link": "download:tomi/alfred/documents/guia.pdf"}, indent=2)


@pytest.fixture()
def fake_pi(tmp_path, monkeypatch):
    home = tmp_path / "pi"
    (home / "node_modules" / ".bin").mkdir(parents=True)
    (home / "ext").mkdir()
    (home / "ext" / "alfred.ts").write_text("// fake")
    binary = home / "node_modules" / ".bin" / "pi"
    state = tmp_path / "rounds.json"
    binary.write_text(FAKE.format(python=sys.executable, state=str(state)))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(H, "PI_BIN", binary)
    monkeypatch.setattr(H, "EXTENSION", home / "ext" / "alfred.ts")

    def script(*rounds):
        state.write_text(json.dumps(list(rounds)))
        return state
    return script


def calls(state: Path) -> list[dict]:
    return [json.loads(l) for l in open(str(state) + ".log")]


EP = H.Endpoint(base_url="http://host.docker.internal:11436/v1", model="gemma4")
TASK = "Buscá una receta de pastel de choclo y armame una guía en PDF."


def run(tmp_path, **kw):
    return asyncio.run(H.run(TASK, EP, tmp_path / "work", **kw))


def test_a_claimed_file_is_not_a_delivered_one(fake_pi, tmp_path):
    """Measured on the prototype: "the guide is created", with no call behind it."""
    state = fake_pi(
        {"tools": [{"name": "web", "args": {"action": "search"}, "result": "1. receta"}],
         "text": "Listo, aquí está su guía: [The guide is successfully created]"},
        {"tools": [{"name": "make_document", "args": {"format": "pdf"}, "result": DOC_RESULT}],
         "text": "Aquí está: download:tomi/alfred/documents/guia.pdf"},
    )
    res = run(tmp_path)
    assert res.delivered and res.rounds == 2
    assert res.links == ["download:tomi/alfred/documents/guia.pdf"]
    assert res.text.count("download:tomi/alfred/documents/guia.pdf") == 1
    second = calls(state)[1]["argv"]
    assert "--continue" in second and "make_document" in second[-1]


def test_the_real_link_is_added_when_the_answer_drops_it(fake_pi, tmp_path):
    fake_pi({"tools": [{"name": "make_document", "result": DOC_RESULT}],
             "text": "Hice la guía.<channel|>"})
    res = run(tmp_path)
    assert res.delivered and res.rounds == 1
    assert "<channel|>" not in res.text
    assert res.text.endswith("download:tomi/alfred/documents/guia.pdf")


def test_nudges_run_out(fake_pi, tmp_path):
    fake_pi({"text": ""})
    res = run(tmp_path, max_nudges=2)
    assert not res.delivered and res.rounds == 3


def test_a_task_without_a_file_is_done_when_it_answers(fake_pi, tmp_path):
    fake_pi({"tools": [{"name": "web", "result": "x"}], "text": "El dólar cerró en 950."})
    res = asyncio.run(H.run("Resumí qué pasó con el dólar esta semana.", EP, tmp_path / "w"))
    assert res.delivered and res.rounds == 1 and res.links == []


def test_pi_holds_no_secret_and_never_phones_home(fake_pi, tmp_path, monkeypatch):
    """pi reads OPENCODE_API_KEY by itself (an unattended caller on the Go plan
    gets the account blocked), and anything in its environment is one injected
    page away from leaving. The skills still get the household's environment --
    through a 0600 file, gone when the run ends."""
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-go")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setenv("HOME_LIGHTS_API_URL", "http://lights")
    state = fake_pi({"tools": [{"name": "make_document", "result": DOC_RESULT}], "text": "ok"})
    run(tmp_path)
    first = calls(state)[0]
    env = first["env"]
    assert env["OPENCODE_API_KEY"] is None and env["OPENROUTER_API_KEY"] is None
    assert env["HOME_LIGHTS_API_URL"] is None
    assert env["PI_TELEMETRY"] == "0" and env["PI_OFFLINE"] == "1"
    assert first["skill_env"]["HOME_LIGHTS_API_URL"] == "http://lights"
    assert first["skill_env_mode"] == "0o600"
    assert not list(Path("/tmp").glob("alfred-skill-env-*")) or all(
        p.stat().st_mtime < __import__("time").time() - 60 for p in Path("/tmp").glob("alfred-skill-env-*"))
    argv = calls(state)[0]["argv"]
    assert argv[argv.index("--provider") + 1] == "house"
    assert argv[argv.index("--model") + 1] == "gemma4"
    assert argv[argv.index("--thinking") + 1] == "off"


def test_models_json_names_one_endpoint_and_turns_thinking_off(fake_pi, tmp_path):
    fake_pi({"text": "ok", "tools": [{"name": "make_document", "result": DOC_RESULT}]})
    run(tmp_path)
    cfg = json.loads((tmp_path / "work" / ".pi-agent" / "models.json").read_text())
    house = cfg["providers"]["house"]
    assert list(cfg["providers"]) == ["house"]
    assert house["baseUrl"] == "http://host.docker.internal:11436/v1"
    assert house["apiKey"] == "ALFRED_HARNESS_API_KEY"      # an env name, not the key
    assert house["models"][0]["thinkingLevelMap"]["off"] == "none"
    assert (tmp_path / "work" / ".pi-agent" / "SYSTEM.md").read_text().startswith("You are Alfred")


@pytest.mark.parametrize("bench", [False, True])
def test_pi_never_gets_bash(fake_pi, tmp_path, bench):
    """nanobot's exec has an env allowlist, a sandbox and deny patterns; pi's bash
    has none, so it is not offered at all -- in the house or in the bench."""
    state = fake_pi({"text": "ok", "tools": [{"name": "make_document", "result": DOC_RESULT}]})
    run(tmp_path, bench=bench)
    first = calls(state)[0]
    assert first["env"]["ALFRED_HARNESS_BENCH"] == ("1" if bench else "0")
    tools = first["argv"][first["argv"].index("--tools") + 1].split(",")
    assert "bash" not in tools and "make_document" in tools and "web" in tools


@pytest.mark.parametrize("url", ["https://opencode.ai/zen/go/v1", "https://OpenCode.AI/zen/go/v1"])
def test_opencode_go_is_refused_unless_the_household_allowed_it(fake_pi, tmp_path, url):
    """Go is the flat plan CLAUDE.md keeps for a person at the keyboard."""
    state = fake_pi({"text": "never"})
    res = asyncio.run(H.run(TASK, H.Endpoint(base_url=url, model="kimi"), tmp_path / "w"))
    assert "refused" in res.error and not os.path.exists(str(state) + ".log")
    assert H.Endpoint(base_url=url, model="kimi", allow_go=True).check() is None


def test_zen_gets_a_session_header_one_per_task(fake_pi, tmp_path):
    """Every request to opencode.ai carries x-opencode-session (CLAUDE.md),
    never a hard-coded one: a fresh id per task, read by pi from an env var."""
    state = fake_pi({"tools": [{"name": "make_document", "result": DOC_RESULT}], "text": "ok"})
    ep = H.Endpoint(base_url="https://opencode.ai/zen/v1", model="deepseek-v4-flash")
    asyncio.run(H.run(TASK, ep, tmp_path / "a"))
    asyncio.run(H.run(TASK, ep, tmp_path / "b"))
    first, second = (c["env"]["ALFRED_HARNESS_HEADER_0"] for c in calls(state))
    assert first and second and first != second
    cfg = json.loads((tmp_path / "a" / ".pi-agent" / "models.json").read_text())
    name = cfg["providers"]["house"]["headers"]["x-opencode-session"]
    assert name.startswith("ALFRED_HARNESS_HEADER_")          # a variable, not a value
    assert H.header_env_names(H.Endpoint(base_url="http://ollama:11434/v1", model="x")) == {}


def test_links_are_read_from_the_text_not_its_json_escaping():
    """The escaped form put a backslash on every link, which then matched nothing."""
    text = H._result_text({"content": [{"type": "text", "text": DOC_RESULT}]})
    assert H.LINK.findall(text) == ["download:tomi/alfred/documents/guia.pdf"]


def test_a_search_result_is_not_a_delivered_file(fake_pi, tmp_path):
    """Search hits end in .html too; only make_document delivers."""
    fake_pi({"tools": [{"name": "web", "args": {"action": "search"},
                        "result": "1. https://site.cl/receta.html download:fake/x.pdf"}],
             "text": "Ahí está la receta."})
    res = run(tmp_path, max_nudges=1)
    assert not res.delivered and res.links == []
    assert "site.cl" not in res.text.split("receta.")[-1]


def test_a_pi_that_dies_stops_the_nudging_and_says_why(fake_pi, tmp_path):
    fake_pi({"exit": 3})
    res = run(tmp_path)
    assert res.rounds == 1 and "pi exited 3" in res.error and "connection refused" in res.error


def test_a_cancelled_task_does_not_leave_pi_running(fake_pi, tmp_path):
    fake_pi({"sleep": 30, "text": "late"})

    async def go():
        t = asyncio.create_task(H.run(TASK, EP, tmp_path / "w"))
        await asyncio.sleep(1.0)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())
    import subprocess
    left = subprocess.run(["pgrep", "-f", str(H.PI_BIN)], capture_output=True, text=True).stdout
    assert not left.strip()


def test_streaming_updates_are_not_logged(fake_pi, tmp_path):
    fake_pi({"tools": [{"name": "make_document", "result": DOC_RESULT}], "text": "ok"})
    run(tmp_path)
    log = (tmp_path / "work" / "events.jsonl").read_text()
    assert "message_update" not in log and "message_end" in log


@pytest.mark.parametrize("task,want", [
    ("Armame una guía en PDF", True), ("una planilla excel del presupuesto", True),
    ("hacé una presentación", True), ("Investigá X y reportame lo que encuentres", False),
    ("research the options and report back", False), ("resumí este documento", False),
])
def test_only_a_format_means_a_file(task, want):
    assert H.wants_file(task) is want


def test_old_workdirs_are_pruned(tmp_path):
    for i in range(14):
        (tmp_path / f"2026092{i % 10}-{i:06d}").mkdir()
    H.prune_workdirs(tmp_path, keep=10)
    assert len(list(tmp_path.iterdir())) == 10


def test_the_extension_and_prompt_ship_with_the_package():
    assert (H.ASSETS / "alfred.ts").is_file() and (H.ASSETS / "SYSTEM.md").is_file()
    ts = (H.ASSETS / "alfred.ts").read_text()
    for tool in ("web", "make_document", "skill", "skill_guide"):
        assert f'name: "{tool}"' in ts
    assert "ALFRED_HARNESS_BENCH" in ts


def test_what_it_says_before_a_tool_call_is_narrated(fake_pi, tmp_path):
    """The background-tasks panel's timeline: the same `thought` the loop publishes."""
    fake_pi({"note": "Voy a buscar la receta.<channel|>",
             "tools": [{"name": "make_document", "result": DOC_RESULT}], "text": "Listo."})
    notes = []

    async def on_note(text):
        notes.append(text)
    asyncio.run(H.run(TASK, EP, tmp_path / "w", on_note=on_note))
    assert notes == ["Voy a buscar la receta."]


def test_a_tool_is_announced_when_it_starts_and_a_nudge_is_announced(fake_pi, tmp_path):
    """The panel shows a call while it runs, and says so when pi is nudged."""
    fake_pi({"tools": [{"name": "web", "args": {"action": "search"}}], "text": "Aquí va."},
            {"tools": [{"name": "make_document", "result": DOC_RESULT}], "text": "Listo."})
    seen = []

    async def on_start(call):
        seen.append(("start", call.name, call.result))

    async def on_tool(call):
        seen.append(("end", call.name))

    async def on_nudge(n, of, short):
        seen.append(("nudge", n, of, short))
    res = asyncio.run(H.run(TASK, EP, tmp_path / "w", max_nudges=3,
                            on_start=on_start, on_tool=on_tool, on_nudge=on_nudge))
    assert res.delivered
    assert seen == [("start", "web", ""), ("end", "web"), ("nudge", 1, 3, "no file"),
                    ("start", "make_document", ""), ("end", "make_document")]


def test_pi_dies_with_its_parent(tmp_path):
    """A killed parent -- the benchmark's Stop -- must not leave pi running."""
    import subprocess
    import textwrap
    import time
    pidfile = tmp_path / "child.pid"
    parent = textwrap.dedent(f"""
        import subprocess, sys
        sys.path.insert(0, {str(Path(H.__file__).parents[2])!r})
        from nanobot.harness.pi_runner import _die_with_parent
        c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             preexec_fn=_die_with_parent)
        open({str(pidfile)!r}, "w").write(str(c.pid))
        import time; time.sleep(60)
    """)
    p = subprocess.Popen([sys.executable, "-c", parent])
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.05)
    child = int(pidfile.read_text())
    p.kill()
    p.wait()
    for _ in range(100):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        # A zombie of an already-dead process is reaped by init; check its state.
        try:
            if open(f"/proc/{child}/stat").read().split()[2] == "Z":
                return
        except OSError:
            return
        time.sleep(0.05)
    os.kill(child, signal.SIGKILL)
    raise AssertionError("the child outlived its parent")


def test_the_event_log_is_written_as_it_goes(fake_pi, tmp_path):
    fake_pi({"tools": [{"name": "make_document", "result": DOC_RESULT}], "text": "Listo."})
    seen = []

    async def on_tool(call):
        seen.append((tmp_path / "w" / "events.jsonl").read_text().count("tool_execution_end"))
    asyncio.run(H.run(TASK, EP, tmp_path / "w", on_tool=on_tool))
    assert seen == [1]


def test_the_households_skills_become_pi_skills(tmp_path, monkeypatch):
    # pi ran with --no-skills, so a small model learned which skills exist only
    # by calling skill_guide list, and mostly did not (2026-09-25).
    from nanobot.harness import alfred_skill as A
    src = tmp_path / "src" / "grocery"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text('---\nname: grocery\ndescription: "Invoke with JSON: '
                                  '{\\"skill\\":\\"grocery\\"}. The shared shopping list."\n---\n\n'
                                  "# Shopping list\n\nAdd with add_grocery.\n")
    monkeypatch.setattr(A, "list_skills", lambda: [
        {"skill": "web", "actions": ["search"], "description": "web"},
        {"skill": "grocery", "actions": ["add_grocery", "list_groceries"],
         "description": 'Invoke with JSON: {"skill":"grocery"}. The shared shopping list.'},
        {"skill": "Bad_Name", "actions": [], "description": "x"}])
    monkeypatch.setattr(A, "find", lambda n: src / "SKILL.md" if n == "grocery" else None)
    root = H._skills_dir(tmp_path / "task")
    made = sorted(p.parent.name for p in root.glob("*/SKILL.md"))
    assert made == ["grocery"]                  # web is a tool; a name pi refuses is skipped
    md = (root / "grocery" / "SKILL.md").read_text()
    assert md.startswith("---\nname: grocery\n")
    assert "Invoke with JSON" not in md.split("---")[1]          # nanobot's convention, not pi's
    assert "The shared shopping list." in md and "skill tool" in md
    assert "Actions: add_grocery, list_groceries" in md and "# Shopping list" in md
    assert str(root).startswith(str(tmp_path / "task"))           # inside what `read` may open
