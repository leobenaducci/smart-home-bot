#!/usr/bin/env python3
"""Editing one member, on that member's own page.

Run: python admin/test_members.py

All of this used to be a row in the members table: name, language and admin
inline, then Off, a phone code, a Paperless token and Remove crammed into a
last column, each behind its own hidden form. What that shape cost was not
only how it looked -- the irreversible action sat beside three routine ones on
a row you might have scrolled to by mistake.

Two rules worth stating, because both are quiet when wrong:

- **An id is never editable and never reissued.** It is woven through paths,
  MQTT topics, derived tokens and backups. Filling a freed slot handed a new
  person the departed member's task token, assistant state and backups.
- **`services.nanobot.members` follows who is active.** One assistant instance
  per active member; if the two lists disagree the deployer allocates ports
  and credentials for people who are not here.
"""
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The reference catalogue. A `why` string that no key answers renders as the
# key name on the page, which is a bug this file could not see while it only
# compared the string against itself.
_EN_KEYS = set(json.loads((HERE.parent / "i18n" / "en.json").read_text(
    encoding="utf-8")))

try:
    import flask  # noqa: F401
except ImportError as exc:
    print(f"SKIP: {exc}")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="admin-members-")
shutil.copy(HERE.parent / "config" / "home-stack.example.yml", f"{tmp}/c.yml")
Path(f"{tmp}/s.env").write_text("ADMIN_PASSWORD_HASH=$2b$12$abcdefghijklmnopqrstuv\n")
# Every path this page can write to, pointed at scratch, and *before* app is
# imported -- module-level constants are computed at import and moving them
# afterwards is too late for anything that already ran.
#
# `HOME_STACK_STATE_DIR` is the one that mattered: without it the user store
# falls back to /var/lib/home-stack/state, so on a machine that actually runs
# this stack the removal cases below deleted a real person's portal account.
# They did, once, and it took recovering a bcrypt hash off the old system.
os.environ.update(ADMIN_SECRET_KEY="t" * 32, HOME_STACK_CONFIG=f"{tmp}/c.yml",
                  HOME_STACK_SECRETS=f"{tmp}/s.env",
                  HOME_STACK_MODELS_CACHE=f"{tmp}/m.json",
                  HOME_STACK_STATE_DIR=f"{tmp}/state",
                  HOME_STACK_CONFIG_DIR=f"{tmp}/config",
                  HOME_STACK_MEDIA_DIR=f"{tmp}/media",
                  HOME_STACK_BACKUPS_DIR=f"{tmp}/backups",
                  HOME_STACK_PLUGINS_DIR=f"{tmp}/plugins",
                  HOME_STACK_DEPLOY_ROOT=f"{tmp}/deployroot")
sys.path.insert(0, str(HERE))
import app as A  # noqa: E402

# Before anything runs, and it stops the file rather than failing a check.
#
# This suite removes members, and removing a member now takes their portal
# account with them. `USERS_FILE` is a module-level constant computed at import
# from `HOME_STACK_STATE_DIR`, whose default is the machine's own
# /var/lib/home-stack -- so on a box that actually runs this stack, these cases
# deleted a real person's login. They did, and recovering the bcrypt hash meant
# going to the machine the accounts were imported from.
#
# A `check()` would be too late: the destructive cases are above the section
# that reads these paths, and a red line at the end does not put a hash back.
for _name, _path in (("USERS_FILE", A.USERS_FILE), ("HISTORY_DIR", A.HISTORY_DIR)):
    if not str(_path).startswith(tmp):
        raise SystemExit(
            f"REFUSING TO RUN: {_name} is {_path}, outside the scratch tree at "
            f"{tmp}.\nThis suite deletes accounts. Point HOME_STACK_STATE_DIR "
            f"at scratch before importing app.")

A.app.config["TESTING"] = True
client = A.app.test_client()
with client.session_transaction() as sess:
    sess["authed"] = True
    sess["pw"] = A._hash_fingerprint(A.admin_password_hash())
    sess["_csrf"] = "tok"
H = {"Origin": "http://localhost"}

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label}" + (f"  <- {detail}" if detail else ""))


def post(path, **data):
    return client.post(path, data={"_csrf": "tok", **data}, headers=H)


def member(mid):
    return next((m for m in (A.load_config().get("members") or [])
                 if m["id"] == mid), None)


print("the list is a list")
html = client.get("/members").get_data(as_text=True)
check("it renders", "<table" in html)
# The five hidden forms per member are what made the row a control panel.
check("no hidden forms per row", 'style="display:none"' not in html,
      "each one was an action a stray click could submit")
check("no per-row action buttons",
      'name="action" value="remove"' not in html,
      "removing somebody belongs on their own page, not beside three "
      "routine buttons in a table cell")
check("every name links to that person",
      html.count("/members/user1") >= 1)
check("and adding is still here", 'value="add"' in html)

print("\none person, one page")
r = client.get("/members/user1")
check("it renders", r.status_code == 200)
page = r.get_data(as_text=True)
for field in ("display_name", "locale", "admin", "active",
              "birthdate", "hobbies", "notes"):
    check(f"  {field} is editable there", f'name="{field}"' in page)
check("  the id is shown and not editable",
      "user1" in page and 'name="id"' not in page,
      "an id is woven through paths, topics, derived tokens and backups")
check("somebody who does not exist goes back to the list",
      client.get("/members/nobody").status_code == 302)

print("\nand one save writes all of it")
post("/members/user1", display_name="Renamed", locale="es", admin="on",
     active="on", birthdate="1990-02-03", hobbies="chess", notes="allergic")
p = member("user1")
check("every field landed",
      p["display_name"] == "Renamed" and p["locale"] == "es"
      and p["admin"] is True and p.get("active") is True
      and p["birthdate"] == "1990-02-03" and p["hobbies"] == "chess", p)

print("\nthe other half of a relationship is offered, not demanded")
# Every relationship has a twin on somebody else's page and the household
# types both. The half nobody goes back to is what makes an assistant say
# "your sister Bo" to Bo herself.
_rel_cfg = A.load_config()
# The page renders in the config's locale, and these are Spanish words.
_rel_cfg.setdefault("locale", {})["default"] = "es"
with client.session_transaction() as _s:
    _s.pop("lang", None)
_rel_cfg["members"] = [
    {"id": "user1", "display_name": "Ana", "locale": "es", "admin": True,
     "relationships": {"user2": "hija", "user3": "hija"}},
    {"id": "user2", "display_name": "Bo", "locale": "es"},
    {"id": "user3", "display_name": "Cy", "locale": "es"},
]
A.save_config(_rel_cfg)
_s2 = A.suggest_relationships(A.load_config(), "user2", "es")
check("the reciprocal of a parent is offered to the child",
      _s2.get("user1") == ["padre", "madre"], _s2)
# In the language of the page it is offered on, not the language the other
# half happened to be typed in -- an imported profile is in whatever the old
# machine wrote, and the household reading this one should not meet it there.
_en2 = A.suggest_relationships(A.load_config(), "user2", "en")
check("  rendered in the language of the page",
      _en2.get("user1") == ["father", "mother"], _en2)
# Nobody has said anything about this pair. It follows from them sharing a
# parent, which is the inference that fills the most boxes in a family and
# the one a household is least likely to type twice.
check("  and two children of one parent are offered each other",
      _s2.get("user3") == ["hermana"], _s2)
check("  narrowed by what the household already said they are",
      "hermano" not in _s2.get("user3", []),
      "both were called `hija`, so `hermano` is not one of the two")
# The dangerous direction: a suggestion must never sit on top of an answer.
_rel_cfg["members"][1]["relationships"] = {"user1": "viejo", "user3": "ñaña"}
A.save_config(_rel_cfg)
check("a box somebody filled in is never suggested over",
      A.suggest_relationships(A.load_config(), "user2", "es") == {},
      A.suggest_relationships(A.load_config(), "user2", "es"))
# Both languages, because these are matched against what a person typed and
# this household has been typing `hija` for years.
# The words are the catalogue's, in every language it ships -- so a household
# typing `hija` and one typing `daughter` are both understood, and each is
# offered its own language back rather than the one the other half was typed
# in. An imported profile is in whatever the old machine wrote.
check("both languages are understood",
      A.relation_key("hija") == "daughter"
      and A.relation_key("daughter") == "daughter", A.relation_key("hija"))
check("  and the informal words a household actually types",
      A.relation_key("papá") == "father" and A.relation_key("mom") == "mother",
      A.relation_key("papá"))
check("  and free text is left alone rather than forced into a table",
      A.relation_key("the one with the dog") is None)
_page = client.get("/members/user3", headers=H).get_data(as_text=True)
# The one this found on a real install. The help text on this page used to say
# the opposite of what the code means, so a household filled four boxes the
# wrong way round: a father whose own page recorded each of his children as
# his `padre`. Each page reads fine alone; only holding the two halves side by
# side shows it.
_rel_cfg["members"] = [
    {"id": "user1", "display_name": "Ana", "locale": "es",
     "relationships": {"user2": "padre"}},          # says Bo is her father
    {"id": "user2", "display_name": "Bo", "locale": "es",
     "relationships": {"user1": "padre"}},          # and Ana is Bo's father
]
A.save_config(_rel_cfg)
_conf = A.relationship_conflicts(A.load_config(), "user1", "es")
# One word, not two: Ana calling Bo `padre` is also the household saying which
# of the two Bo is, so the correction it is offered is narrowed the same way a
# suggestion would be.
check("two halves that cannot both be true are called out",
      _conf.get("user2") == ["hijo"], _conf)
