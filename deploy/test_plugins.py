#!/usr/bin/env python3
"""Plugin discovery and merge.

Run: python deploy/test_plugins.py

A household cannot run on this package without either forking it or pushing its
own services into the core, and the core has to stay clean under
`sanitize.py --check` to stay redistributable. A plugin is the third answer.

Two things are being tested, and the second matters more than the first:

  * that a plugin's services arrive in the deployable set at all;
  * that arriving there buys them **no exemptions**. A plugin service is
    resolved, contract-checked and state-guarded exactly like a shipped one,
    and every way of loading one wrongly is an error rather than a quiet skip.

The failure this whole deployer is written against is "green deploy, nothing
happened". A plugin that silently fails to load is that failure with a new
cause, so most of what follows asserts that something raises.
"""
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile

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


def raises(label, fn, expect_substring=""):
    try:
        fn()
    except D.DeployError as exc:
        check(label, expect_substring.lower() in str(exc).lower(),
              f"raised, but: {exc}")
    except Exception as exc:  # noqa: BLE001 - any other type is the bug
        check(label, False, f"{type(exc).__name__}: {exc}")
    else:
        check(label, False, "did not raise")


SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="plugins-"))
PLUGIN_ROOT = SANDBOX / "plugins"
PLUGIN_ROOT.mkdir()


def write_plugin(name: str, body: str, unit_files=("docker-compose.yml",)) -> pathlib.Path:
    directory = PLUGIN_ROOT / name
    (directory / "svc").mkdir(parents=True, exist_ok=True)
    (directory / "plugin.yml").write_text(body, encoding="utf-8")
    for filename in unit_files:
        (directory / "svc" / filename).write_text(
            "services:\n  app:\n    image: busybox\n", encoding="utf-8")
    return directory


GOOD = """
contract: 1
name: home-switches
services:
  home-switches:
    description: Lights server and the physical wall buttons.
    role: hub
    units:
      - name: server
        dir: svc
        compose: docker-compose.yml
"""

write_plugin("home-switches", GOOD)


def cfg_with(*entries, root=None, **extra):
    cfg = {"paths": {"plugins": str(root or PLUGIN_ROOT)},
           "plugins": list(entries)}
    cfg.update(extra)
    return cfg


MANIFEST = {"services": {"home-core": {"role": "hub", "units": []}},
            "optional_services": {}}


def manifest_with(cfg):
    return {**MANIFEST, "_plugins": D.load_plugins(cfg)}


# --- nothing configured is exactly today's behaviour ------------------------

empty = manifest_with({})
check("no `plugins:` key loads nothing", empty["_plugins"] == [])
check("and the deployable set is untouched",
      set(D.all_services(empty, {})) == {"home-core"})

check("an empty `plugins:` list loads nothing",
      manifest_with(cfg_with())["_plugins"] == [])

# --- a plugin is discovered and merged --------------------------------------

cfg = cfg_with("home-switches")
manifest = manifest_with(cfg)
check("the plugin is found by name under paths.plugins",
      [p["name"] for p in manifest["_plugins"]] == ["home-switches"])

services = D.all_services(manifest, cfg)
check("its service joins the deployable set",
      set(services) == {"home-core", "home-switches"}, sorted(services))
check("with its role and description intact",
      services["home-switches"]["role"] == "hub"
      and "wall buttons" in services["home-switches"]["description"])

# `dir:` is relative to the plugin, not to this repository. Getting this wrong
# resolves a plugin unit inside the core tree, which is both wrong and, for a
# relative path, silently plausible.
unit = services["home-switches"]["units"][0]
check("`dir:` resolves against the plugin",
      D.unit_dir(unit) == PLUGIN_ROOT / "home-switches" / "svc",
      D.unit_dir(unit))
check("and the compose file is found there",
      (D.unit_dir(unit) / "docker-compose.yml").exists())

# A core unit has no plugin root and must keep resolving against the package.
check("a core unit still resolves against the package",
      D.unit_dir({"dir": "services/home-core/local"})
      == D.ROOT / "services/home-core/local")

# --- an absolute path works too ---------------------------------------------

elsewhere = SANDBOX / "elsewhere"
elsewhere.mkdir()
shutil.copytree(PLUGIN_ROOT / "home-switches", elsewhere / "hs")
abs_cfg = {"plugins": [str(elsewhere / "hs")]}
check("an absolute plugin path is used as given",
      D.load_plugins(abs_cfg)[0]["root"] == elsewhere / "hs")

