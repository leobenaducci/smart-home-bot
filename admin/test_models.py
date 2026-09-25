#!/usr/bin/env python3
"""The model catalogue and what it recommends.

Run: python admin/test_models.py

The recommendation is the part worth pinning, because the temptation is to
make it cleverer than the data allows. models.dev carries prices, limits and
booleans — 25 of the 26 hosted models report `reasoning: true` — and no
quality column at all. So the only ordering this can honestly produce is
"cheapest that clears the requirement", and these tests exist to stop that
quietly becoming "best", which it cannot know.

The other half is the trap that a first version walked straight into: a local
model costs nothing per token, so any single ranking that weighs price puts a
3B model on your own box at the top of *every* list, including code review.
Free is not a capability, and hosted and local are ranked separately for that
reason.
"""
import pathlib
import time
import tempfile
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import models as M  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def hosted(mid, *, inp, out, cache, ctx, max_out, vision=False, reasoning=True):
    return {"id": mid, "name": mid, "provider": "opencode", "input": inp,
            "output": out, "cache_read": cache, "context": ctx,
            "max_output": max_out, "vision": vision, "reasoning": reasoning}


def local(mid, *, vision=False):
    return {"id": f"ollama:{mid}", "name": mid, "provider": "ollama",
            "input": 0.0, "output": 0.0, "cache_read": 0.0, "context": None,
            "max_output": None, "vision": vision, "reasoning": False}


CAT = {
    "checked_at": 1,
    "opencode_zen": [
        hosted("tiny",  inp=0.02, out=0.07, cache=0.004, ctx=64_000,  max_out=8_000),
        hosted("cheap", inp=0.10, out=0.20, cache=0.002, ctx=1_000_000, max_out=131_000,
               vision=True),
        hosted("mid",   inp=0.66, out=1.98, cache=0.022, ctx=1_000_000, max_out=384_000),
        hosted("dear",  inp=3.00, out=15.0, cache=0.300, ctx=1_000_000, max_out=384_000,
               vision=True),
    ],
    "ollama": [local("llama3.2:3b"), local("qwen3-vl:8b", vision=True)],
}

print("a persona only sees models that can do its job")
vision = M.recommend("vision", CAT)
check("vision excludes anything without image input",
      all(m["vision"] for m in vision["hosted"] + vision["local"]),
      [m["id"] for m in vision["hosted"]])
check("and local is the right answer there", vision["prefer_local"] is True)
check("its local pick is the vision build",
      vision["local"][0]["id"] == "ollama:qwen3-vl:8b", vision["local"])

prog = M.recommend("programmer", CAT)
check("a 64k model is not offered for code review",
      "tiny" not in [m["id"] for m in prog["hosted"]],
      [m["id"] for m in prog["hosted"]])

print("\nfree is not a capability")
check("hosted and local are separate lists",
      set(prog) >= {"hosted", "local"})
check("a free 3B model does not top the hosted list for code review",
      all(m["provider"] != "ollama" for m in prog["hosted"]), prog["hosted"])
check("and local is not preferred for it", prog["prefer_local"] is False)

print("\nthe ordering is cheapest-that-fits, on the price that dominates")
notif = M.recommend("notifications", CAT)
check("notification triage is priced on input", notif["prefer"] == "input")
check("so it takes the cheapest input", notif["hosted"][0]["id"] == "tiny",
      [m["id"] for m in notif["hosted"]])
every = M.recommend("everyday", CAT)
check("everyday chat is priced on the cached read", every["prefer"] == "cache_read")
check("so it takes the cheapest cached, not the cheapest input",
      every["hosted"][0]["id"] == "cheap", [m["id"] for m in every["hosted"]])
design = M.recommend("designer", CAT)
check("the designer is priced on output", design["prefer"] == "output")

print("\nwhat the house measured is carried, not invented")
check("a persona with a measurement says so", bool(prog["measured"]))
check("and it is dated", "2026-" in prog["measured"], prog["measured"])
check("a persona without one says nothing",
      M.recommend("powerful", CAT)["measured"] == "")

print("\na blank sub-agent says what it will actually run")
# The page must answer "what happens if I leave this empty" from the same map
# the deployer inherits by, not from a second copy that can disagree.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("deployer_for_test",
                                     M.__file__.replace("admin/models.py",
                                                        "deploy/deploy.py"))
_dep = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_dep)
check("the deployer names the map the page reads",
      _dep.SUBAGENT_INHERITS == {"subagent": "everyday", "planner": "everyday",
                                 "plan_steps": "subagent"},
      _dep.SUBAGENT_INHERITS)
check("and both roles it names are roles this page offers",
      set(_dep.SUBAGENT_INHERITS) | set(_dep.SUBAGENT_INHERITS.values())
      <= set(M.PERSONA_NEEDS),
      "a parent this page does not offer is a sentence naming nothing")

print("\nthe per-provider picks follow the role's own placement")
_cat = {"opencode_zen": [dict(id="zen-a", provider="opencode_zen", input=1.0, output=2.0,
                             cache_read=0.5, context=1_000_000,
                             max_output=128_000, vision=True,
                             reasoning=True, release_date="2026-01-01")],
        "ollama": [dict(id="ollama:local-vl", provider="ollama", input=None,
                        output=None, cache_read=None, context=None,
                        max_output=None, vision=True)]}
_local_role = M.recommend("vision", _cat)["by_provider"]
# `events` and `vision` declare `placement: local` -- this house runs them on
# its own hardware on purpose. A per-provider block that walked only the paid
# sources showed those two nothing but providers that charge, which steers a
# role away from the placement its own spec declares.
check("a local-placed role is offered its own hardware at all",
      "ollama" in _local_role, sorted(_local_role))
check("and reads it first", list(_local_role)[0] == "ollama", list(_local_role))
# `prefer_local` decides the order, not the membership: a hosted model is still
# a legitimate choice there.
check("with the paid ones still offered", "opencode_zen" in _local_role)

_hosted_role = M.recommend("everyday", _cat)["by_provider"]
check("a hosted-placed role reads the paid ones first",
      list(_hosted_role)[0] == "opencode_zen", list(_hosted_role))

# "cheapest" is a claim about price, and your own electricity is not one this
# page can compare.
check("a local pick is not labelled cheapest",
      all(m["pick"] != "cheapest" for m in _local_role["ollama"]["picks"]),
      _local_role["ollama"]["picks"])
check("and is marked as local", _local_role["ollama"]["local"] is True)
check("while a paid one is not", _local_role["opencode_zen"]["local"] is False)

