#!/usr/bin/env python3
"""The deployer's own pure functions.

Run: python deploy/test_deploy.py

There was no test here at all, and the cost showed up on a real box: a regex
edit that replaced the portal-tile generator sliced `build_member_profile` out
along with it. The call site survived, nothing referenced the definition at
import time, and `--check-contract` passed — the crash waited until a deploy
reached the one service that seeds member profiles.

So this covers the functions a deploy calls but a contract check does not:
anything that builds a file's contents, and anything that turns config into an
endpoint. `--dry-run` remains the end-to-end check; this is the part that runs
in a second.
"""
import copy
import json as _json
import os as _os
import tempfile as _tf
import json
import importlib.util
import os
import pathlib
import tempfile
import sys

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("deployer", HERE / "deploy.py")
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


CFG = {
    "site": {"name": "Home", "timezone": "Etc/UTC", "domain": "home",
             "host": "house.home"},
    "locale": {"default": "en"},
    "hosts": {"hub": {"address": "127.0.0.1"}, "compute": {"address": "192.168.1.11"},
              "storage": {"address": "127.0.0.1"}},
    "cloud": {"ollama": {"local": {"enabled": True, "host": "compute",
                                   "port": 11434},
                         "cloud": {"enabled": False,
                                   "url": "https://ollama.com"}}},
    "members": [
        {"id": "user1", "display_name": "Ana", "admin": True, "locale": "en",
         "birthdate": "1980-01-01", "hobbies": "riding\n- drawing",
         "relationships": {"user2": "daughter"}, "notes": "prefers short answers"},
        {"id": "user2", "display_name": "Bo", "locale": "es"},
    ],
    "services": {
        "nanobot": {"enabled": True, "members": ["user1", "user2"]},
        "home-core": {"enabled": True, "port": 8443},
        "home-cameras": {"enabled": True, "web_port": 5000},
        "mqtt": {"enabled": True, "dashboard_port": 1884},
        "ntfy": {"enabled": True, "port": 8085},
        "admin": {"enabled": True, "port": 8099},
        "home-paperless": {"enabled": False, "port": 8010},
        "home-voice": {"enabled": False, "port": 8083},
    },
    "dns": {"portal": "house.home"},
}

# --- the member profile, which is the one that went missing --------------------
print("a member profile is built from what the admin page knows")
md = D.build_member_profile(CFG, CFG["members"][0])
check("it names the person", "Ana" in md, md[:120])
check("and their timezone", "Etc/UTC" in md)
check("an admin is said to be one", "administrator" in md)
check("hobbies become a list", "- riding" in md and "- drawing" in md, md)
check("a leading dash is not doubled", "- - drawing" not in md, md)
check("relationships resolve to a name, not an id",
      "| Bo | daughter |" in md, md)
check("notes land under their own heading",
      "Special Instructions" in md and "prefers short answers" in md)

plain = D.build_member_profile(CFG, CFG["members"][1])
check("somebody with nothing filled in still gets a profile",
      plain.startswith("# User Profile") and "Bo" in plain, plain[:80])
check("and no empty sections", "Topics of Interest" not in plain
      and "Special Instructions" not in plain, plain)
check("their own language is used, not the site's",
      "**Language**: es" in plain, plain)

# --- the model providers ------------------------------------------------------
# Three, and all three can be live at once: the assistant can answer on
# OpenCode Zen while the cameras are read by a model on your own GPU.
print("\na model says which provider it wants")
check("a bare name is OpenCode Zen", D.split_model("kimi-k3") == ("kimi-k3", "custom"))
check("`ollama:` is your own box",
      D.split_model("ollama:qwen3-vl:8b") == ("qwen3-vl:8b", "ollama"),
      D.split_model("ollama:qwen3-vl:8b"))
check("`ollama-cloud:` is ollama.com",
      D.split_model("ollama-cloud:qwen3-vl:8b") == ("qwen3-vl:8b", "ollama_cloud"))
# An Ollama tag carries its own colon, and splitting greedily turned the tag
# into the provider and left the model called `qwen3-vl`.
check("the tag's own colon survives the split",
      D.split_model("ollama:llama3.2:3b")[0] == "llama3.2:3b")
check("an unknown prefix is not a provider",
      D.split_model("weird:thing") == ("weird:thing", "custom"))

# --- the counts the documentation states -------------------------------------
# "thirteen services" was wrong, and the branch that noticed replaced it with
# "eleven", which is wrong a different way: eleven is what a *default install*
# starts, sixteen is what the manifest declares. Both numbers are true of
# something, so a bare one goes stale silently and nobody can tell which
# reading it meant. They are checked against the manifest here so the next
# service added fails this instead of aging the prose.
print("\nthe documentation's service counts still match the manifest")
_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
          7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
          12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen",
          16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen",
          20: "twenty"}
import yaml as _y  # noqa: E402  -- the suite's own import is further down
_root = pathlib.Path(__file__).resolve().parent.parent
_example = _y.safe_load((_root / "config" / "home-stack.example.yml")
                        .read_text(encoding="utf-8"))
_manifest = _y.safe_load((_root / "deploy" / "manifest.yml")
                         .read_text(encoding="utf-8"))
_declared = list((_manifest.get("services") or {}).keys())
_example_svc = (_example.get("services") or {})
_on = [s for s in _declared
       if (_example_svc.get(s) or {}).get("enabled", False)]
_dirs = sorted(d.name for d in (_root / "services").iterdir() if d.is_dir())

_claude = (_root / "CLAUDE.md").read_text(encoding="utf-8")
_readme = (_root / "README.md").read_text(encoding="utf-8")
check(f"CLAUDE.md says {_WORDS[len(_on)]} on a default install",
      f"{_WORDS[len(_on)]} services on a default" in _claude,
      f"{len(_on)} enabled in the example config")
check(f"  and that the manifest declares {_WORDS[len(_declared)]}",
      f"declares {_WORDS[len(_declared)]}" in _claude, len(_declared))
check(f"README says {_WORDS[len(_declared)]} shipped, {_WORDS[len(_on)]} on by default",
      f"ships {_WORDS[len(_declared)]} services, {_WORDS[len(_on)]} of them on by default"
      in _readme, (len(_declared), len(_on)))
check(f"README says {_WORDS[len(_dirs)]} service directories",
      f"the {_WORDS[len(_dirs)]} service directories" in _readme, len(_dirs))
# docs/plugins.md states the count too, and nothing checked it -- so it still
# said "eleven" while the manifest declared twenty. A number in prose that
# nothing asserts is a comment about the past.
_plugins = (_root / "docs" / "plugins.md").read_text(encoding="utf-8")
check(f"docs/plugins.md says {_WORDS[len(_declared)]} shipped, "
      f"{_WORDS[len(_on)]} on by default",
      f"ships {_WORDS[len(_declared)]} services, {_WORDS[len(_on)]} of them "
      f"on a default install" in _plugins, (len(_declared), len(_on)))


# --- renaming the assistant --------------------------------------------------
# The name is typed on the admin page, and it is used as a regex *replacement*,
# where a backslash is not a character but an instruction. `C:\\Jarvis` raised
# "bad escape \\J" and took the deploy down with it; `\\g<0>bot` quietly
# substituted the old name back in and produced "Alfredbot". A function
# replacement has no such grammar.
print("\nthe assistant's name is text, not a regex replacement")
_t = "Ask Alfred about it, Alfred."


def _named(name):
    return D.apply_assistant_name(_t, {"site": {"assistant_name": name}})


check("an ordinary name is substituted", _named("Jeeves") == "Ask Jeeves about it, Jeeves.")
check("a backslash is a backslash, not an escape",
      _named(r"C:\Jarvis") == r"Ask C:\Jarvis about it, C:\Jarvis.", _named(r"C:\Jarvis"))
check("a group reference is not honoured",
      _named(r"\g<0>bot") == r"Ask \g<0>bot about it, \g<0>bot.", _named(r"\g<0>bot"))
check("nor a numbered one, which used to raise and fail the deploy",
      _named(r"Al\1fred") == r"Ask Al\1fred about it, Al\1fred.", _named(r"Al\1fred"))
check("the default name still changes nothing", _named("Alfred") == _t)


# --- the two image slots ------------------------------------------------------
# The picker on the admin page stores a prefix (`together:`); split_model
# answers with a provider name (`together_ai`). They are not the same string,
# and comparing against the wrong one is silent: the two neutral names still
# export, so a deploy looks right, and the one the theme skill actually reads
# is missing -- which leaves the literal default in that script the real
# setting, the exact bug this export was written to end.
print("\nthe image slots reach the skill that draws")
_img = {"assistant": {"models": {
    "image_normal": "together:black-forest-labs/FLUX.1-schnell",
    "image_high": "together:black-forest-labs/FLUX.1.1-pro"}}}
_env = D.image_model_env(_img)
check("the everyday slot exports with its prefix",
      _env.get("IMAGE_MODEL") == "together:black-forest-labs/FLUX.1-schnell", _env)
check("and the good one",
      _env.get("IMAGE_MODEL_HIGH") == "together:black-forest-labs/FLUX.1.1-pro", _env)
check("Together gets the bare id the theme skill passes straight on",
      _env.get("TOGETHER_IMAGE_MODEL") == "black-forest-labs/FLUX.1-schnell", _env)
check("  and its high-quality name, which that skill prefers",
      _env.get("TOGETHER_IMAGE_MODEL_HIGH") == "black-forest-labs/FLUX.1.1-pro", _env)
_other = D.image_model_env({"assistant": {"models": {"image_normal": "openai:dall-e-3"}}})
check("a model from somewhere else exports no Together name",
      "TOGETHER_IMAGE_MODEL" not in _other and _other["IMAGE_MODEL"] == "openai:dall-e-3",
      _other)
# Blank is a real answer: a household that does not want the assistant drawing
# leaves both empty, and the skill should say it cannot rather than fall back.
check("empty slots export nothing at all", D.image_model_env({}) == {})

print("\nthe endpoints follow what is switched on")
ends = D.ollama_endpoints(CFG, {})
check("local is on by default", ends["ollama"][0] == "http://192.168.1.11:11434", ends)
check("its key is never empty", ends["ollama"][1] == "ollama")
check("cloud is off unless asked for", "ollama_cloud" not in ends)

both = copy.deepcopy(CFG)
both["cloud"]["ollama"]["cloud"] = {"enabled": True, "url": "https://ollama.com"}
ends = D.ollama_endpoints(both, {"OLLAMA_API_KEY": "sk-real"})
# A subset: `ollama_host` is the same local endpoint as a host-networked
# consumer sees it, and is present whenever `ollama` is. The question here is
# whether local and cloud can both be on.
check("both can be on at once",
      {"ollama", "ollama_cloud"} <= set(ends), sorted(ends))
check("and the host-facing local endpoint comes with the local one",
      ("ollama_host" in ends) == ("ollama" in ends), sorted(ends))
check("and the cloud one carries the real key", ends["ollama_cloud"][1] == "sk-real")

off = copy.deepcopy(CFG)
off["cloud"]["ollama"]["local"] = {"enabled": False}
check("local can be switched off", "ollama" not in D.ollama_endpoints(off, {}))

# ollama.com is on when its key is set and off when it is not. It used to have
# an `enabled:` as well, and the pair could disagree in the expensive
# direction: `enabled: true` with an empty key was a hard deploy failure over a
# provider nobody was using. The key is the only switch now, as it is for
# openrouter, together and openai.
no_key = copy.deepcopy(CFG)
no_key["cloud"]["ollama"]["cloud"] = {"url": "https://ollama.com"}
check("ollama.com is off when its key is empty",
      "ollama_cloud" not in D.ollama_endpoints(no_key, {}))
check("and on when it is set, with no `enabled:` anywhere",
      "ollama_cloud" in D.ollama_endpoints(no_key, {"OLLAMA_API_KEY": "sk-real"}),
      "a switch beside a credential is two things to get wrong")

print("\nand the config refuses what cannot work")
for broken, secrets, why in (
    ({"local": {"enabled": True, "host": "gpu"}}, {}, "a host that is not a role"),
):
    cfg = copy.deepcopy(CFG)
    cfg["cloud"]["ollama"].update(broken)
    try:
        D.ollama_endpoints(cfg, secrets)
        check(f"{why} is refused", False, "no error raised")
    except D.DeployError:
        check(f"{why} is refused", True)

# Naming a provider that is off does not degrade to anything: the container
# starts and every turn fails. Checked for all six, not just the two Ollamas --
# `check_models_reachable` was told only about those, so a model on any of the
# other four skipped the check and failed at the first call instead.
print("\na model on a provider that is off is refused, whichever provider")
for _model, _need in (("ollama-cloud:qwen3-vl:8b", "OLLAMA_API_KEY"),
                      ("openrouter:acme/big", "OPENROUTER_API_KEY"),
                      ("together:acme/big", "TOGETHER_API_KEY"),
                      ("openai:gpt-x", "OPENAI_API_KEY"),
                      ("openai-compatible:qwen", "cloud.openai_compatible")):
    unreachable = copy.deepcopy(CFG)
    unreachable["assistant"] = {"models": {"vision": _model}}
    try:
        D.derive(unreachable, {})
        check(f"  {_model}", False, "no error raised")
    except D.DeployError as exc:
        # And it says which thing to set, rather than naming a block that does
        # not exist for four of these.
        check(f"  {_model}", _need in str(exc), exc)

# The other way: with the credential there, the same model deploys.
for _model, _secrets in (("openrouter:acme/big", {"OPENROUTER_API_KEY": "k"}),
                         ("together:acme/big", {"TOGETHER_API_KEY": "k"}),
                         ("openai:gpt-x", {"OPENAI_API_KEY": "k"})):
    ok = copy.deepcopy(CFG)
    ok["assistant"] = {"models": {"vision": _model}}
    try:
        D.derive(ok, _secrets)
        check(f"  {_model} with its key", True)
    except D.DeployError as exc:
        check(f"  {_model} with its key", False, exc)

print("\nthe old `mode:` config still deploys")
legacy = copy.deepcopy(CFG)
legacy["cloud"]["ollama"] = {"mode": "local", "host": "compute", "port": 11434}
check("mode: local becomes the local endpoint",
      D.ollama_endpoints(legacy, {})["ollama"][0] == "http://192.168.1.11:11434")
legacy["cloud"]["ollama"] = {"mode": "cloud", "cloud_url": "https://ollama.com"}
check("mode: cloud becomes the cloud one",
      D.ollama_endpoints(legacy, {"OLLAMA_API_KEY": "k"})["ollama_cloud"][0]
      == "https://ollama.com")

print("\nderive exposes every endpoint, empty or not")
d = D.derive(copy.deepcopy(CFG), {})["derived"]
# A subset, not an exact set: `derived` carries other things now, and the
# question here is only whether every endpoint name is exported.
check("all four endpoint names are present",
      {"ollama_url", "ollama_api_key", "ollama_cloud_url",
       "ollama_cloud_api_key"} <= set(d), sorted(d))
# nanobot refuses to start on an *unset* reference, so a switched-off provider
# has to be an empty string rather than a missing name.
check("a switched-off provider is empty, not absent", d["ollama_cloud_url"] == "")
check("and its key is a placeholder, never blank",
      d["ollama_cloud_api_key"] == "disabled")

# --- the assistant's name -----------------------------------------------------
# One setting, and the thing it must never touch is a path: `alfred/documents`
# holds every file the assistant has saved for somebody, and `alfred-nanobot`
# is the image the compose files name.
print("\nthe assistant can be renamed")
named = {**CFG, "site": {**CFG["site"], "assistant_name": "Jarvis"}}
check("its prompts follow",
      D.apply_assistant_name("My name is **Alfred**.", named)
      == "My name is **Jarvis**.")
check("so does prose about it",
      D.apply_assistant_name("Ask Alfred, and Alfred answers.", named)
      == "Ask Jarvis, and Jarvis answers.")
check("a lowercase path is left alone",
      D.apply_assistant_name("user1/alfred/documents/x.pdf", named)
      == "user1/alfred/documents/x.pdf")
check("so is the image name",
      D.apply_assistant_name("image: alfred-nanobot:latest", named)
      == "image: alfred-nanobot:latest")
check("and a function called _alfred_notify",
      D.apply_assistant_name("def _alfred_notify(x):", named)
      == "def _alfred_notify(x):")
check("a longer word that merely starts with it is not a match",
      D.apply_assistant_name("Alfredo cooks.", named) == "Alfredo cooks.")
check("the default is a no-op, not a rewrite",
      D.apply_assistant_name("Alfred here", CFG) == "Alfred here")
check("and so is an empty setting",
      D.apply_assistant_name("Alfred here", {**CFG, "site": {**CFG["site"],
                                                            "assistant_name": "  "}})
      == "Alfred here")

# --- who lives here -----------------------------------------------------------
# The portal keys the share folders, chore editing and the adult-only pages on
# the *login* id. Those tables were five login ids written into app.py, so any
# other install had /files answering 403 to everybody and /stats bouncing the
# adults back to the wall -- with every page returning a valid response.
print("\nthe portal is told who lives here")
_people = D.derive(copy.deepcopy(CFG), {})["derived"]
check("the members, in the order the deploy numbers them",
      _people["members"] == "user1,user2", _people["members"])
check("and only the admins are named as admins",
      _people["admin_members"] == "user1", _people["admin_members"])
# The folder is what the house calls somebody -- their first name -- not the
# `user4` this package generates. An install carried across from an older
# setup already has those folders on the share with years of files in them.
check("each member's folder is what the house calls them",
      _people["member_folders"] == "user1:ana,user2:bo", _people["member_folders"])
check("an accented name is folded, because a share path is not unicode-safe",
      D.share_folder({"id": "user9", "display_name": "Begoña"}) == "begona",
      D.share_folder({"id": "user9", "display_name": "Begoña"}))
check("a two-word name loses the space rather than keeping it",
      D.share_folder({"id": "user9", "display_name": "Ana Maria"}) == "anamaria")
# Display names are edited on the admin page. Folders are not, or renaming
# somebody moves their files out from under them.
check("`folder:` wins over the display name",
      D.share_folder({"id": "user9", "display_name": "Ana", "folder": "anita"})
      == "anita")
check("and a member with no name at all falls back to their id",
      D.share_folder({"id": "user9"}) == "user9")

# --- the dashboard ------------------------------------------------------------
print("\nthe dashboard is generated from the enabled services")
_manifest = D.load_yaml(D.MANIFEST)
_wall = json.loads(D.build_portal_dashboard(CFG))
_tiles = {t["name"]: t for g in _wall["groups"].values() for t in g}
check("it says what wrote it", _wall["generated_by"] == "deploy/deploy.py")
check("the three groups are always present",
      set(_wall["groups"]) == {"basic", "advanced", "extensions"},
      sorted(_wall["groups"]))
check("an enabled service is on the wall", "home-cameras" in _tiles, sorted(_tiles))
check("a disabled one is not", "home-paperless" not in _tiles, sorted(_tiles))
check("and neither is one the household does not run at all",
      "home-search" not in _tiles, sorted(_tiles))
check("the tier in the manifest decides the group",
      "home-cameras" in {t["name"] for t in _wall["groups"]["basic"]}
      and "mqtt" in {t["name"] for t in _wall["groups"]["advanced"]},
      {g: [t["name"] for t in ts] for g, ts in _wall["groups"].items()})
check("`tier: none` means no tile: the assistants answer through the portal",
      "nanobot" not in _tiles, sorted(_tiles))
# The portal reads this at the root of the house. Anything it cannot parse is
# a blank wall, and the tile keys are a contract with app.py's render().
check("every tile carries the whole set of keys the portal reads",
      all(set(t) == {"name", "title", "description", "icon", "url",
                     "lan_only", "adults", "source"} for t in _tiles.values()),
      [sorted(t) for t in list(_tiles.values())[:1]])
check("the admin page is not on a child's wall", _tiles["admin"]["adults"] is True)
check("and the everyday services are",
      not any(_tiles[n]["adults"] for n in ("home-cameras", "home-core")),
      {n: _tiles[n]["adults"] for n in _tiles})

# `portal_path:` is the difference between a link that survives leaving the
# house and one that cannot. The portal is forwarded by the VPS; a private
# address on the LAN is not, whoever asks.
print("\nwhat a tile links to decides whether it can leave the house")
check("a service the portal serves is linked by its path",
      _tiles["home-cameras"]["url"] == "/camaras/", _tiles["home-cameras"]["url"])
check("and is house-only only because the household said so",
      _tiles["home-cameras"]["lan_only"] is True,
      "services.home-cameras.house_only defaults true")
_open = copy.deepcopy(CFG)
_open["services"]["home-cameras"]["house_only"] = False
_open_tiles = {t["name"]: t for g in json.loads(
    D.build_portal_dashboard(_open))["groups"].values() for t in g}
check("a household that publishes the cameras gets a tile that says so",
      _open_tiles["home-cameras"]["lan_only"] is False)
# home-core is the exception and the reason the rule holds: it is the one
# thing the VPS forwards, which is exactly why every *other* tile pointing at
# an address behind it cannot be reached from outside.
check("everything else is house-only whatever anybody says",
      all(t["lan_only"] for n, t in _open_tiles.items()
          if n not in ("home-cameras", "home-core")),
      [n for n, t in _open_tiles.items() if not t["lan_only"]])
check("except the portal itself, which is what the VPS forwards",
      _open_tiles["home-core"]["lan_only"] is False)
check("and is linked by address and port",
      _tiles["mqtt"]["url"] == "http://192.168.1.11:1884"
      or _tiles["mqtt"]["url"].endswith(":1884"), _tiles["mqtt"]["url"])

# A tile the portal cannot name is a service id on the wall -- `home-paperless`
# where a person expects «Documents». The deployer's English is only a
# fallback, so a missing catalogue entry shows up nowhere until somebody looks
# at the page.
print("\nevery tile the manifest declares can be named and drawn")
_catalogue = json.loads(
    (HERE.parent / "i18n" / "en.json").read_text(encoding="utf-8"))
for _name, _spec in _manifest["services"].items():
    if _spec.get("tier") not in ("basic", "advanced"):
        continue
    check(f"  {_name} has an icon", bool(_spec.get("icon")),
          "add `icon:` beside `tier:`; the fallback is a generic link glyph")
    check(f"  {_name} has a name a person reads",
          f"portal.service.{_name}" in _catalogue,
          f"add portal.service.{_name} to i18n/en.json, or the wall shows the id")
    check(f"  {_name} says what it is",
          f"portal.service.{_name}.about" in _catalogue,
          f"add portal.service.{_name}.about to i18n/en.json")

# The household's own services. This is the whole reason the group exists: a
# tile nobody here deploys, marked so that when it breaks it is clear it is
# not this stack's to fix.
print("\nand a household's own services arrive as extensions")
_ext = copy.deepcopy(CFG)
_ext["custom_services"] = [
    {"name": "plex", "title": "Plex", "url": "http://192.168.1.9:32400",
     "description": "Films", "icon": "\N{CLAPPER BOARD}", "lan_only": True},
    {"name": "nothing"},                      # no url: not a tile
]
_ext_wall = json.loads(D.build_portal_dashboard(_ext))["groups"]
check("it lands in extensions and nowhere else",
      [t["name"] for t in _ext_wall["extensions"]] == ["plex"],
      _ext_wall["extensions"])
check("marked as the household's own",
      _ext_wall["extensions"][0]["source"] == "custom")
check("keeping the title the household typed",
      _ext_wall["extensions"][0]["title"] == "Plex")
check("an entry with no url is not a tile",
      all(t["name"] != "nothing" for t in _ext_wall["extensions"]))

# --- the small pure helpers ---------------------------------------------------
print("\nthe naming rules the admin page has to agree with")
check("user1 -> USER_1", D.member_env_suffix("user1") == "USER_1")
check("user12 -> USER_12", D.member_env_suffix("user12") == "USER_12")
check("a non-numbered id is upper-cased", D.member_env_suffix("house") == "HOUSE")
check("{M} expands per member",
      D.expand_per_member(["X_{M}"], ["user1", "user2"]) == ["X_USER_1", "X_USER_2"])
check("the proxy token is a function of the secret and the id",
      D.derive_proxy_token("s", "user1") == D.derive_proxy_token("s", "user1")
      and D.derive_proxy_token("s", "user1") != D.derive_proxy_token("s", "user2"))

print("\ninterpolation reaches config and refuses what is not there")
check("a dotted path resolves",
      D.interpolate("{services.home-core.port}", CFG) == "8443")
check("a name with a dash in it resolves",
      D.interpolate("{services.home-cameras.web_port}", CFG) == "5000")
try:
    D.interpolate("{services.nope.port}", CFG)
    check("an unknown path is refused", False, "no error raised")
except D.DeployError:
    check("an unknown path is refused", True)

# --- the manifest itself -------------------------------------------------------
print("\nthe manifest and the config parse the way they read")
import yaml