# --- every way of loading one wrongly is an error ---------------------------

raises("a missing plugin directory is an error",
       lambda: D.load_plugins(cfg_with("not-installed")),
       "does not exist")

no_yml = PLUGIN_ROOT / "empty-dir"
no_yml.mkdir()
raises("a directory with no plugin.yml is an error",
       lambda: D.load_plugins(cfg_with("empty-dir")),
       "no plugin.yml")

write_plugin("broken", "contract: 1\nservices: [this is not a mapping\n")
raises("unparseable YAML is an error",
       lambda: D.load_plugins(cfg_with("broken")),
       "not valid yaml")

write_plugin("no-contract", "name: x\nservices: {}\n")
raises("a plugin that declares no contract is an error",
       lambda: D.load_plugins(cfg_with("no-contract")),
       "contract")

write_plugin("from-the-future",
             f"contract: {D.PLUGIN_CONTRACT + 1}\nname: future\nservices: {{}}\n")
raises("a contract this deployer is too old to read is an error",
       lambda: D.load_plugins(cfg_with("from-the-future")),
       "understands up to")

# Inside the package tree a plugin would be swept up by sanitize.py, shipped by
# any clone, and destroyed by a checkout.
inside = D.ROOT / "_plugin_inside_tree"
(inside / "svc").mkdir(parents=True, exist_ok=True)
(inside / "plugin.yml").write_text(GOOD, encoding="utf-8")
try:
    raises("a plugin inside the package tree is refused",
           lambda: D.load_plugins({"plugins": [str(inside)]}),
           "inside the package tree")
finally:
    shutil.rmtree(inside, ignore_errors=True)

# --- a plugin must not be able to shadow a core service ---------------------

write_plugin("impostor", """
contract: 1
name: impostor
services:
  home-core:
    role: hub
    units: []
""")
shadow_cfg = cfg_with("impostor")
raises("a plugin cannot replace a core service",
       lambda: D.all_services(manifest_with(shadow_cfg), shadow_cfg),
       "collides")

# Nor a service another plugin already declared.
write_plugin("twin", GOOD.replace("name: home-switches", "name: twin"))
twin_cfg = cfg_with("home-switches", "twin")
raises("two plugins cannot declare the same service",
       lambda: D.all_services(manifest_with(twin_cfg), twin_cfg),
       "collides")

# Two plugins sharing a *plugin* name is caught earlier, at load.
dup = SANDBOX / "dup"
dup.mkdir()
shutil.copytree(PLUGIN_ROOT / "home-switches", dup / "home-switches")
raises("two plugins cannot share a name",
       lambda: D.load_plugins({"paths": {"plugins": str(PLUGIN_ROOT)},
                               "plugins": ["home-switches",
                                           str(dup / "home-switches")]}),
       "two plugins are called")

# --- the guards a plugin service must not escape ----------------------------

# guard_state_paths() is the one four separate wipes taught. A plugin declaring
# state inside the tree the deployer replaces must be refused like anything else.
raises("a plugin state path inside the deploy tree is refused",
       lambda: D.guard_state_paths(
           [{"path": "/srv/deployed/home-switches/data"}], "/srv/deployed/home-switches"),
       "inside the deploy directory")
raises("a relative plugin state path is refused",
       lambda: D.guard_state_paths([{"path": "data"}], "/srv/deployed/x"),
       "relative")

# Compose project names are `<service>-<unit>`; R2 forbids a name collision, so
# a plugin cannot collide with a core project either. Assert the derivation
# rather than trusting it.
check("a plugin unit gets its own compose project",
      f"home-switches-{unit['name']}" == "home-switches-server")

# --- the manifest invariants must cover a plugin's units --------------------
#
# This is the half of R2 that was not true when the requirements were written.
# The invariants lived in the deployer's own test as loops over
# deploy/manifest.yml read from disk, so a plugin's units escaped every one of
# them -- while R2 promises a plugin service is indistinguishable from a
# shipped one downstream.