print("\na model that costs nothing is not for the assistant's traffic")
_free = {"id": "big-pickle", "input": 0.0, "output": 0.0}
_paid = {"id": "kimi-k3", "input": 3.0, "output": 15.0}
check("the assistant's roles refuse one", not any(
    M.zero_cost_ok(r, _free) for r in
    ("everyday", "powerful", "notifications", "events", "fallback")))
check("and so do the professions it answers", not any(
    M.zero_cost_ok(r, _free) for r in
    ("programmer", "teacher", "designer", "doctor", "legal")),
    "assistant.models.programmer is the *assistant's* Programmer -- the "
    "children's, and the rescue when opencode is down")
check("a priced model is fine", all(
    M.zero_cost_ok(r, _paid) for r in M.ZERO_COST_BARRED))
# A model missing from the catalogue, or one whose price nobody published, is
# refused elsewhere for being unknown and must not be refused here for cheap.
check("an unknown price is not a free one",
      M.zero_cost_ok("everyday", {"id": "x", "input": None})
      and M.zero_cost_ok("everyday", None))
# The rule covers every role this page offers; a persona added without one
# would be a role that may quietly take free capacity.
check("every role this page offers is covered",
      not (set(M.PERSONA_NEEDS) - M.ZERO_COST_BARRED),
      sorted(set(M.PERSONA_NEEDS) - M.ZERO_COST_BARRED))

print("\nthe hard filter is a filter, not a preference")
check("a model below the context floor is out",
      not M.meets(hosted("x", inp=1, out=1, cache=1, ctx=1_000, max_out=99_000),
                  {"context": 200_000}))
check("a model below the output ceiling is out",
      not M.meets(hosted("x", inp=1, out=1, cache=1, ctx=999_000, max_out=1_000),
                  {"max_output": 32_000}))
check("an unknown limit is not held against it — Ollama reports none",
      M.meets(local("y"), {"context": 200_000, "max_output": 32_000}))
check("but a missing modality is",
      not M.meets(local("y"), {"vision": True}))

print("\nan empty catalogue is empty, not a crash")
blank = M.recommend("programmer", {"opencode_zen": [], "ollama": []})
check("no models, no recommendation", blank["hosted"] == [] and blank["local"] == [])
check("an unknown persona is handled too",
      M.recommend("nobody", CAT)["hosted"] == [])

print("\nevery persona the config names is described here")
import yaml
cfg = yaml.safe_load(
    (pathlib.Path(__file__).resolve().parents[1]
     / "config" / "home-stack.example.yml").read_text(encoding="utf-8"))
named = set((cfg.get("assistant") or {}).get("models") or {})
check("the config and the table agree", named == set(M.PERSONA_NEEDS),
      f"config={sorted(named)} table={sorted(M.PERSONA_NEEDS)}")
for persona, spec in M.PERSONA_NEEDS.items():
    check(f"  {persona} says what it is for", bool(spec.get("why")))

# --- every request out of here introduces itself --------------------------
# models.dev answers 403 to `Python-urllib/3.12`, which is what urllib sends
# when nothing sets a User-Agent. "Consultar ahora" reported the house as
# unable to reach models.dev while curl on the same box got a 200 -- a
# refusal that reads exactly like being offline.
#
# Asserted against a stub rather than the real host: this file must not need
# the internet to pass, and a check that only fails when models.dev is having
# a bad day is not a check. The stub captures the headers urllib would send.

import urllib.request

seen = {}


class _Stub:
    def __init__(self, req):
        seen.update(req.headers)
        seen["_url"] = req.full_url

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_real_urlopen = urllib.request.urlopen
urllib.request.urlopen = lambda req, timeout=None: _Stub(req)
try:
    M.fetch_opencode_zen()
    ua = seen.get("User-agent", "")
    check("the models.dev fetch sends a User-Agent", bool(ua),
          "urllib sends Python-urllib/x.y without one, and models.dev 403s it")
    check("and it is not urllib introducing itself",
          "urllib" not in ua.lower(), ua)

    seen.clear()
    M.fetch_ollama("http://ollama.invalid:11434", api_key="secret-key")
    check("an Ollama call gets one too", bool(seen.get("User-agent")))
    check("without losing the header it was already sending",
          seen.get("Authorization") == "Bearer secret-key",
          "the User-Agent is a default, not an override: "
          f"{ {k: v for k, v in seen.items() if k != '_url'} }")
finally:
    urllib.request.urlopen = _real_urlopen

# --- the OpenAI-compatible source ------------------------------------------
# Same stub, so this needs no server either.

print()
print("an OpenAI-compatible server is asked at exactly one /v1")


import json