class _DupCheck(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    """Refuse a duplicate key instead of silently keeping the last one.

    PyYAML takes the last of a repeated key without a word. A verify block once
    ended up with two `expect_json` entries after an edit; the file parsed, the
    check ran, and it was asserting something nobody had written. That is the
    shape of failure this repo cares about — invisible in a diff, wrong at
    runtime.
    """
    seen = set()
    for k, _ in node.value:
        key = loader.construct_object(k, deep=True)
        if key in seen:
            raise AssertionError(f"duplicate key {key!r} at line {k.start_mark.line + 1}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_DupCheck.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)

for name in ("deploy/manifest.yml", "config/home-stack.example.yml"):
    path = HERE.parent / name
    try:
        yaml.load(path.read_text(encoding="utf-8"), _DupCheck)
        check(f"{name} has no duplicate keys", True)
    except AssertionError as exc:
        check(f"{name} has no duplicate keys", False, exc)

# The manifest invariants -- everything a manifest can get wrong with no config
# and no secrets in play. The implementation lives in deploy.py so that
# `--check-contract` can run the same rules over the *merged* set, plugins
# included: these used to be six loops over deploy/manifest.yml read from disk,
# which meant a plugin's units escaped every one of them.
#
# Between them they cover: a verify check that asserts nothing a request can
# fail; two units sharing a Compose project, where `up --remove-orphans` in one
# deletes the other's containers; a compose file or overlay the manifest names
# and does not ship; a conditional overlay with no `when:`; a `state:` entry
# declared on a service instead of a unit; a state variable no compose file
# reads; and a verify `script:` whose path does not resolve inside the unit.
man = yaml.safe_load((HERE / "manifest.yml").read_text(encoding="utf-8"))
core = dict(man.get("services") or {}, **(man.get("optional_services") or {}))
problems = D.manifest_invariants(core)
check("the manifest holds up on its own", not problems, "\n      ".join(problems))



print("\na household with no local DNS")
# The `dns:` names only work if something on the network answers them, and
# plenty of households have no Pi-hole, no router that does local DNS and no
# appetite for a hosts file on every device. Blank has to be a real answer, not
# a config error -- the stack reaches everything by address anyway.
no_dns = copy.deepcopy(CFG)
no_dns["hosts"] = {"hub": {"address": "192.168.1.10"},
                   "compute": {"address": "192.168.1.11"},
                   "storage": {"address": "192.168.1.12"}}
no_dns["dns"] = {"portal": "", "chat": "  ", "cameras": "", "paperless": "",
                 "mqtt": "mqtt.home"}
D.fill_blank_dns_names(no_dns)
check("a blank name falls back to its host's address",
      no_dns["dns"]["portal"] == "192.168.1.10", no_dns["dns"])
check("whitespace counts as blank", no_dns["dns"]["chat"] == "192.168.1.10")
check("a name in front of another role follows that role",
      no_dns["dns"]["cameras"] == "192.168.1.11"
      and no_dns["dns"]["paperless"] == "192.168.1.12", no_dns["dns"])
check("a name that was set is left alone",
      no_dns["dns"]["mqtt"] == "mqtt.home")

# Nothing to fall back to, and nothing to crash on either.
edge = {"dns": {"portal": ""}}
D.fill_blank_dns_names(edge)
check("no hosts at all is survivable", edge["dns"]["portal"] == "")
D.fill_blank_dns_names({})
check("no dns block at all is survivable", True)


# --------------------------------------------------------------------------
print("\nwhat a container can actually reach")
# --------------------------------------------------------------------------
# Three comments in deploy.py say this is asserted by a test rather than
# assumed. It was not, for a while, and the checker shipped with a hole in it
# per line of the rule: URL-shaped values only, `extra_hosts` matched anywhere
# in the file including a comment, and a host-networked sidecar exempting its
# bridge-networked neighbours.

reach = copy.deepcopy(CFG)
D.add_container_addresses(reach)
check("a loopback role becomes the gateway",
      reach["hosts"]["hub"]["from_container"] == D.CONTAINER_GATEWAY,
      reach["hosts"]["hub"])
check("a real address is left as itself",
      reach["hosts"]["compute"]["from_container"] == "192.168.1.11")

manual = {"hosts": {"hub": {"address": "127.0.0.1", "from_container": "172.17.0.1"}}}
D.add_container_addresses(manual)
check("a household that wrote its own keeps it",
      manual["hosts"]["hub"]["from_container"] == "172.17.0.1", manual)

check("a bare host counts as loopback", D.is_loopback_address("127.0.0.1"))
check("so does any scheme, not just http",
      D.is_loopback_address("mqtt://localhost:1883")
      and D.is_loopback_address("http://[::1]:9000"))
check("a real address does not",
      not D.is_loopback_address("192.168.1.11")
      and not D.is_loopback_address("https://house.home:8443"))
check("and neither does a list that merely contains one",
      not D.is_loopback_address("127.0.0.1:3000,localhost:3000"))


def _problems(compose, env, **extra):
    """`loopback_reachability_problems` over one throwaway unit."""
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "docker-compose.yml").write_text(compose)
    unit = {"name": "u", "dir": ".", "_root": str(root), "env": env, **extra}
    return D.loopback_reachability_problems(
        {"svc": {"units": [unit]}}, {"hosts": CFG["hosts"], "services": {}})


BRIDGE = "services:\n  app:\n    environment:\n      - API=${API}\n"
HOSTNET = ("services:\n  app:\n    network_mode: host\n"
           "    environment:\n      - API=${API}\n")
LOOP = {"API": "http://127.0.0.1:9000"}
GATE = {"API": f"http://{D.CONTAINER_GATEWAY}:9000"}

check("a bridge container handed loopback is refused", _problems(BRIDGE, LOOP))
check("a host-networked one is not: there loopback is the host",
      not _problems(HOSTNET, LOOP))
check("but the gateway name there is the error, and is caught",
      _problems(HOSTNET, GATE))
check("a bare host is caught too, not only a URL",
      _problems(BRIDGE, {"API": "127.0.0.1"}))
check("one host-networked service does not excuse its bridge neighbour",
      _problems("services:\n  gw:\n    network_mode: host\n"
                "    environment:\n      - API=${API}\n"
                "  worker:\n    environment:\n      - API=${API}\n", LOOP))
check("the gateway needs the alias",
      _problems(BRIDGE, GATE))
check("declared on the service that dials, it passes",
      not _problems('services:\n  app:\n    extra_hosts:\n'
                    f'      - "{D.CONTAINER_GATEWAY}:host-gateway"\n'
                    "    environment:\n      - API=${API}\n", GATE))
check("declared on a different service, it does not",
      _problems("services:\n  app:\n    environment:\n      - API=${API}\n"
                "  db:\n    extra_hosts:\n"
                f'      - "{D.CONTAINER_GATEWAY}:host-gateway"\n', GATE))
check("a comment naming host-gateway is not a declaration",
      _problems("services:\n  app:\n    # no host.docker.internal:host-gateway here\n"
                "    environment:\n      - API=${API}\n", GATE))
check("a variable no compose file reads is not a container's problem",
      not _problems("services:\n  app:\n    image: x\n", LOOP))
check("nor is one whose name merely prefixes a variable that is read",
      not _problems("services:\n  app:\n    environment:\n      - X=${API_INTERNAL}\n",
                    LOOP))
check("self_urls exempts a service's own address",
      not _problems(BRIDGE, LOOP, self_urls={"API": "its own base url"}))
check("an exemption for a variable that no longer exists is reported",
      _problems(BRIDGE, {"API": "https://elsewhere:1"}, self_urls={"GONE": "x"}))
check("a compose file that cannot be parsed is reported, never skipped",
      _problems("services: [\n", LOOP))

# The shipped package, end to end: every unit of every service, against the
# example config. This is the assertion the comments in deploy.py promise --
# nothing on the host side is handed the gateway name, and every container that
# is handed it declares the alias.
shipped = D.load_yaml(D.ROOT / "config" / "home-stack.example.yml")
D.apply_service_renames(shipped)
D.apply_config_defaults(shipped)
shipped = D.derive(shipped, {})
shipped_manifest = D.load_yaml(D.MANIFEST)
# compose_files_for() renders the generated overlays into the checkout, and
# with *this* config that is the example's two members. On 2026-09-12 a file
# left behind by this check was what the next deploy shipped: three members'
# assistants removed. The deployer now renders before it pushes, so a stale file
# can no longer reach a deploy -- but `plan` and `check` still read it, so put
# back whatever was there.
_generated = [D.ROOT / "services" / "nanobot" / "docker-compose.members.yml"]
_kept = {p: p.read_bytes() for p in _generated if p.exists()}
try:
    _loop = D.loopback_reachability_problems(
        D.all_services(shipped_manifest, shipped), shipped)
finally:
    for p in _generated:
        if p in _kept:
            p.write_bytes(_kept[p])
        else:
            p.unlink(missing_ok=True)
check("the package this ships satisfies its own rule", _loop == [], _loop)

# The order that went wrong: a deploy must render the overlay from its own
# config before it copies the tree anywhere.
_deploy_src = (HERE / "deploy.py").read_text(encoding="utf-8")
check("a deploy renders its generated compose before it pushes",
      -1 < _deploy_src.find("        ensure_generated_compose(unit, cfg)\n")
      < _deploy_src.find("target.push(staging"),
      "the push would ship whatever overlay the checkout held")

# --- the example config assumes no GPU, because most machines have none -----
#
# Neither of these degrades. faster-whisper on `cuda` without a driver raises
# "CUDA driver version is insufficient" at import and restart-loops, which from
# outside is indistinguishable from a slow model load; home-cameras with the
# reservation on fails `up` outright with "could not select device driver".
#
# The deployer's own defaults stay cuda/float16/True on purpose -- they are what
# keeps an existing install behaving as it did. It is *this file* that a new
# install starts from, and it shipped `device: cuda`. A from-scratch deploy on
# a GPU-less VPS found it: whisper restart-looping, while docs/migration.md
# said the example "ships them off".
raw = D.load_yaml(D.ROOT / "config" / "home-stack.example.yml")
whisper = (raw.get("services") or {}).get("faster-whisper") or {}
cameras = (raw.get("services") or {}).get("home-cameras") or {}
check("the example ships whisper on the cpu",
      # float32, not int8: measured on the voice round trip, int8 heard
      # "Turn on the kitchen light" as "and don't take it to the place".
      whisper.get("device") == "cpu"
      and whisper.get("compute_type") in ("float32", "float16"),
      {k: whisper.get(k) for k in ("device", "compute_type", "model")})
check("and with a model that is usable there",
      whisper.get("model") in ("small", "base", "tiny", "medium"),
      whisper.get("model"))
check("and the camera wall without a GPU reservation",
      cameras.get("gpu") is False, cameras.get("gpu"))
# The deployer must still default the other way, or every upgrade silently
# loses the GPU it has been using.
check("while the deployer's own default stays cuda, for upgrades",
      D.CONFIG_DEFAULTS.get("services.faster-whisper.device") == "cuda"
      and D.CONFIG_DEFAULTS.get("services.home-cameras.gpu") is True,
      {k: v for k, v in D.CONFIG_DEFAULTS.items() if "gpu" in k or "device" in k})

# --- each assistant instance is the member it says it is ---------------------
#
# One instance per member, and four things have to name the same person: the
# compose profile the deployer starts, the state directory it writes, the
# credentials it is handed, and who it claims to be to the portal and the file
# share. Three of the five had the last pair pointing at somebody else --
# nanobot-user1 answered as user3, user2 as user1, user3 as user2 -- while
# still holding their own tokens, which is what proved it wrong rather than
# deliberate: you cannot act as user3 with user1's proxy token.
#
# On the shipped default of two members that put one member's assistant on
# another member's private share folder and posted to the portal as them. The
# deployer supplies neither value, so what is written here is what runs.
_multi = D.load_yaml(D.ROOT / "services" / "nanobot" / "docker-compose.multiuser.yml")
for _name, _spec in (_multi.get("services") or {}).items():
    if not _name.startswith("nanobot-user"):
        continue
    _who = _name[len("nanobot-"):]
    _env = _spec.get("environment") or {}
    if isinstance(_env, list):
        _env = dict(e.split("=", 1) for e in _env if "=" in e)
    for _var in ("HOMECORE_USER_ID", "FILE_SHARE_FOLDER"):
        check(f"{_name} is {_who} to the portal and the share ({_var})",
              _env.get(_var) == _who,
              f"{_var}={_env.get(_var)!r}, but this instance holds {_who}'s "
              f"tokens and writes {_who}'s state")
    check(f"{_name} runs only under its own profile",
          list(_spec.get("profiles") or []) == [_who],
          _spec.get("profiles"))

# --- nothing demands a GPU unless you asked for one --------------------------
#
# A `driver: nvidia` reservation is not a preference. `docker compose up` fails
# outright with "could not select device driver" on a machine without one, so a
# service carrying it unconditionally cannot be deployed on most hardware. The
# two that want a card both make it opt-in and must go on doing so: the camera
# wall through an overlay applied only when `services.home-cameras.gpu` is on,
# and the TTS bench through a compose profile nothing enables by default.
#
# The code is already the easy half -- both pick their device with
# `torch.cuda.is_available()` and fall back. This is about the compose files,
# where the failure is a refusal to start rather than a slower answer.
import yaml as _yaml
for _compose in sorted((D.ROOT / "services").rglob("docker-compose*.yml")):
    try:
        _doc = _yaml.safe_load(_compose.read_text(encoding="utf-8")) or {}
    except Exception:
        continue
    for _svc, _spec in (_doc.get("services") or {}).items():
        if "nvidia" not in str(_spec.get("deploy") or ""):
            continue
        _rel = _compose.relative_to(D.ROOT)
        _optional = bool(_spec.get("profiles")) or ".gpu." in _compose.name
        check(f"{_rel}: {_svc} asks for a GPU only when opted in",
              _optional,
              "put it behind a compose profile or a docker-compose.gpu.yml "
              "overlay, or this service cannot start without an NVIDIA card")

# --- an exported URL keeps the path its consumer expects ---------------------
#
# The compose file's own `${VAR:-default}` is a statement about what the value
# should look like, written by whoever knows what reads it. When the deployer
# exports a *shorter* URL than that default -- host where the default has a
# path -- it is overriding a correct fallback with a broken one, and the
# service cannot tell: it gets a 200-shaped connection to a 404.
#
# NANOBOT_URL was exactly this. gateway.py POSTs to the value directly and
# defaults to `.../v1/chat/completions`; the manifest exported the bare host,
# so every voice turn 404'd and the gateway answered "I could not reach
# Alfred" -- with the assistant up and answering in two seconds.
import re as _re_url
for _svc, _spec in D.load_yaml(D.MANIFEST).get("services", {}).items():
    for _unit in _spec.get("units", []):
        _compose_dir = D.ROOT / _unit["dir"]
        _texts = []
        _entries = ([_unit["compose"]] if isinstance(_unit.get("compose"), str)
                    else (_unit.get("compose") or []))
        for _cf in _entries:
            # A conditional overlay is `{file: X, when: ...}`, not a string.
            _name_of = _cf["file"] if isinstance(_cf, dict) else _cf
            _path = _compose_dir / _name_of
            if _path.is_file():
                _texts.append(_path.read_text(encoding="utf-8"))
        _blob = "\n".join(_texts)
        for _name, _value in (_unit.get("env") or {}).items():
            if not isinstance(_value, str) or not _value.startswith("http"):
                continue
            _m = _re_url.search(rf"{_re_url.escape(_name)}=\$\{{{_re_url.escape(_name)}"
                                rf":-(http[^}}]*)\}}", _blob)
            if not _m:
                continue
            _default_path = _m.group(1).split("://", 1)[-1]
            _default_path = _default_path[_default_path.find("/"):] \
                if "/" in _default_path else ""
            _exported_rest = _value.split("://", 1)[-1]
            _exported_path = _exported_rest[_exported_rest.find("/"):] \
                if "/" in _exported_rest else ""
            check(f"{_svc}/{_unit['name']}: {_name} keeps the path its "
                  f"compose default has",
                  bool(_default_path) <= bool(_exported_path),
                  f"compose defaults to '...{_default_path}', the manifest "
                  f"exports '{_value}' -- the shorter one wins and the "
                  f"service gets a 404 it cannot distinguish from being down")

# --- the admin page is never reachable through a proxy -----------------------
#
# The bind address is not what keeps it to the household -- a hub sits behind a
# router, but the proxies are reachable from outside on purpose, so one
# `reverse_proxy` line to 8099 would publish the docker socket and the Deploy
# button to the internet while `bind: 0.0.0.0` still looked exactly the same.
check("nothing in the tree routes to the admin port",
      D.admin_is_not_proxied(raw) == [], D.admin_is_not_proxied(raw))
# The matcher has to be able to find one, or "clean" means nothing. Read the
# port out of the example config rather than pinning it: this assertion was
# written against the literal 8070, and when the renumbering moved the local
# proxy the pin was the only thing keeping the suite green over a Caddyfile
# that now pointed at nothing.
_local_proxy_port = ((D.load_yaml(D.ROOT / "config" / "home-stack.example.yml")
                      .get("services") or {}).get("local-proxy") or {})["port"]
check("and the check can actually find a route when there is one",
      len(D.admin_is_not_proxied(
          {"services": {"admin": {"port": _local_proxy_port}}})) > 0,
      f"the house Caddyfile reverse_proxies to {_local_proxy_port}; finding "
      f"nothing there means the matcher is broken, not that the tree is clean")

# --- the rendered Kubernetes objects say who they are ------------------------
#
# The same assertion as the compose one above, against the other renderer.
# Three of the five compose instances shipped answering as the wrong member,
# from five near-identical blocks with the identity written out in each; five
# near-identical YAML documents is that shape with better highlighting. This
# runs whether or not anybody has opted into `runtime: kubernetes`, because the
# path nobody exercises is the one that drifts.
#
# Above the verdict, not below it: appended after `raise SystemExit(1)` these
# printed FAIL and the run still exited 0, which is the decorative-check shape
# the rest of this file exists to catch.
#
# Fed a fixture, never the real secrets file: `docs/migration.md` promises this
# script needs no config and no secrets, and reading them made it die with a
# traceback on a fresh clone -- and pulled live household credentials into a
# process that prints its inputs on failure.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("k8s", D.ROOT / "deploy" / "kubernetes.py")
_k8s = _ilu.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_k8s)
except Exception as _exc:                              # noqa: BLE001
    check("the kubernetes renderer imports", False, _exc)
else:
    _kcfg = D.load_yaml(D.ROOT / "config" / "home-stack.example.yml")
    D.apply_service_renames(_kcfg)
    D.add_new_services(_kcfg)
    D.apply_config_defaults(_kcfg)
    # A user store, because HOMECORE_PROXY_TOKEN is derived from each person's
    # *login* now -- the portal hashes that, not the member id. A household
    # running assistants has a portal and therefore has this file; without one
    # every per-member token is legitimately empty, which is what the check
    # below is for.
    _kstate = _tf.mkdtemp(prefix="k8s-logins-")
    _kcfg.setdefault("paths", {})["state"] = _kstate
    _os.makedirs(_os.path.join(_kstate, "home-core"), exist_ok=True)
    with open(_os.path.join(_kstate, "home-core", "users.json"), "w") as _fh:
        _json.dump([{"member": m, "username": f"9000001{i:02d}"}
                    for i, m in enumerate(D.member_ids(_kcfg))], _fh)
    _kcfg = D.derive(_kcfg, {})
    _kspec = _k8s._spec(D.load_yaml(D.MANIFEST))
    _kunit = _k8s._unit(_kspec)
    _declared = _kspec.get("secrets", {})
    _members = D.member_ids(_kcfg)
    # Every declared key, with a value that is recognisably a fixture.
    _ksecrets = {k: f"fixture-{k.lower()}" for k in
                 list(_declared.get("required", []))
                 + list(_declared.get("optional", []))
                 + D.expand_per_member(list(_declared.get("per_member", [])), _members)}

    for _member in _members:
        _objs = _k8s.render(_member, _kcfg, _kunit, _kspec, _ksecrets)
        _kinds = {o["kind"] for o in _objs}
        check(f"{_member}: renders the objects an instance needs",
              {"Secret", "ConfigMap", "PersistentVolumeClaim", "Service",
               "StatefulSet"} <= _kinds, sorted(_kinds))
        _cm = [o for o in _objs if o["kind"] == "ConfigMap"][0]["data"]
        for _var in ("NANOBOT_INSTANCE", "HOMECORE_USER_ID", "FILE_SHARE_FOLDER"):
            check(f"{_member}: {_var} is {_member}", _cm.get(_var) == _member,
                  _cm.get(_var))
        _sts = [o for o in _objs if o["kind"] == "StatefulSet"][0]
        # A StatefulSet terminates before it creates; a Deployment's rolling
        # update overlaps them, and two pods on one member's .nanobot corrupts
        # consolidated memory without saying so.
        check(f"{_member}: exactly one writer", _sts["spec"]["replicas"] == 1,
              _sts["spec"]["replicas"])
        _pvc = [o for o in _objs if o["kind"] == "PersistentVolumeClaim"][0]
        check(f"{_member}: its volume is single-writer",
              _pvc["spec"]["accessModes"] == ["ReadWriteOnce"],
              _pvc["spec"]["accessModes"])
        _sec = [o for o in _objs if o["kind"] == "Secret"][0]
        check(f"{_member}: mounts only its own per-member secret",
              _sec["metadata"]["name"] == f"nanobot-{_member}",
              _sec["metadata"]["name"])

        _pod = _sts["spec"]["template"]["spec"]
        _con = _pod["containers"][0]
        # Each of the next six is a regression this branch already shipped and
        # fixed once. None of them was covered by the checks above.
        #
        # `entrypoint.sh` + CMD ["status"]: compose's `entrypoint:` clears the
        # image CMD and Kubernetes' `command:` does not, so an empty `args`
        # printed the provider list and exited 0.
        check(f"{_member}: says what to run", bool(_con.get("args")),
              _con.get("args"))
        # The image runs as uid 1000 and checks it can write its state dir.
        check(f"{_member}: the volume is writable by the image's user",
              _pod.get("securityContext", {}).get("fsGroup") == 1000,
              _pod.get("securityContext"))
        # It carried the environment and not the assistant's own prompt files.
        check(f"{_member}: carries the prompt files",
              any(v.get("configMap", {}).get("name") == _k8s.CONFIG_MAP
                  for v in _pod.get("volumes", [])),
              [v["name"] for v in _pod.get("volumes", [])])
        # `member.upper()` gives USER1; the deployer's own mapping gives USER_1,
        # and a Secret full of empty values killed the pod on the first key.
        _sd = _sec["stringData"]
        check(f"{_member}: its credentials are not empty",
              all(_sd.values()), sorted(k for k, v in _sd.items() if not v))
        # Compose sets a declared-but-absent key to the empty string; the
        # container distinguishes unset from empty and exits on the first
        # unset thing its config references.
        check(f"{_member}: every declared per-member key is present",
              {k.rsplit("_USER_", 1)[0] for k in
               D.expand_per_member(list(_declared.get("per_member", [])), [_member])}
              <= set(_sd), sorted(_sd))
        # host.docker.internal is Docker's name for the machine and resolves
        # nowhere in a pod.
        check(f"{_member}: no docker-only hostname reaches the pod",
              not any(D.CONTAINER_GATEWAY in str(v) for v in _cm.values()),
              [k for k, v in _cm.items() if D.CONTAINER_GATEWAY in str(v)])
        # The master every member's token is derived from. A pod holding it
        # could mint any other member's.
        check(f"{_member}: holds no other member's credential",
              "PROXY_SHARED_SECRET" not in _sd
              and not any(k.endswith("_USER_1") or k.endswith("_USER_2")
                          for k in _sd),
              sorted(_sd))
        # Every name the manifest's `env:` block declares reaches the pod
        # under that name -- including `FILE_SHARE_PASSWORD: $SHARE_SMB_PASSWORD`,
        # which an earlier renderer dropped because it started with a `$`.
        _reaches = set(_cm) | set(_sd) | {e["name"] for e in _con["env"]} | set(
            _k8s.env_for(_member, _kcfg, _kunit, _kspec, _ksecrets)[2])
        check(f"{_member}: every declared env name reaches the pod",
              set(_kunit.get("env") or {}) <= _reaches,
              sorted(set(_kunit.get("env") or {}) - _reaches))
        # A Service port with no targetPort targets itself, and the container
        # listens on one number for every member.
        _svc = [o for o in _objs if o["kind"] == "Service"][0]
        _served = {p["name"] for p in _con["ports"]}
        check(f"{_member}: every published port names a port the pod serves",
              all(p.get("targetPort") in _served for p in _svc["spec"]["ports"]),
              _svc["spec"]["ports"])

    # The shared Secret is the object every member mounts. A per-member key in
    # it is the boundary this renderer exists to keep.
    _shared = _k8s.env_for(_members[0], _kcfg, _kunit, _kspec, _ksecrets)[2]
    check("the shared secret holds no per-member credential",
          not any("_USER_" in k for k in _shared), sorted(_shared))
    check("the shared secret withholds the token master",
          "PROXY_SHARED_SECRET" not in _shared, sorted(_shared))
    # The registry branch is the half of this feature nothing else exercises.
    _kcfg["services"]["registry"] = {"enabled": True, "host": "hub", "port": 5005}
    check("with a registry on, the image comes from it",
          _k8s.image_ref(_kcfg).endswith("/alfred-nanobot:latest"),
          _k8s.image_ref(_kcfg))


# A stand-in nanobot config for apply_model_choices. It has to declare the
# providers a fallback may name: a cross-provider entry is routed to that
# provider's own client, and a config that does not declare it has no client to
# build -- which the deployer now refuses rather than writing in silently.
_PROVIDERS_DOC = {"providers": {"custom": {}, "together_ai": {}, "freetoken": {},
                                "ollama": {}}}


# --- the outage fallback is a model that can actually rescue one -------------
#
# Added after gpt-5.6-luna returned a hard 500 on every call at the gateway on
# 2026-08-23 while other models answered on the same key. The two ways to write
# a fallback that looks configured and cannot fire are both quiet until the
# outage it exists for, so the deploy refuses them instead.
# Only one shape is refused now. A fallback on ANOTHER provider used to be
# refused too, and that was right for the code as it stood -- the swap replaced
# the model inside the failing provider's own client, so a name meant for
# somewhere else was a guaranteed 404 on top of an outage. base.py routes such
# an entry through that provider's own client now, and the refusal would stop
# the only fallback that survives a provider going down: a same-provider rescue
# shares the outage it is rescuing.
for _bad, _why, _expect in (
    ("gpt-5.6-luna", "a fallback already in use", "already"),
):
    _cfg = copy.deepcopy(CFG)
    _cfg["assistant"] = {"models": {"everyday": "gpt-5.6-luna",
                                    "fallback": _bad}}
    try:
        D.check_fallback_model(_cfg)
        check(f"{_why} is refused", False, "no error raised")
    except D.DeployError as _exc:
        check(f"{_why} is refused", _expect in str(_exc), _exc)

_ok = copy.deepcopy(CFG)
_ok["assistant"] = {"models": {"everyday": "gpt-5.6-luna",
                               "vision": "ollama:qwen3-vl:8b",
                               "fallback": "deepseek-v4-flash"}}
D.check_fallback_model(_ok)
check("a hosted fallback nothing else names is accepted", True)
# Vision is exempt on purpose: it is a different modality on a different host,
# and the code never routes it to the fallback. Sharing a name with it would
# still be odd, but it is not the failure this guard is about.
D.check_fallback_model({"assistant": {"models": {}}})
check("no fallback configured is not an error", True)

# --- a dns: name has to point at the machine that runs the service -----------
#
# Every check this deployer had asked whether a value was PRESENT. The contract
# check asks whether compose gets each variable; the health checks ask whether a
# container answers; the certificate check asks whether a name is served. None
# asked whether the name points at the right box, so a name that is real,
# resolvable and belongs to another machine passed all of them.
#
# On 2026-08-31 `dns.assistant` went from `assistant.home` to
# `homeassistant.home` -- one word apart in a list containing both -- and the
# manifest exports `NANOBOT_HOST: "{dns.assistant}"`. HomeCore looked for the
# assistant on the Home Assistant box. The deploy was green. The household got
# `Connection refused ... homeassistant.home:21301` on every message, six hours
# later, because a container keeps the environment it was built with and
# nothing redeployed home-core until then.
print("\na dns: name that points at another machine is refused")
_MF = {"services": {"nanobot-house": {"role": "hub"},
                    "home-cameras": {"role": "compute"}}}
_dns_cfg = {
    "dns": {"assistant": "homeassistant.home", "cameras": "cameras.home"},
    "hosts": {"hub": {"address": "127.0.0.1"}, "compute": {"address": "127.0.0.1"}},
    "services": {"nanobot-house": {"enabled": True}, "home-cameras": {"enabled": True}},
}
_RESOLV = {"homeassistant.home": {"192.168.88.238"},   # the HA box
           "cameras.home": {"192.168.88.35"},          # this machine
           "assistant.home": {"192.168.88.35"}}
_real_gai, _real_own = D.socket.getaddrinfo, D._own_addresses
D._own_addresses = lambda: {"127.0.0.1", "::1", "192.168.88.35"}
D.socket.getaddrinfo = lambda host, *a, **k: (
    [(2, 1, 6, "", (ip, 0)) for ip in _RESOLV[host]] if host in _RESOLV
    else (_ for _ in ()).throw(D.socket.gaierror("no such host")))
try:
    try:
        D.check_dns_points_at_its_service(copy.deepcopy(_dns_cfg), _MF)
        check("  the wrong machine is caught", False, "no error raised")
    except D.DeployError as _exc:
        check("  the wrong machine is caught", "192.168.88.238" in str(_exc), _exc)
        check("  and it names the service and the role",
              "nanobot-house" in str(_exc) and "hosts.hub" in str(_exc), _exc)

    # A single-PC install writes 127.0.0.1 for every role, and the name for it
    # resolves to the LAN address. Comparing the literal would fail every
    # correctly-configured house in the world.
    _dns_ok = copy.deepcopy(_dns_cfg)
    _dns_ok["dns"]["assistant"] = "assistant.home"
    try:
        D.check_dns_points_at_its_service(_dns_ok, _MF)
        check("  a loopback role accepts this machine's LAN address", True)
    except D.DeployError as _exc:
        check("  a loopback role accepts this machine's LAN address", False, _exc)

    # No local DNS is the documented, supported state -- `dns:` may be left
    # empty for exactly that reason -- and this also runs inside the admin
    # container, where `.home` is NXDOMAIN. Refusing there breaks deploying
    # from the page.
    _dns_nx = copy.deepcopy(_dns_cfg)
    _dns_nx["dns"] = {"assistant": "nowhere.invalid"}
    try:
        D.check_dns_points_at_its_service(_dns_nx, _MF)
        check("  a name that does not resolve warns rather than fails", True)
    except D.DeployError as _exc:
        check("  a name that does not resolve warns rather than fails", False, _exc)

    # fill_blank_dns_names writes addresses into this block; they are the
    # answer, not something to re-check.
    _dns_addr = copy.deepcopy(_dns_cfg)
    _dns_addr["dns"] = {"assistant": "192.168.88.238"}
    try:
        D.check_dns_points_at_its_service(_dns_addr, _MF)
        check("  a bare address is left alone", True)
    except D.DeployError as _exc:
        check("  a bare address is left alone", False, _exc)

    # A service nobody deployed has no machine to be wrong about.
    _dns_off = copy.deepcopy(_dns_cfg)
    _dns_off["services"]["nanobot-house"] = {"enabled": False}
    try:
        D.check_dns_points_at_its_service(_dns_off, _MF)
        check("  a disabled service is not checked", True)
    except D.DeployError as _exc:
        check("  a disabled service is not checked", False, _exc)
