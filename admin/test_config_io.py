#!/usr/bin/env python3
"""Reading and writing config/home-stack.yml from the admin page.

Run: python admin/test_config_io.py

Two properties, both learned the hard way:

**Comments survive a save.** The config is documented as hand-editable and
carries about sixty lines explaining why each setting is what it is. A plain
`safe_dump` deleted all of them on the first Save, which is a bad trade for a
checkbox.

**Reading it is thread-safe.** It was not. A single shared `ruamel.YAML()`
carries parser state across a load, and this page reads the config from more
than one thread -- the model-price watcher runs on its own schedule and reads
the same file a request handler is reading. Whichever caller lost the race got
`'NoneType' object has no attribute 'anchor'` from inside the composer: an
opaque 500 on the page you would go to in order to fix a broken config. It
surfaced as a test that passed on one run and failed on the next.
"""
import os
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


tmp = Path(tempfile.mkdtemp(prefix="admin-config-"))
(tmp / "secrets").mkdir()
(tmp / "secrets" / "smart-home-bot.env").write_text("")
CONFIG = tmp / "home-stack.yml"
CONFIG.write_text(
    "# The comment that must survive a save.\n"
    "site:\n"
    "  # Shown as the portal title on every screen.\n"
    "  name: Test House\n"
    "locale:\n"
    "  default: en\n"
    "services:\n"
    "  admin:\n"
    "    enabled: true\n",
    encoding="utf-8")

os.environ["HOME_STACK_CONFIG"] = str(CONFIG)
os.environ["HOME_STACK_SECRETS"] = str(tmp / "secrets" / "smart-home-bot.env")
os.environ["HOME_STACK_MODELS_CACHE"] = str(tmp / "models.json")
os.environ["ADMIN_SECRET_KEY"] = "test" * 8

sys.path.insert(0, str(HERE))
import app as A  # noqa: E402

# --- comments survive -------------------------------------------------------

cfg = A.load_config()
check("the config loads", cfg.get("site", {}).get("name") == "Test House", cfg)

cfg["site"]["name"] = "Renamed House"
A.save_config(cfg)
written = CONFIG.read_text(encoding="utf-8")
check("the edit is written", "Renamed House" in written)
check("the file's own comments survive the save",
      "# The comment that must survive a save." in written
      and "# Shown as the portal title" in written, written)

# --- reading is safe from more than one thread ------------------------------
#
# Without a parser per call this fails within a few hundred iterations; the
# symptom is an exception from inside ruamel, not a wrong value.

errors = []
seen = []


def hammer():
    for _ in range(60):
        try:
            loaded = A.load_config()
            seen.append((loaded.get("site") or {}).get("name"))
        except Exception as exc:  # noqa: BLE001 - the failure being tested
            errors.append(exc)