_page = client.get("/members/user1", headers=H).get_data(as_text=True)
check("  on the page, beside the box it is about",
      "⚠" in _page and 'name="rel:user2"' in _page, "invisible otherwise")
# The message names whose *other* page disagrees. It named the page you are
# already on, which reads as the page arguing with itself.
check("  naming the page that disagrees, not the one you are on",
      "Bo" in _page.split("⚠")[1][:120] and
      "Ana" not in _page.split("⚠")[1][:120],
      _page.split("⚠")[1][:120])
check("  with the other side's reading one click away",
      'data-for="rel:user2" data-term="hijo"' in _page,
      "a correction you have to retype is one nobody makes")
# Either side can be the wrong one and nothing here can tell which, so the
# other side has to be reachable rather than merely named.
check("  and the page it disagrees with is reachable from here",
      '/members/user2"' in _page.split("⚠")[1][:600],
      _page.split("⚠")[1][:200])
check("  and nothing is rewritten to make it agree",
      A.load_config()["members"][0]["relationships"]["user2"] == "padre",
      "a household's own words, corrected behind their back")
# Agreeing halves must not be nagged about, or the warning is noise.
_rel_cfg["members"][1]["relationships"] = {"user1": "hija"}
A.save_config(_rel_cfg)
check("  and halves that agree say nothing",
      A.relationship_conflicts(A.load_config(), "user1", "es") == {},
      A.relationship_conflicts(A.load_config(), "user1", "es"))
# The direction, with both names in it. A label that names both people cannot
# be read backwards, which is how those four boxes were filled in.
check("the row says which way round it is, by name",
      A.translator("admin.profile.rel_row", locale="es", name="Bo", other="Ana")
      == "Bo es … de Ana",
      A.translator("admin.profile.rel_row", locale="es", name="Bo", other="Ana"))

_rel_cfg["members"] = [
    {"id": "user1", "display_name": "Ana", "locale": "es", "admin": True,
     "relationships": {"user2": "hija", "user3": "hija"}},
    {"id": "user2", "display_name": "Bo", "locale": "es"},
    {"id": "user3", "display_name": "Cy", "locale": "es"},
]
A.save_config(_rel_cfg)
_page = client.get("/members/user3", headers=H).get_data(as_text=True)
# Matched on the attributes that carry the behaviour, not on the exact class
# string: these assertions pinned `class="ghost rel-hint"` and went red the
# moment the markup gained a third class, which is a test failing at a change
# that could not affect what it is about.
import re as _re2
_hints = _re2.findall(r"<button[^>]*\brel-hint\b[^>]*>", _page)
check("the page offers them as something to click",
      _hints and 'data-for="rel:user1"' in _page,
      "suggestions that are not clickable are just more reading")
check("  and clicking one cannot save the page by accident",
      all('type="button"' in b for b in _hints),
      "a bare <button> in a form submits it")
check("  in this locale's words",
      'data-term="padre"' in _page or 'data-term="madre"' in _page, _page[:0])
check("  and nothing is stored until somebody saves",
      not (A.load_config()["members"][2].get("relationships") or {}),
      "a suggestion wrote itself into the config")
_rel_cfg["members"] = _people_backup = [
    {"id": "user1", "display_name": "Ana", "admin": True, "locale": "en"},
    {"id": "user2", "display_name": "Bo", "locale": "en"},
]
A.save_config(_rel_cfg)

print("\na generated secret is the shape its reader demands")
# Everything here is "32 random bytes, written however" -- except the ones
# that are not. PROJECTS_KEY goes to Fernet, which wants exactly 32 bytes
# base64url-encoded; generated as hex it is 64 characters that decode to 48,
# and Fernet refuses it at import. The portal then logs "set but unusable"
# once and the credentials page asks the household for a key it already has.
import base64 as _b64
_fern = A.generate_secret("PROJECTS_KEY")
check("a Fernet key is 32 bytes, base64url",
      len(_b64.urlsafe_b64decode(_fern)) == 32,
      f"{len(_fern)} chars -> {len(_b64.urlsafe_b64decode(_fern))} bytes")
check("  and it is what the portal accepts",
      len(_fern) == 44, _fern[:8] + "…")
# The generic case must not become Fernet-shaped by accident: these are read
# as opaque strings and a change of shape is a change nothing would notice.
_plain = A.generate_secret("PROXY_SHARED_SECRET")
check("anything without a declared shape stays hex",
      len(_plain) == 64 and all(c in "0123456789abcdef" for c in _plain),
      _plain[:12] + "…")
# The installer generates the same keys before this page ever runs, so it has
# to know the same thing. A shape known in one place and not the other is a
# secret that works or does not depending on which filled it in.
_inst = open(os.path.join(str(A.ROOT), "deploy", "install.sh"), encoding="utf-8").read()
check("the installer knows the same shapes",
      "gen_fernet" in _inst and "PROJECTS_KEY)" in _inst,
      "install --generate-secrets would write the wrong shape")

print("\na birthday is written the way the reader writes birthdays")
# `<input type="date">` renders in the *browser's* locale, not the page's, so
# a Spanish-reading household on an English-profile browser was shown
# 03/02/2000 for a birthday everybody there writes 02/03/2000. Stored ISO
# either way -- the assistant reads it, and dates get sorted and compared.
_cfg = A.load_config(); _cfg.setdefault("locale", {})["default"] = "es"
A.save_config(_cfg)
with client.session_transaction() as _s:
    _s.pop("lang", None)
with A.app.test_request_context("/"):
    _pattern = A.date_pattern(A.load_config())
check("the locale says how its dates look", _pattern == "DD/MM/YYYY", _pattern)
post("/members/user1", display_name="Renamed", locale="es", admin="on",
     active="on", birthdate="02/03/2000")
check("a date typed that way is understood",
      member("user1")["birthdate"] == "2000-03-02", member("user1").get("birthdate"))
check("  and stored ISO, whatever was typed",
      A.format_date("2000-03-02", "DD/MM/YYYY") == "02/03/2000")
_page = client.get("/members/user1", headers=H).get_data(as_text=True)
check("  and shown back the way it was typed",
      'value="02/03/2000"' in _page,
      "the box shows the stored form, not the written one")
check("  with the format on screen rather than assumed",
      'placeholder="DD/MM/YYYY"' in _page, "03/04 is two different birthdays")
# The dangerous half. A parser that guesses moves somebody's birthday by
# months and nothing ever says so, so the wrong order has to be refused --
# here with a date that only reads as MM/DD, because the ambiguous ones are
# exactly the ones no parser can tell apart.
post("/members/user1", display_name="Renamed", locale="es", active="on",
     birthdate="12/25/2000")
check("the other order is refused, not guessed at",
      member("user1")["birthdate"] == "2000-03-02",
      member("user1").get("birthdate"))
# In the locale the page is rendered in. `A.t` falls back to English outside a
# request, which would compare a Spanish page against an English string and
# pass only by accident on a house that reads English.
check("  and the page says so",
      A.translator("admin.profile.birthdate_bad", locale="es",
                   format="DD/MM/YYYY")[:20]
      in client.get("/members/user1", headers=H).get_data(as_text=True),
      "refused in silence reads as saved")
post("/members/user1", display_name="Renamed", locale="es", active="on",
     birthdate="")
check("clearing it still clears it", member("user1")["birthdate"] == "",
      member("user1").get("birthdate"))
# ISO is unambiguous, so it is accepted everywhere -- which is what keeps an
# imported or hand-edited config from being rejected by its own page.
post("/members/user1", display_name="Renamed", locale="es", active="on",
     birthdate="2000-03-02")
check("ISO is accepted whatever the locale writes",
      member("user1")["birthdate"] == "2000-03-02", member("user1").get("birthdate"))
_cfg = A.load_config(); _cfg["locale"]["default"] = "en"; A.save_config(_cfg)

print("\nactive is a checkbox, and the assistant list follows it")
post("/members/user1", display_name="Renamed", locale="es")
check("unchecked means off", member("user1").get("active") is False)
check("and the instance goes with them",
      "user1" not in A.load_config()["services"]["nanobot"]["members"],
      A.load_config()["services"]["nanobot"]["members"])
post("/members/user1", display_name="Renamed", locale="es", active="on")
check("back on again", member("user1").get("active") is True)
check("and so does the instance",
      "user1" in A.load_config()["services"]["nanobot"]["members"])

print("\nthe services page asks three questions, not one")
# It had one merged list and two headings a word apart -- "your services" and
# "your own services" -- for three different things: what the package ships,
# what this house added as a plugin, and a bookmark to something nothing here
# runs. `finance-helper` sat among the shipped services as though it came in
# the box.
_ship = A.load_manifest().get("services") or {}
_merged = A.all_services_merged(A.load_config())
check("the shipped list is only what ships",
      "finance-helper" not in _ship and "home-core" in _ship,
      sorted(_ship)[:4])
check("  and the merged one is what deploys",
      set(_ship) <= set(_merged), sorted(set(_ship) - set(_merged)))
check("links are only what somebody typed here",
      all(r["source"] == "custom" for r in A.household_services(A.load_config())),
      [r.get("source") for r in A.household_services(A.load_config())])

# Two cards means two forms, and an unchecked box sends nothing -- so a loop
# over every known service would read "absent" as "switch it off" and disable
# every plugin service each time somebody saved the other card.
# Two services the merged list really has, so the loop genuinely reaches both;
# the form governs one of them.
_cfg = A.load_config()
_cfg.setdefault("services", {})["home-cameras"] = {"enabled": True}
_cfg["services"]["mqtt"] = {"enabled": True}
A.save_config(_cfg)
post("/services", **{"action": "toggle", "scope:home-cameras": "1",
                     "enabled:home-cameras": "on"})