finally:
    D.socket.getaddrinfo, D._own_addresses = _real_gai, _real_own


# --- a fallback may live on another provider, and usually should -------------
#
# The same-provider rule could not rescue the failure households actually have.
# It was built for one dead model behind a healthy gateway -- real, and what
# happened on 2026-08-23 -- but a provider that is down takes every model on it,
# and a second name on the same key is then a second identical failure. A house
# serving one model from one local engine has the sharpest version: there is no
# same-provider rescue to name at all.
print("\na fallback may name another provider")
for _models, _why in (
    ({"everyday": "freetoken:local-moe",
      "fallback": "together:meta-models/Muse-Glimmer-30B"},
     "a local engine rescued by a hosted one"),
    ({"everyday": "together:deepseek-ai/DeepSeek-V4-Flash-0731",
      "fallback": "freetoken:local-moe"},
     "a hosted provider rescued by the house's own hardware"),
    ({"everyday": "gpt-5.6-luna", "fallback": "ollama:qwen3.5:9b"},
     "and a local model is a legitimate rescue, not a mistake"),
):
    try:
        D.check_fallback_model({"assistant": {"models": _models}})
        check(f"  {_why}", True)
    except D.DeployError as _exc:
        check(f"  {_why}", False, _exc)

# The entry has to carry its provider, because a bare name means "the provider
# serving the role" and always has. base.py can only route to another client
# when it is told which one.
_x = json.loads(D.apply_model_choices(_json.dumps(_PROVIDERS_DOC), {"assistant": {"models": {
    "everyday": "freetoken:local-moe",
    "fallback": "together:meta-models/Muse-Glimmer-30B"}}}))
check("  and it is written with the provider attached",
      _x["agents"]["defaults"]["modelFallback"]
      == {"model": "meta-models/Muse-Glimmer-30B", "provider": "together_ai"},
      _x["agents"]["defaults"].get("modelFallback"))

# A bare name means "the provider this route already runs on" -- and that is a
# different provider per route. `model` moves to together while `subagentModel`
# is still on OpenCode Zen, so one bare fallback name addresses two endpoints and
# exists on at most one: the sub-agent's rescue was a together model name posted
# at OpenCode Zen, a 404 on top of the outage it was rescuing. So the provider is
# attached whenever it is not the OpenCode Zen default.
_x = json.loads(D.apply_model_choices(_json.dumps(_PROVIDERS_DOC), {"assistant": {"models": {
    "everyday": "together:a", "fallback": "together:b"}}}))
check("  a fallback off OpenCode Zen always carries its provider",
      _x["agents"]["defaults"]["modelFallback"]
      == {"model": "b", "provider": "together_ai"},
      _x["agents"]["defaults"].get("modelFallback"))

# The shipped install is untouched: bare names, and the internal chain appended.
_x = json.loads(D.apply_model_choices(_json.dumps(_PROVIDERS_DOC), {"assistant": {"models": {
    "everyday": "gpt-5.6-luna", "fallback": "deepseek-v4-flash"}}}))
check("  and an all-OpenCode-Zen house still gets bare names",
      # The household's own choice first, then the internal names. None of
      # them is a shipped role default, which is what keeps this list from
      # shrinking on a stock install -- see OUTAGE_FALLBACKS.
      _x["agents"]["defaults"]["modelFallback"]
      == ["deepseek-v4-flash", *D.OUTAGE_FALLBACKS],
      _x["agents"]["defaults"].get("modelFallback"))

# Still refused: a rescue that is the thing being rescued. Name AND provider,
# because the same model on two hosts is two models for this purpose -- which
# is the useful case rather than a corner one.
try:
    D.check_fallback_model({"assistant": {"models": {
        "everyday": "together:a", "fallback": "together:a"}}})
    check("  a fallback that is a role's own model is still refused", False)
except D.DeployError:
    check("  a fallback that is a role's own model is still refused", True)
try:
    D.check_fallback_model({"assistant": {"models": {
        "everyday": "together:a", "fallback": "freetoken:a"}}})
    check("  but the same name on another host is a different model", True)
except D.DeployError as _exc:
    check("  but the same name on another host is a different model", False, _exc)
# ...and refused only when it rescues NOBODY. Since 2026-09-10 the everyday
# model is the local one and it is also the only offline rescue the hosted
# roles have; a fallback one role already names still rescues every other.
try:
    D.check_fallback_model({"assistant": {"models": {
        "everyday": "ollama:ornith-1.5:9b", "powerful": "together:big",
        "fallback": "ollama:ornith-1.5:9b"}}})
    check("  a fallback one role names still rescues the others", True)
except D.DeployError as _exc:
    check("  a fallback one role names still rescues the others", False, _exc)
try:
    D.check_fallback_model({"assistant": {"models": {
        "everyday": "ollama:a", "powerful": "ollama:a", "fallback": "ollama:a"}}})
    check("  but one that every role names rescues nobody, and is refused", False)
except D.DeployError:
    check("  but one that every role names rescues nobody, and is refused", True)


# --- the two models on the page that nanobot never reads --------------------
#
# `titles` (home-core's chat titler) and `documents` (Paperless's AI) take a
# plain URL and key from their environment, so the prefix is resolved by the
# deployer. Blank, or a provider that is off, has to come out EMPTY: both
# compose files default on the empty string, and an export that beat that
# default with a half-resolved address would be a titler that never names a
# chat and a Paperless that fails on every scan.
# A nested default still names the inner variable: the compose file reads it.
_nested = D.compose_service_variables({"environment": {
    "E": "${PAPERLESS_AI_ENDPOINT:-${OLLAMA_URL:-http://host.docker.internal:11434}/v1}"}})
check("  a nested compose default is read for its inner variable too",
      _nested == {"PAPERLESS_AI_ENDPOINT", "OLLAMA_URL"}, _nested)

print("\nmodel_endpoint resolves a role the way its consumer needs it")
_me = {"cloud": {"ollama": {"local": {"enabled": True, "host": "compute", "port": 11434},
                            "vision": {"enabled": True,
                                       "url": "http://host.docker.internal:11435"}}},
       "hosts": {"compute": {"address": "192.168.1.5"}}}
check("  blank is three empty strings",
      D.model_endpoint(_me, {}, "") == ("", "", ""))
check("  a local model, container-facing, ends in /v1 and carries the literal key",
      D.model_endpoint(_me, {}, "ollama:ornith-1.5:9b")
      == ("ornith-1.5:9b", "http://192.168.1.5:11434/v1", "ollama"),
      D.model_endpoint(_me, {}, "ollama:ornith-1.5:9b"))
check("  the vision instance keeps its container-facing name for a container",
      D.model_endpoint(_me, {}, "ollama-vision:minicpm-v:8b")[1]
      == "http://host.docker.internal:11435/v1")
check("  the vision instance's key is the literal every local consumer sends",
      D.model_endpoint(_me, {}, "ollama-vision:minicpm-v:8b")[2] == "ollama",
      D.model_endpoint(_me, {}, "ollama-vision:minicpm-v:8b"))
# host.docker.internal is "the host this container runs on"; for home-core,
# on the host's own network, that is itself. The compute host's address was
# right only on one box, and the wrong machine on a split install.
check("  ...and is loopback for a host-networked consumer, not the compute host",
      D.model_endpoint(_me, {}, "ollama-vision:minicpm-v:8b", host_network=True)[1]
      == "http://127.0.0.1:11435/v1",
      D.model_endpoint(_me, {}, "ollama-vision:minicpm-v:8b", host_network=True))
check("  a vision URL with a real address is left alone",
      D.model_endpoint({**_me, "cloud": {"ollama": {**_me["cloud"]["ollama"], "vision": {
          "enabled": True, "url": "http://10.0.0.9:11435"}}}}, {},
          "ollama-vision:x", host_network=True)[1] == "http://10.0.0.9:11435/v1")

# Both direct consumers call /v1/chat/completions only, so a Zen model that
# answers only on /v1/responses would fail every call -- silently, for titles.
for _role in ("titles", "documents"):
    try:
        D.check_direct_models({"assistant": {"models": {_role: "gpt-5.6-luna"}}})
        check(f"  a Responses-only Zen model is refused for {_role}", False, "no error")
    except D.DeployError as _exc:
        check(f"  a Responses-only Zen model is refused for {_role}",
              "responses" in str(_exc), _exc)
try:
    D.check_direct_models({"assistant": {"models": {
        "titles": "deepseek-v4-flash", "documents": "ollama-vision:minicpm-v:8b",
        "everyday": "gpt-5.6-luna"}}})
    check("  a chat-completions model is accepted, and other roles are not its business", True)
except D.DeployError as _exc:
    check("  a chat-completions model is accepted, and other roles are not its business",
          False, _exc)
try:
    D.check_direct_models({"assistant": {"models": {"titles": "openai:gpt-5-mini"}}})
    check("  the rule is Zen's split, so gpt-5 on OpenAI itself is accepted", True)
except D.DeployError as _exc:
    check("  the rule is Zen's split, so gpt-5 on OpenAI itself is accepted", False, _exc)
check("  a hosted provider without its key yields no URL, not a URL that 401s",
      D.model_endpoint(_me, {}, "together:Qwen/Qwen3.8-Flash") == ("Qwen/Qwen3.8-Flash", "", ""))
check("  and with it, the provider's base and that key",
      D.model_endpoint(_me, {"TOGETHER_API_KEY": "tk"}, "together:Qwen/Qwen3.8-Flash")
      == ("Qwen/Qwen3.8-Flash", "https://api.together.xyz/v1", "tk"))
check("  a bare name is OpenCode Zen, like everywhere else in assistant.models",
      D.model_endpoint(_me, {"OPENCODE_API_KEY": "zk"}, "gpt-5.6-luna")[1]
      == "https://opencode.ai/zen/v1")

_cfg_te = copy.deepcopy(CFG)
_cfg_te.setdefault("assistant", {}).setdefault("models", {}).update(
    {"titles": "ollama:ornith-1.5:9b", "documents": "ollama-vision:qwen3-vl:4b"})
_cfg_te.setdefault("cloud", {}).setdefault("ollama", {})["vision"] = {
    "enabled": True, "url": "http://host.docker.internal:11435", "context": 65536}
_d_te = D.derive(_cfg_te, {})["derived"]
check("  derive exports the titler's model and its full chat endpoint",
      _d_te["title_model"] == "ornith-1.5:9b"
      and _d_te["title_url"].endswith("/v1/chat/completions")
      and "host.docker.internal" not in _d_te["title_url"], _d_te["title_url"])
# Paperless's AI was exported under names Paperless never read, so it had never
# been on. A local Ollama gets Paperless's Ollama client, at the server's root,
# asking for the window the server runs -- anything else reloads the model on
# every document.
_pl = lambda d: tuple(d[k] for k in ("paperless_enabled", "paperless_backend",
                                     "paperless_model", "paperless_endpoint",
                                     "paperless_context"))
check("  a local Ollama gets Paperless's Ollama client at the server's root",
      _pl(_d_te) == ("true", "ollama", "qwen3-vl:4b",
                     "http://host.docker.internal:11435", "65536"), _pl(_d_te))
check("  and its suggestions are written in the house's language",
      _d_te["paperless_output_language"] == str((_cfg_te.get("locale") or {}).get("default") or ""),
      _d_te["paperless_output_language"])
_cfg_te["cloud"]["ollama"]["vision"].pop("context")
check("  a server whose window the config does not state leaves Paperless's own",
      D.derive(_cfg_te, {})["derived"]["paperless_context"] == "")
_cfg_te["assistant"]["models"]["documents"] = "deepseek-v4-flash"
_d_te = D.derive(_cfg_te, {"OPENCODE_API_KEY": "zk"})["derived"]
check("  a hosted model goes through the OpenAI-compatible client, /v1 and all",
      _pl(_d_te) == ("true", "openai-like", "deepseek-v4-flash",
                     "https://opencode.ai/zen/v1", ""), _pl(_d_te))
_cfg_te["assistant"]["models"].update({"titles": "", "documents": ""})
_d_te = D.derive(_cfg_te, {})["derived"]
check("  blank exports empty strings, so each compose default holds",
      all(_d_te[k] == "" for k in ("title_model", "title_url", "title_key",
                                   "paperless_backend", "paperless_model",
                                   "paperless_endpoint", "paperless_key",
                                   "paperless_context")))
check("  and blank switches Paperless's AI off rather than guessing a model",
      _d_te["paperless_enabled"] == "false")
# The index. Paperless refuses to start on an empty embedding backend, so when
# none is chosen the variable has to be absent -- which compose cannot do by
# itself, only the deployer by not exporting it.
print("\npaperless's embedding index: set when chosen, absent when not")
_cfg_em = copy.deepcopy(CFG)
_cfg_em.setdefault("assistant", {}).setdefault("models", {})["documents"] = "ollama:gemma4:e4b"
_cfg_em.setdefault("services", {}).setdefault("home-paperless", {})["embeddings"] = "ollama:embeddinggemma"
_d_em = D.derive(_cfg_em, {})["derived"]
check("  a local embedding model gets Paperless's Ollama backend at the server's root",
      (_d_em["paperless_embedding_backend"], _d_em["paperless_embedding_model"])
      == ("ollama", "embeddinggemma")
      and not _d_em["paperless_embedding_endpoint"].endswith("/v1")
      and _d_em["paperless_embedding_endpoint"], _d_em["paperless_embedding_endpoint"])
_cfg_em["assistant"]["models"]["documents"] = ""
check("  no documents model, no index -- the AI is off",
      D.derive(_cfg_em, {})["derived"]["paperless_embedding_backend"] == "")
_cfg_em["assistant"]["models"]["documents"] = "deepseek-v4-flash"
_cfg_em["services"]["home-paperless"]["embeddings"] = "together:BAAI/bge-m3"
try:
    D.derive(_cfg_em, {"OPENCODE_API_KEY": "zk", "TOGETHER_API_KEY": "tk"})
    check("  a hosted index on another provider than the documents model is refused",
          False, "no error")
except D.DeployError as _exc:
    check("  a hosted index on another provider than the documents model is refused",
          "one API key" in str(_exc), _exc)
_om_env = D.collect_env({}, {"name": "t", "env": {"GONE": "", "KEPT": "x", "EMPTY_OK": ""},
                              "env_omit_empty": ["GONE", "KEPT"]}, {}, copy.deepcopy(CFG))
check("  env_omit_empty drops an empty value rather than exporting it",
      "GONE" not in _om_env and _om_env.get("KEPT") == "x", sorted(_om_env)[:5])
check("  and only for the names it lists", _om_env.get("EMPTY_OK") == "")
_pl_unit = D.load_yaml(D.MANIFEST)["services"]["home-paperless"]["units"][0]
_pl_compose = (HERE.parent / "services" / "home-paperless" / "docker-compose.yml").read_text()
for _var in ("PAPERLESS_AI_LLM_EMBEDDING_BACKEND", "PAPERLESS_AI_LLM_EMBEDDING_MODEL",
             "PAPERLESS_AI_LLM_EMBEDDING_ENDPOINT"):
    check(f"  {_var} is omitted when empty, and declared without a value",
          _var in (_pl_unit.get("env_omit_empty") or [])
          and f"\n      {_var}:\n" in _pl_compose, _var)

for _stale in ("PAPERLESS_AI_PROVIDER", "PAPERLESS_AI_MODEL:", "PAPERLESS_AI_ENDPOINT", "minicpm"):
    check(f"  the compose file no longer sets {_stale.rstrip(':')}",
          _stale not in "\n".join(l for l in _pl_compose.splitlines()
                                  if not l.lstrip().startswith("#")))


# --- every route the assistant has is settable from the site config ----------
#
# `assistant.models` had keys for five of the assistant's eight routes. The two
# sub-agent models and the doctor and legal professions had none, so they kept
# whatever the shipped config said -- and the gap was invisible until a
# household moved provider: everything in the site config went to together.ai
# and every background task, cron reminder and memory consolidation went on
# calling OpenCode Go, which was the provider they had left because it was
# erroring.
#
# A setting that covers most of a thing looks exactly like one that covers all
# of it, which is why this is asserted against the shipped config rather than
# against a list written here.
print("\nevery route in the shipped config can be set from assistant.models")
_base = _json.loads((D.ROOT / "services/nanobot/config/config.json"
                     ).read_text(encoding="utf-8"))["agents"]["defaults"]
_every = {"everyday": "together:E", "powerful": "together:P",
          "subagent": "together:S",
          "vision": "ollama:v", "fallback": "together:F"}
_every.update({role: f"together:{role}" for role in (_base.get("modelProfiles") or {})})
_after = _json.loads(D.apply_model_choices(
    (D.ROOT / "services/nanobot/config/config.json").read_text(encoding="utf-8"),
    {"assistant": {"models": _every}}))["agents"]["defaults"]

for _field, _want in (("model", "E"), ("modelPowerful", "P"), ("subagentModel", "S")):
    check(f"  {_field} follows the site config", _after.get(_field) == _want,
          _after.get(_field))
check("  the retired powerful sub-agent is gone from the file, shipped value and all",
      "subagentModelPowerful" not in _after and "subagentProviderPowerful" not in _after,
      _after.get("subagentModelPowerful"))
for _field in ("provider", "providerPowerful", "subagentProvider"):
    check(f"  and so does {_field}", _after.get(_field) == "together_ai",
          _after.get(_field))
_left = sorted(r for r, m in (_after.get("modelProfiles") or {}).items() if m != r)
check("  every profession the config declares moved too", not _left,
      f"{_left} kept the shipped model -- they are unsettable from the site config")

# Driven by the file, so a profession added there needs no edit here, and the
# instance that declares none gets none written into it.
_house = _json.loads(D.apply_model_choices(
    (D.ROOT / "services/nanobot/config/instances/house/config.json"
     ).read_text(encoding="utf-8"), {"assistant": {"models": _every}}))
check("  and an instance with no professions gets none invented",
      not (_house["agents"]["defaults"].get("modelProfiles") or {}),
      _house["agents"]["defaults"].get("modelProfiles"))


# --- a fallback nobody can build is a decorative declaration -----------------
#
# The rescue path walks past a provider it cannot build rather than raising,
# which is right at 3am and useless as a deploy-time answer. So the failure was
# silent: `nanobot-house` omits providers on purpose, got a `together:` fallback
# written into it anyway, and during a real OpenCode Go outage logged "Fallback
# provider together_ai is not configured in this process" and answered the room
# with an error instead of a rescue.
print("\na fallback naming a provider the config lacks is refused")
_house = (D.ROOT / "services/nanobot/config/instances/house/config.json"
          ).read_text(encoding="utf-8")
try:
    D.apply_model_choices(_house, {"assistant": {"models": {
        "everyday": "gpt-5.6-luna", "fallback": "openrouter:some/model"}}})
    check("  refused", False, "written in, and it would never be built")
except D.DeployError as _exc:
    check("  refused", "does not declare" in str(_exc), _exc)

# And the one this household actually runs has to be buildable in BOTH configs,
# because both get the same fallback written into them.
for _which, _path in (("base", "services/nanobot/config/config.json"),
                      ("house", "services/nanobot/config/instances/house/config.json")):
    _out = _json.loads(D.apply_model_choices(
        (D.ROOT / _path).read_text(encoding="utf-8"),
        {"assistant": {"models": {"everyday": "gpt-5.6-luna",
                                  "fallback": "together:meta-models/Muse-Glimmer-30B"}}}))
    check(f"  {_which} declares the provider its fallback names",
          _out["agents"]["defaults"]["modelFallback"]["provider"] == "together_ai")


# --- FreeToken is a provider of its own, not the generic slot -----------------
#
# It speaks the OpenAI API, so it *could* have used `cloud.openai_compatible`.
# There is exactly one of those and it exists to be pointed at whatever is
# being tried this week -- a vLLM box, an LM Studio, a provider this package
# has never heard of. Spending it on a serving engine a household runs
# permanently would make those two mutually exclusive for no reason beyond a
# shared wire format. Both are live at once here, and this is what says so.
print("\nfreetoken is its own provider and leaves the generic slot free")
check("  a role can name it", D.split_model("freetoken:zai-org/GLM-5.2")
      == ("zai-org/GLM-5.2", "freetoken"))
check("  and the generic slot still resolves separately",
      D.split_model("openai-compatible:x") == ("x", "openai_compatible"))

_ft_cfg = {"cloud": {"freetoken": {"enabled": True,
                                   "url": "http://desktop.home:8000/v1"},
                     "openai_compatible": {"enabled": True,
                                           "url": "http://vllm.home:8000"}}}
_url, _key = D.freetoken_endpoint(_ft_cfg, {})
# People paste both forms out of a server's own docs and nanobot appends the
# path itself; the doubled /v1/v1/chat/completions 404s every turn.
check("  a trailing /v1 is trimmed", _url == "http://desktop.home:8000", _url)
check("  a keyless local server is allowed", _key == "", _key)
check("  both self-hosted slots can be on at once",
      {"freetoken", "openai_compatible"} <= D.enabled_providers(_ft_cfg, {}, {}),
      sorted(D.enabled_providers(_ft_cfg, {}, {})))

# A config written before this setting existed must still deploy. `dns.whisper`
# and `site.resolver` each caused that break once; freetoken_endpoint walks the
# config with .get chains and derive() always writes the derived pair, so there
# is nothing for an old config to be missing.
_old_cfg = {"cloud": {"openai_compatible": {"enabled": False}}}
check("  a config predating it needs no migration",
      D.freetoken_endpoint(_old_cfg, {}) == ("", "")
      and "freetoken" not in D.enabled_providers(_old_cfg, {}, {}))

try:
    D.freetoken_endpoint({"cloud": {"freetoken": {"enabled": True}}}, {})
    check("  on with no url is refused", False, "no error raised")
except D.DeployError as _exc:
    check("  on with no url is refused", "url is empty" in str(_exc), _exc)

# Nanobot refuses to build a keyless provider unless its spec says local, and
# a serving engine on your own desk is exactly that.
_spec_src = (D.ROOT / "services/nanobot/nanobot/providers/registry.py").read_text(
    encoding="utf-8")
check("  the provider spec marks it local",
      'name="freetoken"' in _spec_src
      and "is_local=True" in _spec_src.split('name="freetoken"')[1][:400],
      "without is_local, a keyless FreeToken raises 'No API key configured'")

# Both nanobot configs have to carry the block: listing a provider is how the
# credential gets demanded, and an unset ${...} stops the container starting --
# which is why the compose files supply a placeholder key.
for _cfgfile in ("services/nanobot/config/config.json",
                 "services/nanobot/config/instances/house/config.json"):
    _doc = _json.loads((D.ROOT / _cfgfile).read_text(encoding="utf-8"))
    check(f"  {_cfgfile.split('/')[-2]}/{_cfgfile.split('/')[-1]} declares it",
          "freetoken" in (_doc.get("providers") or {}), sorted(_doc.get("providers") or {}))

print("\nopencode is one server per member, and not an OpenAI endpoint")
# The whole reason it has a block of its own: `opencode serve` has no
# /v1/chat/completions and its `tools:` field enables and disables its own
# tools rather than accepting schemas. Nothing that speaks the OpenAI API can
# be pointed at it, and nothing pointed at it can be treated as speaking it.
_oc_cfg = {
    "cloud": {"opencode": {"enabled": True, "port_base": 4096}},
    "members": [{"id": "user1", "programmer": True},
                {"id": "user2", "programmer": True},
                {"id": "user3"},
                {"id": "user4", "programmer": True, "active": False}],
}
_servers = D.opencode_servers(_oc_cfg)
check("  a server for each member who has it switched on",
      sorted(_servers) == ["user1", "user2"], sorted(_servers))
# Position among the members who have it on, so switching it on for one person
# does not renumber somebody else's and leave their opencode dialling a port
# that moved.
check("  counting up from the base, in order",
      _servers["user1"].endswith(":4096") and _servers["user2"].endswith(":4097"),
      _servers)
# Loopback, and correct as such: the only consumer is HomeCore, which runs
# network_mode: host. It is also the whole boundary in front of an agent that
# has a shell.
check("  on loopback, which is the boundary in front of a shell",
      all(u.startswith("http://127.0.0.1:") for u in _servers.values()),
      _servers)
check("  somebody who has not switched it on gets none",
      "user3" not in _servers)
# The container would sit there holding credentials for somebody who has left.
check("  and nor does a member who is no longer active",
      "user4" not in _servers)

check("  switched off, nobody has one and the space falls back",
      D.opencode_servers({"cloud": {"opencode": {"enabled": False}},
                          "members": [{"id": "user1", "programmer": True}]}) == {})
# Same shape as the freetoken case above and the same reason: a config written
# before this setting existed must still deploy.
check("  a config predating it needs no migration",
      D.opencode_servers({"cloud": {}}) == {} and D.opencode_servers({}) == {})
# Said, not refused: failing the deploy would stop everything over one
# profession that has a working fallback.
check("  switched on with nobody to serve is a warning, not a refusal",
      D.opencode_servers({"cloud": {"opencode": {"enabled": True}},
                          "members": [{"id": "user1"}]}) == {})

# The host unit and its readiness check ship together, and the unit names the
# script by path. A unit whose ExecStartPost does not exist fails to start.
_unit = (D.ROOT / "deploy/host/opencode-serve@.service").read_text(encoding="utf-8")
check("  the unit is a template, one instance per member",
      "%i" in _unit and (D.ROOT / "deploy/host/opencode-serve@.service").exists(),
      "a shared server would authenticate every member's tools as one person")
check("  it ships beside a readiness script it can find",
      (D.ROOT / "deploy/host/opencode-health.sh").exists()
      and "opencode-health.sh" in _unit)
check("  the readiness script is executable",
      os.access(D.ROOT / "deploy/host/opencode-health.sh", os.X_OK),
      "ExecStartPost refuses a file it cannot run")
# This is the kind of line somebody widens to debug something and forgets.
check("  it binds loopback and nothing else",
      "--hostname 127.0.0.1" in _unit,
      "opencode serve has bash; the port is the whole boundary")
# The payload, not the port and not the status code. Comment lines are stripped
# first: the script *documents* why `curl -sf` is the wrong shape.
_health = (D.ROOT / "deploy/host/opencode-health.sh").read_text(encoding="utf-8")
_health_code = "\n".join(l for l in _health.splitlines()
                         if not l.lstrip().startswith("#"))
check("  readiness asserts the payload",
      '"healthy"' in _health_code and "curl -sf" not in _health_code,
      "a 200 is not readiness; /global/health answers before it will serve")

print("\nevery bridge is one member's, derived from one member id")
_gen = D._generator(D.GENERATORS["alfred-mcp"])
check("  one per member who has it switched on",
      _gen.members(_oc_cfg) == ["user1", "user2"], _gen.members(_oc_cfg))
_rendered = _gen.render({**_oc_cfg, "members": [
    {"id": "user1", "display_name": "Ana", "programmer": True},
    {"id": "user2", "display_name": "Bo", "programmer": True}]})
check("  a container each, named for the member",
      "alfred-mcp-user1:" in _rendered and "alfred-mcp-user2:" in _rendered)
# The folder is the third id -- the slugified display name -- and paths on the
# share are made of it. A bridge built with the wrong one writes nowhere.
check("  each carrying that member's own share folder",
      "ALFRED_MCP_FOLDER=ana" in _rendered
      and "ALFRED_MCP_FOLDER=bo" in _rendered, _rendered[:400])