class _ModelsDev:
    """models.dev's api.json, cut down to one provider and one model.

    Stubbed rather than fetched. The live call is 4.3 MB and this file was
    making two of them: measured at 110 seconds of wall clock for 0.3 seconds
    of CPU, and dependent on somebody else's uptime for a check about our own
    field names. The same trade as the mailbox probe -- a test that needs the
    internet is a test that fails for reasons that are not the code.
    """

    def __init__(self, req):
        seen["_url"] = req.full_url

    def read(self):
        return json.dumps({"opencode": {"models": {"luna": {
            "name": "Luna", "cost": {"input": 1.0, "output": 2.0, "cache_read": 0.1},
            "limit": {"context": 200000, "output": 32000},
            "reasoning": True,
            "modalities": {"input": ["text", "image", "audio"], "output": ["text"]},
        }}}}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Models:
    """A /v1/models answer, and a record of what was asked for."""

    def __init__(self, req):
        seen.update(req.headers)
        seen["_url"] = req.full_url

    def read(self):
        return b'{"data": [{"id": "qwen3-32b"}, {"id": "llama-3.3-70b"}]}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


urllib.request.urlopen = lambda req, timeout=None: _Models(req)
try:
    # Both forms appear in providers' own documentation, and pasting the one
    # with /v1 is what produces `/v1/v1/models` -- a 404 that reads as the
    # server being down. Exactly the NANOBOT_URL failure this package already
    # has a test for, arrived at from the configuration side.
    for given in ("http://box:8000", "http://box:8000/", "http://box:8000/v1",
                  "http://box:8000/v1/"):
        seen.clear()
        models, err = M.fetch_openai_compatible(given)
        check(f"  {given} -> one /v1",
              seen.get("_url") == "http://box:8000/v1/models", seen.get("_url"))
        check(f"  {given} parses both models", len(models) == 2, models)

    seen.clear()
    models, _ = M.fetch_openai_compatible("http://box:8000")
    check("ids carry the prefix assistant.models takes",
          all(m["id"].startswith("openai-compatible:") for m in models),
          [m["id"] for m in models])
    check("and prices come back unknown rather than zero",
          all(m["input"] is None and m["output"] is None for m in models),
          "a zero would sort an unpriced server top of every cheapest-first list")

    seen.clear()
    M.fetch_openai_compatible("http://box:8000", api_key="sk-test")
    check("a key is sent when there is one",
          seen.get("Authorization") == "Bearer sk-test", seen.get("Authorization"))
    seen.clear()
    M.fetch_openai_compatible("http://box:8000")
    check("and no Authorization header when there is not",
          "Authorization" not in seen,
          "a server on the LAN usually wants none, and an empty Bearer is a 401")
finally:
    urllib.request.urlopen = _real_urlopen

print()
print("the two Ollamas stay apart")
# The list itself is not the assertion -- restating it here would make this a
# copy to keep in step. What matters is that the page can render every one, so
# it is checked against MODEL_GROUPS in test_config_io.py's neighbourhood: a
# source with no group is models fetched and shown nowhere, which is exactly
# how thirty OpenCode Zen models became invisible.
check("no source is a duplicate", len(set(M.SOURCES)) == len(M.SOURCES), M.SOURCES)
# They speak the same API and mean opposite things about where the data goes.
# One id space would make the choice unreadable in assistant.models, which is
# the file this page writes.
ol = M.fetch_ollama.__doc__ or ""
check("fetch_ollama takes which one it is asking",
      "provider" in M.fetch_ollama.__code__.co_varnames,
      "otherwise both rosters come back claiming to be local")

# --- a model's provider has to name a source --------------------------------
# The picker groups by `provider` and filters each group against SOURCES. A
# fetcher that sets anything else puts its models in the cache and on offer
# nowhere: `PROVIDER` was "opencode" and served as both the models.dev
# lookup key and the field on every model, so thirty models sat in the
# catalogue and the OpenCode Zen group rendered empty. Nothing failed -- the
# page just quietly offered Ollama and nothing else.

print()
print("every fetcher labels its models with a source that exists")

urllib.request.urlopen = lambda req, timeout=None: _Models(req)
try:
    checks = [
        ("openai_compatible", M.fetch_openai_compatible("http://box:8000")[0]),
        ("ollama_cloud", M.fetch_ollama("http://box:11434", provider="ollama_cloud")[0]),
        ("ollama_vision", M.fetch_ollama("http://box:11435", provider="ollama_vision")[0]),
        ("ollama", M.fetch_ollama("http://box:11434")[0]),
    ]
finally:
    urllib.request.urlopen = _real_urlopen

for expected, models in checks:
    got = {m["provider"] for m in models}
    check(f"  {expected}", got in ({expected}, set()), got)
    check(f"  {expected} is a source", expected in M.SOURCES)

# The id is what assistant.models takes, prefix included, and the vision
# instance has its own: a model pulled on :11435 written `ollama:` would be
# looked for on :11434 and 404 on the first photo.
_prefix = {"ollama": "ollama:", "ollama_cloud": "ollama-cloud:", "ollama_vision": "ollama-vision:"}
for expected, models in checks:
    if expected in _prefix and models:
        check(f"  {expected} models are written {_prefix[expected]}<name>",
              all(m["id"].startswith(_prefix[expected]) for m in models),
              [m["id"] for m in models][:3])
check("  the vision instance is local: free, and on the local side of the split",
      "ollama_vision" in M.LOCAL_SOURCES)

# The one that was wrong, and the only fetcher whose provider string comes out
# of somebody else's JSON -- so the stub answers under models.dev's name and
# the check is that the model does not come back wearing it.
urllib.request.urlopen = lambda req, timeout=None: _ModelsDev(req)
try:
    _live, _err = M.fetch_opencode_zen()
finally:
    urllib.request.urlopen = _real_urlopen
check("  opencode_zen", {m["provider"] for m in _live} == {"opencode_zen"},
      {m["provider"] for m in _live})
check("  and models.dev's own name for it is kept apart",
      M.MODELS_DEV_PROVIDER == "opencode" and "opencode_zen" in M.SOURCES,
      "one constant for the lookup key and the label is how this broke")

# --- the three keyed providers ----------------------------------------------

print()
print("a key is the whole of switching these on")
for source, fetch in (("openrouter", M.fetch_openrouter),
                      ("together", M.fetch_together),
                      ("openai", M.fetch_openai)):
    check(f"  {source} is a source", source in M.SOURCES)

# No key, no call. Together and OpenAI need one even to list, so asking without
# one spends a round trip to be told 401 -- and the honest answer for a
# provider nobody configured is an empty list, not an error on the page.
_called = []
urllib.request.urlopen = lambda req, timeout=None: _called.append(req.full_url) or _Models(req)
try:
    for fetch in (M.fetch_together, M.fetch_openai):
        _called.clear()
        models, err = fetch("")
        check(f"  {fetch.__name__} with no key does not call out",
              not _called and models == [] and err == "", (_called, err))
finally:
    urllib.request.urlopen = _real_urlopen


class _Priced:
    """One OpenRouter row, in the shape their API actually returns."""

    def __init__(self, req):
        seen["_url"] = req.full_url

    def read(self):
        return json.dumps({"data": [{
            "id": "acme/big", "name": "Acme Big",
            # Dollars per *token*, as strings. This is the conversion that
            # matters: left alone it puts 0.0000008 in a column of dollars per
            # million and reads as free.
            "pricing": {"prompt": "0.0000008", "completion": "0.000002",
                        "input_cache_read": "0.0000001"},
            "context_length": 200000,
            "architecture": {"input_modalities": ["text", "image"]},
            "top_provider": {"max_completion_tokens": 32000},
        }]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


import json


urllib.request.urlopen = lambda req, timeout=None: _Priced(req)
try:
    models, err = M.fetch_openrouter("sk-or-test")
    check("openrouter parses a row", len(models) == 1 and not err, (models, err))
    m = models[0]
    check("  per-token prices become per-million",
          (m["input"], m["output"], m["cache_read"]) == (0.8, 2.0, 0.1),
          (m["input"], m["output"], m["cache_read"]))
    check("  and without the float noise the multiplication introduces",
          repr(m["input"]) == "0.8", repr(m["input"]))
    check("  the id carries the prefix assistant.models takes",
          m["id"] == "openrouter:acme/big", m["id"])
    check("  vision comes from the provider, not from the name",
          m["vision"] is True, m)
    check("  and the limits travel",
          (m["context"], m["max_output"]) == (200000, 32000), m)
finally:
    urllib.request.urlopen = _real_urlopen

# A price that will not parse is unknown, never zero: `_cheapest` sorts None
# last, and a zero would sort a paid model to the top of every list.
check("an unparseable price is unknown", M._per_million("nonsense") is None)
check("and so is a missing one", M._per_million(None) is None)

# Both envelopes. OpenAI and OpenRouter answer {"data": [...]}, Together a
# bare array; a fetcher that knows only one silently returns nothing.
check("a bare array is a model list too", M._rows([{"id": "x"}]) == [{"id": "x"}])
check("and so is {'data': [...]}", M._rows({"data": [{"id": "x"}]}) == [{"id": "x"}])
check("anything else is empty rather than an exception", M._rows({"error": "no"}) == [])

# --- every model answers the roster's filters -------------------------------
# The roster filters on `vision` and `audio` and reads them off each row. A
# fetcher that omits one renders `data-audio=""`, which is neither "1" nor
# absent -- the row survives the filter it should not, and the household is
# offered a model for a job it cannot do.

print()
print("every fetcher answers both capability questions")
urllib.request.urlopen = lambda req, timeout=None: _Priced(req)
try:
    _sets = [("openrouter", M.fetch_openrouter("k")[0])]
finally:
    urllib.request.urlopen = _real_urlopen
urllib.request.urlopen = lambda req, timeout=None: _Models(req)
try:
    _sets += [("openai_compatible", M.fetch_openai_compatible("http://box:8000")[0]),
              ("openai", M.fetch_openai("k")[0])]
finally:
    urllib.request.urlopen = _real_urlopen


class _Tags:
    """Ollama's /api/tags, which is `{"models": [...]}` and not `{"data": ...}`."""

    def __init__(self, req):
        seen["_url"] = req.full_url

    def read(self):
        return b'{"models": [{"name": "qwen3-vl:8b", "details": {"family": "qwen3vl"}}]}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


urllib.request.urlopen = lambda req, timeout=None: _Tags(req)
try:
    _sets.append(("ollama", M.fetch_ollama("http://box:11434")[0]))
finally:
    urllib.request.urlopen = _real_urlopen

for source, models in _sets:
    check(f"  {source} states vision and audio on every model",
          models and all(isinstance(m.get("vision"), bool)
                         and isinstance(m.get("audio"), bool) for m in models),
          [{k: m.get(k) for k in ("id", "vision", "audio")} for m in models][:2])

# The one that reads capabilities off a published list rather than off a name.
urllib.request.urlopen = lambda req, timeout=None: _ModelsDev(req)
try:
    _live, _err = M.fetch_opencode_zen()
finally:
    urllib.request.urlopen = _real_urlopen
check("  opencode_zen too", all(isinstance(m.get("audio"), bool) for m in _live))
check("  and both are read from the modality list",
      _live and _live[0]["vision"] is True and _live[0]["audio"] is True,
      "the stub says input is text, image and audio")

# --- together.ai lists models a serverless key cannot call --------------------
#
# 170 chat models listed, a minority actually servable on a serverless key; the
# rest need a dedicated endpoint rented by the hour. Nothing in /v1/models says
# which is which -- a working model and a refused one were diffed field by
# field and differ only in name, context length and price -- and the price is
# not the tell either: models at $1.74, $0.95 and $0.45 per million are all
# refused. So they are asked, once, and the answer is remembered.
#
# This is not cosmetic. Two roles in one household were configured from this
# list to models that answer 400 on every turn, and `programmer` gets no retry
# and no fallback for it, because a 400 is neither transient nor
# model-specific. It simply fails.

print("\nthe together roster leaves out what the key cannot call")
_ROWS = [
    {"id": "together:good/one", "name": "Good One", "audio": False, "vision": False},
    {"id": "together:dedicated/one", "name": "Dedicated", "audio": False, "vision": False},
    {"id": "together:streaming/only", "name": "Streaming", "audio": False, "vision": False},
]
_ANSWERS = {"good/one": True, "dedicated/one": False, "streaming/only": None}
_real_callable, _real_serverless = M._together_callable, M._together_serverless
_asked = []


def _stub_callable(key, model_id):
    _asked.append(model_id)
    return _ANSWERS[model_id]


M._together_callable = _stub_callable
M._together_serverless = lambda key: set()
_real_probe = M.PROBE_MODELS
try:
    # Probing is off by default now -- the page hid models the household had
    # deliberately enabled, and paid for the privilege on a schedule. The logic
    # is kept for a household that turns it back on, and this is what it does.
    M.PROBE_MODELS = True
    _v = {}
    _out = M._together_drop_dedicated([dict(r) for r in _ROWS], "k", _v)
    _ids = {r["id"] for r in _out}
    check("  a model the key can call stays", "together:good/one" in _ids, _ids)
    check("  one that needs a dedicated endpoint goes",
          "together:dedicated/one" not in _ids, _ids)
    # The other 400 together.ai returns. Those models work -- nanobot streams --
    # and reading every 400 as "dedicated" would have hidden two of them.
    check("  and 'only supports streaming' is NOT that refusal",
          "together:streaming/only" in _ids,
          "a 400 that is not the non-serverless one must leave the model alone")
    check("  a definite answer is remembered",
          _v == {"together:good/one": True, "together:dedicated/one": False}, _v)

    # The cache is what makes this affordable: ~150 probes and half a minute on
    # a fresh install, and nothing on every refresh after it. Without it the
    # models page blocks for 33s on the one visit where it must not.
    _asked.clear()
    M._together_drop_dedicated([dict(r) for r in _ROWS], "k", _v)
    check("  and nothing settled is asked twice", _asked == ["streaming/only"], _asked)

    # A rate-limited minute must not become a permanent verdict, and must not
    # empty the picker -- a household reading "these models are gone" rewrites
    # a working config.
    M._together_callable = lambda key, model_id: None
    _v2 = {}
    _out = M._together_drop_dedicated([dict(r) for r in _ROWS], "k", _v2)
    check("  a night when nothing answers hides nothing", len(_out) == len(_ROWS))
    check("  and remembers nothing", _v2 == {}, _v2)

    # The endpoints registry is sound and incomplete -- 16 of 170 chat models on
    # the day this was written -- so it may only SKIP a probe, never hide a row.
    M._together_serverless = lambda key: {"dedicated/one"}
    M._together_callable = _stub_callable
    _asked.clear()
    _out = M._together_drop_dedicated([dict(r) for r in _ROWS], "k", {})
    check("  a model the registry vouches for is not asked",
          "dedicated/one" not in _asked, _asked)
    check("  and is offered", "together:dedicated/one" in {r["id"] for r in _out})
finally:
    M._together_callable, M._together_serverless = _real_callable, _real_serverless
    M.PROBE_MODELS = _real_probe

# --- what the zero-cost bar is actually for -----------------------------------

# It bars a *hosted* free tier, whose own documentation says collected data may
# be used to improve the model -- and unattended, what the assistant reads is
# this household. A model on the box in the cupboard is free for the opposite
# reason and is the most private option available.
#
# Shipped without that distinction, this refused notifications, events and
# vision on their Ollama models: the three highest-volume roles in the house,
# deliberately local, and already configured exactly that way. The page told
# the household its own settled configuration was not allowed.
print("\nthe zero-cost bar is about hosted free tiers, not local models")
check("ollama on notifications is fine",
      M.zero_cost_ok("notifications", {"provider": "ollama", "input": 0.0}))
check("  and on events", M.zero_cost_ok("events", {"provider": "ollama", "input": 0.0}))
check("  and on vision", M.zero_cost_ok("vision", {"provider": "ollama", "input": 0.0}))
check("a row merely flagged local is enough",
      M.zero_cost_ok("everyday", {"local": True, "input": 0.0}))
check("every local source counts, not just ollama",
      all(M.zero_cost_ok("everyday", {"provider": s, "input": 0.0})
          for s in M.LOCAL_SOURCES), M.LOCAL_SOURCES)

check("a hosted free model is still refused",
      not M.zero_cost_ok("everyday", {"provider": "opencode_zen", "input": 0.0}))
check("  including on the fallback",
      not M.zero_cost_ok("fallback", {"provider": "together", "input": 0.0}))
check("a hosted paid model is fine",
      M.zero_cost_ok("everyday", {"provider": "opencode_zen", "input": 0.2}))
check("an unpriced model is not a free one",
      M.zero_cost_ok("everyday", {"provider": "opencode_zen"}))
check("and a role nobody barred takes anything",
      M.zero_cost_ok("image_normal", {"provider": "opencode_zen", "input": 0.0}))


# --- the session header OpenCode began requiring on 2026-09-06 --------------
#
# "Requests missing this header may error." The mail named four callers on this
# account by user-agent; two of them are this repo's, and `home-stack-admin` is
# this file's module. What is pinned is that the header is sent, that it is
# stable, and that it does not leak to the providers it is meaningless for.


def _capture(call):
    """The headers *call* would send, without calling anything."""
    import urllib.request
    real = urllib.request.Request
    seen = {}

    def spy(url, data=None, headers=None, **kw):
        seen.clear()
        seen.update(headers or {})
        seen['__url__'] = url
        raise urllib.error.URLError('not sending this')

    urllib.request.Request = spy
    try:
        call()
    except Exception:
        pass
    finally:
        urllib.request.Request = real
    return seen


def _capture_zen_probe():
    return _capture(lambda: M._zen_callable('a-key', 'a-model'))


_probe = _capture_zen_probe()
check("the Zen probe sends x-opencode-session",
      'x-opencode-session' in _probe, sorted(_probe))
check("  and it is this process's id",
      _probe.get('x-opencode-session') == M._ZEN_SESSION)
check("  on Zen's endpoint, never the flat plan",
      _probe.get('__url__', '').startswith('https://opencode.ai/zen/v1'),
      _probe.get('__url__'))
check("  with the authorization it always sent",
      _probe.get('Authorization', '').startswith('Bearer '))
check("the id is stable for the life of the page",
      _capture_zen_probe().get('x-opencode-session') == M._ZEN_SESSION)
check("and it is not a constant every household would share",
      M._ZEN_SESSION != 'home-stack-admin-' + '0' * 32)

# The other half of this file that reaches opencode.ai: the Models page's
# "test this role" button, which posts a real two-step turn. It is the same
# host and the same requirement, and it is reached with a base that is only
# Zen's for one of the six sources it can be pointed at.
_body = {"model": "m", "messages": [{"role": "user", "content": "ok"}]}
_test_zen = _capture(lambda: M._probe_post(M.ZEN_API_BASE, 'a-key', _body))
check("the role tester sends x-opencode-session too",
      _test_zen.get('x-opencode-session') == M._ZEN_SESSION, sorted(_test_zen))
for _base in ("https://api.together.xyz/v1", "http://127.0.0.1:11434/v1",
              "https://openrouter.ai/api/v1"):
    _sent = _capture(lambda b=_base: M._probe_post(b, 'a-key', _body))
    check(f"  and nobody else is tagged with it ({_base})",
          'x-opencode-session' not in _sent, sorted(_sent))
check("  a keyless endpoint still sends no authorization",
      'Authorization' not in _capture(
          lambda: M._probe_post("http://127.0.0.1:11434/v1", '', _body)))

print("\nand by default the page asks the providers nothing")
# The household could not find models it had enabled on purpose -- gpt-5.4-mini,
# nano, terra, muse-spark, gemini-3.8-flash -- because a probe had judged them
# and a judged model was removed. Two costs, one visible and one not: a roster
# shorter than the provider's own, and paid requests fired at two providers on
# every refresh. What is known is shown beside the model now instead.
_probe_asked = []
_rp, _rc, _rs = M.PROBE_MODELS, M._together_callable, M._together_serverless
_rz = M._zen_callable
M.PROBE_MODELS = False
M._together_callable = lambda k, m: _probe_asked.append(m)
M._zen_callable = lambda k, m: _probe_asked.append(m)
M._together_serverless = lambda k: _probe_asked.append("serverless") or set()
try:
    _rows = [{"id": "together:a/b", "audio": False, "vision": False},
             {"id": "together:c/d", "audio": False, "vision": False}]
    _kept = M._together_drop_dedicated([dict(r) for r in _rows], "a-key", {})
    check("  every together model stays", len(_kept) == 2, _kept)

    _zrows = [{"id": "gemini-3.8-flash"}, {"id": "deepseek-v4-flash"}]
    _zkept = M._zen_drop_unsupported([dict(r) for r in _zrows], "a-key", {})
    check("  every zen model stays, including the ones once hidden",
          {r["id"] for r in _zkept} == {"gemini-3.8-flash", "deepseek-v4-flash"},
          [r["id"] for r in _zkept])
    check("  and no provider was asked anything", _probe_asked == [], _probe_asked)

    _note = next(r for r in _zkept if r["id"] == "gemini-3.8-flash")
    check("  what is known about one is attached, not acted on",
          "500s" in (_note.get("note") or ""), _note)
    _plain = next(r for r in _zkept if r["id"] == "deepseek-v4-flash")
    check("  and a model with nothing known carries no note",
          "note" not in _plain, _plain)
finally:
    M.PROBE_MODELS, M._together_callable, M._together_serverless = _rp, _rc, _rs
    M._zen_callable = _rz

print("\nthe zen roster comes from zen, not from the catalogue")
# models.dev lists 97 opencode models; an account is offered a subset -- 13 here
# -- and nothing in the catalogue says which. That used to be settled by asking
# every model one token and reading the refusals, which hid `gemini-3.8-flash`
# and `gpt-5.6-terra` after the household had gone and enabled them. The
# provider's own /v1/models answers it in one GET, with nothing generated.
_real_get, _real_offered = M._get, M._zen_offered
_catalogue = {
    "opencode": {"models": {
        "in-both":     {"name": "In Both",  "cost": {"input": 1.0, "output": 2.0}},
        "not-offered": {"name": "Gone",     "cost": {"input": 1.0, "output": 2.0}},
        "free-one":    {"name": "Free",     "cost": {"input": 0.0, "output": 0.0}},
    }}
}
M._get = lambda *a, **k: _catalogue
try:
    M._zen_offered = lambda key: {"in-both", "free-one", "brand-new"}
    _rows, _err = M.fetch_opencode_zen("a-key")
    _ids = {r["id"] for r in _rows}
    check("  a model the account has is offered", "in-both" in _ids, _ids)
    check("  one it does not have is not", "not-offered" not in _ids, _ids)
    check("  one the catalogue has not heard of yet is kept",
          "brand-new" in _ids, _ids)
    _new = next(r for r in _rows if r["id"] == "brand-new")
    check("    unpriced, because nothing published a price",
          _new["input"] is None, _new)
    # The free tier is a policy filter and runs first. Re-adding it here as an
    # "unknown" model would undo that rule *and* misprice it.
    check("  the free tier stays out, and not as an unpriced stranger",
          "free-one" not in _ids, _ids)

    # A provider that will not say what it offers must not empty the picker.
    M._zen_offered = lambda key: None
    _rows2, _ = M.fetch_opencode_zen("a-key")
    check("  and when the roster cannot be read, the catalogue stands",
          {r["id"] for r in _rows2} == {"in-both", "not-offered"},
          {r["id"] for r in _rows2})

    # No key, no call: the page still renders for a household that has not set one.
    _asked = []
    M._zen_offered = lambda key: _asked.append(key)
    M.fetch_opencode_zen("")
    check("  no key means the roster is never asked for", _asked == [], _asked)
finally:
    M._get, M._zen_offered = _real_get, _real_offered

print("\nand every call to opencode.ai names its session")
# Added because it was forgotten the moment a new call appeared. `_zen_offered`
# was written weeks after the header work and went out with Authorization only
# -- exactly the request OpenCode said would start erroring. A test that names
# the *function* would not have caught it either; this one watches what leaves.
_sent = []
_real_get2, _real_urlopen = M._get, M.urllib.request.urlopen


def _watch_get(url, timeout=20, headers=None):
    _sent.append((url, dict(headers or {})))
    raise M.urllib.error.URLError("not sending this")


M._get = _watch_get
try:
    M._zen_offered("a-key")
    check("  the roster call was made", len(_sent) == 1, _sent)
    _url, _hdrs = _sent[0]
    check("  to zen, never the flat plan",
          _url.startswith("https://opencode.ai/zen/v1"), _url)
    check("  carrying x-opencode-session",
          "x-opencode-session" in _hdrs, sorted(_hdrs))
    check("  and it is this process's id",
          _hdrs.get("x-opencode-session") == M._ZEN_SESSION, _hdrs.get("x-opencode-session"))
    check("  with the authorization it needs",
          _hdrs.get("Authorization", "").startswith("Bearer "), sorted(_hdrs))
finally:
    M._get = _real_get2

print("\nand a known failure says why, instead of just 500")
# The Test button posts /chat/completions, which is what nanobot posts -- so for
# the six models that answer only on /v1/responses it fails, and that verdict is
# correct: the assistant would fail the same way. What it must not do is report
# a bare 500, which reads as the provider being down and sends somebody to check
# a service that is fine.
#
# Deliberately NOT fixed by probing /v1/responses instead: that would go green
# for a model the assistant still cannot call, and a false pass on this page is
# worse than a confusing failure.
_real_pp = M._probe_post
M._probe_post = lambda base, key, body, timeout=60: (None, "HTTP 500 from the provider")
try:
    _v = M.probe_model("gemini-3.8-flash", "https://opencode.ai/zen/v1", "k")
    _detail = " ".join(str(s.get("detail") or "") for s in _v["steps"])
    check("  it still fails", _v["ok"] is False, _v["ok"])
    check("  and names the reason", "500s" in _detail, _detail[:90])

    _v2 = M.probe_model("deepseek-v4-flash", "https://opencode.ai/zen/v1", "k")
    _d2 = " ".join(str(s.get("detail") or "") for s in _v2["steps"])
    check("  a model with nothing known is left as it was",
          "responses" not in _d2, _d2[:90])
finally:
    M._probe_post = _real_pp

print("\nand a cache built by older code counts as stale")
# The roster moved from models.dev-plus-probing to the provider's own
# /v1/models, taking the picker from 5 models to 10 -- and the page showed 5 for
# two days afterwards. The cache was 47 hours old against a 7-day life, so every
# measure the page had called it fresh, while the code that built it no longer
# existed. Nobody could see the fix, and the advice was to press Refresh, which
# rebuilt it for an unrelated reason.
_fresh = {"checked_at": time.time(), "catalogue_version": M.CATALOGUE_VERSION}
check("  a current cache is not stale",
      M.cache_is_stale(_fresh, 7 * 24 * 3600) is False)
check("  an old one is",
      M.cache_is_stale({"checked_at": time.time() - 8 * 24 * 3600,
                        "catalogue_version": M.CATALOGUE_VERSION},
                       7 * 24 * 3600) is True)
check("  and a fresh one from older code is too",
      M.cache_is_stale({"checked_at": time.time(),
                        "catalogue_version": M.CATALOGUE_VERSION - 1},
                       7 * 24 * 3600) is True)
check("  as is one that predates the stamp entirely",
      M.cache_is_stale({"checked_at": time.time()}, 7 * 24 * 3600) is True)
check("  an empty cache is stale rather than an error",
      M.cache_is_stale({}, 7 * 24 * 3600) is True)

_tmp = pathlib.Path(tempfile.mkdtemp()) / "cat.json"
M.save_cache(_tmp, {"checked_at": time.time(), "opencode_zen": []})
check("  and save_cache stamps what wrote it",
      json.loads(_tmp.read_text()).get("catalogue_version") == M.CATALOGUE_VERSION,
      json.loads(_tmp.read_text()).get("catalogue_version"))

print()
print("the /v1/responses rule is the same in all three places that hold it")

_ROSTER = ["gpt-5.3-codex", "gpt-5.6-luna", "gpt-5.6-terra", "o3-mini",
           "deepseek-v4-flash", "kimi-k2.7-code", "glm-5.2", "big-pickle"]

# The other two copies, read from source rather than imported: deploy/models.py
# and the nanobot provider ship as different services and neither is importable
# from this container. Reading them here is what makes a drift a failing test
# instead of a household reading three working models as broken.
_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TOKENS = ('"gpt-5", "gpt-6", "o1", "o3", "o4"')
for _rel in ("deploy/models.py",
             "services/nanobot/nanobot/providers/openai_compat_provider.py"):
    _src = (_ROOT / _rel).read_text(encoding="utf-8")
    check(f"  {_rel} tests the same four families", _TOKENS in _src)

for _m in _ROSTER:
    _want = any(t in _m for t in ("gpt-5", "gpt-6", "o1", "o3", "o4"))
    check(f"  {_m} -> {'responses' if _want else 'chat/completions'}",
          M._wants_responses(_m) is _want)

check("  and the probe declines rather than failing such a model",
      M.probe_model("gpt-5.6-luna", "https://opencode.ai/zen/v1", "k")["ok"]
      is None)
# The decline is scoped to OpenCode's gateway, and the same model name
# somewhere else must still be probed. Proven with the POST stubbed out
# rather than by letting it fly: a suite that posts to a paid provider is
# one nobody can run twice, and this house has said so explicitly.
_posted = []
_real_post = M._probe_post
M._probe_post = lambda base, key, body, timeout=60: (
    _posted.append(base) or (None, "stubbed"))
try:
    _verdict = M.probe_model("gpt-5.6-luna", "https://api.together.xyz/v1", "k")
finally:
    M._probe_post = _real_post
check("  and declines only on that gateway, not everywhere",
      _posted == ["https://api.together.xyz/v1"] and _verdict["ok"] is False,
      f"posted={_posted} ok={_verdict['ok']!r}")

print()
print("the flat plan is reachable from Code and from nowhere else")

_GO = [{"id": "kimi-k3", "context": 256000, "provider": M.GO_CACHE_KEY}]
_ZEN = [{"id": "gpt-5.3-codex", "context": 400000, "provider": "opencode_zen"}]
_CAT = {M.GO_CACHE_KEY: _GO, "opencode_zen": _ZEN}

check("  Go is not a source, so no role picker can draw it",
      M.GO_CACHE_KEY not in M.SOURCES and "opencode-go" not in M.SOURCES)
check("  and all_models() -- what every role picker reads -- omits it",
      [m for m in M.all_models(_CAT) if m.get("provider") == M.GO_CACHE_KEY] == [])
check("  while the Code picker offers both plans",
      [g["key"] for g in M.code_model_choices(_CAT)] == ["go", "zen"])
check("  spelled the way opencode reads them",
      [m["id"] for g in M.code_model_choices(_CAT) for m in g["models"]]
      == ["opencode-go/kimi-k3", "opencode/gpt-5.3-codex"])
check("  is_go_model catches the flat plan and nothing else",
      M.is_go_model("opencode-go/kimi-k3") is True
      and M.is_go_model("opencode/gpt-5.3-codex") is False
      and M.is_go_model("gpt-5.6-luna") is False
      and M.is_go_model("") is False)

# The cache key must not be the retired one: load_cache() folds RETIRED_SOURCE
# into opencode_zen, so reusing that string would put every Go model into the
# Zen roster -- and therefore into every role picker -- on the next page load.
check("  and the Go cache key is not the retired one",
      M.GO_CACHE_KEY != M.RETIRED_SOURCE, M.GO_CACHE_KEY)
check("  so a cache holding Go does not leak it into the Zen roster",
      M.GO_CACHE_KEY not in ("opencode_zen", M.RETIRED_SOURCE))

print()
print("refresh() keeps the Go roster, which is not a source")

# The bug this pins: refresh() builds its result with `for source in SOURCES`,
# and Go is deliberately not in SOURCES -- so the roster was fetched and then
# dropped, and the Code card would have stayed empty forever. The first test
# for this passed because it hand-built the catalogue instead of going through
# refresh(), which is exactly the seam that was broken.
_real_dev, _real_go, _real_zen = M._get, M.fetch_opencode_go, M.fetch_opencode_zen
_dev_calls = []
M._get = lambda url, timeout=20, headers=None: (
    _dev_calls.append(url) or {"opencode": {"models": {}},
                               "opencode-go": {"models": {}}})
M.fetch_opencode_go = lambda data=None: (
    [{"id": "kimi-k3", "provider": M.GO_CACHE_KEY}], "")
M.fetch_opencode_zen = lambda key="", verdicts=None, data=None: ([], "")
try:
    _out = pathlib.Path(tempfile.mkdtemp()) / "cat.json"
    _cat = M.refresh(_out, endpoints={})
finally:
    M._get, M.fetch_opencode_go, M.fetch_opencode_zen = _real_dev, _real_go, _real_zen

check("  the fetched Go roster survives into the catalogue",
      [m["id"] for m in (_cat.get(M.GO_CACHE_KEY) or [])] == ["kimi-k3"],
      _cat.get(M.GO_CACHE_KEY))
check("  and is written to the cache, not just returned",
      json.loads(_out.read_text()).get(M.GO_CACHE_KEY) is not None)
check("  while still reaching no role picker",
      [m for m in M.all_models(_cat)
       if m.get("provider") == M.GO_CACHE_KEY] == [])
check("  and models.dev is fetched once for both OpenCode rosters",
      _dev_calls.count(M.MODELS_DEV) == 1, _dev_calls)

# The checks that lived here covered `services/z-image`, the house's own image
# container, removed on 2026-09-08. What they were guarding against is worth
# keeping in view for whatever local model comes next, because the failure
# already happened once: a value the picker cannot represent is not merely
# invisible. The images card submits an empty field for it and the save path
# reads empty as "clear it", so `image_normal` set to a model missing from the
# options rendered as "none" -- and saving anything on that card deleted the
# household's choice and sent every drawing back to the paid API.
#
# Any future local model needs *both* halves added back: the option here and
# the prefix the deployer recognises. See docs/local-generation.md.


# ---------------------------------------------------------------------------
# Role probes: each role is asked what it is actually for, and scored
# ---------------------------------------------------------------------------

check("every probed role is a role this page offers",
      set(M.ROLE_PROBES) <= set(M.PERSONA_NEEDS),
      sorted(set(M.ROLE_PROBES) - set(M.PERSONA_NEEDS)))
check("and every probe says what it asks for and how it is scored",
      all(spec.get("why") and spec.get("criteria") and spec.get("messages")
          for spec in M.ROLE_PROBES.values()))
check("every probe checks it got an answer at all, first",
      all(spec["criteria"][0][0] == "answers at all"
          for spec in M.ROLE_PROBES.values()),
      "the empty-content failure is silent; it has to be scored, not assumed")

# The answerless shape, which is the one this house keeps meeting: a reasoning
# model with no effort setting spends the budget thinking and returns empty
# content. It must read as a failed criterion, not as a probe that broke.
_answered = dict(M.ROLE_PROBES["everyday"]["criteria"])["answers at all"]
check("empty content scores as a failure", _answered("", {})[0] is False)
check("and a tool call with no text does not", _answered("", {"tool_calls": [{}]})[0] is True)

check("the last number in a reply is the answer, not the first",
      M._last_int("6 * 48 = 288, minus 32, so 256") == 256)
check("a fenced JSON reply still parses",
      M._json_payload('```json\n{"a": 1}\n```') == {"a": 1})

# The geofence line, and the reason this probe exists at all. `qwen3.5:4b`
# rendered "left work" as "went TO work" and "quit her job" -- fluent,
# confident, backwards, and nothing logged. Both directions are pinned: the
# first cut of this rule matched only `salió` and failed "acaba de salir",
# which is the commonest phrasing there is, and a rubric that fails a right
# answer sends somebody to replace a model that was doing its job.
_left = dict(M.ROLE_PROBES["events"]["criteria"])["says leaving, not arriving"]
for _text in ("Mora se fue de la etiqueta trabajo.",
              "Mora ya no esta en trabajo.",
              "Mora acaba de salir del lugar trabajo.",
              "Mora salio del trabajo.",
              "Mora ha salido del trabajo.",
              "Mora se ha marchado del trabajo.",
              "Mora abandono el trabajo."):
    check(f"reads as leaving: {_text!r}", _left(_text, {})[0] is True,
          _left(_text, {})[1])
for _text in ("Mora se ha ido a trabajar.",       # went TO work -- `a`, not `de`
              "Mora fue a trabajar.",             # went TO work
              "Mora ha dejado su trabajo.",       # quit her job
              "Mora renuncio a su trabajo.",      # resigned
              "Mora llego a trabajo.",            # arrived
              "Mora esta en casa."):              # says nothing about it
    check(f"is not read as leaving: {_text!r}", _left(_text, {})[0] is False,
          _left(_text, {})[1])

# The output rate. Absent when the provider reports no usage, because a 0
# renders as "measured, and very slow" rather than as "not measured" -- and
# Ollama, FreeToken and Zen all do report it, so an absence means something
# unusual rather than something routine.
_probe_posts = []
def _fake_post(base, key, body, timeout=60):
    _probe_posts.append(body)
    return ({"choices": [{"message": {"content": "OK"}}],
             "usage": {"completion_tokens": 120}}, "")
_real_post, M._probe_post = M._probe_post, _fake_post
try:
    _r = M.score_model("everyday", "m", "http://x/v1", "")
    check("a scored probe reports how many tokens came back", _r["tokens"] == 120)
    check("and a rate derived from them", isinstance(_r["tok_s"], float) and _r["tok_s"] > 0)
    M._probe_post = lambda b, k, bo, t=60: ({"choices": [{"message": {"content": "OK"}}]}, "")
    _r2 = M.score_model("everyday", "m", "http://x/v1", "")
    check("no usage reported is absent, not zero",
          _r2["tokens"] is None and _r2["tok_s"] is None)
    M._probe_post = lambda b, k, bo, t=60: (None, "HTTP 500")
    _r3 = M.score_model("everyday", "m", "http://x/v1", "")
    check("and a failed probe carries the same keys rather than omitting them",
          "tok_s" in _r3 and "tokens" in _r3 and _r3["tok_s"] is None)
finally:
    M._probe_post = _real_post

_scores_rate = pathlib.Path(tempfile.mkdtemp()) / "s.json"
M.record_score(_scores_rate, "everyday", "m", _r)
check("the rate is kept, so the page can show it before anybody presses Test",
      M.load_scores(_scores_rate)["everyday"]["m"]["tok_s"] == _r["tok_s"])


# The store. One row per (role, model), and the newer run replaces the older
# one rather than accumulating -- that is the whole shape of the file.
_scores_dir = tempfile.mkdtemp()
_scores = pathlib.Path(_scores_dir) / "model-scores.json"
M.record_score(_scores, "events", "freetoken:x",
               {"score": 3, "max": 4, "ok": False, "ms": 900, "steps": []})
check("a score survives a write and a read",
      M.load_scores(_scores)["events"]["freetoken:x"]["score"] == 3)
M.record_score(_scores, "events", "freetoken:x",
               {"score": 4, "max": 4, "ok": True, "ms": 800, "steps": []})
check("and the newer run replaces it rather than piling up",
      M.load_scores(_scores)["events"]["freetoken:x"]["score"] == 4
      and len(M.load_scores(_scores)["events"]) == 1)
M.record_score(_scores, "events", "ollama:qwen3.5:4b",
               {"score": 1, "max": 4, "ok": False, "ms": 700, "steps": []})
check("two models under one role are two answers, not one",
      len(M.load_scores(_scores)["events"]) == 2)
check("a store written by a future version is ignored, not half-read",
      M.load_scores(pathlib.Path(_scores_dir) / "nothing.json") == {})


# The gate goes LAST, and this is the third time that has mattered. Checks
# appended after it print FAIL, append to `failures`, and then fall through to
# "all checks passed" with exit 0 -- so a suite that cannot fail reports success,
# which is worse than having no suite. Anything added below this line is
# decoration; add checks above it.
print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