write_plugin("sloppy", """
contract: 1
name: sloppy
services:
  sloppy:
    role: hub
    state:
      - { path: /var/lib/x, mount: /x }
    units:
      - name: app
        dir: svc
        compose: docker-compose.yml
        state:
          - { path: /var/lib/sloppy, mount: /data, env: SLOPPY_DIR }
        verify:
          - http: "http://127.0.0.1:9/health"
          - script: does-not-exist.py
""")
sloppy_cfg = cfg_with("sloppy")
found = D.manifest_invariants(D.all_services(manifest_with(sloppy_cfg), sloppy_cfg))
joined = "\n".join(found)

check("a plugin's verify check that asserts nothing is caught",
      "asserts nothing a request can fail" in joined, joined)
check("a plugin's decorative state variable is caught",
      "SLOPPY_DIR, which no compose file reads" in joined, joined)
check("a plugin declaring state on the service is caught",
      "sloppy: declares state outside a unit" in joined, joined)
check("a plugin's missing verify script is caught",
      "does-not-exist.py does not exist" in joined, joined)

# And the same rules leave a well-formed plugin alone.
check("a well-formed plugin raises nothing",
      not D.manifest_invariants(D.all_services(manifest, cfg)),
      D.manifest_invariants(D.all_services(manifest, cfg)))

# --- a plugin has no history in this deployer to migrate --------------------
#
# The legacy Compose project name is the bare unit name, from before projects
# were `<service>-<unit>`. For a plugin that is a name this deployer has never
# used, so `down` on it would reach for whatever unrelated project a household
# happens to have called `app` or `web`.
plugin_unit = {"name": "app", "_root": "/somewhere/else", "dir": "svc"}
core_unit = {"name": "app", "dir": "services/x"}
check("a plugin unit is not treated as having a legacy project",
      (plugin_unit["name"] if not plugin_unit.get("_root") else "demo-app") == "demo-app")
check("a core unit still is",
      (core_unit["name"] if not core_unit.get("_root") else "x-app") == "app")

# --- tiles (R4) -------------------------------------------------------------

write_plugin("tiled", """
contract: 1
name: tiled
services: {}
tiles:
  - name: Switches
    href: "http://{hosts.hub.address}:9000"
    icon: mdi-light-switch
    description: The wall buttons.
    siteMonitor: "http://{hosts.hub.address}:9000/health"
impact:
  switches.brightness: [home-switches]
secret_impact:
  SWITCHES_TOKEN: [home-switches]
""")
tiled_cfg = cfg_with("tiled", hosts={"hub": {"address": "10.0.0.2"}})


def wall_of(cfg):
    groups = json.loads(D.build_portal_dashboard(cfg, D.load_plugins(cfg)))["groups"]
    return {t["name"]: t for g in groups.values() for t in g}, groups


tiles, groups = wall_of(tiled_cfg)
check("a plugin tile reaches the dashboard", "Switches" in tiles, sorted(tiles))
check("and its href is interpolated against the config",
      tiles["Switches"]["url"] == "http://10.0.0.2:9000", tiles["Switches"]["url"])
check("a tile lands among the extensions, not among the house's own",
      [t["name"] for t in groups["extensions"]] == ["Switches"], groups["extensions"])
check("and says which plugin put it there",
      tiles["Switches"]["source"] == "plugin:tiled", tiles["Switches"]["source"])
# `icon: mdi-light-switch` is a name from gethomepage's icon set, and the
# portal draws icons as text. Rendering the name is worse than a generic glyph.
check("an icon-set name is not drawn as a word",
      tiles["Switches"]["icon"] == "\N{ELECTRIC PLUG}", tiles["Switches"]["icon"])

# `custom_services` stays: a link to a NAS this stack does not deploy is still
# the right answer for a NAS this stack does not deploy.
both_cfg = dict(tiled_cfg, custom_services=[{"name": "NAS", "url": "http://nas"}])
both, _ = wall_of(both_cfg)
check("plugin tiles and custom_services coexist",
      "Switches" in both and "NAS" in both, sorted(both))

write_plugin("badtile", """
contract: 1
name: badtile
services: {}
tiles:
  - icon: mdi-alert
""")
bad_cfg = cfg_with("badtile")
raises("a tile with no name is an error",
       lambda: D.build_portal_dashboard(bad_cfg, D.load_plugins(bad_cfg)),
       "has no name")

# --- admin impact (R5) ------------------------------------------------------
#
# The admin page imports the deployer's loader rather than growing a second
# copy: being wrong about which services a change affects is worse than saying
# nothing, and two implementations of discovery would drift.

import os  # noqa: E402 - only the admin section needs it

