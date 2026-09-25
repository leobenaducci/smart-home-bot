"""The Models page's benchmark: which model, where it comes from, what came back.

The benchmark itself runs inside a nanobot container (services/nanobot/bench/
model_bench.py), through the real agent loop. This module is the admin side's
pure logic -- no Flask, no docker -- so it can be tested on its own:

  * `resolve_model` turns what somebody typed into what `assistant.models`
    would write, and a Hugging Face link into the Ollama name that pulls it.
  * `load_results` reads the runs back for the comparison table.
  * `removable` says whether a pulled model can be deleted without breaking a
    role that uses it.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# assistant.models' prefixes (MODEL_PROVIDERS in deploy/deploy.py).
PREFIXES = ("ollama", "ollama-cloud", "ollama-vision", "openrouter", "together",
            "openai", "openai-compatible", "freetoken", "llamacpp")
MODEL_RE = re.compile(r"^[\w.:/@+\-]{1,200}$")
ROLES = ("everyday", "tools", "notifications", "events", "heartbeat", "longtask", "steps",
         "planner")
# What a household picks a model *for*, and which roles answer it. The Models
# page's roles do not map one-to-one onto the benchmark's: the everyday model
# is the one that calls the tools, and notifications, events and heartbeats are
# the same job -- short, unattended, and on the cheapest model that gets them
# right. Each role belongs to exactly one kind besides "overall".
KINDS = (("overall", ROLES),
         ("everyday", ("everyday", "tools")),
         ("background", ("notifications", "events", "heartbeat")),
         ("longtask", ("longtask",)),
         # One step of a plan, through the plan-steps harness: what the Plan
         # steps role is for, measured apart from running the whole house.
         ("steps", ("steps",)),
         # The plan itself: whether each step names what to call and the
         # steps that change something are marked -- what the step model has
         # to go on. Steps are answered without running.
         ("planner", ("planner",)))
KIND_OF_ROLE = {role: kind for kind, roles in KINDS[1:] for role in roles}
EFFORTS = ("", "none", "low", "medium", "high")

# A Hugging Face reference, however it was pasted: a URL, `hf.co/owner/repo`,
# `huggingface.co/owner/repo`, with or without `:QUANT`, with or without
# `ollama:` in front.
_HF_RE = re.compile(
    r"^(?:ollama:)?(?:https?://)?(?:www\.)?(?:hf\.co|huggingface\.co)/"
    r"(?P<repo>[\w.\-]+/[\w.\-]+)(?:/(?:tree|blob|resolve)/[^:]*)?(?::(?P<quant>[\w.\-]+))?/?$",
    re.IGNORECASE)
# Which quant to take when none was named: the usual best size/quality trade
# for a 12 GB card, then progressively smaller ones.
_QUANT_PREFERENCE = ("Q4_K_M", "Q4_K_S", "IQ4_XS", "Q4_0", "Q5_K_M", "Q3_K_M", "Q6_K", "Q8_0")
_QUANT_IN_NAME = re.compile(r"(?i)[-_.]((?:I?Q|F|BF)\d[\w]*?)(?:-\d{5}-of-\d{5})?\.gguf$")


class ResolveError(ValueError):
    """What to tell the person, not a stack trace."""


def split_model(value: str) -> tuple[str, str]:
    """`ollama:hf.co/a/b:Q4_K_M` -> ("hf.co/a/b:Q4_K_M", "ollama").

    The prefix is matched case-insensitively and returned lowercased. It used
    to be exact, so `Ollama:gemma4:e2b` matched no prefix at all, fell through
    to "custom", and was sent to OpenCode Zen -- a hosted endpoint -- while the
    card labelled it like any other row. A capitalised prefix is a typo, not a
    request for a different provider.
    """
    prefix, sep, rest = value.partition(":")
    if sep and prefix.lower() in PREFIXES and rest:
        return rest, prefix.lower()
    # Any other local Ollama instance: `ollama-<id>:` (cloud.ollama.instances).
    if sep and rest and is_local_prefix(prefix.lower()):
        return rest, prefix.lower()
    return value, ""


def is_local_prefix(prefix: str) -> bool:
    """`ollama`, `ollama-vision`, or `ollama-<id>` -- never `ollama-cloud`."""
    return prefix == "ollama" or (bool(re.fullmatch(r"ollama-[a-z][a-z0-9]{0,15}", prefix or ""))
                                  and prefix != "ollama-cloud")


def _hf_get(path: str, timeout: int = 20):
    req = urllib.request.Request(f"https://huggingface.co/api/{path}",
                                 headers={"User-Agent": "home-stack-admin"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


_SHARD_RE = re.compile(r"-\d{5}-of-\d{5}(?=\.gguf$)", re.IGNORECASE)


def gguf_quants(repo: str, fetch=_hf_get) -> dict[str, int]:
    """`{QUANT: bytes}` for every GGUF in *repo*.

    The shards of one split file are summed; two *different* files at the same
    quant are not. A repo can ship a plain build and a variant beside it --
    empero-ai's Qwythos has `…-Q4_K_M.gguf` and `…-MTP-Q4_K_M.gguf` -- and
    summing those reported 11.5 GB for a 5.6 GB pull. Ollama takes the plain
    one, which is the one with the shorter name, so that is the size given.
    """
    try:
        tree = fetch(f"models/{urllib.parse.quote(repo)}/tree/main?recursive=true")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ResolveError(f"{repo} is gated or private on Hugging Face; accept its "
                               f"licence there, or pick a public GGUF build.") from exc
        if exc.code == 404:
            raise ResolveError(f"{repo} does not exist on Hugging Face.") from exc
        raise ResolveError(f"Hugging Face answered {exc.code} for {repo}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ResolveError(f"could not reach Hugging Face: {exc}") from exc
    files: dict[str, dict[str, int]] = {}      # quant -> {file without shard suffix: bytes}
    for f in tree or []:
        path = str(f.get("path") or "")
        if not path.lower().endswith(".gguf") or "mmproj" in path.lower():
            continue
        m = _QUANT_IN_NAME.search(path)
        if m:
            base = _SHARD_RE.sub("", path)
            per = files.setdefault(m.group(1).upper(), {})
            per[base] = per.get(base, 0) + int(f.get("size") or 0)
    return {q: per[min(per, key=len)] for q, per in files.items()}


def resolve_model(text: str, fetch=_hf_get) -> tuple[str, str]:
    """(model as assistant.models writes it, a note for the page).

    A Hugging Face repo becomes `ollama:hf.co/<owner>/<repo>:<QUANT>`, which
    Ollama pulls straight from the hub. Only GGUF can go that way -- a
    safetensors or NVFP4 repo is refused here, with the reason, rather than
    left to fail twenty minutes into a pull.
    """
    text = (text or "").strip()
    if not text:
        raise ResolveError("name a model first.")
    m = _HF_RE.match(text)
    if not m:
        if not MODEL_RE.match(text):
            raise ResolveError("that does not look like a model name.")
        return text, ""
    repo, wanted = m.group("repo"), (m.group("quant") or "").upper()
    quants = gguf_quants(repo, fetch)
    if not quants:
        raise ResolveError(
            f"{repo} has no GGUF files. Ollama can only run GGUF, so safetensors, "
            f"NVFP4, AWQ or MLX builds cannot be tested here -- look for a "
            f"'{repo.split('/')[1]}-GGUF' repository (bartowski, unsloth and "
            f"lmstudio-community publish most of them).")
    if wanted:
        if wanted not in quants:
            raise ResolveError(f"{repo} has no {wanted}; it offers "
                               f"{', '.join(sorted(quants))}.")
        quant = wanted
    else:
        quant = next((q for q in _QUANT_PREFERENCE if q in quants), sorted(quants)[0])
    size_gb = quants[quant] / 1e9
    note = (f"{repo} at {quant} ({size_gb:.1f} GB)"
            + ("" if wanted else f" -- other quants: {', '.join(q for q in sorted(quants) if q != quant)}"))
    return f"ollama:hf.co/{repo}:{quant}", note


def roles_measured(doc: dict) -> set[str]:
    """The roles a run has numbers for. "all" is the total, not a role."""
    return {k for k in (doc.get("summary") or {}) if k != "all"}


def merge_runs(docs: list[dict]) -> dict:
    """Runs of ONE model, newest first -> one row: the newest result per role.

    A run covers only the roles its chips were ticked for. Re-running `tools`
    alone used to replace the whole row -- and `supersede` deleted the file it
    replaced, so the other four roles' numbers were gone for good. The card
    compares models, so it still wants one row per model; it just has to build
    that row per role rather than per file.

    The newest run is the base (its provider, digests, container and speed
    probe describe the model as it stands now). Older runs fill in only the
    roles the newer ones never measured, and each role carries the date it was
    measured so a stale half of a row cannot read as fresh.
    """
    if not docs:
        return {}
    merged = dict(docs[0])
    summary = dict(merged.get("summary") or {})
    cases = list(merged.get("cases") or [])
    effort = dict(merged.get("effort") or {})
    role_from = {r: {"started": str(docs[0].get("started") or ""),
                     "file": docs[0].get("file", "")} for r in roles_measured(docs[0])}
    contributing = [docs[0].get("file", "")]
    for doc in docs[1:]:
        extra = roles_measured(doc) - set(summary)
        if not extra:
            continue
        contributing.append(doc.get("file", ""))
        for role in extra:
            summary[role] = (doc.get("summary") or {})[role]
            role_from[role] = {"started": str(doc.get("started") or ""),
                               "file": doc.get("file", "")}
            if role in (doc.get("effort") or {}):
                effort[role] = (doc.get("effort") or {})[role]
        cases += [c for c in (doc.get("cases") or []) if c.get("role") in extra]
    # `all` is recomputed rather than carried: the newest run's total counts
    # only its own roles, and a merged row that says 4/4 while showing five
    # roles is worse than no total at all.
    passed = sum(v.get("passed", 0) for k, v in summary.items() if k != "all")
    total = sum(v.get("total", 0) for k, v in summary.items() if k != "all")
    rates = sorted(c["tok_s"] for c in cases if c.get("tok_s"))
    summary["all"] = {"passed": passed, "total": total,
                      "tok_s_median": rates[len(rates) // 2] if rates else None}
    merged["summary"] = summary
    merged["cases"] = cases
    merged["effort"] = effort
    merged["role_from"] = role_from
    # Only a genuine merge is worth saying so on the card.
    merged["merged_from"] = contributing if len(contributing) > 1 else []
    merged["seconds"] = sum(d.get("seconds") or 0 for d in docs
                            if d.get("file", "") in contributing)
    return merged


def load_results(directory: Path, limit: int = 30) -> list[dict]:
    """The runs, newest first, one per model, each with its file name for the page's buttons.

    One per model because the card compares models, not runs: a second run of
    the same model replaces the first (`supersede`), and a file that outlived
    that -- written before it existed -- is hidden here the same way, newest
    first, so what a rerun does and what the page shows never disagree.
    """
    by_model: dict[str, list[dict]] = {}
    order: list[str] = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        model = str(doc.get("model") or "")
        doc["file"] = path.name
        if model not in by_model:
            by_model[model] = []
            order.append(model)
        by_model[model].append(doc)
    out = []
    for model in order:
        out.append(merge_runs(by_model[model]))
        if len(out) >= limit:
            break
    return out


def results_for(directory: Path, model: str) -> list[Path]:
    """Every result file *model* has in *directory*, newest first."""
    found = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(doc.get("model") or "") == model:
            found.append(path)
    return found


def supersede(directory: Path, model: str, keep: str) -> list[str]:
    """Drop every earlier result *model* has, so *keep* is the one. The names dropped.

    The last run is the one that counts: the cases may have changed since the
    earlier one, the model may have been re-pulled, and two rows for one model
    make the card answer "which run" when it is asked "which model".
    """
    keep_roles: set[str] = set()
    for path in results_for(directory, model):
        if path.name != keep:
            continue
        try:
            keep_roles = roles_measured(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            keep_roles = set()
        break
    dropped = []
    for path in results_for(directory, model):
        if path.name == keep:
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        # Only what this run genuinely replaces. A run of `tools` alone does
        # not supersede a full run: deleting it threw away four roles nobody
        # asked to re-measure, and load_results now merges them instead.
        if roles_measured(doc) - keep_roles:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        dropped.append(path.name)
    return dropped


def _named_models(cfg: dict) -> set[str]:
    """Every Ollama model name any role, chain or slot in assistant.models uses."""
    names: set[str] = set()
    for value in ((cfg.get("assistant") or {}).get("models") or {}).values():
        for v in (value if isinstance(value, list) else [value]):
            name, prefix = split_model(str(v or "").strip())
            if is_local_prefix(prefix) and name:
                names.add(name)
    return names


def removable(cfg: dict, model: str) -> bool:
    """A local model nothing in the house is configured to use."""
    name, prefix = split_model(model)
    return is_local_prefix(prefix) and name not in _named_models(cfg)


# --- what the page draws -------------------------------------------------------

def _grade(passed: int, total: int) -> str:
    if not total:
        return "none"
    pct = passed * 100 / total
    return "good" if pct >= 90 else ("part" if pct >= 50 else "bad")


def display_name(model: str) -> tuple[str, str]:
    """(what to show, where it runs): `ollama:hf.co/a/b:Q4` -> ("a/b:Q4", "hf")."""
    name, prefix = split_model(model)
    if name.lower().startswith("hf.co/"):
        return name[6:], "hf"
    if is_local_prefix(prefix) or prefix in ("freetoken", "llamacpp"):
        # llamacpp: a llama-server on this machine's GPU -- the fork Ternary
        # Bonsai needs, which Ollama cannot load. Local like the other two.
        # FreeToken is an engine on this household's own GPU, like Ollama. It
        # read as "hosted" purely because the prefix was not in this list, which
        # put a HOSTED badge on a model running in the next room -- and skipped
        # the parameter chip, which only local rows carry.
        return name, "local"
    return (name if prefix else model), "hosted"


# A size in a model name: the `:12b` tag Ollama uses, Google's `e4b`, or a
# `-35B-` buried in a Hugging Face name. Anchored on a word boundary so the
# `4` in `qwen2.5-coder` or a `Q4_K_M` quant is never read as a size.
_SIZE_IN_NAME = re.compile(r"(?i)(?<![a-z0-9.])(e?\d+(?:\.\d+)?)\s*b(?![a-z0-9])")


def params_of(model: str, lookup: dict | None = None) -> str:
    """How many parameters, as a short label, or "" if nothing says.

    Every local row should carry this: `gemma4` and `gemma4:12b` are different
    models and the card exists to compare them, so a row that names neither its
    size nor its tag is a row you cannot read. Three sources, in order of trust:

      1. `lookup` -- Ollama's own `details.parameter_size`, asked of the server
         that holds the model. The only one that is measured rather than parsed.
      2. The tag: `:12b`, `:e4b`. What somebody typed, and usually right.
      3. A size token anywhere in the name, for Hugging Face builds that carry
         it there (`Spark-X2.5-4B`, `Qwen3.6-35B-A3B`).

    A bare `gemma4` has none of 2 or 3 -- which is exactly the row that sent
    somebody asking what it was -- so 1 is what fills it in.
    """
    name, _prefix = split_model(model)
    if lookup:
        for key in (name, name + ":latest", model):
            got = lookup.get(key) or lookup.get(str(key).lower())
            if got:
                return str(got)
    base, _, tag = name.rpartition(":") if ":" in name else (name, "", "")
    for candidate in (tag, base):
        m = _SIZE_IN_NAME.search(candidate or "")
        if m:
            return m.group(1).upper() + "B"
    return ""


def size_chip(model: str, shown: str, lookup: dict | None = None) -> str:
    """The size to put on the card, or "" when the name already says it.

    `Spark-X2.5-4B` with a `4B` chip beside it states one fact twice. If the
    server reports something the name does not -- 4.2B against the name's 4B --
    both are kept, because then they are not the same fact.
    """
    size = params_of(model, lookup)
    if not size:
        return ""
    flat = size.upper().replace(" ", "")
    # `gemma4:e2b` keeps its tag (a variant, not a bare size) and, with nothing
    # pulled to ask, the size is parsed from that same tag -- so the card read
    # "gemma4 e2b E2B". Whatever is already on the card wins; the size chip only
    # earns its place by saying something new.
    if variant_of(shown).upper() == flat:
        return ""
    base = shown.rpartition(":")[0] if ":" in shown else shown
    m = _SIZE_IN_NAME.search(base or "")
    if m and (m.group(1).upper() + "B") == flat:
        return ""
    return size


def name_parts(shown: str) -> dict:
    """`empero-ai/Qwythos-9B-GGUF:Q4_K_M` -> owner, name, tag -- so a long
    Hugging Face name wraps between its parts instead of in the middle of one."""
    base, _, tag = shown.rpartition(":") if ":" in shown else (shown, "", "")
    owner, _, name = base.rpartition("/")
    return {"owner": owner, "name": name or base, "tag": tag}


def humanize(case_id: str) -> str:
    text = case_id.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def variant_of(shown: str) -> str:
    """The tag, as typed: `:12b`, `:e4b`, `:Q4_K_M`. "" when there is none.

    A size-only tag used to be dropped as a restatement of the parameter chip,
    and `gemma4:12b` then read "gemma4 11.9B" -- the name nobody pulls it by.
    The tag is what the model is called; the chip is what it measured. The card
    shows both, "gemma4 [12b] (11.9B)", and `size_chip` leaves the size out
    only when it would repeat the tag exactly.
    """
    _base, _, tag = shown.rpartition(":") if ":" in shown else (shown, "", "")
    return tag


def run_view(run: dict, roles=ROLES, params_lookup: dict | None = None) -> dict:
    """One run, shaped for the results card. Pure, so it is tested rather than eyeballed."""
    summary = run.get("summary") or {}
    cases = run.get("cases") or []
    shown, source = display_name(str(run.get("model") or ""))
    role_rows = []
    for role in roles:
        s = summary.get(role)
        if s:
            role_rows.append({"role": role, "passed": s["passed"], "total": s["total"],
                              "grade": _grade(s["passed"], s["total"]),
                              # When THIS role was measured. On a merged row the
                              # roles can be days apart, and a chip that does not
                              # say so reads as if the whole row were one run.
                              "started": str((run.get("role_from") or {})
                                             .get(role, {}).get("started") or "")[:10]})
    total = summary.get("all") or {"passed": 0, "total": 0}
    secs = sorted(c.get("seconds") or 0 for c in cases)
    firsts = sorted(c["first_s"] for c in cases if c.get("first_s"))
    groups = []
    for role in roles:
        rows = [c for c in cases if c.get("role") == role]
        if rows:
            groups.append({"role": role, "cases": [{
                "label": humanize(str(c.get("id") or "")), "passed": bool(c.get("passed")),
                "rep": c.get("rep"), "seconds": c.get("seconds"), "calls": c.get("llm_calls"),
                "tok_s": c.get("tok_s"),
                "tools": c.get("calls") or [], "failures": c.get("failures") or [],
                "reply": c.get("reply") or ""} for c in rows]})
    return {
        "file": run.get("file", ""), "model": run.get("model", ""), "shown": shown,
        "parts": name_parts(shown),
        # The tag, minus the ones that only restate the size.
        "variant": variant_of(shown),
        # Empty for a hosted model, where the vendor name is the whole
        # identity and a size would be a guess.
        "params": size_chip(str(run.get("model") or ""), shown, params_lookup)
                  if source in ("local", "hf") else "",
        "source": source, "started": str(run.get("started") or "")[:16].replace("T", " "),
        "minutes": round((run.get("seconds") or 0) / 60, 1),
        "roles": role_rows, "passed": total.get("passed", 0), "total": total.get("total", 0),
        "pct": round(total.get("passed", 0) * 100 / total["total"]) if total.get("total") else 0,
        "grade": _grade(total.get("passed", 0), total.get("total", 0)),
        "median_s": secs[len(secs) // 2] if secs else None,
        "median_first_s": firsts[len(firsts) // 2] if firsts else None,
        "calls": round(sum(c.get("llm_calls") or 0 for c in cases) / len(cases), 1) if cases else 0,
        "groups": groups, "repeat": run.get("repeat") or 1,
        # Speed and memory: Ollama's own counters for a local model (the probe
        # in model_bench.py), the calls' tokens over their time otherwise.
        "decode_tok_s": (run.get("speed") or {}).get("decode_tok_s") or total.get("tok_s_median"),
        "decode_measured": bool((run.get("speed") or {}).get("decode_tok_s")),
        "prefill_tok_s": (run.get("speed") or {}).get("prefill_tok_s"),
        "vram_gb": (run.get("memory") or {}).get("vram_gb"),
        "size_gb": (run.get("memory") or {}).get("size_gb"),
        "on_gpu_pct": (run.get("memory") or {}).get("on_gpu_pct"),
        "context": (run.get("memory") or {}).get("context"),
        # Where a local model ran: the benchmark Ollama, or the family's --
        # where its timings include swapping with the house's model.
        # Which cases scored this run; "" for one written before the stamp.
        "cases_digest": str(run.get("cases_digest") or ""),
        # What the model was given (skills, prompts, code, tool lists), and the
        # assistant it ran inside -- setups differ per member. "" before either
        # was recorded.
        "setup_digest": str(run.get("setup_digest") or ""),
        "container_name": str(run.get("container_name") or ""),
        "where": run.get("where") or "",
        "contended": bool(run.get("contended")),
        # Which runs built this row, and when each role was measured.
        "merged_from": run.get("merged_from") or [],
        "role_from": run.get("role_from") or {},
        "removable": bool(run.get("removable")),
    }


def setup_is_stale(view: dict, current: str | None) -> bool:
    """Whether a run was made with a setup other than the one deployed now.

    *current* is the digest of the run's own container as it stands, or None
    while nobody knows it yet (still being worked out, or the container is
    gone) -- unknown is not stale. A run with no digest at all predates the
    stamp, and every one of those predates the fixes that made it necessary,
    so it is marked.
    """
    if not view.get("setup_digest"):
        return True
    return bool(current) and view["setup_digest"] != current


def case_counts(cases_file: Path) -> dict[str, int]:
    """How many cases each role has, for the page's progress bar."""
    try:
        doc = json.loads(cases_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: len(v) for k, v in doc.items() if not k.startswith("_") and isinstance(v, list)}