check("saving one card leaves the other card's services alone",
      (A.load_config()["services"].get("mqtt") or {}).get("enabled") is True,
      A.load_config()["services"].get("mqtt"))
check("  and still saves its own",
      (A.load_config()["services"].get("home-cameras") or {}).get("enabled") is True)
post("/services", **{"action": "toggle", "scope:home-cameras": "1"})
check("  an unticked box in the form that owns it still switches it off",
      (A.load_config()["services"].get("home-cameras") or {}).get("enabled") is False,
      A.load_config()["services"].get("home-cameras"))

print("\nplugins are added and removed from the page, not the config file")
import tempfile as _tf, pathlib as _pl
_plug = _tf.mkdtemp(prefix="plugin-")
_pl.Path(_plug, "plugin.yml").write_text("contract: 1\nname: throwaway\n")
post("/services", action="add_plugin", plugin=_plug)
check("a directory with a plugin.yml is accepted",
      _plug in (A.load_config().get("plugins") or []),
      A.load_config().get("plugins"))
check("  and it is listed with what it contributes",
      any(r["entry"] == _plug and not r["error"]
          for r in A.plugins_view(A.load_config())),
      A.plugins_view(A.load_config()))
# Everything load_plugins refuses, refused here with its own message rather
# than accepted and left to fail at the next deploy.
_missing = _tf.mkdtemp(prefix="plugin-") + "-gone"
check("a directory that is not there is refused",
      A.check_plugin(A.load_config(), _missing) != "", _missing)
_empty = _tf.mkdtemp(prefix="plugin-")
check("  so is one with no plugin.yml",
      A.check_plugin(A.load_config(), _empty) != "")
check("  and one already on the list",
      A.check_plugin(A.load_config(), _plug) != "")
# One bad entry must not cost the good ones their row: load_plugins refuses the
# whole list, which is how the services page came to show only what ships.
_cfg2 = A.load_config()
_cfg2["plugins"] = [_plug, _missing]
A.save_config(_cfg2)
_rows = A.plugins_view(A.load_config())
check("a broken entry is reported beside the ones that work",
      len(_rows) == 2 and any(not r["error"] for r in _rows)
      and any(r["error"] for r in _rows),
      [(r["name"], bool(r["error"])) for r in _rows])
_cfg2["plugins"] = [_plug]
A.save_config(_cfg2)

# --- editing a path in place, which is what a moved directory needs ----------
# Until this existed the only repair was remove-and-re-add, with the old value
# gone from the screen at the moment you needed to retype it -- and on the row
# that needs it most, because a directory that has moved is exactly what makes
# load_plugins refuse the whole list.
_moved = _tf.mkdtemp(prefix="plugin-moved-")
_pl.Path(_moved, "plugin.yml").write_text("contract: 1\nname: throwaway\n")
_first = _tf.mkdtemp(prefix="plugin-first-")
_pl.Path(_first, "plugin.yml").write_text("contract: 1\nname: first\n")
_cfg3 = A.load_config()
_cfg3["plugins"] = [_first, _plug]
A.save_config(_cfg3)

post("/services", action="edit_plugin", plugin=_plug, entry=_moved)
check("editing a plugin writes the new path",
      _moved in (A.load_config().get("plugins") or []),
      A.load_config().get("plugins"))
check("  and drops the old one",
      _plug not in (A.load_config().get("plugins") or []))
# `plugins:` is ordered and the deployer reads it in order, so a corrected path
# must not send its plugin to the bottom of somebody's arranged list.
check("  in place, not appended at the end",
      (A.load_config().get("plugins") or []) == [_first, _moved],
      A.load_config().get("plugins"))

# A broken path is the usual reason to open this form, so validating the new
# value against a list that still holds the broken old one would refuse every
# repair. That is what `replacing` is for.
_cfg3 = A.load_config()
_cfg3["plugins"] = [_missing]
A.save_config(_cfg3)
check("a path can be corrected even when the old one no longer loads",
      A.check_plugin(A.load_config(), _moved, replacing=_missing) == "",
      A.check_plugin(A.load_config(), _moved, replacing=_missing))
post("/services", action="edit_plugin", plugin=_missing, entry=_moved)
check("  and the page does it",
      (A.load_config().get("plugins") or []) == [_moved],
      A.load_config().get("plugins"))

# The refusals the add path already makes, made here too rather than left to
# fail at the next deploy.
_cfg3 = A.load_config()
_cfg3["plugins"] = [_first, _moved]
A.save_config(_cfg3)
post("/services", action="edit_plugin", plugin=_moved, entry=_missing)
check("editing to a directory that is not there is refused",
      (A.load_config().get("plugins") or []) == [_first, _moved],
      A.load_config().get("plugins"))
post("/services", action="edit_plugin", plugin=_moved, entry=_first)
check("  and so is editing one onto another entry's path",
      (A.load_config().get("plugins") or []) == [_first, _moved],
      A.load_config().get("plugins"))
post("/services", action="edit_plugin", plugin=_moved, entry="")
check("  and a blank path changes nothing",
      (A.load_config().get("plugins") or []) == [_first, _moved],
      A.load_config().get("plugins"))
# An edit is not a way back in for a plugin somebody deliberately removed: the
# other half of a browser tab left open while the row was deleted.
post("/services", action="edit_plugin", plugin=_plug, entry=_moved)
check("editing a row that is no longer listed re-adds nothing",
      (A.load_config().get("plugins") or []) == [_first, _moved],
      A.load_config().get("plugins"))
# Same value in and out: not an error, and not a second entry either.
post("/services", action="edit_plugin", plugin=_moved, entry=_moved)
check("  and saving a path unchanged leaves the list alone",
      (A.load_config().get("plugins") or []) == [_first, _moved],
      A.load_config().get("plugins"))
check("  with the directories all still on disk",
      all(os.path.isdir(d) for d in (_first, _moved)),
      "editing a path must not touch anybody's project")

_cfg2 = A.load_config()
_cfg2["plugins"] = [_plug]
A.save_config(_cfg2)
post("/services", action="remove_plugin", plugin=_plug)
check("removing takes it off the list",
      _plug not in (A.load_config().get("plugins") or []))
import os as _os
check("  and leaves the directory alone",
      _os.path.isdir(_plug), "removing a plugin must not delete a project")

print("\na linked phone is per person, and reaches the container")
# It was one member's, written into the compose file by name, along with their
# bridge. The page is the only place a household manages members, so a second
# linked phone has to be a checkbox here rather than an edit to a file.
post("/members/user1", display_name="Renamed", locale="es", active="on",
     whatsapp="on")
check("checked is stored on the person",
      member("user1").get("whatsapp") is True, member("user1"))
_D = A._deployer()
if _D is not None:
    # Through the deployer, the way the admin page reaches everything it must
    # agree with it about -- and the way that proves the renderer is loadable
    # from here at all. It is not importable by name: this page loads the
    # deployer from a file path, so its directory is not on sys.path.
    _CM = _D._generator("compose_members")
    _rendered = _CM.render(A.load_config())
    check("  the renderer gives them a bridge",
          "whatsapp-bridge-user1:" in _rendered,
          "the checkbox saved and no container was rendered for it")
    check("  and turns the skill on for their assistant",
          "WHATSAPP_ENABLED=1" in _rendered.split("nanobot-user1:")[1]
          .split("whatsapp-bridge")[0], "saved, and the skill stays off")
    check("  and nobody else's",
          _rendered.count("WHATSAPP_ENABLED=1") == 1,
          "a per-person setting that reached every person")
    check("  and the deployer asks compose to start it",
          "whatsapp-user1" in _CM.profiles(A.load_config()),
          "a bridge nothing starts is a bridge that is not there")
post("/members/user1", display_name="Renamed", locale="es", active="on")
check("unchecked takes it away", member("user1").get("whatsapp") is False)
if _D is not None:
    check("  and the bridge with it",
          "whatsapp-bridge-user1:" not in _CM.render(A.load_config()))

print("\nremoving is its own action, and takes the credentials with it")
before_keys = {k for k, _ in A.member_keys("user2")}
A.set_secret(sorted(before_keys)[0], "x" * 64)
r = post("/members/user2", action="remove")
check("it redirects to the list", r.status_code == 302)
check("the person is gone", member("user2") is None)
check("their instance too",
      "user2" not in A.load_config()["services"]["nanobot"]["members"])
present = A.secret_keys_present()
check("and every credential of theirs",
      not any(present.get(k) for k in before_keys),
      "a live bearer token with no owner is still a live bearer token")

print("\nand an id is never reissued")
seq_before = A.load_config().get("next_member_seq")
post("/members", action="add", display_name="New Person")
people = A.load_config().get("members") or []
new = [m for m in people if m["display_name"] == "New Person"]
check("the new member exists", len(new) == 1, people)
check("with an id nobody had before",
      new and new[0]["id"] not in ("user1", "user2"), new)
check("and the counter only goes up",
      int(A.load_config()["next_member_seq"]) > int(seq_before or 0),
      "filling a freed slot hands somebody the departed member's tokens")

# --- the Deploy button, and when it must not be offered ---------------------
# This page runs in a container. A role at 127.0.0.1 means "this machine", and
# in here that is the container: the containers would start, because the Docker
# socket is mounted, so the work lands on one machine and the checking on
# another. The deployer has always refused it -- what was missing is that the
# page offered the button anyway, so the only way to learn was to press it and
# read a failed job out of the log.

