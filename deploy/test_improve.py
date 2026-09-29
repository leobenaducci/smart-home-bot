"""The self-improvement pipeline's house side: collect, and redact.

Run: ./.venv/bin/python deploy/test_improve.py

Everything here runs on a scratch state tree with invented people -- the
members Tomi and Mora, the login 999000111, the bulb "Oficina Tomi". Nothing
reads /var/lib/home-stack or the live state.

What is pinned is what would leak if it broke: a name the house knows, in any
case or accent, never survives redaction; a shape that identifies somebody
(an address, a phone, a coordinate, a private host) becomes a token; the
guard withholds an episode rather than letting one through. And what would
make the evaluator useless if it broke: a 👎, a Stop, a correction and a
failed turn each become a signal, and a turn billed in two rows is one
episode.
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "improve"))
sys.path.insert(0, str(HERE))

import collect as C  # noqa: E402
import redact as R  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


tmp = Path(tempfile.mkdtemp(prefix="improve-"))
state = tmp / "state"
data = state / "home-core" / "data"
data.mkdir(parents=True)
LOGIN = "999000111"
DAY = "2026-09-29"
T0 = 1790690000  # 2026-09-29, mid-morning UTC

# --- the house's own records -------------------------------------------------
cfg = {"members": [{"id": "user1", "display_name": "Tomi", "phone": "+54 9 11 5555 0101",
                    "ntfy_topic": "Tomi", "whatsapp": True, "parents": True},
                   {"id": "user2", "display_name": "Mora Fernández"}],
       "site": {"domain": "casa-ejemplo.net", "host": "hub-ejemplo"},
       "dns": {"portal": "portal.casa-ejemplo.net"}}
(state / "home-core" / "users.json").write_text(json.dumps(
    [{"username": LOGIN, "member": "user1", "hash": "x"}]), encoding="utf-8")
con = sqlite3.connect(data / "family.db")
con.executescript("""
    CREATE TABLE family_profiles (person TEXT, display_name TEXT, full_name TEXT,
        relationship TEXT, birthdate TEXT, timezone TEXT, updated_by TEXT, updated_at TEXT, gender TEXT);
    CREATE TABLE family_relations (id INTEGER, person TEXT, kind TEXT, other TEXT,
        updated_by TEXT, updated_at TEXT);
    INSERT INTO family_profiles (person, display_name, full_name) VALUES
        ('juana', 'Juana', 'Juana Sofía Pérez');
    INSERT INTO family_relations (person, kind, other) VALUES ('juana', 'friend', 'Casa');
""")
con.commit()
con = sqlite3.connect(data / "geo.db")
con.executescript("""
    CREATE TABLE places (id INTEGER, name TEXT, name_key TEXT, lat REAL, lng REAL,
        radius REAL, owner TEXT, created_at TEXT);
    INSERT INTO places (name) VALUES ('Club Náutico Ejemplo'), ('Trabajo');
""")
con.commit()
con = sqlite3.connect(data / "whatsapp.db")
con.executescript("""
    CREATE TABLE wa_chats (username TEXT, chat_id TEXT, name TEXT, is_group INTEGER,
        can_read INTEGER, reply_mode TEXT, seen_count INTEGER, last_seen INTEGER, approved_until INTEGER);
    INSERT INTO wa_chats (name) VALUES ('Nico Ejemplar'), ('Mamá');