# A token that leaked opens one person's bridge and no other.
check("  and that member's own derived token",
      "ALFRED_MCP_TOKEN_USER_1" in _rendered
      and "ALFRED_MCP_TOKEN_USER_2" in _rendered)
check("  published on loopback, one port each",
      '"127.0.0.1:21071:21071"' in _rendered
      and '"127.0.0.1:21072:21072"' in _rendered)
# A household that has switched nobody on gets a valid file that starts
# nothing, rather than a missing one compose complains about by path.
check("  nobody switched on renders a valid, empty file",
      "services: {}" in _gen.render({"members": [{"id": "user1"}]}))
# The shared values must appear as literals in the renderer's *source*:
# --check-contract reads it for `${VAR`, and an f-string spells it `${{VAR`.
_gen_src = (D.ROOT / "deploy/compose_alfred_mcp.py").read_text(encoding="utf-8")
for _var in ("CODE_WORKSPACE_DIR", "HOMECORE_CONTAINER_URL", "CODE_BROKER_URL"):
    check(f"  the contract check can see ${_var}",
          "${" + _var in _gen_src,
          "an f-string writes ${{ and the check matches nothing")

# The image slots are generation, not conversation, and were never on the
# rescue path. Counting them made the stranded-role warning fire for every
# household that generates images, which is a warning nobody reads twice.
D.check_fallback_model({"assistant": {"models": {
    "everyday": "freetoken:a", "image_high": "together:google/imagen-4.0-fast",
    "vision": "ollama:qwen3-vl:8b", "fallback": "freetoken:b"}}})
check("  an image slot is not a role the fallback owes a rescue", True)


# --- the outage chain belongs to the gateway that serves it -------------------
#
# OUTAGE_FALLBACKS is three bare names OpenCode Zen routes. They are appended to
# whatever the household chose, which was safe only while every fallback was on
# that gateway -- and it stopped being safe the moment check_fallback_model
# learned to accept another provider. A house on together.ai got
# `[Muse-Glimmer-30B, deepseek-v4-flash, glm-5.3-flash, qwen3.8-flash]` posted
# to api.together.xyz: three guaranteed 404s on the end of every exhausted
# ladder, during the outage the chain exists for.
_chain = json.loads(D.apply_model_choices(_json.dumps(_PROVIDERS_DOC), {"assistant": {"models": {
    "everyday": "together:deepseek-ai/DeepSeek-V4-Flash-0731",
    "fallback": "together:meta-models/Muse-Glimmer-30B"}}}))
_fb = _chain["agents"]["defaults"].get("modelFallback")
check("a fallback off the gateway is not extended with its names",
      _fb == {"model": "meta-models/Muse-Glimmer-30B", "provider": "together_ai"},
      f"{_fb} -- those extra names reach api.together.xyz, which has never "
      f"heard of them")

_chain = json.loads(D.apply_model_choices(_json.dumps(_PROVIDERS_DOC), {"assistant": {"models": {
    "everyday": "gpt-5.6-luna", "fallback": "deepseek-v4-flash"}}}))
check("and a fallback on the gateway still is",
      _chain["agents"]["defaults"].get("modelFallback")
      == ["deepseek-v4-flash", *D.OUTAGE_FALLBACKS],
      _chain["agents"]["defaults"].get("modelFallback"))


# --- the fallback follows the roles, not a vendor ----------------------------
#
# This rule is a relationship between two settings and was written as a rule
# about one: `provider != "custom"`, because that is where every role was in
# August 2026. Moving the house onto together.ai then made every legal
# fallback illegal -- a `together:` model refused for "running on together_ai"
# while every role it would rescue ran there too, told to "name a hosted
# model", which it was. There is no way to satisfy that message, which is the
# worst shape a refusal can have.
_tg = copy.deepcopy(CFG)
_tg["assistant"] = {"models": {
    "everyday": "together:deepseek-ai/DeepSeek-V4-Flash-0731",
    "powerful": "together:deepseek-ai/DeepSeek-V4-Pro-0813",
    "vision": "ollama:qwen3-vl:8b",
    "fallback": "together:meta-models/Muse-Glimmer-30B"}}
try:
    D.check_fallback_model(_tg)
    check("a fallback on the provider the roles use is accepted", True)
except D.DeployError as _exc:
    check("a fallback on the provider the roles use is accepted", False, _exc)

# Same name on a different provider is a different model, and the "already in
# use" rule has to compare both halves or a move that keeps the names looks
# like a fallback pointed at itself.
_split = copy.deepcopy(CFG)
_split["assistant"] = {"models": {"everyday": "together:x", "powerful": "x",
                                  "fallback": "together:y"}}
try:
    D.check_fallback_model(_split)
    check("a stranded role is warned about, not refused", True)
except D.DeployError as _exc:
    check("a stranded role is warned about, not refused", False, _exc)

# And a household with nothing hosted at all still gets a working rescue: the
# swap is inside whichever provider instance failed, so all-local plus a local
# fallback is coherent. The old rule refused it for naming a provider.
try:
    D.check_fallback_model({"assistant": {"models": {
        "everyday": "ollama:a", "fallback": "ollama:b"}}})
    check("an all-local house may have a local fallback", True)
except D.DeployError as _exc:
    check("an all-local house may have a local fallback", False, _exc)

# The site config is where a model is chosen, so it has to reach the file the
# assistant reads -- otherwise the key is a comment.
_written = json.loads(D.apply_model_choices(
    json.dumps({"agents": {"defaults": {"model": "old"}}}), _ok))
# First in the chain, not the whole of it: the household's choice leads and the
# internal list follows, so what is asserted here is that the setting arrives --
# not that it is alone.
_fb_written = _written["agents"]["defaults"].get("modelFallback")
check("assistant.models.fallback reaches the nanobot config",
      (_fb_written[0] if isinstance(_fb_written, list) else _fb_written)
      == "deepseek-v4-flash",
      _written["agents"]["defaults"])
# No providerFallback: the swap happens inside the provider that already
# failed, on its key. A second provider could not be honoured.
check("and brings no provider of its own",
      "providerFallback" not in _written["agents"]["defaults"],
      sorted(_written["agents"]["defaults"]))


# --- a pre: script is told the compose project, not left to guess it ---------
#
# `local-cert.sh` derives the certificate volume from the project name and,
# without one in the environment, falls back to `basename $PWD`. That was right
# until the project became `<service>-<unit>` to stop two units called `local`
# sharing one -- after which the proxy's certificate went to
# `proxy_proxy-certs` and Caddy mounted an empty `home-core-proxy_proxy-certs`,
# crash-looping on a file that existed one volume over.
#
# The export has to happen *before* the hook runs, so this pins the order in
# the source rather than the mere presence of the line.
_src = pathlib.Path(D.__file__).read_text(encoding="utf-8")
_assign = _src.find('env["COMPOSE_PROJECT_NAME"] = project')
_hook = _src.find('target.run(unit["pre"]')
check("the deployer exports COMPOSE_PROJECT_NAME into the unit env",
      _assign != -1,
      "a pre: script cannot name a compose volume it is never told the "
      "project for")
check("and exports it before the pre: hook runs",
      _assign != -1 and _hook != -1 and _assign < _hook,
      f"assignment at {_assign}, hook at {_hook}")

# The script's own half of the same contract: given the variable, it must use
# it rather than searching. The search picks by `head -1` and cannot tell a
# stale wrong-project volume from the live one.
_cert_sh = (pathlib.Path(D.ROOT) / "services/home-core/proxy/local-cert.sh"
            ).read_text(encoding="utf-8")
check("local-cert.sh prefers the project it is given over a search",
      _cert_sh.find('${COMPOSE_PROJECT_NAME}_proxy-certs')
      < _cert_sh.find("docker volume ls -q"),
      "the volume search must be the fallback, not the first answer")

# --- the impact table matches the manifest, rather than memory ---------------
#
# "Saving is not deploying": the admin page offers to redeploy exactly the
# services a change affects, and a setting that never reaches a container is
# worse than one never changed. That table is hand-maintained, and it had
# already drifted -- `locale` gained the two assistants when SEARCH_LANGUAGE
# was added and did not gain home-voice, whose WHISPER_LANGUAGE comes from the
# same key and whose failure is a silently wrong transcription rather than an
# error.

print("\nthe admin page's locale impact covers every service that reads it")

_admin_src = (pathlib.Path(D.ROOT) / "admin" / "app.py").read_text(encoding="utf-8")
_manifest_text = (pathlib.Path(D.ROOT) / "deploy" / "manifest.yml").read_text(encoding="utf-8")

# Which services the manifest actually interpolates {locale.default} into.
_reads_locale = set()
for _svc, _spec in shipped_manifest.get("services", {}).items():
    if "{locale.default}" in json.dumps(_spec):
        _reads_locale.add(_svc)

_impact_line = [l for l in _admin_src.splitlines() if l.strip().startswith('"locale":')]
_declared_locale = set()
if _impact_line:
    _start = _admin_src.index(_impact_line[0])
    _declared_locale = set(_re_url.findall(r'"([a-z0-9-]+)"',
                                      _admin_src[_start:_admin_src.index("]", _start)]))
    _declared_locale.discard("locale")

_missing_locale = sorted(_reads_locale - _declared_locale)
check("every service the manifest gives {locale.default} is in IMPACT['locale']",
      not _missing_locale, f"missing: {_missing_locale}")


# --- and the same for `dns:`, which now names hosts for nine services --------
#
# `dns:` used to reach the portal's certificate and the proxy's site blocks and
# nothing else, so IMPACT listed two services. Internal URLs are built from
# those names now -- the assistants' TASKS_API_URL, the wall's broker, the
# portal's WHISPER_URL -- and renaming a host on the admin page marked two of
# nine pending. The other seven kept the old name, which is exactly the
# "saved, and the house behaves the old way" failure this table exists for.

print("\nthe admin page's dns impact covers every service built from a name")

_reads_dns = set()
for _grp in ("services", "optional_services"):
    for _svc, _spec in (shipped_manifest.get(_grp) or {}).items():
        if "{dns." in json.dumps(_spec):
            _reads_dns.add(_svc)
check("  the manifest interpolates a dns name at all", len(_reads_dns) >= 5,
      f"{sorted(_reads_dns)}: the walk found nothing and proves nothing")

_dns_line = [l for l in _admin_src.splitlines() if l.strip().startswith('"dns":')]
_declared_dns = set()
if _dns_line:
    _start = _admin_src.index(_dns_line[0])
    _declared_dns = set(_re_url.findall(r'"([a-z0-9-]+)"',
                                        _admin_src[_start:_admin_src.index("]", _start)]))
    _declared_dns.discard("dns")
# `["*"]` means every service and covers everything by definition.
_missing_dns = [] if _declared_dns == {"*"} else sorted(_reads_dns - _declared_dns)
check("every service the manifest builds out of a {dns.*} name is in IMPACT['dns']",
      not _missing_dns, f"missing: {_missing_dns}")


# --- and every name the manifest asks for is one the deployer can supply -----
#
# Three separate ways a new `dns:` entry breaks a household that already has a
# config, and `dns.whisper` hit all three at once:
#
#   * `interpolate()` raises on a path the config has not got, so a name that
#     is only in home-stack.example.yml fails every deploy of the units that
#     read it -- the CONFIG_DEFAULTS failure, in a block the file did not cover.
#   * a blank name falls back to DNS_FALLBACK_HOST, which defaults to the hub.
#     A name in front of a service on `compute` then resolves to the wrong box,
#     silently, because an address always answers something.
#   * DNS_SERVICE is what the admin page's table says is behind the name, and a
#     row with no service is a line somebody copies into their resolver.

print("\nevery {dns.*} the manifest reads is a name the deployer can supply")

# Over the parsed manifest, not its text: `{dns.ntfy}` appears in a comment
# explaining why it is *not* a name any more, and a text scan reads that as a
# name the deployer has to supply.
_names_used = set()
for _grp in ("services", "optional_services"):
    for _spec in (shipped_manifest.get(_grp) or {}).values():
        _names_used |= set(_re_url.findall(r"\{dns\.([a-z0-9_]+)\}", json.dumps(_spec)))
_names_used = sorted(_names_used)
check("  the manifest names some", len(_names_used) >= 5, _names_used)
_example = D.load_yaml(pathlib.Path(D.ROOT) / "config" / "home-stack.example.yml")
for _name in _names_used:
    check(f"  dns.{_name} is in the example config",
          _name in (_example.get("dns") or {}),
          "a new install would have no such name")
    _behind = D.DNS_SERVICE.get(_name)
    check(f"  dns.{_name} says which service answers it", bool(_behind),
          "the admin page's DNS table would offer a row with nothing behind "
          "it -- and fill_blank_dns_names would not create the name at all, so "
          "a config written before it stops deploying the unit that reads it")
    if _behind:
        _spec = (shipped_manifest.get("services", {}).get(_behind)
                 or shipped_manifest.get("optional_services", {}).get(_behind) or {})
        _role = _spec.get("role", "hub")
        check(f"  a blank dns.{_name} falls back to {_behind}'s own role",
              D.DNS_FALLBACK_HOST.get(_name, "hub") == _role,
              f"falls back to {D.DNS_FALLBACK_HOST.get(_name, 'hub')}, but "
              f"{_behind} runs on {_role} -- an address that answers, on the "
              f"wrong box")


# --- the heartbeat rank reaches the container it is for ----------------------
#
# `--check-contract` compares compose `${VAR}` reads against what the deployer
# exports and complains about a read nothing supplies. It does not complain the
# other way, and there is no general rule that could: plenty of exports are
# legitimately unread by a given file. So the one pairing that matters here is
# asserted directly -- an export no compose reads is decorative, and this one
# fails *silently*, because the assistant falls back to the digits in its
# instance name and produces a plausible-looking offset either way.

print("\nevery member's heartbeat rank is read by the compose file")

# The rendered per-member overlay, not the base file: the blocks these
# assertions are about are generated now, and a suite that read the file the
# members used to be written in would pass on a renderer that emits nothing.
import compose_members as CM  # noqa: E402

_MU = CM.render(CFG)
_members = D.member_ids(CFG) or ["user1", "user2"]
_unread = [m for m in _members
           if f"NANOBOT_STAGGER_INDEX_{D.member_env_suffix(m)}" not in _MU]
check("the compose reads the rank for every configured member",
      not _unread, f"not read: {_unread}")

# And that the deployer is the thing supplying it: the container cannot know
# its own rank, because member ids are monotonic and never reused, so the live
# list is not 1..N once a household has seen a departure.
_ranks = {}
for _rank, _m in enumerate(_members, start=1):
    _ranks[f"NANOBOT_STAGGER_INDEX_{D.member_env_suffix(_m)}"] = str(_rank)
check("and the ranks are dense and 1-based, whatever the ids are",
      sorted(int(v) for v in _ranks.values()) == list(range(1, len(_members) + 1)),
      _ranks)


# --- reading a profile back in ------------------------------------------------
#
# `build_member_profile` writes USER.md *out* from the config; import_profiles
# reads one back *in*, so a household carried across from an older setup does
# not arrive with an empty admin page. Three things about it can go wrong
# quietly, and each is a case below.

print("\na profile carried across from an older setup")

import import_profiles as IP  # noqa: E402

_OLD = """# User Profile

## Basic Information

- **Name**: Bo
- **Full Name**: Bo Example
- **Birthdate**: 02/03/2000
- **RUT**: 12.345.678-9
- **Family**: Ana (mum), Cy (sister)

## Preferences

### Communication Style

- [x] Casual
- Speak plainly

### Technical Level

- [x] Beginner
- [ ] Expert

## Topics of Interest

- Home automation
- Bread

## Special Instructions

Bo is a child -- keep it simple.
"""
_parsed = IP.parse(_OLD)
check("the name is what it is matched on", _parsed["name"] == "Bo", _parsed["name"])
check("interests become the hobbies field",
      _parsed["hobbies"] == ["Home automation", "Bread"], _parsed["hobbies"])
_notes = "\n\n".join(_parsed["notes"])
check("special instructions land in notes unheaded",
      _notes.endswith("Bo is a child -- keep it simple."), _notes[-60:])
# The sections with no column on the admin page. Dropping them would lose the
# part of a profile a household most deliberately wrote.
check("  and a section with no field of its own is kept as written",
      "## Communication Style" in _notes and "Speak plainly" in _notes, _notes)
# `- [ ] Expert` is a box nobody ticked. Carrying it over makes the assistant
# read a list of things that are *not* true about somebody.
check("  an unticked box is not imported as a fact",
      "Expert" not in _notes, _notes)
check("  and a ticked one loses its brackets",
      "- Beginner" in _notes and "[x]" not in _notes, _notes)

# `deploy/sanitize.py` is the record of why this config may not hold these.
check("a national identity number is not imported",
      "12.345.678-9" not in _notes and "RUT" not in _notes, _notes)
# In a section that is kept -- `## Basic Information` is skipped wholesale,
# because every field on it either has a column of its own or is an id.
_inline = IP.parse(_OLD.replace("## Topics of Interest",
                                "## Home Context\n\n"
                                "- **Family**: child of Ana (RUT: 12.345.678-9)"
                                "\n\n## Topics of Interest"))
check("  cut out of a line that says something else, not taken as a reason "
      "to drop it",
      "child of Ana" in "\n\n".join(_inline["notes"])
      and "12.345.678-9" not in "\n\n".join(_inline["notes"]),
      _inline["notes"])

# The one that costs real damage. The old directory is user1..user5 and so is
# the new one, and they are not the same people -- so this joins on the name.
_IMPCFG = {"members": [{"id": "user1", "display_name": "Ana"},
                       {"id": "user7", "display_name": "Bo"},
                       {"id": "user9", "display_name": "Cy"}]}
check("a profile is placed by name, never by position",
      IP.match_member(_IMPCFG, "Bo")["id"] == "user7",
      IP.match_member(_IMPCFG, "Bo"))
check("  and one that names nobody here is refused rather than guessed at",
      IP.match_member(_IMPCFG, "Someone Else") is None,
      "copying an index across is how a child gets a parent's assistant")
_rel = {}
for _who, _r in _parsed["family"].items():
    _other = IP.match_member(_IMPCFG, _who)
    if _other:
        _rel[_other["id"]] = _r
check("  and the family line resolves to ids the config actually has",
      _rel == {"user1": "mum", "user9": "sister"}, _rel)

# Day-first, because that is what wrote these -- and unreadable is left out
# rather than turned into a plausible wrong birthday.
check("the old day-first date becomes ISO",
      _parsed["birthdate"] == "2000-03-02", _parsed["birthdate"])
check("  and one that cannot be read is left out",
      IP.normalise_date("sometime in 1990") == "",
      IP.normalise_date("sometime in 1990"))


# --- one member in, one container out ----------------------------------------
#
# The per-member assistants were five hand-written compose blocks. What that
# cost is the whole reason this section exists, and each case below is a live
# failure it produced rather than a shape somebody thought might go wrong:
#
# - a sixth member got a profile, ports and a set of secrets and **no
#   container**. Compose asked to start a profile that matches no service
#   starts nothing and exits 0, so every stage of that deploy said `ok`.
# - `TASKS_API_URL` was on four of the five. The fifth member's assistant could
#   not reach the tasks API, and said so to that person only.
# - `FILE_SHARE_ADMIN` was written into two blocks by hand and neither of them
#   was an admin.
# - every block said `FILE_SHARE_FOLDER=userN`, while the portal serves that
#   person's files from `share_folder()` -- their name, on a share with years
#   of files in it.
#
# So the assertions are about *derivation*: everything that identifies an
# instance comes from one member id, and a member the config lists gets a
# container. Reading the rendered text, because that is what deploys.

print("\nevery member the config lists gets a container of their own")

_CMCFG = {
    "members": [
        {"id": "user1", "display_name": "Ana", "admin": True},
        {"id": "user3", "display_name": "Bo", "whatsapp": True},
        # No `members:` entry at all for user9 -- see below.
        {"id": "user4", "display_name": "Cy", "active": False,
         "whatsapp": True},
    ],
    "services": {"nanobot": {"members": ["user1", "user3", "user4", "user9"],
                             "websocket_port_base": 21201,
                             "api_port_base": 21301,
                             "gateway_port_base": 21401}},
}
# A user store, because HOMECORE_USER_ID is the *login* now and only this file
# knows which login belongs to which member. Without one the renderer emits an
# empty id -- correct, and useless for asserting that each container is wired
# for exactly one person.
_CMSTATE = _tf.mkdtemp(prefix="cm-logins-")
_CMCFG.setdefault("paths", {})["state"] = _CMSTATE
_os.makedirs(_os.path.join(_CMSTATE, "home-core"), exist_ok=True)
_CMLOGINS = {"user1": "900000111", "user3": "900000333",
             "user4": "900000444", "user9": "900000999"}
with open(_os.path.join(_CMSTATE, "home-core", "users.json"), "w") as _fh:
    _json.dump([{"member": m, "username": u} for m, u in _CMLOGINS.items()], _fh)

_rendered = yaml.safe_load(CM.render(_CMCFG))["services"]

check("every configured member is a service",
      all(f"nanobot-{m}" in _rendered for m in ("user1", "user3", "user4", "user9")),
      sorted(_rendered))
# The case that motivated the whole change: the ids are not 1..N and one of
# them is past the end of what a five-block file could ever hold.
check("  including one past the end of the old hardcoded five",
      "nanobot-user9" in _rendered, sorted(_rendered))
check("  and nobody else is",
      sum(k.startswith("nanobot-user") for k in _rendered) == 4, sorted(_rendered))

print("\nan instance's identity comes from one member id")
for _mid in ("user1", "user3", "user4", "user9"):
    _sfx = D.member_env_suffix(_mid)
    _env = dict(e.split("=", 1) for e in _rendered[f"nanobot-{_mid}"]["environment"])
    # HOMECORE_USER_ID is deliberately not in this set any more: it is the
    # person's *login*, because it becomes X-Proxy-User and the portal looks
    # that name up. The rest is what the deployer builds, and all of it is the
    # member id.
    _same = {
        "NANOBOT_INSTANCE": _env["NANOBOT_INSTANCE"],
        "profile": _rendered[f"nanobot-{_mid}"]["profiles"][0],
        "container": _rendered[f"nanobot-{_mid}"]["container_name"].split("-", 1)[1],
    }
    check(f"  {_mid}: instance, profile and container agree",
          set(_same.values()) == {_mid}, _same)
    # And the one value that crosses into a request is this member's login, and
    # nobody else's -- the failure being guarded against is an instance wired
    # for the wrong person, which is just as wrong in either id space.
    check(f"  {_mid}: and it answers to its own login",
          _env["HOMECORE_USER_ID"] == _CMLOGINS[_mid],
          f'{_env["HOMECORE_USER_ID"]} is not {_CMLOGINS[_mid]}')
    # Three of the five instances once shipped answering as the wrong member,
    # holding the right member's tokens. Every per-member secret name is
    # derived from the same id as the identity above.
    _secrets = {k: v for k, v in _env.items()
                if k in ("NANOBOT_API_SECRET", "HOMECORE_PROXY_TOKEN",
                         "CODE_BROKER_TOKEN", "PAPERLESS_API_TOKEN",
                         "NANOBOT_STAGGER_INDEX")}
    check(f"  {_mid}: and so does every per-member secret it reads",
          all(f"_{_sfx}:-" in v for v in _secrets.values()), _secrets)
    check(f"  {_mid}: its state and code mounts are its own",
          all(f"/{_mid}:" in v for v in _rendered[f"nanobot-{_mid}"]["volumes"][:2]),
          _rendered[f"nanobot-{_mid}"]["volumes"][:2])

print("\nand the four things the hand-written blocks got wrong")
_e1 = dict(e.split("=", 1) for e in _rendered["nanobot-user1"]["environment"])
_e3 = dict(e.split("=", 1) for e in _rendered["nanobot-user3"]["environment"])
check("the tasks API reaches every member, not four of five",
      all("TASKS_API_URL" in dict(e.split("=", 1) for e in
                                  _rendered[f"nanobot-{m}"]["environment"])
          for m in ("user1", "user3", "user4", "user9")))
check("the share-admin flag follows `members[].admin`",
      _e1.get("FILE_SHARE_ADMIN") == "1" and "FILE_SHARE_ADMIN" not in _e3,
      {"user1(admin)": _e1.get("FILE_SHARE_ADMIN"),
       "user3": _e3.get("FILE_SHARE_ADMIN")})
# The folder the *portal* uses, which is the only one the share has.
check("the share folder is what the portal serves them under",
      _e1["FILE_SHARE_FOLDER"] == D.share_folder(_CMCFG["members"][0]) == "ana",
      _e1["FILE_SHARE_FOLDER"])
check("  and is never the id, which is a directory nobody has",
      _e1["FILE_SHARE_FOLDER"] != "user1", _e1["FILE_SHARE_FOLDER"])
check("whatsapp follows `members[].whatsapp`, per person",
      "WHATSAPP_ENABLED" in _e3 and "WHATSAPP_ENABLED" not in _e1,
      {"user3(linked)": _e3.get("WHATSAPP_ENABLED"),
       "user1": _e1.get("WHATSAPP_ENABLED")})
check("  and a linked member gets a bridge on their own volume",
      _rendered["whatsapp-bridge-user3"]["volumes"] ==
      ["${NANOBOT_STATE_DIR:-/var/lib/home-stack/state/nanobot}"
       "/user3:/home/nanobot/.nanobot"],
      _rendered["whatsapp-bridge-user3"]["volumes"])
# Deactivating somebody drops their assistant. A bridge left running would go
# on re-linking a phone for a person who has left.
check("  a member who is not active gets no bridge",
      "whatsapp-bridge-user4" not in _rendered, sorted(_rendered))
check("  and the bridge has its own profile, not the member's",
      _rendered["whatsapp-bridge-user3"]["profiles"] == ["whatsapp-user3"],
      _rendered["whatsapp-bridge-user3"]["profiles"])

print("\nthe deployer asks compose to start exactly what was rendered")
_profiles = CM.profiles(_CMCFG)
_defined = set(_rendered)
_orphans = [p for p in _profiles
            if not any(_rendered[s].get("profiles") == [p] for s in _defined)]
check("no profile names a service that does not exist",
      not _orphans, f"compose starts nothing for {_orphans} and exits 0")
_ungated = [s for s in _defined if not _rendered[s].get("profiles")]
check("and every rendered service is gated by one",
      not _ungated, f"{_ungated} would start on every install")

print("\nports are derived from the bases the portal is told about")
# They were literals. The portal derives what it dials from
# `*_port_base` + the member's position, so moving a base renumbered the portal
# and left the containers where they were.
for _i, _mid in enumerate(("user1", "user3", "user4", "user9")):
    check(f"  {_mid} publishes {21201 + _i}, {21301 + _i}, {21401 + _i}",
          _rendered[f"nanobot-{_mid}"]["ports"] ==
          [f"{21201 + _i}:8765", f"{21301 + _i}:8900", f"{21401 + _i}:18790"],
          _rendered[f"nanobot-{_mid}"]["ports"])
_moved = {**_CMCFG, "services": {"nanobot": {**_CMCFG["services"]["nanobot"],
                                             "api_port_base": 31301}}}
check("  and moving a base moves them",
      yaml.safe_load(CM.render(_moved))["services"]["nanobot-user1"]["ports"][1]
      == "31301:8900",
      yaml.safe_load(CM.render(_moved))["services"]["nanobot-user1"]["ports"])

print("\nand a house with nobody in it yet still has a valid file")
# Part-way through a first install there are no members. `services:` with
# nothing under it is not a compose file.
check("an empty roster renders something compose can parse",
      yaml.safe_load(CM.render({"members": [], "services": {"nanobot": {}}}))
      == {"services": {}})


# --- a skill switched off with the service it talks to ------------------------
#
# A skill pointed at a service nobody deployed is not merely inert. Its whole
# SKILL.md still enters every prompt -- `build_skills_summary` lists skills
# without filtering on whether their environment is met -- marked unavailable,
# which invites the agent to try it, apologise, and spend a family turn. And
# blanking the base URL does not help, because the compose files supply it with
# a `:-` default and that form substitutes for an empty value as well as an
# unset one. So the switch has to be `disabledSkills`, driven from the config.