print("\ndeploying from the page is refused where it cannot work")
_real_exists = os.path.exists
os.path.exists = lambda q: True if q == "/.dockerenv" else _real_exists(q)
try:
    loopback = {"hosts": {"hub": {"address": "127.0.0.1"},
                          "compute": {"address": "127.0.0.1"}}}
    check("a loopback role in a container names the roles",
          A.cannot_deploy_from_here(loopback) == "compute, hub",
          A.cannot_deploy_from_here(loopback))
    check("a real address is fine — it goes over ssh",
          A.cannot_deploy_from_here({"hosts": {"hub": {"address": "192.168.1.9"}}}) == "")
    check("and one loopback role among several is still refused",
          A.cannot_deploy_from_here({"hosts": {"hub": {"address": "192.168.1.9"},
                                               "compute": {"address": "127.0.0.1"}}}) == "compute")
    os.environ["HOME_STACK_ALLOW_CONTAINER_LOCAL"] = "1"
    check("the override is honoured, for a container that really is the host",
          A.cannot_deploy_from_here(loopback) == "")

    # And that container must not also rewrite loopback to
    # host.docker.internal, which host networking does not resolve. The two
    # settings have to agree: the admin compose sets both, and one without the
    # other is a page that can deploy but cannot see the local Ollama, or the
    # reverse.
    os.environ["HOME_STACK_HOST_NETWORK"] = "1"
    check("on host networking 127.0.0.1 is left alone",
          A._from_this_container("127.0.0.1") == "127.0.0.1")
    del os.environ["HOME_STACK_HOST_NETWORK"]
    check("and rewritten when it is a bridge",
          A._from_this_container("127.0.0.1") == "host.docker.internal")
    check("a real address is never rewritten either way",
          A._from_this_container("192.168.1.9") == "192.168.1.9")
    del os.environ["HOME_STACK_ALLOW_CONTAINER_LOCAL"]

    # And the POST is refused too, not only the button hidden: a stale tab can
    # still submit, and starting a job that can only fail costs a minute and
    # leaves a failed run on the page.
    import copy
    cfg = A.load_config()
    cfg["hosts"] = {"hub": {"address": "127.0.0.1"}}
    A.save_config(cfg)
    page = client.get("/deploy").get_data(as_text=True)
    check("the page says so before the button",
          "blocked" in page or "127.0.0.1" in page,
          "a button whose only outcome is a failed job is worse than no button")
    r = post("/deploy", target="all")
    check("and a submitted form is refused rather than started",
          r.status_code == 302 and not A._deploy_job["running"],
          A._deploy_job)
finally:
    os.path.exists = _real_exists

# --- and what each choice actually submits ----------------------------------
# The blocked case above returns before the route computes anything, so it
# proved nothing about the path that runs. `names` was read there and defined
# below it, and every Preview and Deploy of «All» was a 500 -- with the page
# deployed, tested by hand, and reported as working.
#
# `_start_deploy` is stubbed: the point is which targets the route decides on,
# and actually running the deployer from a unit test would deploy the house.

print("\nand a submitted deploy asks for the right thing")
_started = []
_real_start = A._start_deploy
A._start_deploy = lambda targets, dry: _started.append((list(targets), dry)) or True
try:
    for _target, _want_dry in (("all", False), ("all", True), ("mqtt", False)):
        _started.clear()
        data = {"target": _target}
        if _want_dry:
            data["dry_run"] = "1"
        r = post("/deploy", **data)
        check(f"  target={_target}{' (preview)' if _want_dry else ''} is accepted",
              r.status_code == 302 and len(_started) == 1,
              f"{r.status_code}, started={_started}")
        if _started:
            got, dry = _started[0]
            check(f"    and asks for {'a dry run' if _want_dry else 'a real run'}",
                  dry is _want_dry, dry)
            if _target == "all":
                check("    all means every enabled service",
                      got == ["all"], got)
            else:
                check(f"    {_target} means just that one", got == [_target], got)

    # The same, as the container: «All» has to become an explicit list with this
    # page left out, because the deployer would otherwise take down the
    # container the deploy is running in.
    _real_exists = os.path.exists
    os.path.exists = lambda q: True if q == "/.dockerenv" else _real_exists(q)
    os.environ["HOME_STACK_ALLOW_CONTAINER_LOCAL"] = "1"
    try:
        _started.clear()
        r = post("/deploy", target="all")
        check("  in a container, all is expanded", r.status_code == 302 and _started,
              f"{r.status_code}, started={_started}")
        if _started:
            got, _ = _started[0]
            check("    to a real list rather than the word",
                  got and got != ["all"], got)
            check("    with this page left out of it",
                  A.SELF_SERVICE not in got, got)
        _started.clear()
        r = post("/deploy", target=A.SELF_SERVICE)
        check("  and deploying this page is refused outright",
              r.status_code == 302 and not _started, _started)
    finally:
        os.path.exists = _real_exists
        os.environ.pop("HOME_STACK_ALLOW_CONTAINER_LOCAL", None)
finally:
    A._start_deploy = _real_start

# --- every page opens -------------------------------------------------------
# The cheapest check there is, and the one that was missing. Two 500s shipped
# in a day and neither was found by a test: `names` read before it was defined
# (every Preview and Deploy of «All»), and the model cache with nowhere
# writable once this container stopped being root -- /state is a directory in
# the image and root owns it, so save_cache raised PermissionError on
# models.json.tmp and the Models page returned a 500.
#
# Both were found by opening the page. So this opens all of them.

print("\nevery page opens")
for _path in ("/", "/site", "/services", "/members", "/models", "/secrets",
              "/deploy", "/backups", "/healthz"):
    try:
        _r = client.get(_path)
        _ok = _r.status_code < 500
    except Exception as _exc:                     # noqa: BLE001 - report, do not raise
        _ok, _r = False, _exc
    check(f"  {_path}", _ok,
          f"{getattr(_r, 'status_code', type(_r).__name__)}: "
          f"{getattr(_r, 'status_code', _r)}")

# And one member's own page, which needs an id that exists.
_people = A.load_config().get("members") or []
if _people:
    _r = client.get(f"/members/{_people[0]['id']}")
    check(f"  /members/{_people[0]['id']}", _r.status_code < 500, _r.status_code)

# --- every form actually saves ----------------------------------------------
# Opening a page proves nothing about saving on it. `save_config` writes
# `home-stack.yml.tmp` beside the config and renames -- and once this container
# stopped running as root, /state was not writable, so *every* Save on the page
# was a 500: members, site, services, models. The page-opens check above passed
# throughout, because a GET never writes.
#
# So each form is submitted with its own required fields, and the assertion is
# that the response is not a 500 and the config is still readable afterwards.

print("\nevery form saves")
_before = A.load_config()
_forms = [
    ("/members/" + (_before.get("members") or [{"id": "user1"}])[0]["id"],
     {"display_name": "Probe", "locale": "en", "active": "on"}),
    ("/site", {"name": "Probe House", "domain": "home",
               "timezone": "UTC", "default_locale": "en", "available": "en"}),
    ("/services", {}),
    ("/models", {"price_check": "daily"}),
]
for _path, _data in _forms:
    _r = post(_path, **_data)
    check(f"  POST {_path}", _r.status_code < 500, _r.status_code)
    # A save that leaves the config unreadable is worse than one that fails:
    # the deployer refuses to run and the portal refuses to start, from a file
    # nobody knowingly edited.
    try:
        _reread = A.load_config()
        _ok = isinstance(_reread, dict) and bool(_reread)
    except Exception as _exc:                     # noqa: BLE001
        _ok, _reread = False, _exc
    check(f"    and the config still parses", _ok, _reread)

# Put back what the probes changed, so running this twice is the same as once.
A.save_config(_before)
check("  the config is restored afterwards",
      A.load_config().get("site", {}).get("name") == _before.get("site", {}).get("name"))

# --- the backup page tells the truth about what is kept ---------------------
# It exists so somebody can point their own tool at the right directories, so
# the one thing it must not do is invent the list. It is computed by
# deploy/backup.py from the manifest's `state:` entries -- the same code
# `./home-stack backup` runs -- and this asserts the two agree.

print("\nthe backup page is derived, not authored")
_inv = A.backup_inventory(A.load_config())
check("  it computes without error", not _inv["error"], _inv["error"])
_by = {g["name"]: g["rows"] for g in _inv["groups"]}
check("  something is kept by every run", _by.get("essential"),
      "an empty essential list means a backup that saves nothing")
check("  and the groups are the three that mean different things",
      set(_by) <= {"essential", "bulk", "skipped"}, sorted(_by))

_paths = {r["path"] for rows in _by.values() for r in rows}
check("  every path is absolute",
      all(p.startswith("/") for p in _paths),
      [p for p in _paths if not p.startswith("/")])
check("  and interpolated, not left as a template",
      not any("{" in p for p in _paths),
      [p for p in _paths if "{" in p])

# The archive directory must not be in its own list: backing up a backup is
# not the point, and including it makes each run copy the last one.
check("  the archive directory is not in the list",
      _inv["root"] not in _paths, _inv["root"])

# What `./home-stack backup` would actually keep, compared with what the page
# shows as kept. Two answers to one question is the failure this page invites.
_mod = A._backup_module()
if _mod is None:
    check("  deploy/backup.py loads", False, "the page falls back to an error")
else:
    _real = {e["path"] for e in _mod.entries(A.load_config(), include_bulk=True)}
    _shown = {r["path"] for name in ("essential", "bulk") for r in _by.get(name, [])}
    check("  the page shows exactly what a run would keep", _real == _shown,
          f"only in the run: {sorted(_real - _shown)}; "
          f"only on the page: {sorted(_shown - _real)}")