""")
con.commit()

(tmp / "config").mkdir()
(tmp / "config" / "smart-home-bot.env").write_text(
    "OPENCODE_API_KEY=sk-test-0123456789abcdefghij\nEMPTY=\n", encoding="utf-8")
cfg["paths"] = {"config": str(tmp / "config")}
print("\nwhat the house knows about itself")
k = R.harvest(cfg, state)
check("and every credential in the env file", "sk-test-0123456789abcdefghij" in k.exact)
check("members, by display name", {"Tomi", "Mora Fernández"} <= k.people, k.people)
check("and the parts of a full name", {"Juana Sofía Pérez", "Pérez", "Sofía"} <= k.people, k.people)
check("logins and phones as exact values", {LOGIN, "+54 9 11 5555 0101"} <= k.exact, k.exact)
check("places, contacts and hosts", "Club Náutico Ejemplo" in k.places
      and "Nico Ejemplar" in k.contacts and "portal.casa-ejemplo.net" in k.hosts)
check("nor a flag, whose «true» is in every episode as JSON", "True" not in k.all(), k.exact)
check("but not the labels that identify nobody", not ({"Trabajo", "Mamá", "Casa"} & k.all()),
      {"Trabajo", "Mamá", "Casa"} & k.all())

print("\nredaction")
red = R.Redactor(k, b"k" * 32)
raw = ("Tomi dijo que MORA FERNÁNDEZ y juana sofia perez van al Club Nautico Ejemplo; "
       "escribile a nico ejemplar al +54 9 11 5555 0101 o a tomi@ejemplo.org. "
       "El portal es https://portal.casa-ejemplo.net/chat y la luz está en 192.168.1.40 "
       "(aa:bb:cc:dd:ee:ff), en -34.60372, -58.38157. Mi login es 999000111. "
       "Mirá https://es.wikipedia.org/wiki/Luz y la de casa y el trabajo.")
out = red.text(raw)
check("no known name survives, in any case or accent", red.survivors(out) == 0, out)
for shape in ("@", "192.168", "aa:bb", "-34.6", "5555", "999000111", "casa-ejemplo"):
    check(f"«{shape}» is gone", shape not in out, out)
bare = red.text("el servicio en `pi-viejo.home:5010`, el nas.lan, api.example.org:8443 "
                "y github.com son distintos")
check("a machine named without a scheme is a host", "pi-viejo" not in bare and "nas.lan" not in bare
      and "example.org:8443" not in bare and "github.com" in bare, bare)
check("a public link keeps its host", "https://es.wikipedia.org/wiki/Luz" in out, out)
check("and ordinary words stay", "de casa y el trabajo" in out, out)
check("the same name is the same token every time",
      red.text("Tomi") == red.text("tomi") == R.Redactor(k, b"k" * 32).text("TOMI"))
check("and a different salt makes a different token",
      red.text("Tomi") != R.Redactor(k, b"j" * 32).text("Tomi"))
check("a name inside a word is not a name", red.text("Tomillo y romero") == "Tomillo y romero",
      red.text("Tomillo y romero"))

print("\nthe local model's pass, and what it may not do")
m = R.Redactor(k, b"k" * 32, names_model=lambda t: ["Ricardo Gutiérrez", "casa", "[persona-0000]", "x"])
out = m.text("Voy con Ricardo Gutiérrez a casa.")
check("a name no list knows becomes a token", "Ricardo" not in out and "[nombre-" in out, out)
check("but a generic word it offers is left alone", "a casa" in out, out)
m = R.Redactor(k, b"k" * 32, names_model=lambda t: ["LIGHTS_API_URL", "lights/SKILL.md", "main.py",
                                                     "get_forecast()", "HTTP"])
out = m.text("La skill lee LIGHTS_API_URL en lights/SKILL.md y main.py llama get_forecast() por HTTP.")
check("and nor is code: variables, files, calls", "[nombre-" not in out, out)

print("\nthe guard")
g = R.Redactor(k, b"k" * 32, sanitize=lambda t: t)
g._table = []  # a redaction that forgot everything
ep = g.episode({"request": "hola Tomi", "answer": "hola"}, ("request", "answer"))
check("an episode with a surviving name is withheld", ep is None and g.withheld == 1)
ep = red.episode({"request": "hola Tomi", "feedback": {"rating": "down", "note": "Mora no estaba"},
                  "answer": "hola"}, ("request", "answer", "feedback"))
check("a redacted one passes, note included", ep is not None and "Mora" not in ep["feedback"]["note"]
      and ep["feedback"]["rating"] == "down", ep)

# --- collect -----------------------------------------------------------------
print("\ncollecting a day")
hist = state / "home-core" / "history" / LOGIN
hist.mkdir(parents=True)
(hist / "designer").mkdir()
ms = 1000


def msg(role, text, t, **kw):
    return {"role": role, "text": text, "ts": t * ms, **kw}


(hist / f"{DAY}.json").write_text(json.dumps([
    msg("user", "prendé Oficina Tomi", T0),
    msg("bot", "Listo, prendida.", T0 + 20, feedback={"rating": "down", "note": "no prendió"}),
    msg("user", "¿qué hora es?", T0 + 600),
    msg("bot", "Son las 10.", T0 + 605),
    msg("user", "armá un plan de estudio largo", T0 + 1200),
    msg("bot", "Empiezo por…", T0 + 1260, interrupted=True),
    msg("user", "cuánto cuesta el pasaje a Rosario", T0 + 1800),
    msg("bot", "No sé.", T0 + 1810),
    msg("user", "no, buscalo en la web", T0 + 1840),
]), encoding="utf-8")
(hist / "designer" / f"{DAY}.json").write_text(json.dumps([
    msg("user", "un afiche", T0 + 3000), msg("bot", "Acá está.", T0 + 3100)]), encoding="utf-8")
con = sqlite3.connect(data / "usage.db")
con.executescript("""
    CREATE TABLE token_usage (id INTEGER PRIMARY KEY, username TEXT, ts INTEGER, day TEXT,
        scope TEXT, model TEXT, prompt_tokens INTEGER, tool_names TEXT DEFAULT '', tier TEXT DEFAULT '',
        stop_reason TEXT DEFAULT '', turn_id TEXT DEFAULT '', latency_ms INTEGER,
        call_errors INTEGER DEFAULT 0, tool_errors INTEGER DEFAULT 0, escalated INTEGER DEFAULT 0,
        escalated_from TEXT DEFAULT '');
    CREATE TABLE turn_events (id INTEGER PRIMARY KEY, turn_id TEXT, username TEXT, ts INTEGER,
        day TEXT, scope TEXT, code TEXT, n INTEGER);