threads = [threading.Thread(target=hammer) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

check("concurrent reads do not raise", not errors,
      f"{len(errors)} error(s), first: {errors[0] if errors else ''}")
check("and every one of them read the real value",
      seen and all(name == "Renamed House" for name in seen),
      set(seen))

# --- every path the config names is a path the page can edit ----------------
# `backups:` and `plugins:` were in the file and not on the Site page, so the
# two trees a household is most likely to want on another disk -- the archives
# and their own services -- could only be moved by hand-editing YAML the page
# is meant to replace. The form handler and the template now read one list, and
# this asserts the list is the whole of `paths:`.

import re

SITE_HTML = (HERE / "templates" / "site.html").read_text(encoding="utf-8")
EXAMPLE = HERE.parent / "config" / "home-stack.example.yml"

check("the page renders the list rather than its own copy",
      "for kind in path_kinds" in SITE_HTML,
      "a literal list here is a second thing to keep in step")
check("and the handler is given it",
      "path_kinds=PATH_KINDS" in (HERE / "app.py").read_text(encoding="utf-8"))

shipped = set(re.findall(r"^  (\w+): /", EXAMPLE.read_text(encoding="utf-8"), re.M))
# The example's `paths:` block, read out of the file rather than restated here.
shipped = {k for k in shipped if k in
           {"config", "state", "media", "backups", "plugins", "deploy"}}
check("the page covers every path the example ships",
      shipped and shipped <= set(A.PATH_KINDS),
      f"in the example and not on the page: {sorted(shipped - set(A.PATH_KINDS))}")

# --- every model source has somewhere to appear -----------------------------
# A source the fetchers fill and the picker has no group for is models fetched
# and offered nowhere. That is not hypothetical: the OpenCode Go group was
# filtering on a provider string no model carried, so thirty models sat in the
# cache and the page showed Ollama and nothing else, with no error anywhere.

import sys as _sys
_sys.path.insert(0, str(HERE))
import models as _M  # noqa: E402

# The titler and Paperless call /v1/chat/completions only; a Zen model that
# answers only on /v1/responses would fail every call for them.
check("a Responses-only Zen model is refused for titles",
      A._responses_only_for("titles", "gpt-5.6-luna"))
check("  and for documents", A._responses_only_for("documents", "gpt-5.6-terra"))
check("  but not a chat-completions Zen model",
      not A._responses_only_for("titles", "deepseek-v4-flash"))
check("  nor gpt-5 on a provider that serves it on chat completions",
      not A._responses_only_for("titles", "openai:gpt-5-mini"))
check("  nor for a role that goes through nanobot, which routes by model",
      not A._responses_only_for("everyday", "gpt-5.6-luna"))
check("  and the prefixes cover the vision ollama",
      not A._responses_only_for("documents", "ollama-vision:minicpm-v:8b"))

_grouped = {g["provider"] for g in A.MODEL_GROUPS}
check("every source is in MODEL_GROUPS", set(_M.SOURCES) <= _grouped,
      f"fetched but never shown: {sorted(set(_M.SOURCES) - _grouped)}")
check("and every group is a real source", _grouped <= set(_M.SOURCES),
      f"a group nothing can ever fill: {sorted(_grouped - set(_M.SOURCES))}")
check("the keyed sources are sources too",
      {s for s, _ in A.KEYED_SOURCES} <= set(_M.SOURCES),
      sorted({s for s, _ in A.KEYED_SOURCES} - set(_M.SOURCES)))

# --- a missing file is empty, not an error ----------------------------------

CONFIG.unlink()
check("a missing config reads as empty", A.load_config() == {})

# --- the config and the secrets file keep their inode ------------------------
# Both are bind-mounted into every container that reads them, as single files.
# A bind mount resolves the inode once, at mount time, and never again -- so a
# writer that renames a new file into place leaves the containers reading the
# old one. On the host that rename SUCCEEDS, which is the dangerous half: the
# change is on disk, the page shows the previous value, and nothing anywhere
# says they disagree. It took recreating a container to notice.
print("\nwriting these files does not replace them")

import os as _os  # noqa: E402

# The check above this one removes the config to prove a missing one reads as
# empty, so put a real file back before asking anything about its identity.
A.save_config({"site": {}, "services": {}})
_cfg_before = _os.stat(A.CONFIG).st_ino
_c = A.load_config()
_c.setdefault("site", {})["_inode_probe"] = "x"
A.save_config(_c)
check("save_config keeps the config's inode",
      _os.stat(A.CONFIG).st_ino == _cfg_before,
      f"{_cfg_before} -> {_os.stat(A.CONFIG).st_ino}")
check("  and the value really was written",
      (A.load_config().get("site") or {}).get("_inode_probe") == "x")
_c = A.load_config()
(_c.get("site") or {}).pop("_inode_probe", None)
A.save_config(_c)
check("  and removing it again also keeps it",
      _os.stat(A.CONFIG).st_ino == _cfg_before)
check("no .yml.tmp is left behind",
      not A.CONFIG.with_suffix(".yml.tmp").exists())

A.set_secret("INODE_PROBE", "x")            # make sure the file exists first
_sec_before = _os.stat(A.SECRETS).st_ino
A.set_secret("INODE_PROBE", "y")
check("set_secret keeps the secrets file's inode",
      _os.stat(A.SECRETS).st_ino == _sec_before,
      f"{_sec_before} -> {_os.stat(A.SECRETS).st_ino}")
check("  and the value really was written", A.get_secret("INODE_PROBE") == "y")
A.delete_secret("INODE_PROBE")
check("delete_secret keeps it too",
      _os.stat(A.SECRETS).st_ino == _sec_before)
check("  and the key is gone", A.get_secret("INODE_PROBE") == "")

# --- _t_or fills its placeholders ---------------------------------------------

# This shipped as a 500 waiting for one branch to run. `_t_or(key, fallback)`
# took no params, and the one caller that needed `{roles}` filled in raised
# TypeError -- so POST /models died with Werkzeug's own "Internal Server Error"
# page at the exact moment it had something to tell the household, and the
# refusal it was trying to explain was never seen.
print("\n_t_or fills its placeholders, from either source")
_msg = A._t_or("admin.models.zero_cost_refused",
               "Not saved for {roles}: fallback copy.",
               roles="everyday (a-free-model)")
check("  a translated string interpolates", "everyday (a-free-model)" in _msg, _msg)
check("  and leaves no placeholder behind", "{roles}" not in _msg, _msg)

_miss = A._t_or("admin.no.such.key.anywhere",
                "Not saved for {roles}: fallback copy.", roles="fallback (x)")
check("  the fallback interpolates too -- it is the branch that runs when a "
      "locale is missing the key", "fallback (x)" in _miss, _miss)
check("  and leaves no placeholder behind", "{roles}" not in _miss, _miss)
check("  a key with no params still resolves",
      A._t_or("admin.no.such.key.anywhere", "plain") == "plain")


print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