# And the manifest's own policy has to match what docs/backups.md claims. The
# inventory test checks a declared path is *documented*; it does not check
# which table, so `{state}/admin` was documented as not-kept while the manifest
# had it as essential and every archive carried it.
# The doc separates them with `### ` headings, so find the one each path is
# written under rather than guessing at a section. A first version split on
# `## ` and took the last chunk, which reported seven false failures for
# entries that were documented perfectly well -- an assertion about the
# document's shape, dressed up as one about its content.
_docs = (HERE.parent / "docs" / "backups.md").read_text(encoding="utf-8")
_sections = {}
_current = ""
for _line in _docs.splitlines():
    if _line.startswith("### "):
        _current = _line[4:].strip()
        _sections[_current] = []
    elif _line.startswith("## "):
        # A `##` ends the `###` above it. Without this everything after the
        # next top-level heading was folded into the last subsection, so
        # «Do not bother» swallowed the databases section and this reported
        # seven contradictions that were nothing of the kind.
        _current = ""
    elif _current:
        _sections[_current].append(_line)
_not_kept = "\n".join(
    "\n".join(body) for head, body in _sections.items()
    if "do not bother" in head.lower())
check("  the doc has a not-kept section to check against", bool(_not_kept.strip()),
      sorted(_sections))

# What has to agree is *whether* a path is kept, not the shape it is written
# in. `{media}/paperless-consume` is explained in a paragraph rather than a
# table row, and rightly: the reason is that restoring the inbox makes
# Paperless ingest those files a second time, which does not fit a cell. So a
# skipped path counts as documented if it is in the not-kept table, or
# anywhere that says `backup: skip` about it.
for _row in _by.get("skipped", []):
    _leaf = _row["path"].split("/")[-1]
    _para = [blk for blk in _docs.split("\n\n")
             if _leaf in blk and "backup: skip" in blk]
    check(f"  skipped {_leaf} is documented as not kept",
          _leaf in _not_kept or _para,
          "the manifest skips it; docs/backups.md should either list it under "
          "«Do not bother» or say `backup: skip` about it, or the two disagree "
          "quietly -- which is how {state}/admin ended up documented as "
          "not-kept while every archive carried it")

# The reverse direction -- everything the doc calls not-kept really being
# skipped -- is deliberately not asserted. «Do not bother» mixes two different
# claims. Some rows are rules a manifest can hold: `{state}/registry` is
# rebuilt by pushing the images again, and could carry `backup: skip`. Others
# are advice to a person: a cache that is merely large and re-downloadable is
# still *captured* by a run that asks for it, and whether that is worth the
# gigabytes is the household's call, not the manifest's. A check that cannot
# tell the two apart would be asserting a rule the document does not follow,
# and those rows would be edited to satisfy the test rather than because
# anybody decided.
# --- the portal login ------------------------------------------------------
#
# This page decided everything about a member except whether they could sign
# in. `install --create-user` made exactly one account and nothing made a
# second, so every member added afterwards existed in the config, had an
# assistant and a folder on the share, and no way to reach either.
#
# The store is the portal's, at `{paths.state}/home-core/users.json`.
print("\ngiving somebody a way into the house")
# Not reassigned here: the environment above already points them at scratch,
# and a test that repaired the paths after import would have said nothing about
# whether they were right for the twenty checks that ran before it.
_users = A.USERS_FILE
os.makedirs(A.HISTORY_DIR, exist_ok=True)
os.makedirs(os.path.dirname(_users), exist_ok=True)
Path(_users).write_text("[]")

_cfg = A.load_config()
_first = (_cfg.get("members") or [{}])[0].get("id")
check("the example config has somebody to give an account to", bool(_first), _first)

# A password is not optional for a new account: a record with no hash is one
# nobody can use and every bcrypt check downstream has to defend against.
ok, why = A.set_portal_login(_cfg, _first, "ana", None)
check("a new account without a password is refused",
      (ok, why) == (False, "password_required"), (ok, why))
ok, why = A.set_portal_login(_cfg, _first, "ana", "short")
check("and one with a password too short is too",
      (ok, why) == (False, "password_short"), (ok, why))
check("neither of those wrote anything", A.load_users() == [], A.load_users())

ok, why = A.set_portal_login(_cfg, _first, "ana", "a-long-enough-password")
check("a login and a password makes an account", ok and why == "saved", (ok, why))
_rec = A.login_of(_first)
check("stored against the member, not only the login name",
      _rec["member"] == _first, _rec)
check("the hash is stored, not the password", _rec["hash"].startswith("$2"),
      _rec["hash"][:12])
check("the plaintext is nowhere in the file",
      "a-long-enough-password" not in Path(_users).read_text())
check("the file is readable by nobody else",
      oct(os.stat(_users).st_mode & 0o777) == "0o600",
      oct(os.stat(_users).st_mode & 0o777))
# The assistant instance is the member's *position* in the deployed list, not
# the digits in their id: ids are monotonic and never reused, so a household
# that has seen departures runs user2 beside user15 -- and 15 is a port
# nothing is listening on.
check("the assistant instance is a position, not the digits in the id",
      _rec["nanobot_id"] == A.member_nanobot_id(_cfg, _first), _rec)
_sparse = {"members": [{"id": "user2"}, {"id": "user15"}],
           "services": {"nanobot": {"members": ["user2", "user15"]}}}
check("a sparse member list still numbers from one",
      (A.member_nanobot_id(_sparse, "user2"),
       A.member_nanobot_id(_sparse, "user15")) == (1, 2),
      (A.member_nanobot_id(_sparse, "user2"),
       A.member_nanobot_id(_sparse, "user15")))

# The duplicate check has to compare records, not two separately-loaded
# copies of the same one: it did the latter, so re-saving your own login came
# back "already taken".
ok, why = A.set_portal_login(_cfg, _first, "ana", None)
check("re-saving your own login is not a collision with yourself",
      ok and why == "saved", (ok, why))

print("\nand not into somebody else's")
_second = (_cfg.get("members") or [{}, {}])[1].get("id")
ok, why = A.set_portal_login(_cfg, _second, "ana", "another-long-password")
# `taken`, not `login_taken`: the caller flashes `admin.members.login_{why}`,
# and the catalogue key is `admin.members.login_taken`. This assertion is what
# made the doubled `login_login_taken` invisible.
check("a login already in use by another member is refused",
      (ok, why) == (False, "taken"), (ok, why))
check("and its flash key is one the catalogue has",
      f"admin.members.login_{why}" in _EN_KEYS, why)
check("and the account it belongs to is untouched",
      A.login_of(_first)["username"] == "ana", A.login_of(_first))

print("\nchanging a password keeps everything else")
_before_hash = A.login_of(_first)["hash"]
ok, _ = A.set_portal_login(_cfg, _first, "ana", "a-different-long-password")
check("the hash changes", ok and A.login_of(_first)["hash"] != _before_hash)
ok, _ = A.set_portal_login(_cfg, _first, "ana", None)
check("and a blank password leaves the old one alone",
      A.login_of(_first)["hash"] != _before_hash
      and A.login_of(_first)["hash"] is not None)

print("\nrenaming a login carries the conversations with it")
os.makedirs(os.path.join(A.HISTORY_DIR, "ana"), exist_ok=True)
Path(os.path.join(A.HISTORY_DIR, "ana", "2026-08-26.json")).write_text("[]")
ok, why = A.set_portal_login(_cfg, _first, "anita", None)
check("the rename is reported as one", ok and why == "renamed", (ok, why))
check("the history is under the new name",
      os.path.isfile(os.path.join(A.HISTORY_DIR, "anita", "2026-08-26.json")))
check("and not still under the old one",
      not os.path.exists(os.path.join(A.HISTORY_DIR, "ana")))
# Two directories for one person is worse than one, because the portal reads
# exactly one of them and nothing says which.
os.makedirs(os.path.join(A.HISTORY_DIR, "occupied"), exist_ok=True)
Path(os.path.join(A.HISTORY_DIR, "occupied", "keep.json")).write_text("[1]")
A.set_portal_login(_cfg, _first, "occupied", None)
check("a rename onto a name that already has history leaves it alone",
      Path(os.path.join(A.HISTORY_DIR, "occupied", "keep.json")).read_text() == "[1]")
A.set_portal_login(_cfg, _first, "anita", None)

print("\nremoving a member takes their way in with them")
# It did not, so somebody removed from the house could still sign in to it --
# with an assistant that is no longer deployed and a folder nothing lists.
check("there is an account to remove", A.login_of(_first) is not None)
check("removing it says so", A.drop_portal_login(_first) is True)
check("and it is gone", A.login_of(_first) is None, A.load_users())
check("removing nobody's account says so too",
      A.drop_portal_login("user404") is False)
# Their conversations stay: an id is never reissued, so nothing can inherit
# them, and deleting somebody's history is not a side effect of tidying config.
check("their conversations are left on disk",
      os.path.isdir(os.path.join(A.HISTORY_DIR, "anita")))

print("\nand the page offers it")
_page = client.get(f"/members/{_first}").get_data(as_text=True)
check("the card is on the member's page",
      A.t("admin.profile.heading_login") in _page,
      "the login card is not rendering")
check("it says there is no account yet",
      A.t("admin.profile.login_none") in _page, "the empty state is not shown")
_r = post(f"/members/{_first}", action="portal_login", login="bo",
          password="one-more-long-password")
check("posting the form creates the account", _r.status_code in (302, 200), _r.status_code)
check("and it is in the store", (A.login_of(_first) or {}).get("username") == "bo",
      A.load_users())
_page = client.get(f"/members/{_first}").get_data(as_text=True)
check("the page now shows the login name", ">bo<" in _page or 'value="bo"' in _page,
      "the account is not shown back")
