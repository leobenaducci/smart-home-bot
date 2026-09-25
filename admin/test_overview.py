#!/usr/bin/env python3
"""What the landing page tells somebody who has just installed this.

Run: python admin/test_overview.py

The overview was a service count and a table of hosts. Both true; neither any
help on a fresh install, where the three things that actually stop the house
working are a missing model API key, nobody in the household, and settings
saved here but never deployed. The last is the failure the whole admin page is
written against -- saving is not deploying -- and the page a person lands on
said nothing about it.

So this asserts the page names each of those, and names the page that fixes
it, rather than asserting that a template renders.
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


tmp = Path(tempfile.mkdtemp(prefix="admin-overview-"))
(tmp / "config").mkdir()
(tmp / "secrets").mkdir()
CONFIG = tmp / "config" / "home-stack.yml"
SECRETS = tmp / "secrets" / "smart-home-bot.env"
SECRETS.write_text("")

os.environ["HOME_STACK_CONFIG"] = str(CONFIG)
os.environ["HOME_STACK_SECRETS"] = str(SECRETS)
os.environ["HOME_STACK_MODELS_CACHE"] = str(tmp / "config" / "models.json")
os.environ["ADMIN_SECRET_KEY"] = "test" * 8

sys.path.insert(0, str(HERE))
try:
    import bcrypt
except ImportError:
    print("SKIP: bcrypt is not installed (pip install -r admin/requirements.txt)")
    raise SystemExit(0)

FRESH = ("site:\n  name: Test House\nlocale:\n  default: en\n"
         "hosts:\n  hub:\n    address: 127.0.0.1\n    user: someone\n"
         "services:\n  admin:\n    enabled: true\n")
CONFIG.write_text(FRESH)

A = importlib.import_module("app")
A.app.config.update(TESTING=True)
client = A.app.test_client()

SECRETS.write_text("ADMIN_PASSWORD_HASH="
                   + bcrypt.hashpw(b"a-long-test-password", bcrypt.gensalt()).decode()
                   + "\n")
with client.session_transaction() as sess:
    sess["authed"] = True
    sess["pw"] = A._hash_fingerprint(A.admin_password_hash())


def page() -> str:
    return client.get("/").get_data(as_text=True)


def todo_keys(cfg_text: str, pending=None) -> set[str]:
    CONFIG.write_text(cfg_text)
    A.save_pending(pending or [])
    return {item["key"] for item in A.setup_todo(A.load_config(), A.load_manifest())}


# --- a fresh install is told all three, in the order they block -------------

A.save_pending([])
# A fresh install has no containers, and that -- not a log file this page writes
# only when it runs the deploy itself -- is what "nothing deployed yet" means.
# Stubbed for the whole file: every case below is about a config, and none of
# them is about what happens to be running on the machine the tests run on.
_REAL_PS = A._docker_ps
A._docker_ps = lambda: []


def _one_container():
    """A container belonging to a real project, so `anything_deployed` is true."""
    for name, spec in (A.load_manifest().get("services") or {}).items():
        for unit in (spec.get("units") or []):
            if unit.get("name"):
                proj = unit.get("compose_project") or f"{name}-{unit['name']}"
                return [{"project": proj, "name": "x", "state": "running",
                         "status": "Up 1 hour", "image": "img"}]
    return []

fresh = todo_keys(FRESH)
check("a missing model API key is named", "admin.overview.todo_secret" in fresh, fresh)
check("an empty household is named", "admin.overview.todo_members" in fresh, fresh)
check("never having deployed is named", "admin.overview.todo_never" in fresh, fresh)

body = page()
check("and the page renders them as sentences, not keys",
      "admin.overview.todo" not in body
      and "model API key" in body, body[:400])
check("each one links to the page that fixes it",
      '/secrets' in body and '/members' in body and '/deploy' in body)

# --- each one clears when the thing is actually done ------------------------

SECRETS.write_text(SECRETS.read_text() + "OPENCODE_API_KEY=sk-test\n")
check("the key stops being asked for once it is set",
      "admin.overview.todo_secret" not in todo_keys(FRESH), todo_keys(FRESH))

WITH_MEMBER = FRESH + "members:\n  - id: member1\n    display_name: A Person\n"
check("the household stops being asked for once somebody is in it",
      "admin.overview.todo_members" not in todo_keys(WITH_MEMBER))

# --- saved-but-not-deployed is the one that matters most --------------------
#
# A setting that never reaches a container is worse than one never changed,
# and until now the page a person lands on was silent about it.

A.DEPLOY_LOG.write_text("a previous run")
keys = todo_keys(WITH_MEMBER, pending=["home-core"])
check("pending changes are named", "admin.overview.todo_pending" in keys, keys)
check("and they replace the never-deployed line rather than joining it",
      "admin.overview.todo_never" not in keys, keys)

body = page()
check("the pending line says which services", "home-core" in body)

# --- and when there is genuinely nothing left, it says so -------------------

# Deployed *and* nothing pending: the one state where the list is empty. The
# stub covers the render too, because the page asks the same question again.
A._docker_ps = _one_container
try:
    settled = todo_keys(WITH_MEMBER, pending=[])
    check("a finished install has an empty list", settled == set(), settled)
    check("and the page says it is settled rather than showing an empty box",
          "set up and deployed" in page())
finally:
    A._docker_ps = lambda: []

# --- every string it can render exists in the catalogue ---------------------

en = json.loads((HERE.parent / "i18n" / "en.json").read_text(encoding="utf-8"))
wanted = set()
for cfg_text, pend in ((FRESH, []), (WITH_MEMBER, ["home-core"])):
    for item in A.setup_todo(*(lambda c: (c, A.load_manifest()))(
            (CONFIG.write_text(cfg_text), A.save_pending(pend), A.load_config())[2])):
        wanted.add(item["key"])
        wanted.add(item["action"])
wanted |= {"admin.overview.settled", "admin.overview.todo_intro",
           "admin.overview.at_a_glance", "admin.overview.members_count",
           "admin.overview.change", "admin.overview.hosts_intro"}
missing = sorted(k for k in wanted if k not in en)
check("no to-do renders as its own key name", not missing, missing)

# --- "nothing is deployed" has to be about the house ---------------------------
#
# It used to be `DEPLOY_LOG.exists()`, and that log is only written when a deploy
# is started *from this page*. Deploying with `./home-stack deploy` -- which is
# what the README documents and what this household does -- never creates it, so
# the landing page told a house running forty-four containers that nothing had
# been deployed and nothing would work until it was. The one page whose job is to
# say what needs doing, saying the largest possible wrong thing.
print("\nthe deploy to-do reflects containers, not this page's own paperwork")
_cfg, _man = A.load_config(), A.load_manifest()
A._docker_ps = _REAL_PS
try:
    check("  a stack with containers is deployed", A.anything_deployed(_cfg, _man))
finally:
    A._docker_ps = lambda: []
check("  a manifest whose projects never ran is not",
      not A.anything_deployed(_cfg, {"services": {"ghost": {"units": [{"name": "n"}]}}}))
# Fails towards "go and deploy" rather than "all is well": an admin container
# that cannot see the socket knows nothing, and silence is the wrong answer to
# give about a house.
_real_run, A._docker_ps = A.subprocess.run, _REAL_PS
A.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("docker"))
try:
    check("  and with no docker it does not claim success",
          not A.anything_deployed(_cfg, _man))
finally:
    A.subprocess.run = _real_run
    A._docker_ps = lambda: []

# The override the deployer honours, which `service_status()` also has to match:
# home-paperless deploys as `paperless`, so building only `<service>-<unit>`
# misses every container it owns.
check("  a unit's compose_project is what is matched",
      any((u.get("compose_project") or "") == "paperless"
          for s_ in (A.load_manifest().get("services") or {}).values()
          for u in (s_.get("units") or [])),
      "home-paperless no longer overrides it; this check has lost its subject")

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