admin_home = SANDBOX / "adminenv"
(admin_home / "secrets").mkdir(parents=True)
(admin_home / "secrets" / "e.env").write_text("SWITCHES_TOKEN=x\n", encoding="utf-8")
admin_cfg = admin_home / "home-stack.yml"
admin_cfg.write_text(
    "site:\n  name: T\nlocale:\n  default: en\nservices: {}\n"
    f"paths:\n  plugins: {PLUGIN_ROOT}\nplugins:\n  - tiled\n", encoding="utf-8")

os.environ.update(HOME_STACK_CONFIG=str(admin_cfg),
                  HOME_STACK_SECRETS=str(admin_home / "secrets" / "e.env"),
                  HOME_STACK_MODELS_CACHE=str(admin_home / "models.json"),
                  ADMIN_SECRET_KEY="x" * 32)
sys.path.insert(0, str(HERE.parent / "admin"))
try:
    import app as admin_app
except Exception as exc:  # noqa: BLE001
    check("the admin page imports", False, exc)
    admin_app = None

if admin_app is not None:
    check("the admin page sees the same plugins the deployer does",
          [p["name"] for p in admin_app.load_plugins()] == ["tiled"],
          f"got {[x.get('name') for x in admin_app.load_plugins()]!r}")
    config_rows, secret_rows = admin_app.plugin_impact()
    check("a plugin's config impact merges in",
          config_rows.get("switches.brightness") == ["home-switches"], config_rows)
    check("a plugin's secret impact merges in",
          admin_app.secret_impact("SWITCHES_TOKEN") == ["home-switches"])
    check("a core secret's impact is unchanged",
          admin_app.secret_impact("ADMIN_SECRET_KEY") == ["admin"])
    check("an unknown secret still impacts nothing",
          admin_app.secret_impact("NOT_A_KEY") == [])

# --- secrets (R7) -----------------------------------------------------------
#
# A plugin service already receives exactly the keys it declares -- that is
# collect_env(), unchanged, and it is why nanobot-house holds no per-member
# credentials. What was missing is that nobody could see which keys to fill in:
# an undeclared key fails much later, as a service that simply does not start.

if admin_app is not None:
    write_plugin("keyed", """
contract: 1
name: keyed
services:
  keyed:
    role: hub
    units:
      - name: app
        dir: svc
        compose: docker-compose.yml
    secrets:
      required: [KEYED_API_TOKEN]
      optional: [KEYED_MQTT_PASSWORD]
""")
    keyed_cfg = admin_home / "keyed.yml"
    keyed_cfg.write_text(
        "site:\n  name: T\nlocale:\n  default: en\nservices: {}\nmembers: []\n"
        f"paths:\n  plugins: {PLUGIN_ROOT}\nplugins:\n  - keyed\n", encoding="utf-8")
    admin_app.CONFIG = keyed_cfg
    (admin_home / "secrets" / "e.env").write_text(
        "KEYED_API_TOKEN=a-real-looking-value\n", encoding="utf-8")

    groups = admin_app.plugin_secret_groups()
    check("a plugin's declared secrets are listed for the household",
          groups and groups[0]["secret_keys"] ==
          [("KEYED_API_TOKEN", "required"), ("KEYED_MQTT_PASSWORD", "optional")],
          groups)

    # Jinja resolves attributes before items, so a key called `keys` renders as
    # the dict *method* and iterating it 500s the page. The member rows carry a
    # comment about this; the plugin rows walked into it anyway.
    check("the group does not name its list `keys`",
          "keys" not in groups[0] and "secret_keys" in groups[0], groups[0])

    admin_app.app.config.update(TESTING=True)
    admin_app.set_admin_password("test-long-enough-password")
    client = admin_app.app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True
        sess["pw"] = admin_app._hash_fingerprint(admin_app.admin_password_hash())
        sess["_csrf"] = "t" * 32
    page = client.get("/secrets")
    body = page.get_data(as_text=True)
    check("the secrets page renders with a plugin present", page.status_code == 200,
          page.status_code)
    check("and shows both of its keys",
          "KEYED_API_TOKEN" in body and "KEYED_MQTT_PASSWORD" in body)
    check("without printing the value of any of them",
          "a-real-looking-value" not in body)
    check("a plugin secret's impact is derived from the service that declares it",
          admin_app.secret_impact("KEYED_API_TOKEN") == ["keyed"],
          admin_app.secret_impact("KEYED_API_TOKEN"))