check("and never the hash",
      (A.login_of(_first) or {}).get("hash", "$2b$")[:4] not in _page,
      "a password hash must not reach the browser")

# --- a VPS this stack does not deploy ---------------------------------------
print("\nthe VPS can be configured without being deployed from here")
_c = A.load_config()
_c.setdefault("cloud", {}).setdefault("vps", {}).update(
    {"enabled": True, "host": "vps.example", "user": "", "tunnel_port": 21100,
     "serve_entry_page": False, "managed": True})
A.save_config(_c)
_form = {"name": (_c.get("site") or {}).get("name", "House"),
         "vps": "on", "vps_host": "vps.example", "vps_user": "",
         "vps_tunnel_port": "21100"}
post("/site", **_form)                      # vps_managed absent = unticked
_v = (A.load_config().get("cloud") or {}).get("vps") or {}
check("unticking it is recorded", _v.get("managed") is False, _v)
check("and the VPS itself stays configured",
      _v.get("enabled") is True and _v.get("host") == "vps.example", _v)
post("/site", **_form, vps_managed="on")
_v = (A.load_config().get("cloud") or {}).get("vps") or {}
check("ticking it again is too", _v.get("managed") is True, _v)
_page = client.get("/site").get_data(as_text=True)
check("the control is on the page", 'name="vps_managed"' in _page)

# --- reaching another machine -----------------------------------------------
#
# `{config}/ssh` was mounted read-only and started empty, so nothing could mint
# a key or record a host key: a split install worked from a terminal and not
# from this page, and the reason arrived as an ssh error inside a deploy log.
#
# Underneath that was something worse. The container runs as the deploying
# account's numeric id with no matching /etc/passwd entry, and every OpenSSH
# tool calls getpwuid() before anything else -- `ssh`, `ssh-keygen` and
# `ssh-keyscan` all exit 255 with "No user exists for uid 1000". Reaching a
# second machine from this page could not have worked at all.
print("\ngetting to another machine")
_sshdir = os.path.join(tmp, "ssh")
A.SSH_DIR, A.SSH_KEY = _sshdir, os.path.join(_sshdir, "id_ed25519")
A.KNOWN_HOSTS = os.path.join(_sshdir, "known_hosts")

check("there is no key to begin with", A.deploy_key()["present"] is False)
_ok, _why = A.make_deploy_key()
check("one can be generated", _ok and _why == "key_made", (_ok, _why))
_key = A.deploy_key()
check("and it is present afterwards", _key["present"])
check("with a public half to put on the other machine",
      _key["public"].startswith("ssh-ed25519"), _key["public"][:30])
check("and a fingerprint to check it by",
      "SHA256:" in _key["fingerprint"], _key["fingerprint"])
check("the private half is never handed out",
      not any("PRIVATE" in str(v) for v in _key.values()),
      "the private key must not leave the file")
check("the key file is readable by nobody else",
      oct(os.stat(A.SSH_KEY).st_mode & 0o777) == "0o600",
      oct(os.stat(A.SSH_KEY).st_mode & 0o777))

# Replacing it would lock this page out of every machine that trusts the old
# one, from a button whose label says "generate".
_again = A.make_deploy_key()
check("a second one is refused", _again == (False, "key_exists"), _again)
check("and the first is untouched",
      A.deploy_key()["public"] == _key["public"])

# Only machines that are actually elsewhere. A loopback role is this box and
# the deployer skips ssh for it entirely, so offering a fingerprint for it
# would describe a connection that never happens.
_rcfg = {"hosts": {"hub": {"address": "127.0.0.1"},
                   "compute": {"address": "192.168.1.11", "user": "homestack"},
                   "storage": {"address": "localhost"}},
         "cloud": {"vps": {"enabled": True, "host": "vps.example",
                           "user": "root"}}}
_remote = {r["role"]: r for r in A.remote_roles(_rcfg)}
check("a role on this machine is not listed", "hub" not in _remote, sorted(_remote))
check("nor one pointing at localhost", "storage" not in _remote, sorted(_remote))
check("a role with a real address is", "compute" in _remote, sorted(_remote))
check("and so is the vps, which is not a hosts: entry at all",
      _remote.get("vps", {}).get("address") == "vps.example", _remote.get("vps"))
_rcfg["cloud"]["vps"]["enabled"] = False
check("unless the proxy is off",
      "vps" not in {r["role"] for r in A.remote_roles(_rcfg)})

# Importing matters more than generating when a machine already trusts a key:
# a fresh one has to be authorised there first, and the only way in may be the
# key you are trying to replace. That is this household's case exactly -- the
# credential that reaches the VPS lives in a CI credential store.
print("\nand a key that already exists somewhere else")
_before = A.deploy_key()["public"]
_ok, _why = A.import_deploy_key("not a private key at all")
check("a paste that is not a key is refused",
      (_ok, _why) == (False, "key_invalid"), (_ok, _why))
check("and the working key is untouched", A.deploy_key()["public"] == _before)
_ok, _why = A.import_deploy_key("   ")
check("an empty paste is refused too", (_ok, _why) == (False, "key_empty"), (_ok, _why))

# A real one, made the way somebody else's tooling would have made it.
import subprocess as _sp
_other = os.path.join(tmp, "elsewhere")
_sp.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "from-ci",
         "-f", _other], capture_output=True)
_ok, _why = A.import_deploy_key(open(_other, encoding="utf-8").read())
check("a real key is taken", _ok and _why == "key_imported", (_ok, _why))
check("and it is the one that is now used",
      A.deploy_key()["public"].split()[1]
      == open(_other + ".pub", encoding="utf-8").read().split()[1],
      A.deploy_key()["public"][:40])
# Replacing is the point of the button and still locks this page out of every
# machine trusting the old one, so the old one stays.
check("the key it replaced is kept, not destroyed",
      any(n.startswith("id_ed25519.replaced-") for n in os.listdir(A.SSH_DIR)),
      sorted(os.listdir(A.SSH_DIR)))
check("the imported key is readable by nobody else",
      oct(os.stat(A.SSH_KEY).st_mode & 0o777) == "0o600",
      oct(os.stat(A.SSH_KEY).st_mode & 0o777))
# BatchMode everywhere means a locked key could never be unlocked.
_sp.run(["ssh-keygen", "-t", "ed25519", "-N", "hunter2", "-C", "locked",
         "-f", os.path.join(tmp, "locked")], capture_output=True)
_ok, _why = A.import_deploy_key(open(os.path.join(tmp, "locked"), encoding="utf-8").read())
check("a key with a passphrase is refused",
      (_ok, _why) == (False, "key_invalid"), (_ok, _why))

# Which account to log in as, beside the machine it applies to.
print("\nand which account to log in as")
_uc = {"hosts": {"compute": {"address": "192.168.1.11", "user": "old"}},
       "cloud": {"vps": {"enabled": True, "host": "vps.example", "user": ""}}}
check("a hosts: role takes it under hosts:",
      A.set_remote_user(_uc, "compute", "homestack")
      and _uc["hosts"]["compute"]["user"] == "homestack", _uc["hosts"])
check("and the vps under cloud.vps, where its address lives",
      A.set_remote_user(_uc, "vps", "deployer")
      and _uc["cloud"]["vps"]["user"] == "deployer", _uc["cloud"])
check("a role this stack does not have is refused",
      A.set_remote_user(_uc, "nowhere", "someone") is False)
check("and so is a blank name", A.set_remote_user(_uc, "compute", "  ") is False)

# Trusting a *changed* key is the one action here that can hand a session to
# somebody in the middle, so it is typed rather than clicked.
#
# Against a host that answers, which is the whole point: a first version of
# this posted at an unreachable address, so the route stopped at "no key to
# compare" and the confirmation gate never ran at all -- the check passed with
# the gate deleted.
_FP = "SHA256:pretend-this-came-off-the-machine"
_trusted = []
_real_state, _real_trust = A.host_key_state, A.trust_host
A.host_key_state = lambda addr: {
    "state": "changed", "presented": f"{addr} ssh-ed25519 AAAA",
    "fingerprint": f"256 {_FP} {addr} (ED25519)",
    "trusted_fingerprint": "256 SHA256:the-old-one x (ED25519)"}
A.trust_host = lambda addr: (_trusted.append(addr), (True, "trusted_ok"))[1]
try:
    post("/access", action="trust", address="vps.example", confirm="")
    check("trusting with nothing typed does nothing", not _trusted, _trusted)
    post("/access", action="trust", address="vps.example",
         confirm="SHA256:not-the-fingerprint")
    check("nor does a fingerprint that does not match", not _trusted, _trusted)
    post("/access", action="trust", address="vps.example", confirm=_FP)
    check("the fingerprint it presented does", _trusted == ["vps.example"], _trusted)
finally:
    A.host_key_state, A.trust_host = _real_state, _real_trust

# And an address that does not answer is refused before any of that, rather
# than trusted on an empty fingerprint matching an empty box.
post("/access", action="trust", address="nothing.invalid", confirm="")
check("an unreachable host is not trusted on a blank confirmation",
      not os.path.exists(A.KNOWN_HOSTS), "a host with no key was written anyway")

_page = client.get("/access").get_data(as_text=True)
check("the page renders", A.t("admin.access.heading") in _page)
check("and never prints the private key",
      open(A.SSH_KEY, encoding="utf-8").read().split("\n")[1][:40] not in _page,
      "the private key reached the browser")