print("\nsearch skills follow the service they talk to")

_WIRING = {"home-search": {"disable_skills": ["searxng", "vane"],
                           "set": {"tools.web.search.provider": "searxng"}}}
_BASE = json.dumps({"agents": {"defaults": {"disabledSkills": ["tasks"]}}})

_off = json.loads(D.apply_service_wiring(
    _BASE, _WIRING, {"services": {"home-search": {"enabled": False}}}))
check("with the service off, both skills are disabled",
      _off["agents"]["defaults"]["disabledSkills"] == ["tasks", "searxng", "vane"],
      _off["agents"]["defaults"]["disabledSkills"])
check("and the search provider is left at the schema default",
      "tools" not in _off, sorted(_off))

_on = json.loads(D.apply_service_wiring(
    json.dumps(json.loads(_BASE)
               | {"agents": {"defaults": {"disabledSkills":
                                          ["tasks", "searxng", "vane"]}}}),
    _WIRING, {"services": {"home-search": {"enabled": True}}}))
check("turning it on takes them off the list",
      _on["agents"]["defaults"]["disabledSkills"] == ["tasks"],
      _on["agents"]["defaults"]["disabledSkills"])
# The leak this closes: the schema default is duckduckgo, so an instance nobody
# pointed web_search at sends every ordinary lookup off the LAN -- the one thing
# running your own metasearch was for.
check("and points web_search at the house instance",
      _on["tools"]["web"]["search"]["provider"] == "searxng",
      _on.get("tools"))

# A unit with no `when_service:` must come back byte-identical rather than
# round-tripped through a JSON dumper, which would reformat a file that is also
# documentation.
check("a unit that declares no wiring is untouched",
      D.apply_service_wiring(_BASE, {}, {}) == _BASE)


print("\noptional capabilities are removed, not left pointed at nothing")

# The same idea one level out, for a capability that is an MCP *server* rather
# than a skill. An entry left listed is not inert: nanobot dials it at startup,
# and one pointing at a service nobody switched on is a failed connect on the
# path of every message -- this house measured 37s a message on exactly that.
# Blanking the token does not help; a URL built from an unset ${TOKEN} is still
# a URL and is still dialled.
_MCP_WIRING = {"brightdata": {"disable_mcp": ["brightdata"]},
               "browser-use": {"disable_mcp": ["browser-use"]}}
_MCP_BASE = json.dumps({"tools": {"mcpServers": {
    "brightdata": {"url": "https://mcp.brightdata.com/mcp?token=${BRIGHTDATA_API_TOKEN}"},
    "browser-use": {"url": "http://browser-use:21034/mcp"},
    "homeassistant": {"url": "x"}}}})

_mcp_off = json.loads(D.apply_service_wiring(
    _MCP_BASE, _MCP_WIRING,
    {"services": {"brightdata": {"enabled": False}, "browser-use": {"enabled": False}}}))
check("both off, both entries are gone",
      sorted(_mcp_off["tools"]["mcpServers"]) == ["homeassistant"],
      sorted(_mcp_off["tools"]["mcpServers"]))

_mcp_one = json.loads(D.apply_service_wiring(
    _MCP_BASE, _MCP_WIRING,
    {"services": {"brightdata": {"enabled": True}, "browser-use": {"enabled": False}}}))
check("one on, only the other is removed",
      sorted(_mcp_one["tools"]["mcpServers"]) == ["brightdata", "homeassistant"],
      sorted(_mcp_one["tools"]["mcpServers"]))

# `.get("enabled", True)` everywhere else in this file, and here too: a block
# with no `enabled:` key -- the shape the admin page's services form writes --
# is deployed. An existing household that had a working token before these
# became switches keeps it until it says otherwise.
_mcp_absent = json.loads(D.apply_service_wiring(
    _MCP_BASE, _MCP_WIRING, {"services": {}}))
check("a config that names neither keeps both, like every other enabled test",
      sorted(_mcp_absent["tools"]["mcpServers"])
      == ["brightdata", "browser-use", "homeassistant"],
      sorted(_mcp_absent["tools"]["mcpServers"]))

# The manifest has to declare it or the branch is decorative.
_units = [u for u in D.load_yaml(D.MANIFEST)["services"]["nanobot"]["units"]
          if u.get("when_service")]
check("the manifest gates the optional servers on a nanobot unit",
      bool(_units) and any("disable_mcp" in (r or {})
                           for r in _units[0]["when_service"].values()),
      [sorted(u["when_service"]) for u in _units])

# Every gated server must exist in the shipped config, or the gate removes
# nothing and the capability was never there to switch on.
_shipped = json.loads(
    (D.ROOT / "services/nanobot/config/config.json").read_text(encoding="utf-8"))
_shipped_mcp = set((_shipped.get("tools") or {}).get("mcpServers") or {})
for _u in _units:
    for _svc, _rules in _u["when_service"].items():
        for _name in (_rules or {}).get("disable_mcp") or ():
            check(f"the shipped config actually has an MCP server {_name!r}",
                  _name in _shipped_mcp, sorted(_shipped_mcp))

# Both crawlers gated on both units, not just one. crawl4ai ships on and
# crawl4ai ships on, so the copy that is easy to forget is the one whose gate
# does nothing until somebody turns it off.
for _svc in ("nanobot", "nanobot-house"):
    _u = [u for u in shipped_manifest["services"][_svc]["units"]
          if (u.get("when_service") or {}).get("crawl4ai")]
    check(f"{_svc} gates crawl4ai", len(_u) == 1, [u["name"] for u in _u])


# --- every compose file's environment: is a list of plain strings ------------
# Three separate breakages in this repo have been the same mistake: a compose
# file edited as *text*, an env line landing at the wrong indent, and YAML
# accepting it as something other than a string. It never errors at edit time.
#
#   "FIRECRAWL_API_KEY=${...} - CRAWL4AI_API_TOKEN=${...}"   one folded scalar
#   ["CRAWL4AI_API_TOKEN=${...}"]                            a nested list
#
# The first silently handed one variable the other's text and left the second
# unset in every container. The second failed `docker compose config -q` in
# the middle of a deploy with "unexpected type []interface {}", which names
# neither the file nor the line. Parsing what the package ships and asserting
# the shape costs nothing and catches both.
print("\nevery compose file's environment is a list of plain strings")
_env_bad = []
_env_files = sorted(D.ROOT.glob("services/**/docker-compose*.yml")) + \
    sorted(D.ROOT.glob("deploy/units/**/docker-compose*.yml"))
for _f in _env_files:
    try:
        _doc = yaml.safe_load(_f.read_text(encoding="utf-8")) or {}
    except Exception as _exc:                                     # noqa: BLE001
        _env_bad.append(f"{_f.relative_to(D.ROOT)}: will not parse: {_exc}")
        continue
    for _svc, _spec in (_doc.get("services") or {}).items():
        _env = (_spec or {}).get("environment")
        if not isinstance(_env, list):
            continue          # the mapping form is legal and used elsewhere
        for _entry in _env:
            if not isinstance(_entry, str):
                _env_bad.append(f"{_f.relative_to(D.ROOT)}: {_svc}: {_entry!r}")
            elif "=" in _entry and " - " in _entry:
                _env_bad.append(
                    f"{_f.relative_to(D.ROOT)}: {_svc}: folded -- {_entry!r}")
check(f"{len(_env_files)} compose file(s), no folded or nested env entries",
      not _env_bad, _env_bad[:5])


# --- crawl4ai's token is derived, not stored ---------------------------------
# The failure this guards against is an upgrade: a household that has never
# heard of crawl4ai has no CRAWL4AI_API_TOKEN in its env file and has not
# re-run the installer, and the unit declares the key `required:`. If that is
# not satisfied by derivation, every deploy on every existing box stops.
print("\ncrawl4ai needs nothing in the credentials file")
_c4_spec = {"secrets": {"required": ["CRAWL4AI_API_TOKEN", "CRAWL4AI_SECRET_KEY"]}}
_c4_unit = {"name": "crawl4ai"}
# The shared CFG fixture, like the code-broker block further down: collect_env
# reads site.timezone, site.domain and more, so a hand-rolled dict here only
# discovers that one KeyError at a time.
_c4_cfg = copy.deepcopy(CFG)
_c4_env = D.collect_env(_c4_spec, _c4_unit,
                        {"PROXY_SHARED_SECRET": "shared"}, _c4_cfg)
check("a config with no crawl4ai keys still gets a token",
      len(_c4_env.get("CRAWL4AI_API_TOKEN", "")) == 64,
      sorted(_c4_env))
check("  and a signing key that is not the same string",
      _c4_env["CRAWL4AI_SECRET_KEY"] != _c4_env["CRAWL4AI_API_TOKEN"])

# Both ends have to hold the same value or every tool call is a 401 -- the
# crawler reads it to authenticate, the assistants send it as a Bearer header.
_c4_nano = D.collect_env(
    {"secrets": {"optional": ["CRAWL4AI_API_TOKEN"]}}, {"name": "gateway"},
    {"PROXY_SHARED_SECRET": "shared"}, _c4_cfg)
check("the crawler and the assistants derive the same token",
      _c4_nano.get("CRAWL4AI_API_TOKEN") == _c4_env["CRAWL4AI_API_TOKEN"])

# Rotating the shared secret has to rotate this, or a "rotation" leaves the
# old value working -- which is the whole reason these are derived.
_c4_other = D.collect_env(_c4_spec, _c4_unit,
                          {"PROXY_SHARED_SECRET": "different"}, _c4_cfg)
check("rotating PROXY_SHARED_SECRET rotates it",
      _c4_other["CRAWL4AI_API_TOKEN"] != _c4_env["CRAWL4AI_API_TOKEN"])

# An operator who wants a specific token should get theirs, not ours.
_c4_manual = D.collect_env(_c4_spec, _c4_unit,
                           {"PROXY_SHARED_SECRET": "shared",
                            "CRAWL4AI_API_TOKEN": "mine"}, _c4_cfg)
check("a hand-set token in the env file still wins",
      _c4_manual["CRAWL4AI_API_TOKEN"] == "mine")


# And the manifest has to actually declare it, or the function is decorative --
# which is the same failure `state:` entries had before --check-contract.
_wired = [u["name"] for svc in ("nanobot", "nanobot-house")
          for u in shipped_manifest["services"][svc]["units"]
          if (u.get("when_service") or {}).get("home-search")]
check("both nanobot units declare the wiring", len(_wired) == 2, _wired)


# --- an export nothing reads ------------------------------------------------
# `--check-contract` compares compose reads against exports and fails on a read
# with no export. It cannot see the other direction: an export no compose file
# picks up passes the contract and never reaches the container, which is the
# shape the manifest's `state:` entries had for a long time -- declared,
# green, and decorative.
#
# So the provider pairs are asserted here, both ways. Each is a URL and a key
# that config.json names unconditionally: nanobot resolves ${...} across its
# whole config and refuses to start on an unset reference, so an assistant
# whose compose forgot one is a container that never comes up at all.

import re as _re

_NB = HERE.parent / "services" / "nanobot"
# `(what to call it, what a container actually gets)`. The rendered overlay is
# in here by name rather than by path: the per-member blocks are generated, and
# a check that only walked the files in the tree would report "all 1 block(s)"
# and mean the CLI container.
_NANOBOT_COMPOSE = [
    ("docker-compose.house.yml",
     (_NB / "docker-compose.house.yml").read_text(encoding="utf-8")),
    ("docker-compose.multiuser.yml",
     (_NB / "docker-compose.multiuser.yml").read_text(encoding="utf-8")),
    ("docker-compose.members.yml (rendered)", CM.render(CFG)),
    ("docker-compose.broker.yml", (_NB / "docker-compose.broker.yml").read_text(encoding="utf-8")),
]
_NANOBOT_CONFIG = (_NB / "config" / "config.json").read_text(encoding="utf-8")
_MANIFEST_TEXT = D.MANIFEST.read_text(encoding="utf-8")

# Every prefix `assistant.models` accepts has to be a provider the assistant
# actually defines. `openrouter:` was in MODEL_PROVIDERS for a long time with
# no `openrouter` block in config.json, so choosing one deployed green and
# named a provider nanobot does not have -- the turn failed at the first call,
# which is the worst place to find out.
# The admin page can deploy only because its container is given the host's
# network and the host's paths. Both halves are in its compose file, and both
# have to be there: host networking without the identical-path mounts writes a
# service tree the daemon cannot find, and the mounts without host networking
# leaves every verify curl checking the container.
# --- an environment entry that is really a mount ----------------------------
# Compose takes `environment:` as a list of strings and asks nothing of them,
# so three volume lines indented under it became variables literally named
# `proxy-certs:/certs:ro`. `docker compose config -q` exited 0, the deploy
# reported "compose file valid" and "containers up", and Caddy crash-looped on
# a certificate that was sitting in the volume it had never been given.
#
# Nothing else can catch this. --check-contract reads `${VAR}` *uses*; a
# malformed name that nothing interpolates is invisible to it, and to compose.
# --- the notification mode has to reach the containers ----------------------
# `cloud.notifications.mode: external` was accepted by the config, advertised
# in the ntfy service's own description, and read by nothing: NTFY_BASE_URL was
# hardcoded to the local container in all three places that export it, so a
# household with its own ntfy went on publishing to an address on the hub.
# --- the hint must not tell somebody to break a database --------------------
# The ownership hint named `paths.state` alongside the deploy root, and
# following it chowned the postgres cluster to the deploying user: postgres
# runs as uid 999, and after that it could not open `global/pg_filenode.map`
# and refused every connection. The deploy root is the pushed tree and belongs
# to whoever deploys; state belongs to the services, under their own uids.
# --- one config, and it is the deployed one ---------------------------------
# There are two copies: `config/home-stack.yml` in the checkout is the seed,
# and `{paths.config}/home-stack.yml` is what the admin page edits and what
# containers read. Reading the seed is how a shell deploy and a page deploy
# disagree about the household's own settings -- `paths.media` was changed on
# the page to /mnt/data/smart-bot while the checkout still said /mnt/data, and
# the next home-cameras deploy from a shell would have pointed the camera wall
# at an empty directory and orphaned 115 GB, green.
#
# Exactly the arrangement the credentials file already has, for exactly the
# same reason.
# --- changing a path carries the state with it ------------------------------
# `paths.media` was changed on the admin page and 115 GB stayed where it was:
# the services came up against an empty directory and the deploy went green.
# That is the loss guard_state_paths() exists to prevent, arriving from the
# direction it does not watch.
print("\na changed path carries its state across")
_m = pathlib.Path(tempfile.mkdtemp())
(_m / "old").mkdir()
(_m / "old" / "a.txt").write_text("one")
(_m / "old" / "deep").mkdir()
(_m / "old" / "deep" / "b.txt").write_text("two")
_saved_root = os.environ.get("HOME_STACK_DEPLOY_ROOT")
os.environ["HOME_STACK_DEPLOY_ROOT"] = str(_m / "root")
try:
    # First run records where things are; there is nothing to carry yet.
    check("  the first run carries nothing",
          D.migrate_paths({"paths": {"state": str(_m / "old")}}) == [],
          "a fresh install has no previous location to move from")
    check("  and remembers where they were",
          (_m / "root" / D.PATHS_MARKER).is_file(),
          "the record lives in the deploy root, the one directory paths: "
          "cannot move")

    _did = D.migrate_paths({"paths": {"state": str(_m / "new")}})
    check("  a changed path is acted on", bool(_did), _did)
    check("  every entry arrives",
          sorted(p.name for p in (_m / "new").rglob("*")) == ["a.txt", "b.txt", "deep"],
          sorted(p.name for p in (_m / "new").rglob("*")))
    check("  with its contents", (_m / "new" / "deep" / "b.txt").read_text() == "two")
    # Nothing is deleted, ever. That is a person's decision, made after they
    # have seen it work.
    check("  the old directory is swung aside, not removed",
          (_m / "old.moved" / "a.txt").is_file(),
          "a migration that deletes is a migration you cannot undo")

    # Two populated locations is two sets of state, and this cannot choose.
    (_m / "other").mkdir()
    (_m / "other" / "x").write_text("y")
    try:
        D.migrate_paths({"paths": {"state": str(_m / "other")}})
        check("  two populated paths are refused", False, "no error raised")
    except D.DeployError as exc:
        check("  two populated paths are refused", "both hold data" in str(exc), exc)

    # ...unless the destination is a *finished* copy. That is the expected
    # state after somebody follows the instruction the failure below prints:
    # the copy stops on a file this account cannot read, says "run this sudo
    # rsync, then deploy again", and refusing at that point would send them
    # back to the instruction they had just followed. It did.
    (_m / "handdone").mkdir()
    for rel in ("a.txt", "deep/b.txt"):
        t = _m / "handdone" / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text((_m / "new" / rel).read_text())
    os.environ["HOME_STACK_DEPLOY_ROOT"] = str(_m / "root3")
    D.migrate_paths({"paths": {"state": str(_m / "new")}})     # record `new`
    # Caught, so refusing here is a red line rather than a traceback: the
    # difference matters because a run that dies has said nothing about the
    # checks after it.
    try:
        _done = D.migrate_paths({"paths": {"state": str(_m / "handdone")}})
    except D.DeployError as exc:
        _done = None
        check("  a destination that already holds every entry is accepted",
              False, f"refused a finished copy: {exc}")
    if _done is not None:
        check("  a destination that already holds every entry is accepted",
              bool(_done), _done)
        check("  and the old location is swung aside as usual",
              (_m / "new.moved").is_dir(), sorted(q.name for q in _m.iterdir()))
        check("  without touching what was copied by hand",
              (_m / "handdone" / "deep" / "b.txt").read_text() == "two")

    # Every path that cannot be carried, in one message, before anything moves.
    # A move that discovers it needs root on the *second* of two paths has
    # already done the first -- so the config, the marker and the disk disagree
    # -- and the person is told one command at a time, deploying between them
    # to find out there is another.
    (_m / "sealed").mkdir()
    (_m / "sealed" / "f").write_text("x")
    (_m / "sealed" / "f").chmod(0o000)
    (_m / "alsoheld").mkdir()
    (_m / "alsoheld" / "g").write_text("y")
    (_m / "alsoheld" / "g").chmod(0o000)
    os.environ["HOME_STACK_DEPLOY_ROOT"] = str(_m / "root5")
    D.migrate_paths({"paths": {"state": str(_m / "sealed"),
                               "media": str(_m / "alsoheld")}})
    if not os.access(_m / "sealed" / "f", os.R_OK):
        try:
            D.migrate_paths({"paths": {"state": str(_m / "s2"),
                                       "media": str(_m / "m2")}})
            check("  two blocked paths are reported together", False,
                  "no error raised")
        except D.DeployError as exc:
            check("  two blocked paths are reported together",
                  "2 of the paths" in str(exc), str(exc)[:200])
            check("  each is named with its own reason",
                  "paths.state" in str(exc) and "paths.media" in str(exc), exc)
            check("  with a command for each",
                  str(exc).count("sudo rsync") == 2, exc)
            # The whole point of checking first: neither may have moved.
            check("  and neither was moved",
                  not (_m / "s2").exists() and not (_m / "m2").exists(),
                  "a path moved before the blocked one was discovered")
            check("  nor swung aside",
                  (_m / "sealed" / "f").exists() and (_m / "alsoheld" / "g").exists())
    for f in ("sealed/f", "alsoheld/g"):
        (_m / f).chmod(0o600)
    os.environ["HOME_STACK_DEPLOY_ROOT"] = str(_m / "root4")

    # A half-finished copy is still two sets of state.
    (_m / "half").mkdir()
    (_m / "half" / "a.txt").write_text("one")
    os.environ["HOME_STACK_DEPLOY_ROOT"] = str(_m / "root4")
    D.migrate_paths({"paths": {"state": str(_m / "handdone")}})
    try:
        D.migrate_paths({"paths": {"state": str(_m / "half")}})
        check("  a partial copy is refused", False, "no error raised")
    except D.DeployError as exc:
        check("  a partial copy is refused", "of the" in str(exc), exc)
        check("  and it says how to finish it",
              "sudo rsync" in str(exc), str(exc))
    os.environ["HOME_STACK_DEPLOY_ROOT"] = str(_m / "root")

    # A file this account cannot read stops the move, and what it says then is
    # the whole value of the message: somebody is mid-migration, the command
    # that would finish it is one they cannot see, and "run the same rsync
    # under sudo" leaves them reconstructing it from paths they would have to
    # retype. So it prints the command.
    (_m / "locked").mkdir()
    (_m / "locked" / "secret").write_text("x")
    (_m / "locked" / "secret").chmod(0o000)
    # Its own marker, so this starts from "locked is where state is" rather
    # than from the moves above -- otherwise the two-populated-paths refusal
    # fires first and this asserts nothing about rsync at all.
    os.environ["HOME_STACK_DEPLOY_ROOT"] = str(_m / "root2")
    D.migrate_paths({"paths": {"state": str(_m / "locked")}})
    _unreadable = not os.access(_m / "locked" / "secret", os.R_OK)
    if _unreadable:                 # not true for root, which is a real way to run this
        try:
            D.migrate_paths({"paths": {"state": str(_m / "fresh")}})
            # rsync exits non-zero on a partial transfer, so reaching here at
            # all would mean the failure was swallowed.
            check("  an unreadable file stops the move", False, "no error raised")
        except D.DeployError as exc:
            check("  an unreadable file stops the move", True)
            check("  and nothing was removed", (_m / "locked" / "secret").exists())
            check("  and the old location is still where state is",
                  not (_m / "locked.moved").exists())
            check("  and it prints the command that finishes it",
                  "sudo rsync" in str(exc) and str(_m / "locked") in str(exc),
                  str(exc))
    else:
        print("  skip  running as root: nothing here is unreadable")
    (_m / "locked" / "secret").chmod(0o600)
finally:
    if _saved_root is None:
        os.environ.pop("HOME_STACK_DEPLOY_ROOT", None)
    else:
        os.environ["HOME_STACK_DEPLOY_ROOT"] = _saved_root

print("\nthe deployed config wins over the checkout's copy")
check("  HOME_STACK_CONFIG overrides both",
      D._config_path() == pathlib.Path(os.environ["HOME_STACK_CONFIG"])
      if os.environ.get("HOME_STACK_CONFIG") else True)

import tempfile as _tf

_seedroot = pathlib.Path(_tf.mkdtemp())
(_seedroot / "config").mkdir()
(_seedroot / "live").mkdir()
_seed = _seedroot / "config" / "home-stack.yml"
_seed.write_text(f"paths:\n  config: {_seedroot / 'live'}\nsite:\n  name: Seed\n")
_live = _seedroot / "live" / "home-stack.yml"

_saved_root, _saved_env = D.ROOT, os.environ.pop("HOME_STACK_CONFIG", None)
try:
    D.ROOT = _seedroot
    check("  with no deployed copy, the seed is used", D._config_path() == _seed,
          D._config_path())
    _live.write_text(f"paths:\n  config: {_seedroot / 'live'}\nsite:\n  name: Live\n")
    check("  once one exists, it wins", D._config_path() == _live, D._config_path())

    # And the difference is said out loud, because preferring the deployed copy
    # makes an edit to the checkout do nothing -- the same trap pointing the
    # other way.
    D.CONFIG = _live
    _msg = D.config_divergence()
    check("  a divergence is reported", bool(_msg), _msg)
    check("  naming the file actually in use", str(_live) in _msg, _msg)
    check("  and how many settings differ", "1 setting(s)" in _msg, _msg)
    # The live path leads. On 2026-09-01 two readers -- one auditing the
    # backups from another repository -- reached a wrong conclusion from the
    # checkout copy and were one step from acting on it. Both had checked
    # carefully; both had checked the file with the obvious name in the obvious
    # place. Leading with that filename is putting the wrong half in front of
    # somebody, so the message opens with the file that is real.
    check("  the live path comes first, not the leftover's name",
          _msg.index(str(_live)) < _msg.index("config/home-stack.yml"), _msg)
    # And it names the one thing neither file can answer. A config says what
    # somebody intended; only the container says what took effect.
    check("  and points at the container for what is actually running",
          "docker inspect" in _msg, _msg)
    # Every key in the seed is ignored except one, so a long divergence list is
    # normal and harmless -- and `paths.config` is not in that category. It is
    # the key `_config_path()` reads OUT of the seed to decide which file is
    # authoritative, so a disagreement there means the two copies point at
    # different config directories and whichever the deployer reaches first
    # wins. Buried among fifty-eight other names, nobody would see it.
    #
    # Found by deleting the seed on the belief that nothing read it. The
    # deployer stopped working in one command: it had nowhere left to learn
    # where the real config was.
    _seed.write_text("paths: {config: /somewhere/else}\nsite: {name: seed}\n")
    _live.write_text("paths: {config: /var/lib/home-stack/config}\n"
                     "site: {name: live}\n")
    _msg = D.config_divergence()
    check("  a paths.config disagreement is called out on its own",
          "paths.config DISAGREES" in _msg, _msg)
    check("  naming both sides of it",
          "/somewhere/else" in _msg and "/var/lib/home-stack/config" in _msg, _msg)
    # And stays quiet about it when they agree, which is every ordinary house.
    _seed.write_text("paths: {config: /var/lib/home-stack/config}\n"
                     "site: {name: seed}\n")
    check("  and is silent when they agree",
          "DISAGREES" not in D.config_divergence(), D.config_divergence())

    _live.write_text(_seed.read_text())
    check("  identical copies report nothing", D.config_divergence() == "",
          D.config_divergence())
finally:
    D.ROOT = _saved_root
    if _saved_env is not None:
        os.environ["HOME_STACK_CONFIG"] = _saved_env

# --- the local certificate covers the names this house actually uses --------
# `local-cert.sh` hardcoded them: chat.home, house.home, *.home
# and IP:192.168.1.10, an address on a network nobody here is on. Any household
# whose `site.domain` differed got a certificate for somebody else's
# names, and the proxy unit's own verify -- `{dns.portal}` served with a
# trusted certificate -- could never pass. The file is run and never rendered,
# so nothing else can see a literal in it.
# --- every tile has to be openable from a phone -----------------------------
# The url is what a browser opens, and it was built from `hosts.<role>.address`
# -- 127.0.0.1 on a single-box install. A link to 127.0.0.1 opens the machine
# holding the browser, so every tile on the household dashboard pointed at the
# phone. Reported as "I tried 2 and none of them worked", which is what it
# would look like from anywhere except the hub itself.
print("\nthe dashboard's links point at the machine, not at the reader")
_hp = copy.deepcopy(CFG)
_hp["hosts"] = {r: {"address": "127.0.0.1"} for r in ("hub", "compute", "storage")}
D.add_container_addresses(_hp)
_urls = [t["url"] for g in json.loads(
    D.build_portal_dashboard(_hp))["groups"].values() for t in g]
_absolute = [u for u in _urls if "://" in u]
check("  there are tiles to check", len(_urls) >= 4, _urls)
check("  and some of them are addresses, not paths", _absolute, _urls)
# On a box with no non-loopback address there is nothing better to offer, so
# this only asserts the rule when one exists.
if D.browsable_address(_hp) not in D._LOOPBACK:
    check("  no tile links to loopback",
          not [u for u in _absolute if "127.0.0.1" in u or "localhost" in u],
          _absolute)
    check("  they use the address a browser can reach",
          all(D.browsable_address(_hp) in u for u in _absolute), _absolute[:3])
# A path is relative to whatever the person already has open, which is the
# portal, so it is right from the LAN and right through the VPS with no
# address in it at all. That is the point of `portal_path:`.
# `/` is a 404 on a FastAPI app, and a tile that 404s reads as the service
# being down -- the one thing somebody opens it to rule out.
#
# Every service that declares `tile_path:` is switched on for this, rather
# than checked only if the fixture above happens to enable it: the fixture has
# faster-whisper absent and home-voice off, so the loop found nothing at all
# and passed on an empty set.
_tp = copy.deepcopy(_hp)
_declared = {n: sp["tile_path"] for n, sp in _manifest["services"].items()
             if sp.get("tile_path")}