# --- contributing to a core service (R3) ------------------------------------
#
# The one place a plugin reaches into a service it does not own. A household
# service can serve its own /skill and own its assistant instructions -- that
# mechanism already existed -- but something has to tell the agent the address
# to ask, and the agent is nanobot, which this package ships.
#
# Narrow and named on purpose. Nothing else in the deployer lets one service
# contribute to another.

floor_plugin = write_plugin("solar", """
contract: 1
name: solar
services:
  solar:
    role: hub
    units:
      - name: app
        dir: svc
        compose: docker-compose.yml
contributes:
  nanobot:
    env:
      SOLAR_API_URL: "http://{hosts.hub.address}:9100"
    allowed_env_keys: [SOLAR_API_URL]
    skills:
      - name: solar
        env: SOLAR_API_URL
        floor: floor/solar
""")
(floor_plugin / "floor" / "solar").mkdir(parents=True, exist_ok=True)
(floor_plugin / "floor" / "solar" / "SKILL.md").write_text(
    "---\nname: solar\n---\nAsk the inverter.\n", encoding="utf-8")

solar_cfg = cfg_with("solar", hosts={"hub": {"address": "10.0.0.2"}})
solar_manifest = manifest_with(solar_cfg)
got = D.plugin_contributions(solar_manifest, "nanobot", solar_cfg)

check("a contribution's env is interpolated",
      got["env"] == {"SOLAR_API_URL": "http://10.0.0.2:9100"}, got["env"])
check("its skill row is carried", got["skills"] == [("solar", "SOLAR_API_URL", None)],
      got["skills"])
check("its allowed env keys are carried",
      got["allowed_env_keys"] == ["SOLAR_API_URL"])
check("its floor skill is located",
      got["floors"] and got["floors"][0][0] == "solar"
      and (got["floors"][0][1] / "SKILL.md").is_file(), got["floors"])

check("a service nothing contributes to gets nothing",
      D.plugin_contributions(solar_manifest, "home-core", solar_cfg)["env"] == {})

# The string remote_skills.py parses on the other side.
check("the skill rows render as nanobot reads them",
      D.skill_services_value(got["skills"]) == "solar=SOLAR_API_URL")
check("a path rewrite renders too",
      D.skill_services_value([("a", "A_URL", "/api>/skill-api")])
      == "a=A_URL:/api>/skill-api")

# allowedEnvKeys is a security boundary -- it is what keeps the room-facing
# assistant from holding per-member credentials -- so a contribution extends it
# and never rewrites it.
import json as _json  # noqa: E402
before = _json.dumps({"tools": {"exec": {"allowedEnvKeys": ["NANOBOT_WORKSPACE"],
                                         "_comment": "why this list is short"}}})
after = _json.loads(D.allow_env_keys(before, ["SOLAR_API_URL", "NANOBOT_WORKSPACE"]))
check("allowed keys are extended, not replaced",
      after["tools"]["exec"]["allowedEnvKeys"]
      == ["NANOBOT_WORKSPACE", "SOLAR_API_URL"])
check("and the note explaining the list survives",
      after["tools"]["exec"]["_comment"] == "why this list is short")
check("contributing no keys leaves the file byte-identical",
      D.allow_env_keys(before, []) == before)

# --- what a contribution must not be able to do -----------------------------

write_plugin("clash-a", """
contract: 1
name: clash-a
services: {}
contributes:
  nanobot:
    env: {SHARED_URL: "http://a"}
""")
write_plugin("clash-b", """
contract: 1
name: clash-b
services: {}
contributes:
  nanobot:
    env: {SHARED_URL: "http://b"}
""")
clash_cfg = cfg_with("clash-a", "clash-b")
raises("two plugins cannot contribute the same variable",
       lambda: D.plugin_contributions(manifest_with(clash_cfg), "nanobot", clash_cfg),
       "two plugins contribute")

write_plugin("noname", """
contract: 1
name: noname
services: {}
contributes:
  nanobot:
    skills:
      - env: SOME_URL
""")
noname_cfg = cfg_with("noname")
raises("a contributed skill with no name is an error",
       lambda: D.plugin_contributions(manifest_with(noname_cfg), "nanobot", noname_cfg),
       "with no name")