# --- paths belong to a machine ----------------------------------------------
#
# `paths:` is one set of directories for the whole stack, which is right until
# a role is a machine with different disks. Moving `state` onto a second disk
# on the hub told every other role to use that path too -- and the VPS, which
# has no such disk and runs one proxy, failed its deploy on `mkdir -p`. It was
# correct to fail; it was wrong to have been asked.
print("\na machine that keeps things elsewhere")
_cfg = A.load_config()
_cfg.setdefault("paths", {}).update(
    {k: f"/var/lib/home-stack/{k}" for k in A.PATH_KINDS})
_cfg["paths"].pop("by_host", None)
A.save_config(_cfg)

# Every machine's boxes post, whichever one is on screen -- the combo only
# decides what is visible, so switching it cannot discard an edit made on
# another. Which means the ordinary save is every machine agreeing.
_same = {f"path:{k}": f"/var/lib/home-stack/{k}" for k in A.PATH_KINDS}
_same.update({f"path:{r}:{k}": f"/var/lib/home-stack/{k}"
              for r in A.PATH_ROLES for k in A.PATH_KINDS})
post("/site", **_same)
check("every machine agreeing stores no exception",
      "by_host" not in (A.load_config().get("paths") or {}),
      A.load_config().get("paths"))

# One machine differing is one entry, not four copies of five paths.
_one = dict(_same)
_one["path:vps:state"] = "/home/deployer/home-stack/state"
post("/site", **_one)
_paths = A.load_config().get("paths") or {}
check("a machine with its own disk is recorded",
      (_paths.get("by_host") or {}).get("vps", {}).get("state")
      == "/home/deployer/home-stack/state", _paths.get("by_host"))
check("and only that machine is",
      sorted(_paths.get("by_host") or {}) == ["vps"], _paths.get("by_host"))

# Moving the shared path moves everyone who was sharing it, and leaves the one
# that is not alone -- which is the whole point of the shape.
_moved = {**_one, "path:state": "/mnt/data/state"}
_moved.update({f"path:{r}:state": "/mnt/data/state"
               for r in A.PATH_ROLES if r != "vps"})
post("/site", **_moved)
_paths = A.load_config().get("paths") or {}
check("the shared path moves", _paths.get("state") == "/mnt/data/state",
      _paths.get("state"))
check("and the machine that differs keeps its own",
      (_paths.get("by_host") or {}).get("vps", {}).get("state")
      == "/home/deployer/home-stack/state", _paths.get("by_host"))

# Typing the shared value back is how a machine goes back to sharing.
_back = {**_moved, "path:vps:state": "/mnt/data/state"}
post("/site", **_back)
check("matching the shared value puts it back to sharing",
      "by_host" not in (A.load_config().get("paths") or {}),
      A.load_config().get("paths"))

# A path is read on another machine, where this page's working directory means
# nothing. Refused on both halves: the shared set is the one a single-PC
# household ever edits, so a rule that only guarded the exceptions would guard
# the boxes almost nobody touches.
post("/site", **{**_back, "path:compute:media": "relative/path"})
check("a relative path is refused",
      "compute" not in ((A.load_config().get("paths") or {}).get("by_host") or {}),
      A.load_config().get("paths", {}).get("by_host"))
# The save redirects; the complaint is on the page that comes back.
check("and said so, not silently dropped",
      A.t("admin.site.paths_absolute")[:24]
      in client.get("/site", headers=H).get_data(as_text=True),
      "the box keeps the typed text and the old value is what saved")
post("/site", **{**_back, "path:state": "relative/path"})
check("the shared set is guarded the same way",
      (A.load_config().get("paths") or {}).get("state") == "/mnt/data/state",
      A.load_config().get("paths", {}).get("state"))

# The deployer is what acts on it, imported rather than reimplemented here.
_D = A._deployer()
if _D is not None:
    _rc = {"paths": {**{k: f"/var/lib/home-stack/{k}" for k in A.PATH_KINDS},
                     "by_host": {"vps": {"state": "/srv/state"}}}}
    check("  the deployer resolves a machine's own paths",
          _D.paths_for_role(_rc, "vps")["paths"]["state"] == "/srv/state")
    check("  and leaves one without any alone",
          _D.paths_for_role(_rc, "hub")["paths"]["state"]
          == "/var/lib/home-stack/state")
    check("  and `by_host` is not mistaken for one of the five",
          "by_host" not in _D.base_paths(_rc), sorted(_D.base_paths(_rc)))

_page = client.get("/site").get_data(as_text=True)
check("the page offers a machine to pick",
      'id="paths-machine"' in _page and A.t("admin.site.paths_all") in _page)
check("with every machine's fields present, so switching loses nothing",
      all(f'name="path:{r}:state"' in _page for r in A.PATH_ROLES),
      "a combo that navigates would discard unsaved edits")
check("and only one group shown at a time",
      _page.count('class="paths-group"') == len(A.PATH_ROLES) + 1
      and _page.count("hidden>") >= len(A.PATH_ROLES),
      "every group visible at once is the grid this replaced")

# --- the names table only offers names that answer -------------------------
#
# Nothing in this stack resolves a `dns:` name; they are the names people type,
# and something on the network has to answer them. So a row for a service the
# household does not deploy is a line somebody copies into their Pi-hole for an
# address where nothing is listening, and then spends an evening on.
#
# `ntfy` was the case that prompted it, and is no longer an example this can
# use: it is not a `dns:` name at all any more -- where notifications go is
# `cloud.notifications`, and the one thing that needed an address takes the
# hub's directly. The cameras make the same point and can still be switched
# off.
print("\nthe names table says which names nothing answers")
_cfg = A.load_config()
_cfg.setdefault("services", {}).setdefault("home-cameras", {})["enabled"] = True
_cfg["services"].setdefault("home-paperless", {})["enabled"] = False
_t = A.dns_targets(_cfg)
check("a name for a service that is on is live",
      _t["cameras"]["live"] is True, _t["cameras"])
check("and one for a service that is off is not",
      _t["paperless"]["live"] is False, _t["paperless"])
check("the row names the service, so the fix is findable",
      _t["paperless"]["service"] == "home-paperless", _t["paperless"])
check("the portal is always live: it is what serves this page",
      _t["portal"]["live"] is True, _t["portal"])

_cfg["services"]["home-cameras"]["enabled"] = False
_t = A.dns_targets(_cfg)
check("switching the cameras off marks their name dead",
      _t["cameras"]["live"] is False, _t["cameras"])
check("and ntfy is not in this table at all any more",
      "ntfy" not in _t, sorted(_t))

# A name this table has never heard of is a household's own. Calling it dead
# would be a guess, and a wrong one is worse here than saying nothing.
_cfg.setdefault("dns", {})["nas"] = "nas.home"
check("a name nothing here knows about is left alone",
      A.dns_targets(_cfg)["nas"]["live"] is True)

# And it has to reach the page, which is where somebody reads it. Saved, not
# only computed: the table renders from the stored config, and a check against
# an in-memory copy would pass while the page showed every row alive.
_saved = A.load_config()
_saved.setdefault("services", {}).setdefault("home-cameras", {})["enabled"] = True
A.save_config(_saved)
_before = client.get("/site").get_data(as_text=True)
_saved["services"]["home-cameras"]["enabled"] = False
A.save_config(_saved)
_after = client.get("/site").get_data(as_text=True)
_note = A.t("admin.site.dns_service_off", service="home-cameras")
check("the page is quiet while every service is on", _note not in _before,
      "a note about a service that is running is noise")
check("and says so beside the name once one is off", _note in _after,
      "the dns_service_off help text is not rendering")
_saved["services"]["ntfy"]["enabled"] = True
A.save_config(_saved)

# --- a household's own services --------------------------------------------
#
# The form collected `health_url`, for a status dot the portal stopped painting
# when the dashboard came back into home-core. A field somebody fills in that
# reaches nothing is worse than one that is not offered.
print("\nadding a service this stack does not deploy")
_before = list(A.load_config().get("custom_services") or [])
post("/services", action="add_custom", name="NAS",
     url="http://192.168.1.20:5000", description="The big disk",
     icon="\N{FILE CABINET}", lan_only="on")
_added = [c for c in (A.load_config().get("custom_services") or [])
          if c.get("name") == "NAS"]
check("it is saved", len(_added) == 1, A.load_config().get("custom_services"))
if _added:
    _nas = _added[0]
    check("with everything the dashboard draws",
          _nas.get("description") == "The big disk"
          and _nas.get("icon") == "\N{FILE CABINET}", _nas)
    # The portal cannot know this about an address somebody typed, and drawing
    # a link to a private address for a request from outside is a dead square.
    check("and whether it is reachable from outside the house",
          _nas.get("lan_only") is True, _nas)
    check("and nothing the page no longer reads",
          "health_url" not in _nas, _nas)

post("/services", action="add_custom", name="Anywhere",
     url="https://example.invalid/")
_any = [c for c in (A.load_config().get("custom_services") or [])
        if c.get("name") == "Anywhere"]
check("an entry with the box unticked is not house-only",
      _any and not _any[0].get("lan_only"), _any)

# Only the portal reads the dashboard file, so only the portal redeploys. This
# said `homepage` until that service stopped existing, which is a Deploy button
# aimed at nothing.
check("adding one asks for the portal to be deployed",
      "home-core" in (A.load_pending() or []), A.load_pending())

post("/services", action="remove_custom", name="NAS")
check("and it can be taken off again",
      not [c for c in (A.load_config().get("custom_services") or [])
           if c.get("name") == "NAS"], A.load_config().get("custom_services"))
_cfg = A.load_config(); _cfg["custom_services"] = _before; A.save_config(_cfg)