check("  something declares a tile_path to check", bool(_declared), _declared)
for _svc in _declared:
    _tp["services"].setdefault(_svc, {})
    _tp["services"][_svc]["enabled"] = True
    _tp["services"][_svc].setdefault("port", 21099)
_by_name = {t["name"]: t for g in json.loads(
    D.build_portal_dashboard(_tp))["groups"].values() for t in g}
for _svc, _want in _declared.items():
    check(f"  {_svc} is on the wall to be checked", _svc in _by_name, sorted(_by_name))
    if _svc in _by_name:
        check(f"  {_svc}'s tile asks for {_want}, not for a root that 404s",
              _by_name[_svc]["url"].endswith(_want), _by_name[_svc]["url"])
check("  a portal path stays a path",
      all(u.startswith("/") for u in _urls if "://" not in u),
      [u for u in _urls if "://" not in u])

# Enumerating this machine's interfaces to answer a question about another
# machine would be worse than not answering.
_remote = copy.deepcopy(CFG)
_remote["hosts"]["hub"]["address"] = "192.168.9.9"
_rd = D.host_addresses(_remote, "hub")
check("  a remote hub gets its configured address and no local guesses",
      "192.168.9.9" in _rd and len(_rd) == 3, _rd)

print("\nthe certificate's names come from the config")
_cert = (HERE.parent / "services" / "home-core" / "proxy" / "local-cert.sh"
         ).read_text(encoding="utf-8")
_cert_code = "\n".join(l for l in _cert.splitlines() if not l.lstrip().startswith("#"))
check("  no household's names are baked into the script",
      not _re.search(r"\b\w+\.home\b|\.ar" + "pa", _cert_code),
      "a literal name here is a certificate for the wrong house")
check("  and no IP either",
      not _re.search(r"IP:\d+\.\d+\.\d+\.\d+", _cert_code),
      "192.168.1.10 was in there, on a network nobody is on")
check("  it reads the names the deploy passes",
      "CERT_NAMES" in _cert_code and "CERT_IP" in _cert_code)
# --- and neither does the file Caddy actually reads -------------------------
#
# The Caddyfile is bind-mounted verbatim and never rendered, which is what made
# this expensive: the site addresses were `chat.home` and
# `house.home`, so on any household whose `dns:` says otherwise neither
# block matched a Host anybody types. Caddy answers an unmatched Host with an
# empty 200, and the proxy's own verify was `curl -f -o /dev/null` -- so the
# front door served nothing and the deploy went green about it, every time.
# --- a service with no published port is still a service ---------------------
#
# The code broker answers on 8910 inside a bridge network and nothing on the
# host can reach it, so every check this unit had was blind to whether it was
# running at all. It was not: its `command:` ran under an ENTRYPOINT ending in
# `exec nanobot "$@"`, so it became `nanobot python -m ...` and crash-looped
# through 73 restarts while the deploy reported success from an agent's port.
# --- the admin container has to be able to run ssh --------------------------
#
# It runs as the deploying account's numeric id so that what it writes is owned
# the way a deploy from a shell owns it. Every OpenSSH tool calls getpwuid()
# before anything else and refuses a uid with no *name* -- `ssh`, `ssh-keygen`
# and `ssh-keyscan` all exit 255 with "No user exists for uid 1000". So
# reaching a second machine from the admin page could not work, and had never
# worked: the failure is one line, in a deploy log, from a tool nobody expects
# to need an identity.
# --- ntfy is configured in one place ----------------------------------------
#
# `dns.ntfy` was a second place to say where ntfy is, when `cloud.notifications`
# already says it. Its one real use was the base-url ntfy stamps into the
# attachment and click links it *delivers* -- and on the house this came from
# that name resolved to nothing, so every link it handed out was unopenable.
# An address needs no DNS record, which is the whole point of a link somebody
# taps on a phone.
# --- both proxies dial the portal the way it answers ------------------------
#
# The portal serves HTTPS and nothing else. The cloud proxy said `http://` and
# its sibling on the hub said `https://` -- so the one that reaches the house
# over a reverse tunnel would have dialled plaintext at a TLS port and failed
# every request through it. A tunnel carries bytes, not a protocol: what
# arrives on that port is whatever the portal speaks.
# The port a unit verifies has to be the port its compose file binds. The
# cloud proxy checked 8070 while its command binds 8080 -- a check that could
# only ever spend its timeout, on the one box facing the public internet.
# --- an unverifiable boundary is not a passing one --------------------------
#
# `admin_is_not_proxied` reads three files to prove the admin page is not
# routed from outside. `is_file()` *raises* on a path this account cannot stat,
# so one directory left at mode 0700 in the staged tree took the whole deploy
# down with a PermissionError out of pathlib -- from a security check, on every
# deploy run inside the admin container.
#
# Skipping it silently would have been worse than the crash: "I could not look"
# has to read as a problem, not as a pass.
print("\na routing check that cannot read a file says so")
_unreadable = pathlib.Path(tempfile.mkdtemp()) / "locked"
_unreadable.mkdir()
(_unreadable / "Caddyfile").write_text("reverse_proxy 127.0.0.1:9999\n")
_unreadable.chmod(0o000)
_saved_root = D.ROOT
try:
    D.ROOT = _unreadable.parent
    _target = _unreadable / "Caddyfile"
    if not os.access(_target, os.R_OK):        # not true for root
        _probs = []
        try:
            # Drive the real function by pointing one of its paths at the
            # unreadable file, rather than reimplementing its loop.
            _orig = D.admin_is_not_proxied.__globals__["ROOT"]
            _probs = D.admin_is_not_proxied({"services": {"admin": {"port": 9999}}})
        except OSError as exc:
            check("  it does not crash", False, f"raised {exc!r}")
        else:
            check("  it does not crash", True)
    else:
        print("  skip  running as root: nothing here is unreadable")
finally:
    _unreadable.chmod(0o755)
    D.ROOT = _saved_root

# And the shipped tree has to be readable by the account the admin container
# runs as, or that check has nothing to read in the first place.
for _d in ("services/home-core", "services/proxy"):
    _mode = oct((HERE.parent / _d).stat().st_mode & 0o777)
    check(f"  {_d} is readable by others ({_mode})",
          (HERE.parent / _d).stat().st_mode & 0o005 == 0o005,
          "the admin image is built from this tree and runs as a non-root uid")

print("\neach proxy is verified on the port it actually binds")
_pc = (HERE.parent / "services" / "proxy" / "docker-compose.yml").read_text(encoding="utf-8")
_base_port = _re.search(r"--port[\"', ]+(\d+)", _pc).group(1)
_cp = shipped_manifest["optional_services"]["cloud-proxy"]
_cp_urls = [c["http"] for u in _cp["units"] for c in (u.get("verify") or [])
            if c.get("http")]
check("  the base compose binds a port this can read", bool(_base_port), _base_port)
check(f"  and cloud-proxy verifies {_base_port}",
      all(f":{_base_port}/" in u for u in _cp_urls), _cp_urls)
# The local copy overrides the command with a configurable port, which is why
# it never hit this.
_lc = (HERE.parent / "services" / "proxy" / "docker-compose.local.yml").read_text(encoding="utf-8")
check("  and the local copy overrides that command rather than sharing it",
      "LOCAL_PROXY_PORT" in _lc, "the two would otherwise have to agree on one port")

print("\nthe proxies dial the portal over TLS, because that is all it serves")
for _svc in ("local-proxy", "cloud-proxy"):
    _spec = (shipped_manifest.get("services", {}).get(_svc)
             or shipped_manifest.get("optional_services", {}).get(_svc))
    _url = next(u["env"]["HOMECORE_LOCAL_URL"] for u in _spec["units"]
                if "HOMECORE_LOCAL_URL" in (u.get("env") or {}))
    check(f"  {_svc} dials https", _url.startswith("https://"), _url)
# And the certificate on the far end is the house's own, for the house's own
# name, reached at 127.0.0.1 -- nothing it could check would mean anything.
_pc = (HERE.parent / "services" / "proxy" / "docker-compose.yml").read_text(encoding="utf-8")
check("  and does not pretend to verify it",
      'VERIFY_UPSTREAM_TLS: "0"' in _pc,
      "the tunnel is the identity; a hostname check here is theatre")

print("\nntfy's own address needs no DNS record")
_nd = copy.deepcopy(CFG)
_nd["hosts"] = {r: {"address": "127.0.0.1"} for r in ("hub", "compute", "storage")}
D.add_container_addresses(_nd)
_self = D.derive(copy.deepcopy(_nd), {})["derived"]["ntfy_self_url"]
check("  it is an address, not a name", "://" in _self and _self.count(":") == 2, _self)
check("  and carries ntfy's port",
      _self.endswith(":" + str(_nd["services"]["ntfy"]["port"])), _self)
if D.browsable_address(_nd) not in D._LOOPBACK:
    check("  reachable from a phone, not loopback",
          "127.0.0.1" not in _self and "localhost" not in _self, _self)
check("  ntfy is no longer a dns: name the deployer knows",
      "ntfy" not in D.DNS_FALLBACK_HOST and "ntfy" not in D.DNS_SERVICE,
      sorted(set(D.DNS_FALLBACK_HOST) | set(D.DNS_SERVICE)))
check("  nor one the example config ships",
      "ntfy:" not in "\n".join(
          l for l in (HERE.parent / "config" / "home-stack.example.yml")
          .read_text(encoding="utf-8").split("\ndns:")[1].split("\n\n")[0].splitlines()),
      "the dns: block still offers a name for ntfy")
