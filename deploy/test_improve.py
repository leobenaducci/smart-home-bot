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

print("\nthe command, end to end, on the scratch tree")
os.environ["HOME_STACK_IMPROVE_DIR"] = str(tmp / "improve")
import cli  # noqa: E402
cli.live_config = lambda: {**cfg, "paths": {"state": str(state)}, "site": {"timezone": "UTC"}}
rc = cli.main(["collect", "--day", DAY, "--no-model", "--sample", "0"])
inbox = (tmp / "improve" / "inbox" / f"{DAY}.jsonl").read_text(encoding="utf-8")
check("it writes the inbox", rc == 0 and inbox.count("\n") == len(eps), inbox[:200])
check("with no login and no name in it", LOGIN not in inbox and "Tomi" not in inbox
      and "member-" in inbox, inbox[:300])
check("readable by this user alone",
      oct((tmp / "improve" / "inbox").stat().st_mode & 0o777) == "0o700"
      and oct((tmp / "improve" / "inbox" / f"{DAY}.jsonl").stat().st_mode & 0o777) == "0o600")
check("and the salt stays private",
      oct((tmp / "improve" / "private" / "salt").stat().st_mode & 0o777) == "0o600")

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)