# --- the backups page ------------------------------------------------------
#
# It was read-only: what to point your own tool at, and nothing else. What a
# household actually needs from it is the two things it could not do -- turn on
# encryption, and put one service back when something came back wrong.
print("\nthe backups page can encrypt, and can put one service back")
_cfg = A.load_config()
_cfg.setdefault("backups", {})["encrypt"] = False
A.save_config(_cfg)
A.set_secret("BACKUP_ENCRYPTION_KEY", "")

# The key before the switch. Turning encryption on without one makes every run
# fail at the last step, after it has copied everything, with a message about
# openssl.
post("/backups", action="encrypt", encrypt="on")
check("encryption cannot be switched on without a key",
      not (A.load_config().get("backups") or {}).get("encrypt"),
      A.load_config().get("backups"))

post("/backups", action="generate_key")
_key = A.get_secret("BACKUP_ENCRYPTION_KEY")
check("a key can be generated here", len(_key) > 20, _key[:8])

# Rotating it silently is how every archive already written becomes noise.
post("/backups", action="generate_key")
check("and generating a second one is refused, not silently done",
      A.get_secret("BACKUP_ENCRYPTION_KEY") == _key,
      "an existing key was replaced; every archive under the old one is now unreadable")

post("/backups", action="encrypt", encrypt="on")
check("with a key, encryption goes on",
      (A.load_config().get("backups") or {}).get("encrypt") is True,
      A.load_config().get("backups"))
post("/backups", action="encrypt")
check("and off again", (A.load_config().get("backups") or {}).get("encrypt") is False)

# The restore guard. This overwrites live state, so a slip must not be enough.
_started = []
_real_start = A._start_backup
A._start_backup = lambda argv, targets: (_started.append((argv, targets)), True)[1]
try:
    post("/backups", action="restore", archive="2026-01-01T00-00-00",
         service="home-paperless", confirm="")
    check("a restore with nothing typed does not start", not _started, _started)
    post("/backups", action="restore", archive="2026-01-01T00-00-00",
         service="home-paperless", confirm="home-core")
    check("nor one where the typed name is a different service",
          not _started, _started)
    post("/backups", action="restore", archive="2026-01-01T00-00-00",
         service="home-paperless", confirm="home-paperless")
    check("the matching name starts it", len(_started) == 1, _started)
    if _started:
        _argv = _started[0][0]
        check("narrowed to that service and nothing else",
              "--only" in _argv and _argv[_argv.index("--only") + 1] == "home-paperless",
              _argv)
        check("and it is a real restore, not a dry run",
              "--confirm" in _argv and "--dry-run" not in _argv, _argv)
        check("of the archive that was chosen",
              "2026-01-01T00-00-00" in _argv, _argv)
    check("and the service is noted as needing a deploy",
          "home-paperless" in (A.load_pending() or []), A.load_pending())
finally:
    A._start_backup = _real_start

# The page itself has to render both halves.
_page = client.get("/backups").get_data(as_text=True)
check("the encryption card is on the page",
      A.t("admin.backups.encryption") in _page)
check("and the key is never printed on it", _key not in _page,
      "the backup key must not reach the browser")

# --- the address of an OpenAI-compatible server ------------------------------
# Typed by hand, and the stack calls whatever it is given. A value that is not
# a callable http(s) URL fails later, from inside a model request, as "the
# assistant could not answer" -- so it is refused here where somebody is
# standing.
print("\nan OpenAI-compatible URL is checked before it is stored")


def _compat(url):
    # `scope:sources` is what the sources card carries, and only a POST that
    # carries it may speak for these fields: /models is three forms to one
    # endpoint, and an unchecked box sends nothing, so without the marker
    # saving the personas or the image card read as "compat off, URL blank".
    post("/models", **{"scope:sources": "1", "compat": "on", "compat_url": url})
    return ((A.load_config().get("cloud") or {})
            .get("openai_compatible") or {}).get("url")


for _bad in ("not a url", "ftp://host/v1", "http://", "javascript:alert(1)"):
    check(f"refused: {_bad!r}", _compat(_bad) != _bad, _compat(_bad))
for _good in ("http://192.168.1.20:8000/v1", "https://api.example.com/v1"):
    check(f"accepted: {_good!r}", _compat(_good) == _good, _compat(_good))
check("and blank is a real answer, not a refusal", _compat("") == "")

# The refusal has to say something. A key missing from every catalogue renders
# as its own name, which is how a household learns the phrase
# `admin.models.invalid_url`.
check("the message it flashes is a translation that exists",
      "admin.models.invalid_url" in _EN_KEYS)

# --- /models is three forms to one endpoint --------------------------------
# The same shape the services page had, and it arrived the same way: a commit
# added a third card without noticing that the handler wrote every field on
# every POST. An unchecked box sends nothing, so saving the personas card read
# as "no image models, local Ollama off, compatible URL blank" and silently
# undid all three. `scope:` markers say which card spoke; this checks the
# markers are actually load-bearing rather than decorative.
print("\nsaving one card on /models leaves the other cards alone")

_m = A.load_config()
_m.setdefault("assistant", {}).setdefault("models", {}).update(
    {"image_high": "together:x/FLUX.1.1-pro", "image_normal": "together:x/FLUX.1-schnell"})
_cloud = _m.setdefault("cloud", {})
_cloud.setdefault("ollama", {}).setdefault("local", {})["enabled"] = True
_cloud.setdefault("openai_compatible", {}).update(
    {"enabled": True, "url": "http://192.168.1.20:8000/v1"})
A.save_config(_m)

# The personas card: model choices only, no scope marker for images or sources.
post("/models", **{"model:everyday": "gpt-5.6-luna"})
_after = A.load_config()
_img = (_after.get("assistant") or {}).get("models") or {}
_oc = (_after.get("cloud") or {}).get("openai_compatible") or {}
_ol = (((_after.get("cloud") or {}).get("ollama") or {}).get("local") or {})
check("the image slots survive a personas save",
      _img.get("image_high") and _img.get("image_normal"),
      {k: v for k, v in _img.items() if k.startswith("image")})
check("  and local Ollama is still on", _ol.get("enabled") is True, _ol)
check("  and the compatible URL is not blanked",
      _oc.get("url") == "http://192.168.1.20:8000/v1", _oc)

# And the card that DOES carry the marker still writes: a scope that stops
# every write is a page where nothing saves, which is the opposite failure.
post("/models", **{"scope:images": "1", "model:image_high": "", "model:image_normal": ""})
_img = ((A.load_config().get("assistant") or {}).get("models") or {})
check("but the images card can still clear its own slots",
      not _img.get("image_high") and not _img.get("image_normal"),
      {k: v for k, v in _img.items() if k.startswith("image")})

# --- the shared Parents topic -------------------------------------------------
#
# A member's own topic is for *sending*; `Parents` is a topic several people
# *receive*. Subscription lives in the chat proxy's ntfy_config.json, and
# nothing in this stack maintained it -- which is exactly how this household
# lost notifications: the VPS proxy had three of four members registered by
# hand, the local proxy had none, and the member who was missing was told 404
# by /api/ntfy-config and subscribed to nothing at all.
print("\nthe Parents topic is per member, and Family is always last")
_cfg = {"members": [
    {"id": "user1", "display_name": "Tomi", "ntfy_topic": "Tomi", "parents": True},
    {"id": "user3", "display_name": "Juana", "ntfy_topic": "Juana"},
]}
_tomi = A.member_ntfy_topics(_cfg, _cfg["members"][0])
_juana = A.member_ntfy_topics(_cfg, _cfg["members"][1])
check("  an adult gets their own topic, Parents, then Family",
      _tomi == ["Tomi", "Parents", "Family"], _tomi)
check("  a child gets their own and Family only",
      _juana == ["Juana", "Family"], _juana)
check("  Parents is never given to somebody without the flag",
      "Parents" not in _juana, _juana)

# `ntfy_topic` falls back to the display name, and a household could name
# somebody's topic Family. Listing it twice subscribes them twice.
_dupe = A.member_ntfy_topics({}, {"id": "userX", "display_name": "Family",
                                  "parents": True})
check("  a topic is never repeated", _dupe == ["Family", "Parents"], _dupe)

# The one property that matters when this writes: the token belongs to that
# person's ntfy account, this page cannot mint one, and a rewrite that dropped
# it would unsubscribe them as completely as having no entry at all.
print("\nrewriting the topics keeps the token")
_store = os.path.join(tempfile.mkdtemp(prefix="ntfycfg-"), "ntfy_config.json")
with open(_store, "w", encoding="utf-8") as fh:
    json.dump({"900000111": {"topics": ["Tomi", "Family"], "token": "tk_keepme"}}, fh)
_env = dict(os.environ, NTFY_CONFIG_FILE=_store)
_r = __import__("subprocess").run(
    [sys.executable, "-", "900000111", "Tomi,Parents,Family"],
    input=A._NTFY_SET, capture_output=True, text=True, env=_env)
with open(_store, encoding="utf-8") as fh:
    _after = json.load(fh)["900000111"]
check("  the new topics are written", _after["topics"] == ["Tomi", "Parents", "Family"],
      _after)
check("  and the token is still there", _after["token"] == "tk_keepme", _after)

# A login that was never registered gets an entry with no token rather than an
# error: the topics are still right, and the token arrives when somebody mints
# one. Silently doing nothing would leave the same 404 that started all this.
_r2 = __import__("subprocess").run(
    [sys.executable, "-", "900000222", "Mora,Family"],
    input=A._NTFY_SET, capture_output=True, text=True, env=_env)
with open(_store, encoding="utf-8") as fh:
    _new = json.load(fh).get("900000222") or {}
check("  a member with no entry yet gets one", _new.get("topics") == ["Mora", "Family"],
      _new)

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