""")
rows = [  # (ts, scope, model, stop, turn, latency, call_err, tool_err, escalated, tools)
    (T0 + 18, "", "cheap", "max_iterations", "aa01", 9000, 0, 1, 0, "skill:lights"),
    (T0 + 20, "", "strong", "completed", "aa01", 4000, 0, 0, 1, "skill:lights"),
    (T0 + 604, "", "cheap", "completed", "bb02", 1500, 0, 0, 0, ""),
    (T0 + 1809, "", "cheap", "completed", "cc03", 2000, 0, 0, 0, ""),
    (T0 + 3099, "dsg", "sol", "completed", "dd04", 95000, 0, 0, 0, "document"),
    (T0 + 4000, "ev-task", "local", "empty_final_response", "ee05", 3000, 1, 0, 0, ""),
    (T0 + 4100, "ev-heartbeat", "local", "", "", None, 0, 0, 0, ""),
]
for r in rows:
    con.execute("INSERT INTO token_usage (username, ts, day, scope, model, stop_reason, turn_id, "
                "latency_ms, call_errors, tool_errors, escalated, tool_names) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (LOGIN, r[0], DAY) + r[1:])
con.execute("INSERT INTO turn_events (turn_id, username, ts, day, scope, code, n) "
            "VALUES ('aa01', ?, ?, ?, '', 'parse:dsml', 2)", (LOGIN, T0 + 18, DAY))
con.commit()

eps = C.collect(state, DAY, sample=0.0)
by = {e["request"] or e["scope"]: e for e in eps}
first = by.get("prendé Oficina Tomi")
check("a 👎 answer is an episode", first is not None, list(by))
check("its two billed rows are one turn", first and first["models"] == ["cheap", "strong"]
      and first["stop_reasons"] == ["max_iterations", "completed"], first)
check("with the runner's codes", first and first["events"] == {"parse:dsml": 2}, first)
check("and its signals, worst first", first and first["signals"][:1] == ["thumbs_down"]
      and {"stop:max_iterations", "tool_error", "escalated", "event:parse"} <= set(first["signals"]),
      first and first["signals"])
check("a clean, unrated answer is not kept at sample 0", "¿qué hora es?" not in by, list(by))
check("a Stop is a signal", "stopped" in by["armá un plan de estudio largo"]["signals"])
check("«no, …» right after is a correction",
      by["cuánto cuesta el pasaje a Rosario"]["followup"] == "correction"
      and by["cuánto cuesta el pasaje a Rosario"]["next"] == "no, buscalo en la web")
check("a profession's turn joins its own scope, and a slow one says so",
      by["un afiche"]["scope"] == "dsg" and "slow" in by["un afiche"]["signals"], by.get("un afiche"))
check("a failed turn nobody reads in the chat is kept from its usage alone",
      "ev-task" in by and "stop:empty_final_response" in by["ev-task"]["signals"], list(by))
check("a heartbeat with nothing wrong is not", "ev-heartbeat" not in by)
all_eps = C.collect(state, DAY, sample=1.0)
check("sampling keeps the clean ones too, and says they were sampled",
      any(e["request"] == "¿qué hora es?" and e["sampled"] for e in all_eps))
check("but never a heartbeat", not any(e["scope"] == "ev-heartbeat" for e in all_eps))
check("the day before is empty, not an error", C.collect(state, "2026-09-28") == [])

print("\nthe professions' folders, old names included, agree with the portal")
import re as _re  # noqa: E402
_portal = (HERE.parent / "services" / "home-core" / "local" / "app.py").read_text(encoding="utf-8")
_spaces = dict(_re.findall(r"'(\w+)': \{\s*'dir': '\w+', 'scope': '(\w+)'", _portal))
_aliases = dict(_re.findall(r"'(\w+)': '(\w+)'", _portal[_portal.index("CHAT_SPACE_ALIASES = {"):
                                                           _portal.index("CHAT_MODE_ALIASES")]))
check("every profession the portal has maps to its scope", _spaces and all(
      C.scope_of_folder(space) == scope for space, scope in _spaces.items()), (_spaces, C.SPACE_SCOPES))
check("and every old folder name to the same one", _aliases and all(
      C.scope_of_folder(old) == _spaces[new] for old, new in _aliases.items()), _aliases)
(hist / "programador").mkdir()
(hist / "programador" / f"{DAY}.json").write_text(json.dumps([
    msg("user", "revisá el deploy", T0 + 5000), msg("bot", "No pude.", T0 + 5010, interrupted=True)]),
    encoding="utf-8")
check("a Programmer turn in the old folder is a `dev` episode",
      any(e["scope"] == "dev" and e["request"] == "revisá el deploy" for e in C.collect(state, DAY, sample=0.0)))

print("\nthe command, end to end, on the scratch tree")
os.environ["HOME_STACK_IMPROVE_DIR"] = str(tmp / "improve")
import cli  # noqa: E402
cli.live_config = lambda: {**cfg, "paths": {"state": str(state)}, "site": {"timezone": "UTC"}}
rc = cli.main(["collect", "--day", DAY, "--no-model", "--sample", "0"])
inbox = (tmp / "improve" / "inbox" / f"{DAY}.jsonl").read_text(encoding="utf-8")
check("it writes the inbox", rc == 0 and inbox.count("\n") == len(C.collect(state, DAY, sample=0.0)), inbox[:200])
check("with no login and no name in it", LOGIN not in inbox and "Tomi" not in inbox
      and "member-" in inbox, inbox[:300])
check("readable by this user alone",
      oct((tmp / "improve" / "inbox").stat().st_mode & 0o777) == "0o700"
      and oct((tmp / "improve" / "inbox" / f"{DAY}.jsonl").stat().st_mode & 0o777) == "0o600")
check("and the salt stays private",
      oct((tmp / "improve" / "private" / "salt").stat().st_mode & 0o777) == "0o600")

print("\nwhere a fix may be made")
import subprocess  # noqa: E402
import repos as RP  # noqa: E402
check("a GitHub remote opens on GitHub",
      RP.web_url("git@github.com:ejemplo/stack.git") == "https://github.com/ejemplo/stack")
check("an Azure DevOps one on Azure DevOps",
      RP.web_url("u@vs-ssh.visualstudio.com:v3/org/proj/repo") == "https://dev.azure.com/org/proj/_git/repo")
check("and credentials in an https remote never reach the link",
      RP.web_url("https://user:tok@git.example.org/r.git") == "https://git.example.org/r")
plug = tmp / "luces-plugin"
plug.mkdir()
(plug / "plugin.yml").write_text(
    "name: luces\ncontract: 1\nservices:\n  luces-api:\n    description: x\n    units: []\n"
    "contributes:\n  nanobot:\n    env: {LUCES_API_URL: 'http://x/api'}\n", encoding="utf-8")
for cmd in (["init", "-q"], ["remote", "add", "origin", "git@github.com:ejemplo/luces.git"]):
    subprocess.run(["git", "-C", str(plug), *cmd], check=True)
extra = tmp / "otro"
extra.mkdir()
subprocess.run(["git", "-C", str(extra), "init", "-q"], check=True)
found = RP.discover({"plugins": [str(plug), str(tmp / "no-git-here")],
                     "assistant": {"improve": {"repos": [{"path": str(extra), "about": "notas"}],
                                               "exclude": []}}})
names = {r["name"]: r for r in found}
check("the stack itself, from this checkout", any(r["kind"] == "stack" for r in found), list(names))
check("a plugin, with what it provides", names.get("luces", {}).get("provides", {}).get("env")
      == ["LUCES_API_URL"] and names["luces"]["web"] == "https://github.com/ejemplo/luces", names.get("luces"))
check("and an extra one from the config", names.get("otro", {}).get("source") == "config", list(names))
check("each says what deploying it means: a plugin, by its services",
      "luces-api" in names["luces"]["deploys"] and "./home-stack deploy" in names["luces"]["deploys"],
      names["luces"].get("deploys"))
stack_name = next(r["name"] for r in found if r["kind"] == "stack")
found = RP.discover({"plugins": [str(plug)], "assistant": {"improve": {"exclude": ["luces", stack_name]}}})
check("exclude leaves an automatic one out", found == [], [r["name"] for r in found])

print("\na fix is made in a worktree, and committed by code")
import work as W  # noqa: E402
import checks as K  # noqa: E402


class _Ok:
    returncode = 0
    stdout = "1 passed"
    stderr = ""


def tested_commit(d, repos_, rid, name, message):
    """What the Programmer does: its tests (a stand-in runner here), then commit."""
    K.run_tests(d, repos_, rid, name, run=lambda *a, **k: _Ok())
    return W.commit(d, repos_, rid, name, message)
imp = tmp / "improve-work"
for cmd in (["config", "user.email", "dueno@example.org"], ["config", "user.name", "Dueño"],
            ["commit", "-q", "--allow-empty", "-m", "base"]):
    subprocess.run(["git", "-C", str(plug), *cmd], check=True)
(plug / "skill.py").write_text("BASE = 'http://viejo.home:5010/api'\n", encoding="utf-8")
subprocess.run(["git", "-C", str(plug), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(plug), "commit", "-q", "-m", "skill"], check=True)
repos = RP.discover({"plugins": [str(plug)]})
repos = [r for r in repos if r["name"] == "luces"] + [dict(r, name="pila", kind="stack")
                                                     for r in repos if r["name"] == "luces"]
wt = W.start(imp, repos[:1], "7", "luces")
check("start makes a worktree on improve/<id>, inside the pipeline's folder",
      wt == imp / "work" / "7-luces" and W._git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "improve/7")
check("and the plugin's own checkout is untouched",
      W._git(plug, "rev-parse", "--abbrev-ref", "HEAD") in ("master", "main"))
check("a second start reuses it", W.start(imp, repos[:1], "7", "luces") == wt)
for bad in (("x7", "luces"), ("7", "../etc"), ("7", "otro-que-no-esta")):
    try:
        W.start(imp, repos[:1], *bad)
        check(f"refuses {bad}", False)
    except W.WorkError:
        check(f"refuses {bad}", True)
try:
    tested_commit(imp, repos[:1], "7", "luces", "arreglo con mensaje")
    check("nothing changed is refused", False)
except W.WorkError as exc:
    check("nothing changed is refused", "nothing changed" in str(exc))
(wt / "skill.py").write_text("import os\nBASE = os.environ['LUCES_API_URL']\n", encoding="utf-8")
out = tested_commit(imp, repos[:1], "7", "luces", "Lights skill: no fallback to a dead host")
log = W._git(wt, "log", "-1", "--format=%ae|%B")
check("the commit is made, as the repository's owner", "dueno@example.org|" in log
      and "Fix request #7" in log, log)
check("and not on the plugin's own branch", "no fallback" not in W._git(plug, "log", "-1", "--format=%s"))
check("the stack refuses changes to how it is deployed, judged or guarded",
      W.refusals("stack", [(" M", "deploy/deploy.py"), (" M", "services/nanobot/bench/cases.json"),
                           (" M", "CLAUDE.md"), (" M", "services/home-core/local/app.py")])
      and len(W.refusals("stack", [(" M", "deploy/deploy.py")])) == 1
      and W.refusals("stack", [(" M", "services/home-core/local/app.py")]) == [])
check("and anywhere, a deleted test", W.refusals("plugin", [(" D", "tests/test_x.py")]) != [])
stack = tmp / "pila"
(stack / "services" / "nanobot" / "bench").mkdir(parents=True)
(stack / "services" / "nanobot" / "bench" / "cases.json").write_text('{"routing": []}', encoding="utf-8")
(stack / "services" / "x.py").write_text("x = 1\n", encoding="utf-8")
(stack / "deploy").mkdir()
(stack / "deploy" / "sanitize.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")
for cmd in (["init", "-q"], ["config", "user.email", "dueno@example.org"], ["add", "-A"],
            ["commit", "-q", "-m", "base"]):
    subprocess.run(["git", "-C", str(stack), *cmd], check=True)
srepo = {"name": "pila", "kind": "stack", "path": str(stack), "branch": W._git(stack, "rev-parse", "--abbrev-ref", "HEAD")}
swt = W.start(imp, [srepo], "8", "pila")
check("the benchmark's cases are not in the stack's worktree",
      not (swt / "services" / "nanobot" / "bench" / "cases.json").exists())
check("and their absence is not a change", W._changed(swt) == [], W._changed(swt))
(swt / "services" / "x.py").write_text("x = 2\n", encoding="utf-8")
tested_commit(imp, [srepo], "8", "pila", "Change x so the test passes")
check("so a commit there never deletes them",
      "cases.json" not in W._git(swt, "show", "--stat", "--format=", "HEAD")
      and W._git(stack, "cat-file", "-e", "improve/8:services/nanobot/bench/cases.json", check=False) == "")
(stack / "deploy" / "sanitize.py").write_text("raise SystemExit('boom')\n", encoding="utf-8")
subprocess.run(["git", "-C", str(stack), "commit", "-qam", "broken sanitizer"], check=True)
swt2 = W.start(imp, [srepo], "9", "pila")
(swt2 / "services" / "x.py").write_text("x = 3\n", encoding="utf-8")
try:
    tested_commit(imp, [srepo], "9", "pila", "Change x again for the test")
    check("a sanitizer that cannot run refuses the commit, and says so", False)
except W.WorkError as exc:
    check("a sanitizer that cannot run refuses the commit, and says so",
          "could not run" in str(exc) and "household data" not in str(exc), str(exc))
print("\npublishing and deploying a fix, with the checks in code")
import ship as S  # noqa: E402
remote = tmp / "luces-remote.git"
subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
lp = tmp / "luces-live"
subprocess.run(["git", "clone", "-q", str(remote), str(lp)], check=True)
for cmd in (["config", "user.email", "dueno@example.org"], ["config", "user.name", "Dueño"]):
    subprocess.run(["git", "-C", str(lp), *cmd], check=True)
(lp / "skill.py").write_text("BASE = 'http://viejo.home:5010/api'\n", encoding="utf-8")
subprocess.run(["git", "-C", str(lp), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(lp), "commit", "-q", "-m", "base"], check=True)
br = W._git(lp, "rev-parse", "--abbrev-ref", "HEAD")
subprocess.run(["git", "-C", str(lp), "push", "-q", "origin", br], check=True)
lrepo = {"name": "luces2", "kind": "plugin", "path": str(lp), "branch": br, "remote": str(remote),
         "provides": {"services": ["luces-api"]}}
lwt = W.start(imp, [lrepo], "11", "luces2")
(lwt / "skill.py").write_text("import os\nBASE = os.environ['LUCES_API_URL']\n", encoding="utf-8")
try:
    S.publish(imp, [lrepo], "11", "luces2")
    check("an uncommitted fix is not published", False)
except W.WorkError as exc:
    check("an uncommitted fix is not published", "uncommitted" in str(exc), str(exc))
tested_commit(imp, [lrepo], "11", "luces2", "Lights skill: no dead fallback")
(lp / "notes.txt").write_text("somebody's work\n", encoding="utf-8")
try:
    S.publish(imp, [lrepo], "11", "luces2")
    check("a checkout with somebody's uncommitted work is not touched", False)
except W.WorkError as exc:
    check("a checkout with somebody's uncommitted work is not touched",
          "uncommitted work" in str(exc) and W._git(lp, "log", "-1", "--format=%s") == "base", str(exc))
(lp / "notes.txt").unlink()
try:
    S.deploy(imp, [lrepo], "11", "luces2", tmp, run=lambda *a, **k: None, busy=lambda: False)
    check("an unpublished fix is not deployed", False)
except W.WorkError as exc:
    check("an unpublished fix is not deployed", "not published" in str(exc), str(exc))
out = S.publish(imp, [lrepo], "11", "luces2")
check("publishing fast-forwards the plugin's checkout",
      W._git(lp, "log", "-1", "--format=%s") == "Lights skill: no dead fallback", out)
check("and pushes it to the plugin's remote", "pushed to origin" in out
      and W._git(remote, "log", "-1", "--format=%s", br) == "Lights skill: no dead fallback", out)
try:
    S.publish(imp, [lrepo], "11", "luces2")
    check("publishing twice is refused, not repeated", False)
except W.WorkError as exc:
    check("publishing twice is refused, not repeated", "nothing" in str(exc), str(exc))
ran = []


class _R:
    returncode = 0
    stdout = "ok  all 1 service(s) deployed and answering\n"


out = S.deploy(imp, [lrepo], "11", "luces2", tmp, run=lambda argv, **k: ran.append(argv) or _R(),
               busy=lambda: False)
check("deploying runs the deployer for the plugin's services, and not admin",
      [a[2] for a in ran] == ["luces-api"] and "luces-api: ok" in out, (ran, out))
try:
    S.deploy(imp, [lrepo], "11", "luces2", tmp, run=lambda *a, **k: _R(), busy=lambda: True)
    check("not beside another deploy", False)
except W.WorkError as exc:
    check("not beside another deploy", "another deploy" in str(exc))
(tmp / "admin" / "bench").mkdir(parents=True)
(tmp / "admin" / "bench" / "job.json").write_text('{"running": true}', encoding="utf-8")
try:
    S.deploy(imp, [lrepo], "11", "luces2", tmp, run=lambda *a, **k: _R(), busy=lambda: False)
    check("nor during a benchmark", False)
except W.WorkError as exc:
    check("nor during a benchmark", "benchmark" in str(exc))
(tmp / "admin" / "bench" / "job.json").write_text('{"running": false}', encoding="utf-8")

# The stack: published by fast-forward only, deployed by the files it changed, admin last.
(stack / "deploy" / "sanitize.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")
(stack / "deploy" / "manifest.yml").write_text(
    "services:\n  portal:\n    units: [{dir: services/portal}]\n"
    "  otro:\n    units: [{dir: services/otro}]\n", encoding="utf-8")
(stack / "services" / "portal").mkdir()
(stack / "services" / "portal" / "app.py").write_text("x = 1\n", encoding="utf-8")
subprocess.run(["git", "-C", str(stack), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(stack), "commit", "-qm", "portal"], check=True)
swt3 = W.start(imp, [srepo], "12", "pila")
(swt3 / "services" / "portal" / "app.py").write_text("x = 2\n", encoding="utf-8")
tested_commit(imp, [srepo], "12", "pila", "Portal: x is two")
out = S.publish(imp, [srepo], "12", "pila")
check("this stack is published by fast-forward and never pushed",
      "never pushes" in out and W._git(stack, "log", "-1", "--format=%s") == "Portal: x is two", out)
ran.clear()
out = S.deploy(imp, [srepo], "12", "pila", tmp, run=lambda argv, **k: ran.append(argv) or _R(),
               busy=lambda: False)
check("its deploy is the services the fix touched, then admin last",
      [a[2] for a in ran] == ["portal", "admin"], [a[2] for a in ran])
(stack / "services" / "otro.txt").write_text("sin commitear\n", encoding="utf-8")
try:
    S.deploy(imp, [srepo], "12", "pila", tmp, run=lambda *a, **k: _R(), busy=lambda: False)
    check("and never from a stack checkout with uncommitted work", False)
except W.WorkError as exc:
    check("and never from a stack checkout with uncommitted work", "uncommitted" in str(exc))

print("\nthe fix requests, as the portal filed them")
import sqlite3 as _sq  # noqa: E402
(state / "home-core" / "data").mkdir(parents=True, exist_ok=True)
_c = _sq.connect(state / "home-core" / "data" / "improve.db")
_c.executescript("""CREATE TABLE improve_requests (id INTEGER PRIMARY KEY, username TEXT, created_at INTEGER,
    problem TEXT, context TEXT, status TEXT, day TEXT, conv INTEGER);
    INSERT INTO improve_requests VALUES (1, '999000111', 1790690000, 'la luz', '', 'closed', '', 0);
    INSERT INTO improve_requests VALUES (2, '999000222', 1790690100, 'otra cosa', '', 'open', '', 0);
    INSERT INTO improve_requests VALUES (3, '999000111', 1790690200, 'el clima', '', 'investigating', '', 0);""")
_c.commit(); _c.close()
check("newest first, and only that person's when asked",
      [q["id"] for q in cli.requests(state, "999000111")] == [3, 1]
      and [q["id"] for q in cli.requests(state)] == [3, 2, 1])
check("and no database is no requests, not an error", cli.requests(tmp / "nada") == [])

print("\nwhat a commit has to pass: its tests, and the benchmark for behaviour")
k_wt = W.start(imp, [lrepo], "13", "luces2")
(k_wt / "skill.py").write_text("x = 'otra cosa'\n", encoding="utf-8")
try:
    W.commit(imp, [lrepo], "13", "luces2", "A change nobody tested")
    check("a change nobody tested is not committed", False)
except W.WorkError as exc:
    check("a change nobody tested is not committed", "no test run for this exact change" in str(exc))


class _Fail:
    returncode = 1
    stdout = "FAILED test_x - NameError: name 'turn_on' is not defined"
    stderr = ""


(k_wt / "test_skill.py").write_text("def test_x(): pass\n", encoding="utf-8")
rec = K.run_tests(imp, [lrepo], "13", "luces2", run=lambda *a, **k: _Fail())
check("a failing suite is found beside what changed, and recorded", rec["suites"] and not rec["ok"], rec)
try:
    W.commit(imp, [lrepo], "13", "luces2", "A change whose tests fail")
    check("a change whose tests fail is not committed", False)
except W.WorkError as exc:
    check("a change whose tests fail is not committed", "tests fail" in str(exc), str(exc))
K.run_tests(imp, [lrepo], "13", "luces2", run=lambda *a, **k: _Ok())
(k_wt / "skill.py").write_text("x = 'y otra más'\n", encoding="utf-8")
try:
    W.commit(imp, [lrepo], "13", "luces2", "Changed after it was tested")
    check("a change edited after its test run needs a new run", False)
except W.WorkError as exc:
    check("a change edited after its test run needs a new run", "no test run for this exact change" in str(exc))
check("the stack's nanobot suite runs for a runner change",
      [x[0] for x in K._suites("stack", stack, ["services/nanobot/nanobot/agent/runner.py"])] == ["nanobot"])
check("and only the stack's assistant code and config call for the benchmark",
      K.touches_behaviour("stack", ["services/nanobot/nanobot/skills/weather/SKILL.md"])
      and K.touches_behaviour("stack", ["services/nanobot/config/config.json"])
      and not K.touches_behaviour("stack", ["services/home-core/local/app.py"])
      and not K.touches_behaviour("plugin", ["services/nanobot/nanobot/x.py"]))
runs = {"before": [{"summary": {"tools": {"passed": 6, "total": 7}}, "failing": []}],
        "after": [{"summary": {"tools": {"passed": 4, "total": 7}}, "failing": ["t1"]}]}
check("a role two cases worse is worse", K.compare(runs, "tools")["worse"] == ["tools"])
runs["after"][0]["summary"]["tools"]["passed"] = 5
check("one case is the noise the models make", K.compare(runs, "tools")["ok"])
calls = []


class _Doc:
    returncode = 0
    stderr = ""
    stdout = json.dumps({"summary": {"everyday": {"passed": 5, "total": 6}}, "cases": [
        {"id": "e1", "passed": True}, {"id": "e2", "passed": False}]})


def fake_docker(argv, **kw):
    calls.append(argv)
    return _Doc()


bwt = W.start(imp, [srepo], "14", "pila")
(bwt / "services" / "nanobot").mkdir(parents=True, exist_ok=True)
(bwt / "services" / "nanobot" / "nanobot").mkdir(exist_ok=True)
(bwt / "services" / "nanobot" / "nanobot" / "x.py").write_text("x = 1\n", encoding="utf-8")
rec = K.run_bench(imp, [srepo], "14", "pila", "nanobot-prueba", "nanogpt:m", "everyday", 1,
                  run=fake_docker, busy=lambda: False)
cps = [c for c in calls if c[:2] == ["docker", "cp"]]
check("the benchmark's cases come from this checkout, never the worktree",
      all(str(bwt) not in c[2] for c in cps if c[2].endswith("/bench/."))
      and any(c[2] == str(K.ROOT / "services" / "nanobot" / "bench") + "/." for c in cps), cps)
execs = [c for c in calls if c[:2] == ["docker", "exec"] and "model_bench.py" in " ".join(c)]
check("before runs the container's own code, after the worktree's, first on the path",
      len(execs) == 2 and "-e" not in execs[0] and any(a.startswith("PYTHONPATH=/tmp/improve-14-")
                                                       for a in execs[1]), execs)
check("and what comes back is totals and failing ids, not the cases",
      rec["table"]["everyday"] == {"before": 5, "after": 5, "total": 6} and rec["ok"]
      and rec["runs"]["after"][0]["failing"] == ["e2"] and "prompt" not in json.dumps(rec), rec)
try:
    K.run_bench(imp, [srepo], "14", "pila", "c", "m", "everyday; rm -rf /", 1, run=fake_docker,
                busy=lambda: False)
    check("a role list is only role names", False)
except W.WorkError:
    check("a role list is only role names", True)
try:
    K.run_bench(imp, [lrepo], "13", "luces2", "c", "m", "everyday", 1, run=fake_docker, busy=lambda: False)
    check("a plugin has no benchmark: its tests judge it", False)
except W.WorkError as exc:
    check("a plugin has no benchmark: its tests judge it", "no benchmark" in str(exc))
K.run_tests(imp, [srepo], "14", "pila", run=lambda *a, **k: _Ok())
check("a behaviour change with tests and a benchmark for this exact diff may be committed",
      K.gate(imp, dict(srepo, branch=srepo["branch"]), "14", bwt) == [],
      K.gate(imp, srepo, "14", bwt))
(bwt / "services" / "nanobot" / "nanobot" / "x.py").write_text("x = 2\n", encoding="utf-8")
check("and edited afterwards, it needs both again",
      len(K.gate(imp, srepo, "14", bwt)) == 2, K.gate(imp, srepo, "14", bwt))

print("\nnumbers, not the assistant's word")
import measure as M  # noqa: E402
_u = sqlite3.connect(data / "usage.db")
for _col in ("cached_tokens", "completion_tokens"):
    _u.execute(f"ALTER TABLE token_usage ADD COLUMN {_col} INTEGER DEFAULT 0")
_u.commit(); _u.close()
out = M.usage(state, days=10000)
check("usage by scope and model", "deepseek" not in out and "cheap" in out and "strong" in out, out)
check("how turns ended", "max_iterations" in M.stops(state, days=10000))
check("and the runner's codes", "parse:dsml" in M.events(state, days=10000))
check("which tools", "skill:lights" in M.tools(state, days=10000))

print("\nwhat the Programmer reads first")
dd = cli.improve_dir({"paths": {"state": str(state)}})
check("the map and the rules, beside the inbox",
      (dd / "docs" / "MAP.md").exists() and (dd / "docs" / "CLAUDE.md").exists())
check("and the lessons, seeded once", "A claim the assistant makes about itself is a lead"
      in (dd / "lessons.md").read_text(encoding="utf-8"))
(dd / "lessons.md").write_text("# Lessons\n- una lección de la casa\n", encoding="utf-8")
cli.improve_dir({"paths": {"state": str(state)}})
check("and never reseeded over the household's own",
      (dd / "lessons.md").read_text(encoding="utf-8") == "# Lessons\n- una lección de la casa\n")

shim = W.write_shim(imp)
check("the Programmer's shim lives in the pipeline's folder and runs this cli",
      shim == imp / "bin" / "improve" and "deploy/improve/cli.py" in shim.read_text()
      and os.access(shim, os.X_OK))

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)