def kind_scores(view: dict) -> dict[str, dict]:
    """The run's score for each kind: passed, total, pct and median seconds.

    A kind none of whose roles was measured is absent -- not zero, because a
    model nobody ran on long tasks has not failed them.
    """
    by_role = {r["role"]: r for r in view.get("roles") or []}
    secs_by_role = {g["role"]: [c.get("seconds") or 0 for c in g["cases"]]
                    for g in view.get("groups") or []}
    out = {}
    for kind, roles in KINDS:
        rows = [by_role[r] for r in roles if r in by_role]
        total = sum(r["total"] for r in rows)
        if not total:
            continue
        passed = sum(r["passed"] for r in rows)
        secs = sorted(x for r in roles for x in secs_by_role.get(r, []))
        out[kind] = {"passed": passed, "total": total, "pct": round(passed * 100 / total),
                     "grade": _grade(passed, total),
                     "median_s": secs[len(secs) // 2] if secs else None}
    return out


def rank_kinds(views: list[dict]) -> None:
    """Each view's place in every kind's ordering, and the kinds it is best at.

    Sets `kinds` (kind_scores), `rank` ({kind: 0-based place}, absent when
    unmeasured) and `best_for` (kinds where it is first among two or more).
    The page sorts by whichever kind the household picks without asking the
    server again. Overall keeps sort_runs' order; the others break a tie on
    speed, because for a notification or a long task the faster of two
    equally right models is the better one to pick.
    """
    for v in views:
        v["kinds"] = kind_scores(v)
        v["rank"], v["best_for"] = {}, []
    for kind, _roles in KINDS:
        scored = [v for v in views if kind in v["kinds"]]
        if kind == "overall":
            ordered = sort_runs(scored)
        else:
            ordered = sorted(scored, key=lambda v: (
                -v["kinds"][kind]["pct"], -v["kinds"][kind]["passed"],
                v["kinds"][kind]["median_s"] if v["kinds"][kind]["median_s"] is not None else 1e9,
                v.get("file") or ""))
        for i, v in enumerate(ordered):
            v["rank"][kind] = i
        if len(ordered) > 1 and ordered[0]["kinds"][kind]["passed"]:
            ordered[0]["best_for"].append(kind)


def sort_runs(views: list[dict]) -> list[dict]:
    """Best score first; among equals, more cases passed, then the newest.

    The list used to be newest-first, which answers "what did I just run" and
    not "which of these should the house use" -- the question the card is for.
    """
    return sorted(views, key=lambda v: (v.get("pct") or 0, v.get("passed") or 0,
                                        v.get("started") or "", v.get("file") or ""),
                  reverse=True)