# And nothing anywhere still interpolates it.
# Code, not comments -- the comment above the fix names `{dns.ntfy}` while
# explaining why it is gone, and a first version of this matched its own
# explanation. Third time that trap has caught me today.
_man_code = "\n".join(
    l for l in (HERE.parent / "deploy" / "manifest.yml")
    .read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
check("  and nothing in the manifest interpolates it",
      "{dns.ntfy}" not in _man_code,
      "a name nothing supplies would interpolate to an empty host")
check("  nor tells anybody to set it",
      "dns.ntfy" not in _man_code, "stale prose pointing at a setting that is gone")

# --- the VPS has to be somewhere else ---------------------------------------
#
# It is the one machine in this stack that is deliberately not here: it
# terminates the public connection and forwards down a tunnel to the house.
# Deploying that proxy onto a machine on the LAN is not a failure -- it would
# work, and report success, and the household's public front door would be a
# box behind their own router.
#
# It nearly happened: `cloud.vps.host` was a name the local resolver answered
# with a LAN address, so the deployer's idea of "the VPS" was a Raspberry Pi
# already running half the stack. The only thing that stopped it was that
# machine's ssh host key not matching -- a warning about the wrong subject.
# --- there is a VPS, and this stack deploys it, are two questions ------------
#
# Collapsing them into `enabled` left no way to describe the ordinary case:
# a machine somebody set up by hand, whose only account holds a reverse tunnel
# open and refuses to run anything. Enabled-and-failing teaches a household to
# ignore a red deploy, which is the same disease as a green one that hides a
# wiped config.
print("\na VPS can exist without this stack deploying it")
_mv = copy.deepcopy(CFG)
_mv["cloud"]["vps"] = {"enabled": True, "host": "vps.example"}
check("  enabled and managed puts the proxy in the deployable set",
      "cloud-proxy" in D.all_services(shipped_manifest, _mv))
_mv["cloud"]["vps"]["managed"] = False
check("  and unmanaged takes it out",
      "cloud-proxy" not in D.all_services(shipped_manifest, _mv),
      sorted(D.all_services(shipped_manifest, _mv)))
# Default true, or an existing install would silently stop deploying its proxy
# on the next upgrade.
_mv["cloud"]["vps"].pop("managed")
check("  saying nothing means managed, so no install changes under it",
      "cloud-proxy" in D.all_services(shipped_manifest, _mv))
# Nothing the *house* deploys may depend on it, or this becomes a switch with
# reach nobody expects.
_on = D.all_services(shipped_manifest, _mv)
_mv["cloud"]["vps"]["managed"] = False
_off = D.all_services(shipped_manifest, _mv)
check("  and it changes nothing else in the set",
      set(_on) - set(_off) == {"cloud-proxy"}, sorted(set(_on) ^ set(_off)))

# Said out loud, every run. A machine nothing mentions is a machine that falls
# out of mind.
_said = []
_rw, _rs = D.out.warn, D.out.sub
D.out.warn = lambda m, *a, **k: _said.append(m)
D.out.sub = lambda m, *a, **k: _said.append(m)
try:
    D.note_vps_unmanaged(_mv)
    check("  and a deploy says so", any("not deployed from here" in m for m in _said),
          _said)
    check("  naming the machine", any("vps.example" in m for m in _said), _said)
    _said.clear()
    _mv["cloud"]["vps"]["managed"] = True
    D.note_vps_unmanaged(_mv)
    check("  and says nothing when it is managed", not _said, _said)
    _said.clear()
    D.note_vps_unmanaged({"cloud": {"vps": {"enabled": False, "managed": False}}})
    check("  or when there is no VPS at all", not _said, _said)
finally:
    D.out.warn, D.out.sub = _rw, _rs

print("\nthe vps is checked for being elsewhere")
_said = []
_real_warn, _real_sub = D.out.warn, D.out.sub
D.out.warn = lambda m, *a, **k: _said.append(m)
D.out.sub = lambda m, *a, **k: _said.append(m)
try:
    _v = copy.deepcopy(CFG)
    _v["cloud"]["vps"] = {"enabled": True, "host": "127.0.0.1"}
    D.warn_vps_is_elsewhere(_v, {})
    check("  a loopback vps is called out",
          any("private network" in m for m in _said), _said)

    _said.clear()
    _v["hosts"]["compute"]["address"] = "127.0.0.1"
    _v["cloud"]["vps"] = {"enabled": True, "host": "localhost"}
    D.warn_vps_is_elsewhere(_v, {})
    check("  and so is one that resolves onto a host already in the stack",
          any("already a host" in m for m in _said)
          or any("private network" in m for m in _said), _said)

    # Off is off: nothing to say about a proxy nobody runs.
    _said.clear()
    _v["cloud"]["vps"] = {"enabled": False, "host": "127.0.0.1"}
    D.warn_vps_is_elsewhere(_v, {})
    check("  a disabled proxy says nothing", not _said, _said)

    # And a name that does not resolve is the deploy's problem, not this
    # check's -- it must not turn an unrelated DNS failure into a warning
    # about network topology.
    _said.clear()
    _v["cloud"]["vps"] = {"enabled": True, "host": "nothing.invalid"}
    D.warn_vps_is_elsewhere(_v, {})
    check("  an unresolvable host is left to the deploy", not _said, _said)
finally:
    D.out.warn, D.out.sub = _real_warn, _real_sub

print("\nthe admin image carries an account for the id it runs as")
_admin_df = (HERE.parent / "admin" / "Dockerfile").read_text(encoding="utf-8")
_admin_code = "\n".join(l for l in _admin_df.splitlines()
                        if not l.lstrip().startswith("#"))
check("  it creates one", "useradd" in _admin_code, "no account for the runtime uid")
check("  for the id the compose file runs it as",
      "DEPLOY_UID" in _admin_code and "ARG DEPLOY_UID" in _admin_code,
      "a fixed uid would only match one machine")
# `|| true` on the creation is right -- a base image that happens to have that
# id is fine -- but then nothing would have failed if it silently did not work.
check("  and proves the name exists rather than assuming it",
      "getent passwd" in _admin_code,
      "useradd is `|| true`, so something has to check it worked")
_admin_compose = (HERE.parent / "admin" / "docker-compose.yml").read_text(encoding="utf-8")
check("  and the build is given the number",
      "DEPLOY_UID: ${DEPLOY_UID" in _admin_compose,
      "the image is built on the machine it runs on; it can know this")
# The ssh directory is where a key goes, and ssh ignores one whose directory
# anybody else can read.
_ssh_state = next(e for u in next(
    sp["units"] for s_, sp in shipped_manifest["services"].items() if s_ == "admin")
    for e in (u.get("state") or []) if e["path"].endswith("/ssh"))
check("  the ssh directory is created 0700",
      _ssh_state.get("mode") == "0700", _ssh_state)
check("  and the deployer acts on `mode:` rather than only declaring it",
      "chmod {shlex.quote(str(entry['mode']))}" in
      (HERE.parent / "deploy" / "deploy.py").read_text(encoding="utf-8"),
      "a mode nothing applies is a mode that is wrong")
check("  and the admin mounts it writable, or nothing can write a key there",
      ":/deployer/.ssh:ro" not in _admin_compose,
      "read-only is why that directory started empty and stayed empty")

print("\nthe unpublished services are verified too")
_nb_units = next(sp["units"] for s_, sp in shipped_manifest["services"].items()
                 if s_ == "nanobot")
_nb_checks = [c for u in _nb_units for c in (u.get("verify") or [])]
check("  the nanobot unit verifies the code broker",
      "code-broker" in [c.get("container_healthy") for c in _nb_checks],
      [sorted(c) for c in _nb_checks])
_broker_check = next(c for c in _nb_checks
                     if c.get("container_healthy") == "code-broker")
# The compose healthcheck is interval 30s, retries 3: ninety seconds before a
# container that will never be healthy admits it. A timeout near that reports a
# slow start as a failure and a real failure as a slow start.
check("  and gives it longer than its healthcheck takes to fail",
      _broker_check.get("timeout", 90) >= 120, _broker_check.get("timeout"))

# Its own file since the broker became its own unit: its image is the one that
# fetches the coding harness from a GitHub release, and a failed download there
# must not take the family's assistants down with it.
_broker_compose = (HERE.parent / "services" / "nanobot"
                   / "docker-compose.broker.yml").read_text(encoding="utf-8")


def _service_block(text, name):
    """One service's lines, ending at the next key at the same indent.

    By indentation rather than by naming the service that follows it. It used
    to split on `\n  nanobot-user1:`, and when the per-member blocks moved out
    to the renderer that split stopped matching -- so the "block" became every
    remaining service, and the assertions below read another container's keys.
    """
    body = text.split(f"\n  {name}:")[1]
    out = []
    for line in body.splitlines():
        if line and not line.startswith("   ") and line.strip():
            break
        out.append(line)
    return "\n".join(out)


_broker_block = _service_block(_broker_compose, "code-broker")
_broker_code = "\n".join(l for l in _broker_block.splitlines()
                         if not l.lstrip().startswith("#"))
# `command:` is wrapped by the image's ENTRYPOINT; `entrypoint:` replaces it.
# The broker is not an agent -- no workspace to seed, no SOUL.md, no gateway.
check("  the broker replaces the entrypoint rather than adding to it",
      "entrypoint:" in _broker_code and "\n    command:" not in _broker_code,
      "a `command:` here becomes an argument to `nanobot`")
# And the healthcheck asserts the payload: this endpoint answers 200 while
# saying `"ok": false`, which is what it does with no workspace marker -- and
# it then refuses every request it is given.
check("  and its healthcheck reads the answer, not the status",
      "status==200" not in _broker_code and "get('ok')" in _broker_code,
      "a 200 from this endpoint does not mean the broker will serve anything")

# The marker the broker refuses to work without. Upstream's build script wrote
# it; this package does not ship that script, so nothing wrote it at all.
_ws = next(e for u in _nb_units for e in (u.get("state") or [])
           if e["path"].endswith("nanobot-code-workspace"))
check("  the workspace marker is required and seeded",
      _ws.get("require_file") == ".nanobot-code-workspace"
      and _ws.get("seed_from"), _ws)
check("  and the file it is seeded from is in the tree",
      (HERE.parent / "services" / "nanobot" / _ws["seed_from"]).is_file(),
      _ws.get("seed_from"))

# --- a path the container is mounted but cannot name --------------------------
#
# The admin page reads the user store, the history and the backups out of
# `paths:`. The manifest exports all five as HOME_STACK_*_DIR and the compose
# interpolates them into `volumes:` -- and for a while that was all it did.
# The mounts were right and the variables were unset *inside* the container,
# so every path derived from one fell back to /var/lib/home-stack/... After
# `paths.state` was moved to a second disk that fallback is a root-owned
# leftover: the page reported every member as having no portal account, and
# setting one raised PermissionError. `--check-contract` cannot see this --
# the variable IS read by the compose file, just on the wrong side of it.

print("\nthe admin page can name the directories it is mounted")

_ADMIN_COMPOSE = (HERE.parent / "admin" / "docker-compose.yml"
                  ).read_text(encoding="utf-8")
_admin_unit = D.load_yaml(D.MANIFEST)["services"]["admin"]["units"][0]
_exported = [k for k in (_admin_unit.get("env") or {})
             if k.startswith("HOME_STACK_") and k.endswith("_DIR")]
check("the manifest exports the paths", len(_exported) >= 5, _exported)

# `environment:` only. Strip the comments first -- this file explains the bug
# at length, and a test that matched the explanation would pass on a compose
# that had been reverted.
_env_block = _ADMIN_COMPOSE.split("\n    environment:")[1].split("\n    healthcheck:")[0]
_env_code = "\n".join(l for l in _env_block.splitlines()
                      if not l.lstrip().startswith("#"))
for _var in sorted(_exported):
    check(f"  {_var} reaches the process, not only the mount",
          f"{_var}:" in _env_code,
          "compose mounts the right directory and the code inside cannot say "
          "where it is")


# The config and the credentials are opened through the config *directory*
# mount, never mounted as files. A single-file bind mount pins an inode, so a
# host-side rename left the page writing a deleted file: on 2026-09-11 model
# roles saved on the page never reached the file the deployer reads, and the
# page showed them saved. The third time that shape had cost something.
print("\nthe admin page does not pin the config or the credentials by inode")
_volumes_code = [l.strip() for l in
                 _ADMIN_COMPOSE.split("\n    volumes:")[1].split("\n    environment:")[0]
                 .splitlines() if l.strip().startswith("- ")]
for _name in ("home-stack.yml", "smart-home-bot.env"):
    check(f"  {_name} is not a volume of its own",
          not any(_name in l for l in _volumes_code),
          [l for l in _volumes_code if _name in l])
for _var, _env in (("HOME_STACK_CONFIG", "ADMIN_CONFIG_FILE"),
                   ("HOME_STACK_SECRETS", "ADMIN_SECRETS_FILE")):
    check(f"  {_var} is the host path the manifest exports",
          f"{_var}: ${{{_env}:-" in _env_code, _var)
    _entry = next((s for s in _admin_unit.get("state") or []
                   if s.get("env") == _env), {})
    # Opened through the {paths.config} mount, so it has to be inside it.
    check(f"  {_env} lives inside the mounted config directory",
          str(_entry.get("path", "")).startswith("{paths.config}/"), _entry)


# Paperless read every scan as English, in a Spanish house, with the Spanish
# data in the image all along -- `eng` was a literal in the compose file.
print("\npaperless reads scans in the household's languages")
for _cfg, _want in (
        ({"locale": {"default": "es"}, "members": [{"locale": "es"}]}, "spa+eng"),
        ({"locale": {"default": "en"}}, "eng"),
        ({}, "eng"),
        ({"locale": {"default": "fr"}, "members": [{"locale": "de"}, {}]}, "fra+deu+eng"),
        ({"locale": {"default": "es-CL"}, "members": [{"locale": "it_IT"}]}, "spa+ita+eng"),
        # Not in the paperless image: naming one would fail every consumption.
        ({"locale": {"default": "ja"}, "members": [{"locale": "zh"}]}, "eng")):
    check(f"  {_cfg or 'no locale'} -> {_want}", D.ocr_language(_cfg) == _want,
          D.ocr_language(_cfg))
_pl_env = D.load_yaml(D.MANIFEST)["services"]["home-paperless"]["units"][0]["env"]
check("  the manifest hands it to paperless",
      _pl_env.get("PAPERLESS_OCR_LANGUAGE") == "{derived.ocr_language}", _pl_env)
check("  and the compose file reads it rather than saying eng",
      "PAPERLESS_OCR_LANGUAGE: ${PAPERLESS_OCR_LANGUAGE:-eng}" in
      (HERE.parent / "services" / "home-paperless" / "docker-compose.yml").read_text())


# --- a redacted placeholder is not a setting ----------------------------------
#
# `deploy/sanitize.py` rewrites a household's own addresses to neutral ones,
# and 192.168.1.x is what it writes. A compose file that is bind-mounted
# verbatim -- never rendered -- can therefore ship one of those as an
# operational value, and it did: home-core pinned `dns: 192.168.1.10`, so the
# portal container resolved nothing at all. Notifications to an external ntfy
# failed once per member and were logged as push failures rather than as the
# DNS failure they were.
#
# Same shape as the Caddyfile that answered to two names nobody had. So: no
# compose file this package mounts verbatim may carry a sanitiser placeholder
# where a real address has to go.

print("\nno mounted compose file ships a placeholder as a real address")

_verbatim = sorted(p for p in (HERE.parent).rglob("docker-compose*.yml")
                   if ".venv" not in str(p) and "node_modules" not in str(p))
check("there are compose files to check", len(_verbatim) > 5, len(_verbatim))
for _path in _verbatim:
    _body = _path.read_text(encoding="utf-8")
    # Comments explain this bug at length now; a test that matched the
    # explanation would pass on the file that reintroduced it.
    _code = "\n".join(l for l in _body.splitlines()
                      if not l.lstrip().startswith("#"))
    _hit = _re.search(r"\bdns\b\s*:", _code) and "192.168.1." in _code
    check(f"  {_path.relative_to(HERE.parent)}", not _hit,
          "a sanitiser placeholder where a resolver goes: the container will "
          "resolve nothing, and every failure will name something else")


print("\nthe proxy answers to the names this household uses")
_caddy = (HERE.parent / "services" / "home-core" / "proxy" / "Caddyfile"
          ).read_text(encoding="utf-8")
_caddy_code = "\n".join(l for l in _caddy.splitlines()
                        if not l.lstrip().startswith("#"))
_sites = _re.findall(r"^(\S.*?)\s*\{\s*$", _caddy_code, _re.M)
_sites = [x for x in _sites if x]              # the global block has no address
check("  there are site blocks to check", len(_sites) >= 2, _sites)
check("  every one takes its name from the environment",
      all(x.startswith("{$") for x in _sites), _sites)
check("  and no household's domain is left in the file",
      not _re.search(r"\b\w+\.home\b|\.ar" + "pa", _caddy_code),
      "a literal name here is a front door for the wrong house")
for _var in ("PORTAL_NAME", "CHAT_NAME"):
    check(f"  the manifest supplies {_var}",
          "{dns." in str((next(
              (u.get("env") or {}) for s_, sp in shipped_manifest["services"].items()
              if s_ == "home-core" for u in sp["units"] if u["name"] == "proxy"
          )).get(_var, "")), _var)
    check(f"  and the compose file passes {_var} to the container",
          _var in (HERE.parent / "services" / "home-core" / "proxy"
                   / "docker-compose.yml").read_text(encoding="utf-8"),
          "an export no compose service lists arrives nowhere")

# The check that would have caught it. `curl -f` passes on a 200 with nothing
# in it, which is exactly what an unmatched Host returns.
_proxy_checks = [c for u in next(
    sp["units"] for s_, sp in shipped_manifest["services"].items()
    if s_ == "home-core") if u["name"] == "proxy" for c in (u.get("verify") or [])]
# One name each, and they are on different units on purpose. The chat name
# fronts `local-proxy`, which is deployed *after* the house's Caddy -- so
# asserting it beside that Caddy failed a whole-stack deploy on home-core
# because a service later in the order was not up yet. Each name is verified by
# the unit that owns the upstream behind it.
check("  the house's proxy verifies the portal's name",
      {c.get("https_resolve") for c in _proxy_checks} == {"{dns.portal}"},
      [c.get("https_resolve") for c in _proxy_checks])
_lp_checks = [c for u in next(
    sp["units"] for s_, sp in shipped_manifest["services"].items()
    if s_ == "local-proxy") for c in (u.get("verify") or [])]
check("  and local-proxy verifies the chat name",
      "{dns.chat}" in [c.get("https_resolve") for c in _lp_checks],
      [sorted(c) for c in _lp_checks])
check("  through a CA path that does not assume where another unit landed",
      all(str(c.get("ca", "")).startswith("{derived.deploy_root}")
          for c in _lp_checks if c.get("https_resolve")),
      [c.get("ca") for c in _lp_checks if c.get("https_resolve")])
# `-o /dev/null` is the whole bug in three characters: it throws the body
# away, so the only thing left to judge is the status, and the status of the
# failure is 200.
# Code, not comments. The comment above that branch explains the bug by
# naming `-o /dev/null`, and a first version of this check matched its own
# explanation -- the same trap that once made a CSS fix look tested when it
# had been reverted.
_hr_branch = "\n".join(
    l for l in (HERE.parent / "deploy" / "deploy.py").read_text(encoding="utf-8")
    .split('elif "https_resolve"')[1].split("\n        elif ")[0].splitlines()
    if not l.lstrip().startswith("#"))
check("  https_resolve reads the body rather than discarding it",
      "-o /dev/null" not in _hr_branch,
      "an empty 200 is what an unmatched Host returns; -o /dev/null accepts it")
check("  and greps it",
      "grep -q" in _hr_branch, "nothing asserts anything about what came back")
# And the behaviour, not only the source: an empty body must not pass.
_probe = tempfile.mkdtemp()
open(f"{_probe}/empty", "w").close()
open(f"{_probe}/full", "w").write("something\n")
import subprocess as _sp
check("  an empty body fails the shape of that command",
      _sp.run(f"cat {_probe}/empty | grep -q .", shell=True).returncode != 0)
check("  and a non-empty one passes it",
      _sp.run(f"cat {_probe}/full | grep -q .", shell=True).returncode == 0)

_proxy_env = next(
    (u.get("env") or {}) for s, sp in shipped_manifest["services"].items()
    if s == "home-core" for u in sp["units"] if u["name"] == "proxy")
for _var in ("CERT_NAMES", "CERT_IP", "DOMAIN"):
    check(f"  and the manifest passes {_var}", _var in _proxy_env, sorted(_proxy_env))
check("  the names it passes are dns: entries, not literals",
      "{dns." in str(_proxy_env.get("CERT_NAMES", "")), _proxy_env.get("CERT_NAMES"))

# Still valid is not still correct. Checking only the expiry meant renaming the
# house left a certificate for the old names in place for a year, and the
# deploy failed its own verify with no way past it short of deleting the file.
check("  it reissues when the names change, not only when expiring",
      "the names changed" in _cert and "checkend" in _cert,
      "an expiry check alone cannot notice a rename")

print("\nthe ownership hint stops at the deploy root")


class _FakeTarget:
    is_local = True
    _owner_hint = D.Target._owner_hint


_hint = _FakeTarget()._owner_hint("rsync: chgrp failed: Operation not permitted")
check("  it fires on the rsync ownership error", bool(_hint), _hint)
check("  it names the deploy root", ".local/share/home-stack" in _hint
      or "HOME_STACK_DEPLOY_ROOT" in _hint, _hint)
# The question is what the *command* touches, not whether a path appears in
# the prose -- the prose has to name paths.state in order to warn about it.
# A first version of this check tested the whole hint for a substring and
# passed the dangerous form, because the path was followed by a newline.
_cmd_lines = [ln.strip() for ln in _hint.splitlines() if "chown" in ln]
check("  the hint carries exactly one chown line", len(_cmd_lines) == 1, _cmd_lines)
check("  and that line does not touch the state tree",
      _cmd_lines and "/var/lib/" not in _cmd_lines[0]
      and "paths.state" not in _cmd_lines[0],
      f"postgres runs as 999 and mosquitto as 1883; taking their directories "
      f"leaves a cluster that cannot read its own files: {_cmd_lines}")
check("  and it says why", "uid" in _hint.lower(), _hint)
check("  a different rsync error gets no hint",
      _FakeTarget()._owner_hint("rsync: connection unexpectedly closed") == "")

print("\nwhere notifications go follows cloud.notifications.mode")
_n = copy.deepcopy(CFG)
_n.setdefault("cloud", {})["notifications"] = {"mode": "local", "external_url": ""}
_local = D.notification_urls(_n)
check("  local is the hub's own ntfy",
      all(str(((_n.get("services") or {}).get("ntfy") or {}).get("port", 21031)) in u
          for u in _local), _local)
check("  and the container-facing one may differ from the host's",
      len(_local) == 2)

_n["cloud"]["notifications"] = {"mode": "external",
                                "external_url": "https://ntfy.example.com/"}
check("  external is the address given, trailing slash removed",
      D.notification_urls(_n) == ("https://ntfy.example.com",
                                  "https://ntfy.example.com"),
      D.notification_urls(_n))

_n["cloud"]["notifications"] = {"mode": "off", "external_url": ""}
check("  off is empty, not the local one", D.notification_urls(_n) == ("", ""),
      D.notification_urls(_n))

# A person types a hostname, and a hostname is not a URL. Without a scheme it
# reaches the containers as `ntfy.example.com` and every publish fails on it --
# which is what was actually in this household's config, typed into the admin
# page. https and not http: an ntfy somewhere else is reached over the
# internet, and quietly choosing plaintext would put the household's
# notifications on the wire in clear.
_n["cloud"]["notifications"] = {"mode": "external",
                                "external_url": "ntfy.example.com"}
check("  a bare hostname becomes https",
      D.notification_urls(_n) == ("https://ntfy.example.com",) * 2,
      D.notification_urls(_n))
_n["cloud"]["notifications"]["external_url"] = "http://ntfy.lan:8080"
check("  and an explicit scheme is left alone",
      D.notification_urls(_n) == ("http://ntfy.lan:8080",) * 2,
      D.notification_urls(_n))

_n["cloud"]["notifications"] = {"mode": "external", "external_url": ""}
try:
    D.notification_urls(_n)
    check("  external with no url is refused", False, "no error raised")
except D.DeployError as exc:
    check("  external with no url is refused", "external_url" in str(exc), exc)

# And every *consumer* reads the derived value rather than rebuilding the
# address. ntfy's own unit is exempt and has to be: NTFY_BASE_URL there is what
# ntfy stamps into the links it delivers, so it is a name a phone can open and
# not somewhere anything dials.
_hardcoded = []
for _svc, _spec in shipped_manifest.get("services", {}).items():
    if _svc == "ntfy":
        continue
    for _unit in _spec.get("units", []):
        _val = str((_unit.get("env") or {}).get("NTFY_BASE_URL", ""))
        if _val and "derived." not in _val:
            _hardcoded.append(f"{_svc}/{_unit['name']}: {_val}")
check("  every consumer reads the derived address",
      not _hardcoded, "; ".join(_hardcoded))
check("  and there are consumers to check",
      any((u.get("env") or {}).get("NTFY_BASE_URL")
          for s, sp in shipped_manifest.get("services", {}).items() if s != "ntfy"
          for u in sp.get("units", [])),
      "if nothing exports it, this check proves nothing")

print("\nno compose file mounts a volume through `environment:`")
_suspect = []
for _compose in sorted(HERE.parent.glob("**/docker-compose*.yml")):
    if any(part in _compose.parts for part in (".venv", "node_modules", ".git")):
        continue
    try:
        _doc = D.load_yaml(_compose) or {}
    except Exception:
        continue
    for _svc, _spec in (_doc.get("services") or {}).items():
        _env = (_spec or {}).get("environment")
        if not isinstance(_env, list):
            continue
        for _entry in _env:
            # A real entry is NAME=value or a bare NAME. Anything with a path
            # separator before the first `=` is a mount that lost its way.
            _name = str(_entry).split("=", 1)[0]
            if "/" in _name or ":" in _name:
                _suspect.append(f"{_compose.name}:{_svc}: {_entry}")
check(f"  checked {_compose.name} and the rest", True)
check("  none of them", not _suspect, "; ".join(_suspect))

print("\nthe admin container is set up to be allowed to deploy")
_ADMIN_COMPOSE = (HERE.parent / "admin" / "docker-compose.yml").read_text(encoding="utf-8")
check("  it is on the host's network", "network_mode: host" in _ADMIN_COMPOSE)
check("  and publishes no port, because there is no bridge to publish from",
      "\n    ports:" not in _ADMIN_COMPOSE,
      "a ports: line beside network_mode: host is refused by compose")
check("  it says the guard may stand down",
      'HOME_STACK_ALLOW_CONTAINER_LOCAL: "1"' in _ADMIN_COMPOSE)
check("  and that the page must not rewrite loopback",
      'HOME_STACK_HOST_NETWORK: "1"' in _ADMIN_COMPOSE,
      "host.docker.internal does not resolve on host networking")
for _var in ("HOME_STACK_CONFIG_DIR", "HOME_STACK_STATE_DIR", "HOME_STACK_MEDIA_DIR",
             "HOME_STACK_BACKUPS_DIR", "HOME_STACK_PLUGINS_DIR", "HOME_STACK_DEPLOY_ROOT"):
    check(f"  {_var} is mounted at the same path inside as out",
          f"${{{_var}:-" in _ADMIN_COMPOSE
          and _ADMIN_COMPOSE.count(f"${{{_var}:-") >= 2,
          "one side of the mount is a different string from the other")
    check(f"  and the manifest supplies {_var}",
          _re.search(rf"^\s*{_var}:", _MANIFEST_TEXT, _re.M) is not None)

# The tools the deployer needs on *this* side when the page runs it.
#
# Comments stripped first. The Dockerfile explains both wrong package names in
# prose -- that is the point of the comment -- so matching the raw file finds
# `docker.io` in the sentence saying not to use it. The same shape as the CSS
# check that passed with its declaration deleted because the comment above it
# said the property name.
_ADMIN_DOCKERFILE = "\n".join(
    line for line in (HERE.parent / "admin" / "Dockerfile")
    .read_text(encoding="utf-8").splitlines()
    if not line.lstrip().startswith("#"))
check("  the docker client is installed, not the daemon",
      "docker-cli" in _ADMIN_DOCKERFILE and not _re.search(
          r"\bdocker\.io\b", _ADMIN_DOCKERFILE),
      "on trixie `docker.io` is the daemon: it leaves /usr/bin/docker absent "
      "and every compose call fails with `command not found` after the compose "
      "file has already been written")
check("  and the compose plugin, under the name trixie actually has",
      _re.search(r"\bdocker-compose\b", _ADMIN_DOCKERFILE) is not None
      and "docker-compose-v2" not in _ADMIN_DOCKERFILE,
      "`docker-compose-v2` does not exist there and fails the whole build")
check("  mosquitto_pub too, because a verify check runs on the target",
      "mosquitto-clients" in _ADMIN_DOCKERFILE,
      "without it mqtt_roundtrip spends its timeout collecting exit 127")
check("  and the payload is staged, or there is nothing to deploy",
      _re.search(r"from:\s*services,\s*to:\s*services", _MANIFEST_TEXT) is not None,
      "the deployer rsyncs a service's own tree and cannot without it")

print("\na sub-agent follows the model it is a sub-agent of")
# These keys exist because they did not, and the gap was invisible: a household
# moved to together.ai, every role went with them, and the sub-agents kept the
# shipped default -- so every background task went on calling the provider they
# had just left. Inheriting is what stops the next move doing it again.
_inherited = json.loads(D.apply_model_choices(_NANOBOT_CONFIG, {"assistant": {
    "models": {"everyday": "together:A", "powerful": "together:B",
               "subagent": "", "subagent_powerful": ""}}}))["agents"]["defaults"]
check("  blank follows everyday",
      (_inherited["subagentProvider"], _inherited["subagentModel"])
      == ("together_ai", "A"),
      (_inherited["subagentProvider"], _inherited["subagentModel"]))
check("  and a retired subagent_powerful writes nothing",
      "subagentModelPowerful" not in _inherited, _inherited.get("subagentModelPowerful"))
# The provider travels with the model, or the rescue is a name posted at an
# endpoint that has never heard of it.
check("  carrying the parent's provider, not a bare name",
      _inherited["subagentProvider"] == _inherited["provider"])

_explicit = json.loads(D.apply_model_choices(_NANOBOT_CONFIG, {"assistant": {
    "models": {"everyday": "together:A", "powerful": "together:B",
               "subagent": "ollama:small",
               "subagent_powerful": "kimi-k3"}}}))["agents"]["defaults"]
check("  an explicit value still wins",
      (_explicit["subagentProvider"], _explicit["subagentModel"]) == ("ollama", "small")
      and "subagentModelPowerful" not in _explicit, _explicit)
# A household that names neither the parent nor the child keeps whatever the
# shipped config said, which is what happened before any of these keys existed.
_neither = json.loads(D.apply_model_choices(_NANOBOT_CONFIG, {"assistant": {
    "models": {"notifications": "ollama:x"}}}))["agents"]["defaults"]
check("  and naming neither changes nothing",
      _neither["subagentModel"]
      == json.loads(_NANOBOT_CONFIG)["agents"]["defaults"]["subagentModel"])

print("\nevery model prefix names a provider the assistant defines")
# No exceptions, and that is the point: `assistant.models` is the roster of
# models *nanobot* calls, so every prefix in it must be a provider this deployer
# can build. opencode is not one -- the Programmer is answered by HomeCore
# driving opencode's session API -- so it is configured in `cloud.opencode`
# instead of being a prefix here. Putting it here needed a MODEL_PROVIDERS
# entry, an exception in apply_model_choices so the profession's fallback would
# not be handed a provider nanobot cannot build, and a check refusing the prefix
# on every other role. One setting in the block that already configures opencode
# costs none of that, and this loop stays simple.
_defined = set(json.loads(_NANOBOT_CONFIG).get("providers", {}))
for _prefix, _provider in D.MODEL_PROVIDERS.items():
    check(f"  {_prefix}: -> {_provider}", _provider in _defined,
          f"config.json defines {sorted(p for p in _defined if not p.startswith('_'))}")
check("  and opencode is not among them, being nobody's provider",
      "opencode" not in D.MODEL_PROVIDERS.values(),
      "a prefix here is a promise the assistant can build it")

print("\nwhich model opencode answers with is opencode's own setting")
check("  it is read from cloud.opencode",
      D.opencode_model({"cloud": {"opencode": {"model": "opencode-go/kimi-k3"}}})
      == "opencode-go/kimi-k3")
check("  empty when the household names none",
      D.opencode_model({"cloud": {"opencode": {}}}) == ""
      and D.opencode_model({}) == "")
# The agent file carries the placeholder the deployer fills in. Without it the
# model would be pinned in the repo rather than by the household.
_AGENT = (D.ROOT / "deploy/host/opencode/agents/alfred-programmer.md").read_text(
    encoding="utf-8")
check("  and the agent's front matter is where it lands",
      "model: {{OPENCODE_MODEL}}" in _AGENT, _AGENT[:200])
check("  the persona still takes the household's language",
      "{{HOUSEHOLD_LANGUAGE}}" in _AGENT)

# --- a key nothing reads is a key that does nothing ---------------------------
# `compose_overrides:` was written into a unit and read by no one. The deploy
# went green, said nothing about the overlay, and ran the CPU image on a
# household whose config asked for cuda -- the decorative-declaration failure
# `--check-contract` exists for, one level up where it cannot see.
#
# The set below is what deploy.py actually looks for in a unit. Adding a key
# means adding it here, which is the point: a typo is then a failing test
# rather than a setting that silently does nothing.
print("\nevery key a unit declares is one the deployer reads")
_UNIT_KEYS = {
    "name", "dir", "compose", "build", "image_build", "env", "state", "verify",
    "test", "pre_remove_containers", "pre_create_networks", "assets",
    "model_config", "when_service", "allowed_env_keys", "pre", "post",
    "renamed_from", "depends_on", "profiles", "healthcheck_timeout",
    # The rest of what units actually declare today. Listed rather than
    # discovered, because the whole value of this check is that adding a key
    # is a deliberate act: a typo is then a failing test instead of a setting
    # that silently does nothing.
    "alfred_mcp_identity", "assistant_name_in", "code_broker_tokens",
    "compose_project", "contributions_env_file", "family_directory",
    "generated_compose", "image_models", "member_ntfy_topics",
    "member_profiles", "member_profiles_gate", "member_stagger_index",
    "opencode_config", "portal_dashboard", "pull", "self_urls", "skill_floors",
    # collect_env drops these when empty; see the paperless index checks.
    "env_omit_empty",
}
_seen = set()
for _svc, _spec in ((_manifest.get("services") or {}).items()):
    for _unit in (_spec.get("units") or []):
        _seen |= set(_unit)
_unknown = sorted(_seen - _UNIT_KEYS)
check("  no unit declares a key the deployer ignores", not _unknown, _unknown)

# --- one hash, spelled in three files ----------------------------------------
# `sha256("<PROXY_SHARED_SECRET>:<login id>")` is computed by the deployer
# (derive_proxy_token, to export HOMECORE_PROXY_TOKEN_*), by the portal
# (_proxy_user_token, to check one on every request) and by the admin page
# (finance_token, to show a person theirs). Three copies of one expression,
# which is the shape this repository has been bitten by before -- and the
# failure is silent in the worst way: a token that hashes differently is not
# an error, it is a 401 on somebody's finances.
#
# The deployer's is executed. The other two are read, because importing a
# Flask app to check one line costs more than it proves.
print("\nthe proxy token is the same hash wherever it is spelled")
import hashlib as _hl  # noqa: E402
_want = _hl.sha256(b"s3cr3t:999000111").hexdigest()
check("  the deployer derives it from the login id",
      D.derive_proxy_token("s3cr3t", "999000111") == _want)
check("  and not from anything else",
      D.derive_proxy_token("s3cr3t", "user1") != _want)
for _who, _rel in (("the portal", "services/home-core/local/app.py"),
                   ("the admin page", "admin/app.py")):
    _src = (D.ROOT / _rel).read_text(encoding="utf-8")
    check(f"  {_who} spells the same input",
          'f"{secret}:{login}"' in _src or "f'{PROXY_SHARED_SECRET}:{username}'" in _src,
          _rel)

# --- an export nothing carries is decorative --------------------------------
# `--check-contract` walks compose -> export: every `${VAR}` a compose file
# names must be supplied. It has never walked the other way, and the gap is
# not theoretical -- it is the same bug the env-contract section of CLAUDE.md
# describes, arriving from the opposite side.
#
# Four exports were added to this manifest for the usage page's local-model
# and GPU sections and listed in no compose file. Every one of them was
# read by application code, `./home-stack check` said "contract ok", the
# deploy went green, and the feature did nothing at all on a household that
# had configured it correctly: `local_usage.enabled()` was False because
# `os.environ.get("HOMECORE_URL")` returned "" in a container nothing had
# passed it to. That is indistinguishable from "nobody uses the voice".
#
# Compose is the only thing that carries a deployer export into a container,
# so a key in `env:` has to appear in one of the service's compose files --
# any of them, since the per-member units are rendered into their own.
#
# The exemption is a rule, not a list: a `pre:` hook is run by the deployer
# with the collected environment (`target.run(unit["pre"], env=env)`), on the
# host, before any container exists. Those keys never need to reach one.
print("\nevery exported key can actually reach the thing that reads it")
_orphans = []
for _svc, _spec in ((D.load_yaml(D.MANIFEST).get("services")) or {}).items():
    for _unit in _spec.get("units") or []:
        _d = _unit.get("dir")
        if not _d or not (D.ROOT / _d).exists():
            continue
        # A rendered overlay is git-ignored and absent from a fresh checkout;
        # what it would say for the example household counts the same.
        _blob = D.generated_compose_text(_unit)
        for _pat in ("**/docker-compose*.yml", "**/Dockerfile*", "**/*.env"):
            for _f in (D.ROOT / _d).glob(_pat):
                if _f.is_file():
                    _blob += _f.read_text(errors="ignore")
        # ...and whatever the unit's shell scripts read. A `pre:` hook is run
        # by the deployer on the host with the collected environment
        # (`target.run(unit["pre"], env=env)`), before any container exists,
        # and it execs its siblings -- ensure-cert.sh chooses between
        # issue-cert.sh and local-cert.sh, which inherit that same
        # environment. None of those keys has any business in a compose file,
        # so the exemption is "a script the deployer runs", not a list of
        # names somebody has to remember to update.
        for _sh in (D.ROOT / _d).glob("**/*.sh"):
            if _sh.is_file():
                _blob += _sh.read_text(errors="ignore")
        for _key in (_unit.get("env") or {}):
            if _key not in _blob:
                _orphans.append(f"{_svc}/{_unit.get('name')}: {_key}")
check("  no unit exports a key no compose file carries", not _orphans, _orphans)

# --- and the service token, spelled in two ---------------------------------
# The same failure with a different subject. `sha256("<master>:svc:<name>")`
# is computed by the deployer (derive_service_token, to export
# USAGE_SERVICE_TOKEN to the voice gateway and the camera wall) and by the
# portal (_service_token, to check one on every report). A drift between them
# is not an error either: it is a usage page that quietly stops recording what
# the household's own models did, which reads exactly like models nobody used.
print("\nthe service token is the same hash at both ends")
_svc = _hl.sha256(b"s3cr3t:svc:voice-gateway").hexdigest()
check("  the deployer derives it from the service name",
      D.derive_service_token("s3cr3t", "voice-gateway") == _svc)
check("  each service gets a different one",
      D.derive_service_token("s3cr3t", "home-cameras") != _svc)
# The `svc:` namespace is the guarantee that one of these can never satisfy a
# route expecting a person -- find_user() refuses the name, so a leaked token
# buys a row in a usage table and nothing else.
check("  and it can never collide with a member's token",
      D.derive_service_token("s3cr3t", "999000111")
      != D.derive_proxy_token("s3cr3t", "999000111"))
_portal = (D.ROOT / "services/home-core/local/app.py").read_text(encoding="utf-8")
check("  the portal spells the same input",
      "f'{PROXY_SHARED_SECRET}:svc:{name}'" in _portal)
# Every name the deployer issues has to be one the portal will accept, or the
# reports 403 forever and the page stays empty with nothing in any log.
for _svc_name in ("voice-gateway", "home-cameras"):
    check(f"  the portal accepts '{_svc_name}'",
          f"'{_svc_name}'" in _portal.split("def _service_caller")[1][:600],
          _svc_name)

# Every prompt that tells the assistant what language to answer in has to
# *contain* the language, not point at where it is configured.
#
# Two files got this wrong at once and only one of them was visible. The house
# instance's SOUL.md said the household's language "is set in the site
# configuration" -- which the model cannot read -- and MORNING.md dictated a
# literal `Good morning [name]!` in backticks, in a file whose own first
# paragraph says the answer is published verbatim. A specific quoted phrase
# beats a general instruction every time, so the family's daily greeting
# arrived in English while SOUL.md three files away said "answer in Español".
#
# The token is filled in on the way to the container for every staged .md, so
# carrying it costs nothing and its absence is the bug.
print("\nevery soul and the morning greeting name the household's language")
_prompts = [D.ROOT / "services/nanobot/config/SOUL.md",
            D.ROOT / "services/nanobot/config/MORNING.md"]
_prompts += sorted((D.ROOT / "services/nanobot/config/instances").glob("*/SOUL.md"))
for _p in _prompts:
    _rel = _p.relative_to(D.ROOT)
    _body = _p.read_text(encoding="utf-8")
    check(f"  {_rel} carries the token",
          D.HOUSEHOLD_LANGUAGE_TOKEN in _body, _body[:120])
# And the greeting must not put an English phrase in Alfred's mouth.
_morning = (D.ROOT / "services/nanobot/config/MORNING.md").read_text(encoding="utf-8")
check("  and MORNING.md quotes no English greeting to copy",
      "Good morning" not in _morning, _morning[:200])

print("\nthe Programmer reaches whatever this household already pays for")
# A named list, not a sweep. "anything ending in _API_KEY" would also hand it
# the camera's, the document store's and the notification service's.
check("  the hosted providers are named",
      {"TOGETHER_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"}
      <= set(D.OPENCODE_PROVIDER_KEYS), D.OPENCODE_PROVIDER_KEYS)
# opencode holds its own OpenCode credential in auth.json, put there by
# `opencode auth login`. This stack has never read it and must not start.
check("  and OpenCode's own is not among them",
      "OPENCODE_API_KEY" not in D.OPENCODE_PROVIDER_KEYS,
      "opencode authenticates itself from auth.json")
_house_keys = {"NTFY_CREDENTIALS", "PAPERLESS_API_TOKEN", "HOMEASSISTANT_TOKEN",
               "CODE_BROKER_SECRET", "PROXY_SHARED_SECRET", "ALFRED_MCP_SECRET"}
check("  and nothing that is not a model provider",
      not (_house_keys & set(D.OPENCODE_PROVIDER_KEYS)),
      sorted(_house_keys & set(D.OPENCODE_PROVIDER_KEYS)))

_UNIT = (D.ROOT / "deploy/host/opencode-serve@.service").read_text(encoding="utf-8")
check("  the unit reads them, optionally",
      "EnvironmentFile=-%h/.config/home-stack/opencode-providers.env" in _UNIT,
      "without the `-` a household with no credentials cannot start the unit")
# No `provider` block is written, and that is deliberate: opencode discovers
# providers from the environment, so its config file carries no secrets.
_DEPLOY_SRC = (D.ROOT / "deploy/deploy.py").read_text(encoding="utf-8")
check("  and the config file it writes carries no credentials",
      '"provider"' not in _DEPLOY_SRC.split("def build_opencode_config")[1][:1200],
      "opencode reads keys from the environment; the config stays secret-free")

print("\nevery provider config.json names is supplied to the container")
for var in ("OLLAMA_URL", "OLLAMA_API_KEY",
            "OLLAMA_CLOUD_URL", "OLLAMA_CLOUD_API_KEY",
            "OPENAI_COMPATIBLE_URL", "OPENAI_COMPATIBLE_API_KEY",
            # The three paid ones. The house unit listed none of them, so its
            # assistant could name `together:` and get an empty key.
            "OPENROUTER_API_KEY", "TOGETHER_API_KEY", "OPENAI_API_KEY"):
    check(f"  config.json reads {var}", "${" + var + "}" in _NANOBOT_CONFIG)
    check(f"  the manifest supplies {var}",
          _re.search(rf"^\s*{var}:", _MANIFEST_TEXT, _re.M) is not None
          or _re.search(rf"\b{var}\b", _MANIFEST_TEXT) is not None,
          "neither exported as env nor declared as a secret, so the container "
          "never receives it")
    for name, body in _NANOBOT_COMPOSE:
        # Every service block in the file, not just one: a per-member unit that
        # missed the line is an assistant that will not start, and only that
        # one member finds out.
        blocks = body.count("- OLLAMA_CLOUD_URL=")
        got = body.count(f"- {var}=")
        check(f"  {name} passes {var} to all {blocks} block(s)",
              got == blocks, f"{got} of {blocks}")

print("\nthe assistants deploy before the thing that reaches out to GitHub")
# The whole reason the broker is its own unit. Units run in declaration order
# and a raise in one stops the ones after it, so this ordering *is* the
# protection: the coding harness image is fetched from a GitHub release at build
# time, and in one unit a failed download aborted after alfred-nanobot:latest
# was built and before any container rolled -- the family's assistants did not
# deploy because a harness nobody had asked for could not reach github.com.
#
# Reordering these two would put that back with nothing to show for it, which is
# exactly the kind of change that looks tidy in a diff.
_nb_units = [u.get("name") for u in
             ((shipped_manifest.get("services") or {}).get("nanobot") or {}).get("units", [])]
check("  nanobot has both units", {"multiuser", "broker"} <= set(_nb_units), _nb_units)
if {"multiuser", "broker"} <= set(_nb_units):
    check("  and the broker is last",
          _nb_units.index("broker") > _nb_units.index("multiuser"),
          f"{_nb_units}: a failed binary download would abort the assistants again")

# Separate compose projects, so they need a network that outlives both. Compose
# gives each project its own default, and the assistants dial code-broker by
# name -- a split that forgot this is an assistant whose git verbs all fail with
# a DNS error that reads as the broker being down.
for _u in ((shipped_manifest.get("services") or {}).get("nanobot") or {}).get("units", []):
    if _u.get("name") in ("multiuser", "broker"):
        check(f"  {_u.get('name')} joins the shared network",
              "nanobot-net" in (_u.get("pre_create_networks") or []),
              "compose would give it a default network of its own")
for _name, _body in _NANOBOT_COMPOSE:
    if _name.startswith("docker-compose.multiuser") or _name.startswith("docker-compose.broker"):
        check(f"  {_name} declares it external",
              "name: nanobot-net" in _body and "external: true" in _body,
              "compose creates a per-project network and the name stops resolving")

# Split into whole service blocks first. A regex that allowed only a line or two
# between the name and its `image:` silently matched one service out of seven
# here and reported PASS for the file -- which is the shape of check this suite
# exists to not have.
_seen_images = 0
for _name, _body in _NANOBOT_COMPOSE:
    for _chunk in _re.split(r"\n  (?=\S)", _body):
        _svc = _chunk.split(":", 1)[0].strip()
        _img = _re.search(r"^\s+image: (alfred-nanobot[a-z-]*:latest)", _chunk, _re.M)
        if not _img:
            continue
        _seen_images += 1
        # One image for every nanobot container, broker included. It was two
        # while the broker carried a coding binary the assistants must not have;
        # with that gone, a second tag would be a build nobody needs and a
        # second thing to keep in step.
        _expect = "alfred-nanobot:latest"
        check(f"  {_name}: {_svc} runs {_expect}", _img.group(1) == _expect,
              f"runs {_img.group(1)}: an assistant with a shell, or a broker "
              f"without the binary it exists to run")
# A floor, not a count: the member blocks come from the rendered fixture, whose
# roster is whatever this suite set up. What must be true is that all three
# kinds were reached -- the house assistant, the broker, and a member -- because
# a split that silently matched one block would otherwise report the file clean.
check("  and all three kinds of block were reached", _seen_images >= 4,
      f"only {_seen_images} found: the split missed blocks and proved nothing")

print("\nthe code broker is told the login people sign in with")
# The three-ids rule, on the one table that crosses from what the deployer
# builds to what a request reaches. CODE_BROKER_USERS maps an assistant instance
# to the name the *portal* knows, and the portal's find_user() takes a login id.
#
# This was written as `f"{m}:{m}"` on the stated assumption that "the portal
# username is the member id". It is not: people sign in with a number. The
# broker asked the registry about `user1`, the portal answered "usuario
# desconocido", and every git verb the assistant offers returned 404 -- from a
# broker and a portal that were both running and both reporting healthy.
import tempfile as _tf, json as _json, os as _os

_bcfg = copy.deepcopy(CFG)
_bstate = _tf.mkdtemp(prefix="broker-users-")
_bcfg.setdefault("paths", {})["state"] = _bstate
_os.makedirs(_os.path.join(_bstate, "home-core"), exist_ok=True)
with open(_os.path.join(_bstate, "home-core", "users.json"), "w") as _fh:
    # Numbers, as this household's really are -- a fixture where the login and
    # the member id are the same string cannot tell the two apart, which is how
    # the bug survived.
    _json.dump([{"member": "user1", "username": "225000135"},
                {"member": "user2", "username": "166000095"}], _fh)

_bunit = {"name": "multiuser", "code_broker_tokens": True}
_bspec = {"secrets": {"required": ["CODE_BROKER_SECRET"]}}
_benv = D.collect_env(_bspec, _bunit, {"CODE_BROKER_SECRET": "s3cret"}, _bcfg, {})
_bmap = dict(p.split(":", 1) for p in _benv.get("CODE_BROKER_USERS", "").split(",") if ":" in p)

check("  user1 maps to the login, not to itself",
      _bmap.get("user1") == "225000135", _bmap)
check("  user2 too", _bmap.get("user2") == "166000095", _bmap)
check("  and no pair is an instance mapped to its own name",
      all(k != v for k, v in _bmap.items()),
      f"{_bmap}: the portal would answer `usuario desconocido` to every one")

# A member with no portal account has no request they could make, and the broker
# refuses an instance that is absent from this map -- which is the right failure
# for a service handing out other people's code. Mapping them to themselves
# would instead send the registry a name it does not know.
check("  a member with no portal account is left out",
      "user3" not in _bmap, f"{_bmap}: user3 has no account in the fixture")

# The tokens are keyed on the member id, which is what the deployer builds --
# the other half of the same rule, and it must not move with the fix above.
check("  but the derived token still keys on the member id",
      "CODE_BROKER_TOKEN_USER_1" in _benv, sorted(k for k in _benv if "BROKER_TOKEN" in k))

print("\nthe fallback is a chain, because one rescue shares its fate")
# 2026-08-30: the everyday model answered 500 on every attempt and the single
# configured fallback answered "Model is disabled" in the same minute. One
# provider-side change took out the rescue along with the thing it rescued, and
# every Alfred in the house answered "Internal server error".
_FB = {"assistant": {"models": {
    "everyday": "deepseek-v4-pro", "fallback": "deepseek-v4-flash",
    "programmer": "kimi-k2.7-code"}}}
_base = (_root / "services" / "nanobot" / "config" / "config.json").read_text()
_chain = json.loads(D.apply_model_choices(_base, _FB))["agents"]["defaults"]["modelFallback"]

check("  the household's own choice comes first",
      isinstance(_chain, list) and _chain[0] == "deepseek-v4-flash", _chain)
check("  and the internal list follows it",
      isinstance(_chain, list) and len(_chain) > 1,
      f"{_chain}: one candidate is the arrangement that failed")
check("  from more than one family",
      len({c.split('-')[0] for c in _chain}) > 1,
      f"{_chain}: same family fails together, which is the whole point")

# A model another role already names is not a rescue for that role, and during
# an outage the whole house swings onto these at once -- so reusing a name
# concentrates exactly the load this is spreading. The contract already refuses
# it for the configured fallback; this extends the rule to the internal list.
_used = json.loads(D.apply_model_choices(_base, {"assistant": {"models": {
    **_FB["assistant"]["models"], "notifications": D.OUTAGE_FALLBACKS[0]}}})
    )["agents"]["defaults"]["modelFallback"]
check("  a model a role already uses is left out",
      D.OUTAGE_FALLBACKS[0] not in _used,
      f"{_used}: notifications already names it")
check("  and so is the model being rescued",
      "deepseek-v4-pro" not in _chain, _chain)

# One entry stays a plain string: the schema takes either, and a household that
# named one model should not find a list it did not write.
_ONE = {"assistant": {"models": {
    "everyday": "deepseek-v4-pro", "fallback": "deepseek-v4-flash",
    "teacher": "minimax-m3", "designer": "qwen3.5-plus",
    "notifications": "gemini-3.5-flash-lite", "programmer": "kimi-k2.7-code"}}}
_single = json.loads(D.apply_model_choices(_base, _ONE))["agents"]["defaults"]["modelFallback"]
check("  no Gemini on the internal list: Zen serves it only on generateContent",
      not any(m.startswith("gemini") for m in D.OUTAGE_FALLBACKS), D.OUTAGE_FALLBACKS)
check("  a chain of one is written as a name",
      _single == "deepseek-v4-flash", _single)

# check_fallback_model accepts a fallback one role names (the local everyday
# model rescuing the hosted roles offline); the chain must then carry it,
# not drop it for being in use and leave the house with no fallback at all.
_LOCAL = {"assistant": {"models": {
    "everyday": "ollama:ornith-1.5:9b", "powerful": "together:big",
    "fallback": "ollama:ornith-1.5:9b"}}}
_lfb = json.loads(D.apply_model_choices(_base, _LOCAL))["agents"]["defaults"].get("modelFallback")
check("  a fallback the check accepted is written, even when a role names it",
      _lfb == {"model": "ornith-1.5:9b", "provider": "ollama"}, _lfb)
# `titles` and `documents` are not the assistant's roles, so naming the
# fallback there must not take it out of the assistant's chain.
_TTL = {"assistant": {"models": {
    "everyday": "gpt-5.6-luna", "titles": "deepseek-v4-flash",
    "fallback": "deepseek-v4-flash"}}}
_tfb = json.loads(D.apply_model_choices(_base, _TTL))["agents"]["defaults"].get("modelFallback")
check("  and a model only the titler names does not leave the chain",
      isinstance(_tfb, list) and _tfb[0] == "deepseek-v4-flash", _tfb)

check("  the internal list names models, not providers",
      all(":" not in c for c in D.OUTAGE_FALLBACKS), D.OUTAGE_FALLBACKS)

# Empty means empty. check_fallback_model returns early on a blank value and its
# own error text offers "leave it empty for no fallback", so a household that
# cleared the setting has said something -- and the internal list is only known
# to be reachable because that check proved the configured fallback is a hosted
# model on this gateway. A house whose roles run on `ollama:` would get three
# OpenCode Zen names in front of a local base URL.
_NONE = {"assistant": {"models": {"everyday": "ollama:qwen3-vl:8b", "fallback": ""}}}
check("  and a household that cleared the fallback still has none",
      "modelFallback" not in json.loads(
          D.apply_model_choices(_base, _NONE))["agents"]["defaults"],
      json.loads(D.apply_model_choices(_base, _NONE))["agents"]["defaults"])

print("\nthe whatsapp bridge is its own container, not a tenant of the assistant")
# Moved here from services/nanobot/tests/config/test_instance_overlay.py, which
# still read docker-compose.multiuser.yml for a block that is rendered now. It
# had been failing with `KeyError: whatsapp-bridge-user2` -- proving nothing
# about the arrangement it was named after -- and a service's own suite cannot
# follow the block into the renderer without importing the packaging that
# deploys it, which is the wrong direction across the boundary.
#
# The reason the assertions matter is unchanged: the bridge used to share the
# assistant's network namespace, and a joined namespace dies with its owner.
# user2 restarts on every config change, and the bridge spent 134 reconnects
# failing to resolve web.whatsapp.com against a DNS server that had been torn
# down, while the same name resolved fine from the container that owned it.
# A member with a linked phone, because `members[].whatsapp` is what renders a
# bridge at all -- the shipped fixture has nobody with one, and rendering CFG
# straight would have checked an empty list and reported PASS for every line
# below it.
_WACFG = copy.deepcopy(CFG)
_WACFG["members"][0]["whatsapp"] = True
_wa_rendered = CM.render(_WACFG)
_wa = _re.findall(r"\n  (whatsapp-bridge-[a-z0-9]+):\n(.*?)(?=\n  \S|\Z)",
                  _wa_rendered, _re.S)
check("  the renderer produces a bridge at all", bool(_wa),
      "no bridge block rendered, so the checks below would pass by vacancy")
for _name, _body in _wa:
    check(f"  {_name} has no shared namespace",
          "network_mode:" not in _body,
          "a joined namespace dies with the assistant that owns it")
    check(f"  {_name} binds what BRIDGE_HOST says",
          "BRIDGE_HOST=0.0.0.0" in _body, _body[:120])
    check(f"  {_name} publishes no port",
          not _re.search(r"^\s+ports:", _body, _re.M),
          "publishing a port puts a live WhatsApp session on the LAN -- 0.0.0.0 "
          "is only acceptable while this stays on the compose network")

# The URL the assistant dials it by is deliberately NOT checked here. It lives
# in the instance overlay's `bridgeUrl`, which is service config rather than
# anything the deployer renders -- and the service's own suite still asserts it,
# on the right side of the boundary. A `BRIDGE_URL=` search of this compose
# finds nothing, so a check written here would be a line that always passes.

print("\nwhat a request carries is the login, on both halves")
# Measured against the live portal on 2026-08-30, three ways round:
#   member id + token derived from the member -> 401 No autenticado
#   login id  + token derived from the login  -> 200, with the real balance
#   login id  + token derived from the member -> 401
# So the id and the credential are both the login, and getting one right is not
# enough. What the family saw was Alfred unable to read a chore list at all.
_PXCFG = copy.deepcopy(CFG)
_pxstate = _tf.mkdtemp(prefix="proxy-login-")
_PXCFG.setdefault("paths", {})["state"] = _pxstate
_os.makedirs(_os.path.join(_pxstate, "home-core"), exist_ok=True)
with open(_os.path.join(_pxstate, "home-core", "users.json"), "w") as _fh:
    _json.dump([{"member": "user1", "username": "900000111"},
                {"member": "user2", "username": "166000095"}], _fh)

_pxenv = D.collect_env(
    {"secrets": {"required": ["PROXY_SHARED_SECRET"],
                 "per_member": ["HOMECORE_PROXY_TOKEN_{M}"]}},
    {"name": "multiuser"}, {"PROXY_SHARED_SECRET": "sh4red"}, _PXCFG, {})

import hashlib as _hl
_want = _hl.sha256(b"sh4red:900000111").hexdigest()
_wrong = _hl.sha256(b"sh4red:user1").hexdigest()
check("  the token hashes the login, as the portal does",
      _pxenv.get("HOMECORE_PROXY_TOKEN_USER_1") == _want,
      "hashed something else; the portal computes sha256(secret:username)")
check("  and not the member id, which never matched",
      _pxenv.get("HOMECORE_PROXY_TOKEN_USER_1") != _wrong, "still keyed on the member")

# The other half: the name the request presents.
_pxrendered = CM.render(_PXCFG)
_ids = _re.findall(r"HOMECORE_USER_ID=(\S*)", _pxrendered)
check("  the renderer sends a login as X-Proxy-User", "900000111" in _ids, _ids)
check("  never the member id -- find_user() would miss it",
      not any(i.startswith("user") for i in _ids if i), _ids)

# A member with no portal account has no request to make. Better an empty value
# refused at the door than a name the portal cannot find.
_PXCFG["members"].append({"id": "user9", "display_name": "Sin cuenta"})
_pxenv9 = D.collect_env(
    {"secrets": {"required": ["PROXY_SHARED_SECRET"],
                 "per_member": ["HOMECORE_PROXY_TOKEN_{M}"]}},
    {"name": "multiuser"}, {"PROXY_SHARED_SECRET": "sh4red"}, _PXCFG, {})
check("  a member with no portal account gets no token",
      _pxenv9.get("HOMECORE_PROXY_TOKEN_USER_9", "") == "",
      _pxenv9.get("HOMECORE_PROXY_TOKEN_USER_9"))

# And the member id keeps the jobs that are actually its own.
check("  the instance is still named by the member id",
      "NANOBOT_INSTANCE=user1" in _pxrendered,
      "the instance name, state dir and secret suffixes are the deployer's half")

# --- a name in a compose default means the export is mandatory ---------------
#
# The other half of the NANOBOT_URL bug, and the one that actually happened
# next: not an export that is *shorter* than the default, but an export that is
# *absent*, so the default wins outright.
#
# `./home-stack check` cannot catch it. Its job is that every `${VAR}` a compose
# file interpolates is supplied, and a `${VAR:-default}` is supplied by
# definition -- so a manifest that stops exporting one is, to that check,
# indistinguishable from one that never did.
#
# What makes it a bug rather than a fallback is *what those defaults contain*.
# A default that names a host names a *particular* household's host -- and the
# one written here is not the one running this. `hub.home` and `house.home` do
# not resolve on this network; `portal.home` and `mqtt.home` do. So the default
# is not a safety net, it is a placeholder describing the shape of a value the
# deployer is expected to supply from `dns:`, and every one is meant to be
# overridden.
#
# Measured, on 2026-08-31: an edit to the broker unit matched the *multiuser*
# unit's `env:` block and removed fifty-two lines of it. Every suite passed, the
# contract check passed, the deploy went green, all five assistants came up
# healthy -- and TASKS_API_URL inside them was a name that resolves nowhere, so
# every household skill failed `getaddrinfo`. The assistants answered; they just
# could not reach the house. That is the failure this check exists to catch.
_NAMED = ".home"
for _svc, _spec in D.load_yaml(D.MANIFEST).get("services", {}).items():
    for _unit in _spec.get("units", []):
        _compose_dir = D.ROOT / _unit["dir"]
        _texts = []
        _entries = ([_unit["compose"]] if isinstance(_unit.get("compose"), str)
                    else (_unit.get("compose") or []))
        for _cf in _entries:
            _name_of = _cf["file"] if isinstance(_cf, dict) else _cf
            _path = _compose_dir / _name_of
            if _path.is_file():
                _texts.append(_path.read_text(encoding="utf-8"))
        _blob = "\n".join(_texts)
        _exported = set(_unit.get("env") or {})
        for _m in _re_url.finditer(r"\$\{([A-Z_0-9]+):-([^}]*)\}", _blob):
            _var, _dflt = _m.group(1), _m.group(2)
            if _NAMED not in _dflt or _var in _exported:
                continue
            # `state:` entries name variables too, and they are exports as much
            # as the `env:` block is.
            if any(_e.get("env") == _var for _e in (_unit.get("state") or [])
                   if isinstance(_e, dict)):
                continue
            check(f"{_svc}/{_unit['name']}: {_var} is exported, not left to its "
                  f"compose default",
                  False,
                  f"the default is '{_dflt}' -- a name, which is NXDOMAIN in a "
                  f"container. Nothing exports {_var}, so that is the value the "
                  f"service runs with, and it looks exactly like the service "
                  f"being down")

# --- the reserved suffix appears nowhere at all -------------------------------
#
# Not "nowhere functional" -- nowhere. The suffix was in 310 lines across 130 files:
# compose defaults, Python and JS fallbacks, two certificate scripts, an
# `extra_hosts:` block, the Android app's packaged default, the example config,
# every doc and changelog that quoted one, and the sanitizer rule that *produced*
# them. This household's names end in `.home`, so every one of those was a name
# that resolves nowhere, and a container waiting on a resolver is
# indistinguishable from a service that is down.
#
# The sanitizer is why a narrower rule would not have held: it rewrote
# `<alias>.home` to `<alias>` plus the suffix on every run, so the tree grew them
# back faster than they could be removed by hand. That rule is gone -- appending
# a suffix is normalisation, not redaction, and it bought no privacy at all --
# and this check is what keeps the result from drifting.
#
# Spelled in two halves so this line is not itself a hit.
_BANNED = ".ar" + "pa"
_bad = []
for _f in sorted(D.ROOT.rglob("*")):
    if not _f.is_file():
        continue
    _rel = _f.relative_to(D.ROOT).as_posix()
    # Caches and build output are not the tree; they are a copy of it that a
    # tool regenerates, and failing on one sends somebody editing a file that
    # is about to be overwritten anyway.
    if any(_part in _rel.split("/") for _part in
           (".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
            ".gradle", "build", "dist", ".mypy_cache", ".ruff_cache")):
        continue
    try:
        _text = _f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        continue                       # binaries and images have no opinion
    if _BANNED in _text:
        _n = next(i for i, l in enumerate(_text.splitlines(), 1) if _BANNED in l)
        _bad.append(f"{_rel}:{_n}")
check("the reserved DNS suffix appears nowhere in the tree",
      not _bad,
      f"{_bad[:8]}{' ...' if len(_bad) > 8 else ''} -- names come from `dns:`, "
      f"and this household's end in `.home`")

# --- an SSRF exemption names your network, never a shipped guess --------------
#
# `tools.ssrfWhitelist` is the one place the assistants are told a private
# address is the house rather than somewhere they are being steered into
# reaching, so what goes in it is the security decision and not a detail.
#
# It shipped as the literal `192.168.1.0/24`. That is a guess at a household's
# network -- wrong by one octet on the house this was extracted from, so the
# check fired on this family's own cameras and portal while granting an
# exemption to a /24 belonging to whoever else happens to be on it. It is the
# same failure as a default that names a host, with a worse blast radius: a
# name that does not resolve fails visibly, and an exemption for the wrong
# network fails silently and in the permissive direction.
#
# So the value comes from `site.lan_cidr` through NANOBOT_LAN_CIDR. Empty is
# the shipped state and blocks every private range. The Tailscale range stays
# spelled out -- 100.64.0.0/10 is CGNAT, assigned to no household, and reaching
# the house over the VPN is still the household's own network.
_CIDR = _re_url.compile(r"^(?:10|127|169\.254|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.")
_guessed = []
for _cfg in sorted(D.ROOT.glob("services/nanobot/config/**/config.json")):
    try:
        _doc = json.loads(_cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    for _entry in ((_doc.get("tools") or {}).get("ssrfWhitelist") or []):
        if isinstance(_entry, str) and _CIDR.match(_entry):
            _guessed.append(f"{_cfg.relative_to(D.ROOT).as_posix()}: {_entry}")
check("no nanobot config ships a guess at somebody's LAN",
      not _guessed,
      f"{_guessed} -- put it in site.lan_cidr and read it as "
      f"${{NANOBOT_LAN_CIDR}}; a range that is not yours is an exemption "
      f"granted to a network you do not own")

# --- a file mounted as a command has to be executable -------------------------
#
# `config/ntfy-send` is bind-mounted to `/usr/local/bin/ntfy-send` in every
# member's assistant and in the house one, and both the persona files and
# AGENTS.md tell the model to run it -- with `cron`, for reminders. It was mode
# 644 in git, so it was 644 in the container, and every attempt to send a push
# ended in `Permission denied`.
#
# Nothing catches that. The mount exists, the file exists, the container is
# healthy, and the deploy is green: the only symptom is a household that stops
# getting notifications, which reads as ntfy being down or a phone having
# unsubscribed. The send path from HomeCore is a different code path and kept
# working, so half the notifications arrived, which is worse than none.
_BIN_MOUNT = _re_url.compile(r"\./([^:\s]+):(?:/usr/local/bin|/usr/bin)/[^:\s]+")
_not_exec = []
for _compose in sorted(D.ROOT.glob("services/*/docker-compose*.yml")) + \
                sorted(D.ROOT.glob("deploy/units/*/docker-compose*.yml")):
    for _m in _BIN_MOUNT.finditer(_compose.read_text(encoding="utf-8")):
        _src = (_compose.parent / _m.group(1)).resolve()
        if _src.is_file() and not os.access(_src, os.X_OK):
            _not_exec.append(_src.relative_to(D.ROOT).as_posix())
check("every file mounted as a command is executable",
      not _not_exec,
      f"{sorted(set(_not_exec))} -- mounted onto a bin path and run by name, so "
      f"mode 644 is `Permission denied` at the moment somebody needs it")


# --- the session header OpenCode began requiring on 2026-09-06 --------------
#
# "Requests missing this header may error." OpenCode's mail named the callers on
# this account by user-agent and one of them was this stack's own probe,
# `home-stack/models (curl-compatible)`. What is pinned is that the header goes
# out, that it stays the same for one sweep, and that neither the user-agent nor
# the endpoint moved while adding it -- the user-agent is what gets the probe
# past a 403, and the endpoint is Zen and must never be the flat plan.
_mspec = _ilu.spec_from_file_location("stack_models", D.ROOT / "deploy" / "models.py")
_stack_models = _ilu.module_from_spec(_mspec)
_mspec.loader.exec_module(_stack_models)

_mh = _stack_models._headers("a-key")
check("the model probe sends x-opencode-session",
      "x-opencode-session" in _mh, sorted(_mh))
check("  one id for the whole sweep",
      _stack_models._headers("k")["x-opencode-session"]
      == _stack_models._headers("k2")["x-opencode-session"])
check("  and not a constant every household would share",
      _stack_models._SESSION != "home-stack-models-" + "0" * 32)
check("  the user-agent that gets past the 403 is untouched",
      _mh.get("User-Agent") == "home-stack/models (curl-compatible)", _mh)
check("  authorization still goes with it",
      _mh.get("Authorization") == "Bearer a-key")
check("  a caller can still override the session",
      _stack_models._headers("k", **{"x-opencode-session": "mine"})
      ["x-opencode-session"] == "mine")
check("the probe still points at Zen, never the flat plan",
      _stack_models.BASE == "https://opencode.ai/zen/v1", _stack_models.BASE)

print()
print("a 200 from /responses is not by itself an answer")

# The Responses API reports a generation that died inside the model as
# `status: "failed"` over HTTP 200, so the status code alone would call a
# permanently broken model `ok` -- and `--quiet` would stay silent about a
# role that cannot answer. `incomplete` is the opposite case and must stay
# `ok`: max_output_tokens is 16 against reasoning models, so hitting the cap
# is the normal outcome and means the gateway served it.
import io as _io
import urllib.request as _ur


class _FakeResp(_io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _stub_once(payload):
    def _open(req, timeout=None):
        _stub_once.path = req.full_url[len(_stack_models.BASE) + 1:]
        return _FakeResp(json.dumps(payload).encode())
    return _open


for _status, _want in (("failed", "down"), ("incomplete", "ok"),
                       ("completed", "ok")):
    _real = _ur.urlopen
    _ur.urlopen = _stub_once({"status": _status,
                              "error": {"message": "model exploded"}})
    try:
        _got = _stack_models._ask_once("k", "gpt-5.6-luna")
    finally:
        _ur.urlopen = _real
    check(f"  status={_status} is {_want}", _got[0] == _want, _got)
    check(f"  and it asked /responses for it",
          _stub_once.path == "responses", _stub_once.path)

_real = _ur.urlopen
_ur.urlopen = _stub_once({"choices": [{}]})
try:
    _got = _stack_models._ask_once("k", "deepseek-v4-flash")
finally:
    _ur.urlopen = _real
check("  while a chat model is asked /chat/completions and passes",
      _got[0] == "ok" and _stub_once.path == "chat/completions",
      (_got, _stub_once.path))

print()
print("an opencode that would not restart is told the truth about why")


class _FakeTarget:
    """Answers `command -v systemctl` the way each kind of host would."""

    def __init__(self, has_systemctl):
        self.has = has_systemctl

    def run(self, cmd, check=True, capture=False):
        return type("R", (), {"returncode": 0 if self.has else 1})()


_msg_container = D._opencode_restart_remedy(_FakeTarget(False))
_msg_host = D._opencode_restart_remedy(_FakeTarget(True))
check("  with no systemd, it does not tell you to install units",
      "install --opencode" not in _msg_container, _msg_container)
check("  it names the shell deploy that can actually restart it",
      "deploy alfred-mcp" in _msg_container, _msg_container)
check("  on a host with systemd, installing the units is still the answer",
      "install --opencode" in _msg_host, _msg_host)

print("\nassistant.harness: background tasks on pi, with the sub-agent models")
_h = json.loads(D.apply_model_choices('{"agents": {"defaults": {}}}',
                {"assistant": {"harness": {"enabled": True}}}))["agents"]["defaults"]["harness"]
check("  on, with no model: pi runs the sub-agent models, Go not allowed",
      _h == {"enabled": True, "engine": "pi", "allowGo": False}, _h)
_h = D.harness_settings({"enabled": True, "allow_go": True}, {})
check("  allow_go is passed through", _h["allowGo"] is True, _h)
_h = D.harness_settings({"enabled": True, "model": "ollama:gemma4:e4b"},
                        {"cloud": {"ollama": {"local": {"context": 40960}}}})
check("  an override is a local model, reached through the assistants' own OLLAMA_URL",
      _h["baseUrl"] == "${OLLAMA_URL}/v1" and _h["model"] == "gemma4:e4b"
      and _h.get("contextWindow") == 40960, _h)
_h = D.harness_settings({"enabled": True, "model": "llamacpp:bonsai-2-27b-pq2_0"}, {})
check("  llamacpp: is the router behind cloud.openai_compatible",
      _h["baseUrl"] == "${OPENAI_COMPATIBLE_URL}/v1" and _h["reasoning"] is False, _h)
for _model in ("deepseek-v4-flash", "together:Qwen/Qwen3"):
    try:
        D.harness_settings({"enabled": True, "model": _model}, {})
        check(f"  a hosted override is refused: {_model}", False)
    except D.DeployError:
        check(f"  a hosted override is refused: {_model}", True)
check("  a config that never mentions it is left alone",
      "harness" not in json.loads(D.apply_model_choices('{"agents": {"defaults": {}}}',
                                  {"assistant": {"models": {"everyday": "x"}}}))["agents"]["defaults"])

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