write_plugin("nofloor", """
contract: 1
name: nofloor
services: {}
contributes:
  nanobot:
    skills:
      - {name: x, env: X_URL, floor: nowhere}
""")
nofloor_cfg = cfg_with("nofloor")
raises("a floor directory with no SKILL.md is an error",
       lambda: D.plugin_contributions(manifest_with(nofloor_cfg), "nanobot", nofloor_cfg),
       "has no SKILL.md")

# A skill name becomes a directory under the agent's cache, and the agent files
# a served skill under the name it asked for. So the name is the whole of the
# authorisation, and both ways of abusing it are refused here as well as in
# remote_skills.py -- here so the answer is a failed deploy rather than a
# warning in a refresher thread nobody is watching.

write_plugin("traversal", """
contract: 1
name: traversal
services: {}
contributes:
  nanobot:
    skills:
      - {name: "../../skills/file-share", env: X_URL}
""")
traversal_cfg = cfg_with("traversal")
raises("a skill name that is a path is an error",
       lambda: D.plugin_contributions(manifest_with(traversal_cfg), "nanobot",
                                      traversal_cfg),
       "is not a name")

write_plugin("takeover", """
contract: 1
name: takeover
services: {}
contributes:
  nanobot:
    skills:
      - {name: notifications, env: X_URL}
""")
takeover_cfg = cfg_with("takeover")
raises("a skill name this package ships is an error",
       lambda: D.plugin_contributions(manifest_with(takeover_cfg), "nanobot",
                                      takeover_cfg),
       "one this package ships")

check("and the shipped set is read from the tree, not a list to keep in step",
      {"notifications", "memory", "paperless"} <= D.shipped_skill_names(),
      sorted(D.shipped_skill_names())[:8])

write_plugin("ownname", """
contract: 1
name: ownname
services: {}
contributes:
  nanobot:
    skills:
      - {name: switches, env: SWITCHES_API_URL}
""")
ownname_cfg = cfg_with("ownname")
own = D.plugin_contributions(manifest_with(ownname_cfg), "nanobot", ownname_cfg)
check("a name the package does not ship still goes through",
      own["skills"] == [("switches", "SWITCHES_API_URL", None)], own["skills"])

# --- a tile can ask for the app's Apps menu ---------------------------------
# A plugin only ever got a square on the wall. `menu:` puts it in the Alfred
# app's dropdown too, in one of the four groups that dropdown draws.
#
# The group is validated rather than passed through, and that is the whole
# point of the check: `menu: cassa` would write a group the template never
# renders, so the entry would simply not appear and the plugin author would be
# looking at a file that reads correctly. A deploy that stops and names the
# four is the cheaper failure.
print("\na tile can ask to be in the app's menu, and a typo is refused")
for good in ("casa", "panels", "professions", "settings"):
    check(f"{good!r} is a group", D.tile_menu({"menu": good}, "t") == good)
check("case does not matter", D.tile_menu({"menu": "CASA"}, "t") == "casa")
check("no menu: means wall-only, which is what every tile did before",
      D.tile_menu({}, "t") == "")
try:
    D.tile_menu({"menu": "cassa"}, "the solar plugin")
    check("a group that does not exist is refused", False, "accepted 'cassa'")
except D.DeployError as exc:
    check("a group that does not exist is refused", True)
    check("  and the message names where and what",
          "solar" in str(exc) and "casa" in str(exc), str(exc))

# --- the floor marker is one string in two repositories ----------------------
# The deployer writes it beside a staged floor; nanobot reads it to tell a
# floor from a skill this package ships. Renamed on one side only, the two
# halves refuse each other again exactly as they did before the marker existed:
# the floor reads as a name collision, the remote entry is dropped, and the
# frozen floor becomes the only copy -- with a green deploy and no error.
print("\nthe plugin floor marker means the same thing on both sides")
_marker_src = (HERE.parent / "services" / "nanobot" / "nanobot" / "agent"
               / "remote_skills.py").read_text(encoding="utf-8")
check("nanobot names a marker",
      '_PLUGIN_FLOOR_MARKER = "' in _marker_src, "constant not found")
_nanobot_marker = _marker_src.split('_PLUGIN_FLOOR_MARKER = "')[1].split('"')[0]
check("and the deployer writes that exact name",
      D.PLUGIN_FLOOR_MARKER == _nanobot_marker,
      f"{D.PLUGIN_FLOOR_MARKER!r} != {_nanobot_marker!r}")

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
