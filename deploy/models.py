#!/usr/bin/env python3
"""What the model gateway is offering today, and which of it this house uses.

    ./home-stack models              every model, its state, and who names it
    ./home-stack models --roles      only the models a role actually points at
    ./home-stack models --quiet      print nothing unless a role is broken

Why this exists rather than a page in the admin: the catalogue is not the
answer. `GET /v1/models` happily lists models the same gateway then refuses --
on 2026-08-30 it advertised 33 and four of them answered `Model is disabled`
on the next breath, including the one this house had set as its *fallback*. A
list you cannot act on is worse than no list, because it reads like a menu.

So every model is asked a real question. One token, in parallel, and the answer
is the state:

    ok            it answered
    disabled      401, the gateway knows it and will not route it
    unsupported   401, this gateway does not have it at all
    down          it is there and broken -- a 5xx, or, on /responses, a 200
                  whose body carries `status: "failed"`. The shape that took
                  the household's chat out twice, on 2026-08-23 and again today
    unreachable   the gateway itself did not answer

**A single sample is not a verdict.** Those same deepseek models answered
`disabled` at 18:17 and `200` at 18:19 -- the gateway flaps, and a check that
sampled once would have had this house move off two perfectly healthy models.
So a model that fails is asked again, and only a model that fails *every*
attempt is called broken. Retry noise you have measured; never a failure.

Exit status is for the roles, not the catalogue: 0 when every role this house
names can answer, 1 when one cannot. A model being disabled somewhere in the
list is the provider's business; a *role* pointing at it is the household's.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

# The one provider this reaches. Everything else a role can name -- ollama on
# your own box, openrouter, together -- has its own endpoint and its own key,
# and probing those from here would be four half-tested integrations instead of
# one that works. A role pointing at them is listed and skipped, not guessed at.
BASE = "https://opencode.ai/zen/v1"
ATTEMPTS = 3          # a failure has to repeat before it counts. See above.
TIMEOUT = 45
# Drawing is slower than answering, and a model that takes 20s is not broken:
# Qwen-Image-2.0-Pro drew a detailed prompt in 16.7s here. Long enough to tell a
# slow model from a dead one, short enough that `--quiet` in a cron job does not
# hang on a provider having a bad afternoon.
IMAGE_TIMEOUT = 90
RETRY_WAIT = 20       # a 429 is this checker's own burst; give the window time

# The gateway refuses Python's default `Python-urllib/3.x` with a 403 -- the
# same key and the same URL answer 200 the moment a User-Agent it recognises is
# sent. Measured both ways round before writing this, because a 403 on a
# credentialled request reads as "the key is wrong" and sends you to rotate a
# key that was never the problem.
# One id for one sweep, sent as x-opencode-session. OpenCode began requiring it
# on 2026-09-06 -- without it "requests may error" -- and asks for one stable id
# per conversation. This is not a conversation: it is a probe that asks every
# model the same one-token question and keeps no history. So the honest mapping
# is one id per run of this command, which is what a sweep is, rather than a
# fresh id on each of ~64 requests or one constant baked into the file that
# would make every household on earth look like the same caller.
_SESSION = f"home-stack-models-{uuid.uuid4().hex}"
_HEADERS = {
    "User-Agent": "home-stack/models (curl-compatible)",
    "x-opencode-session": _SESSION,
}


def _headers(key: str, **extra: str) -> dict:
    return {"Authorization": f"Bearer {key}", **_HEADERS, **extra}


def _secrets() -> dict:
    """The deployed env if there is one, the checkout's copy otherwise.

    Same order the deployer uses: the admin page edits the deployed copy, so a
    key rotated there is the one that matters and the seed in the checkout is
    the fallback.
    """
    out: dict[str, str] = {}
    live = Path(os.environ.get("HOME_STACK_CONFIG", "/var/lib/home-stack/config"))
    for path in (live / "smart-home-bot.env", ROOT / "secrets" / "smart-home-bot.env"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out.setdefault(k.strip(), v.strip().strip('"').strip())
        if out.get("OPENCODE_API_KEY") or out.get("OPENCODE_GO_API_KEY"):
            return out
    return out


def _config() -> dict:
    live = Path(os.environ.get("HOME_STACK_CONFIG", "/var/lib/home-stack/config"))
    import yaml
    for path in (live / "home-stack.yml", ROOT / "config" / "home-stack.yml"):
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except OSError:
            continue
    return {}


def catalogue(key: str) -> list[str]:
    req = urllib.request.Request(f"{BASE}/models", headers=_headers(key))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = json.load(resp)
    rows = body.get("data") if isinstance(body, dict) else body
    return [r.get("id") for r in (rows or []) if isinstance(r, dict) and r.get("id")]


def wants_responses(model: str) -> bool:
    """True for the models this gateway serves on `/v1/responses` only.

    The same test `_should_use_responses_api()` makes in the assistant's
    provider, and deliberately the same one: this command's whole job is to say
    whether a model works, so asking it on an endpoint the assistant would never
    use answers a question nobody has.

    The gateway splits by model, not by account. `gpt-5.6-luna` and its siblings
    answer on `/v1/responses` and return 500 on `/chat/completions`;
    `gpt-6` is here because the next generation arrived and this list did not:
    `gpt-6-astra` answered 500 on `/chat/completions` and 200 on `/responses`,
    so the catalogue called it down and a household could not choose it. It also
    refuses `temperature`, the same way, which is the other list this token had
    to be added to. Measured on both, against `gpt-5.6-terra` as the control.
    `deepseek-v4-flash` on the same key and the same base is the other way
    round. Posting the one shape at both is how this reported three working
    models as broken -- and `--quiet` is wired to a household's attention, so a
    false alarm there is worse than silence.

    The model decides and *only* the model, for the reason the provider gives:
    a reasoning effort is not a reason to move deepseek to an endpoint it 404s.
    """
    name = (model or "").lower()
    return any(token in name for token in ("gpt-5", "gpt-6", "o1", "o3", "o4"))


def _ask_once(key: str, model: str) -> tuple[str, str]:
    """One real question. Returns (state, detail)."""
    # Two shapes, because they are two APIs. Responses takes `input` and
    # `max_output_tokens`; chat/completions takes `messages` and `max_tokens`,
    # and neither accepts the other's spelling.
    if wants_responses(model):
        path, body = "responses", {
            "model": model, "input": "ok", "max_output_tokens": 16}
    else:
        path, body = "chat/completions", {
            "model": model,
            "messages": [{"role": "user", "content": "ok"}],
            "max_tokens": 1,
        }
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE}/{path}", data=payload,
        headers=_headers(key, **{"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            # A 200 is not an answer on /responses. That API reports a
            # generation that died inside the model as `status: "failed"` with
            # the reason in `error`, over HTTP 200 -- so a status-code check
            # alone would call a permanently broken model `ok` and leave
            # `--quiet` silent about it, which is the same lie as a false
            # alarm pointed the other way.
            #
            # `incomplete` is deliberately not a failure: `max_output_tokens`
            # is 16 and these are reasoning models, so hitting the cap is the
            # *normal* outcome here and means the gateway served the model.
            if path == "responses":
                try:
                    out = json.load(resp)
                except Exception:  # noqa: BLE001 - a gateway may answer badly
                    out = {}
                if isinstance(out, dict) and out.get("status") == "failed":
                    err = out.get("error")
                    msg = (err.get("message") if isinstance(err, dict)
                           else str(err or ""))
                    return "down", msg or "the model reported a failure"
            return "ok", ""
    except urllib.error.HTTPError as exc:
        raw = (exc.read() or b"").decode(errors="replace")
        try:
            msg = json.loads(raw).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001 - a gateway is allowed to answer badly
            msg = raw[:80]
        if exc.code >= 500:
            return "down", msg or f"HTTP {exc.code}"
        # 401 covers two different things and the difference decides what you do:
        # a model the gateway knows and has switched off may come back on its
        # own, and one it has never heard of is a name to stop using.
        low = (msg or "").lower()
        if "not supported" in low or "unknown" in low:
            return "unsupported", msg
        if "disabled" in low:
            return "disabled", msg
        return f"http {exc.code}", msg
    except Exception as exc:  # noqa: BLE001 - urlopen raises a family of these
        return "unreachable", f"{type(exc).__name__}: {exc}"


def probe(key: str, model: str) -> tuple[str, str]:
    """Ask until it answers, or until it has failed `ATTEMPTS` times.

    The retry is what makes this trustworthy rather than a coin toss: this
    gateway returns `disabled` for a model that answers thirty seconds later,
    and a one-shot check turns that into a recommendation to move house off a
    working model.
    """
    state = detail = ""
    for _ in range(ATTEMPTS):
        state, detail = _ask_once(key, model)
        if state == "ok":
            return state, detail
    return state, detail


TOGETHER_IMAGES_URL = "https://api.together.xyz/v1/images/generations"
# Together sits behind Cloudflare, which rejects urllib's default agent outright
# with `error code: 1010` -- a 403 that never reaches Together and reads exactly
# like a dead key. `find_image.py` and `theme.py` both document this trap; this
# is the third caller and it is not walking into it.
IMAGE_UA = "home-stack-images/1.0"


def image_roles(cfg: dict) -> dict[str, list[str]]:
    """{model: [role, ...]} for the roles that draw, on Together.

    Kept apart from `roles()` because they are a different API on a different
    host with a different key -- but reported beside them, because "is the
    picture model working" is the same question a household is asking.
    """
    models = ((cfg.get("assistant") or {}).get("models") or {})
    out: dict[str, list[str]] = {}
    for role, value in models.items():
        if not role.startswith("image_"):
            continue
        name = str(value or "").strip()
        if name.startswith("together:"):
            out.setdefault(name.split(":", 1)[1], []).append(role)
    return out


def probe_image(key: str, model: str) -> tuple[str, str]:
    """Ask Together to draw the smallest thing that proves the model answers.

    This exists because both image models were dead for an unknown length of
    time and nothing said so: Together still lists `google/imagen-4.0-*` in its
    catalogue while the generations endpoint refuses them with "Model must be a
    string value representing a valid AIR identifier". Every picture the
    assistant tried to draw failed, and `--roles` reported the models as "not
    asked (another provider)" -- absent rather than broken.

    A real generation, not a catalogue lookup, for exactly that reason: the
    catalogue was the thing that was wrong.
    """
    if not key:
        return "?", "no TOGETHER_API_KEY in the env file"
    body = json.dumps({"model": model, "prompt": "a red circle", "n": 1}).encode()
    req = urllib.request.Request(
        TOGETHER_IMAGES_URL, data=body,
        headers={"Authorization": f"Bearer {key}", "User-Agent": IMAGE_UA,
                 "Content-Type": "application/json"})
    for attempt in range(ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=IMAGE_TIMEOUT) as r:
                payload = json.loads(r.read().decode())
            if payload.get("data"):
                return "ok", ""
            return "down", str(payload.get("error") or payload)[:200]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            try:
                detail = (json.loads(detail).get("error") or {}).get("message") or detail
            except Exception:                                     # noqa: BLE001
                pass
            # 429 is this checker's own burst, not a broken model: asking three
            # of these in a row rate-limits the account, and reporting that as
            # "down" would send somebody to replace a model that is fine.
            if exc.code == 429 and attempt < ATTEMPTS - 1:
                time.sleep(RETRY_WAIT)
                continue
            return ("busy" if exc.code == 429 else "down"), f"HTTP {exc.code}: {detail}"
        except Exception as exc:                                  # noqa: BLE001
            if attempt < ATTEMPTS - 1:
                time.sleep(RETRY_WAIT)
                continue
            return "down", f"{type(exc).__name__}: {exc}"
    return "down", ""


def roles(cfg: dict) -> dict[str, list[str]]:
    """{model: [role, ...]} for every role that names a model on this gateway.

    A role naming another provider (`ollama:`, `together:`) is kept out: this
    reaches one gateway, and reporting a model it cannot ask about as broken
    would be a lie in the direction that costs somebody an afternoon.
    """
    models = ((cfg.get("assistant") or {}).get("models") or {})
    out: dict[str, list[str]] = {}
    for role, value in models.items():
        name = str(value or "").strip()
        if not name or ":" in name:
            continue
        out.setdefault(name, []).append(role)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roles", action="store_true",
                    help="only the models a role points at")
    ap.add_argument("--quiet", action="store_true",
                    help="say nothing unless a role is broken")
    args = ap.parse_args()

    # The old name still answers, exactly as deploy.py's SECRET_ALIASES has
    # it: the key did not change when the endpoint did, and an env file
    # written before the move must not make this command give up.
    _sec = _secrets()
    key = _sec.get("OPENCODE_API_KEY") or _sec.get("OPENCODE_GO_API_KEY") or ""
    if not key:
        print("OPENCODE_API_KEY is not set, so there is nothing to ask.",
              file=sys.stderr)
        return 2

    cfg = _config()
    used = roles(cfg)
    try:
        names = catalogue(key)
    except Exception as exc:  # noqa: BLE001
        print(f"the gateway did not answer with its catalogue: {exc}", file=sys.stderr)
        return 2

    # A role can name a model the catalogue does not list -- that is exactly the
    # `ox-alpha-free` case, and leaving it out would hide the one entry somebody
    # needs to see.
    names = sorted(set(names) | set(used))
    if args.roles:
        names = [n for n in names if n in used]

    with ThreadPoolExecutor(max_workers=8) as pool:
        states = dict(zip(names, pool.map(lambda m: probe(key, m), names)))

    broken = {m: used[m] for m, (s, _) in states.items()
              if m in used and s != "ok"}

    # The drawing models, on Together rather than this gateway. Asked because
    # both of them were answering HTTP 400 on every request and this command
    # said "not asked (another provider)" -- which reads as "fine, elsewhere".
    # A picture nobody can draw is exactly as broken as a role that cannot
    # answer, and it belongs in the same exit status.
    drawn = image_roles(cfg)
    drawn_roles = {r for who in drawn.values() for r in who}
    image_states: dict[str, tuple[str, str]] = {}
    if drawn:
        tkey = _sec.get("TOGETHER_API_KEY", "")
        # Serially, unlike the gateway sweep above: two generations at once is
        # what earns the 429 that `probe_image` then has to wait out.
        for model in sorted(drawn):
            image_states[model] = probe_image(tkey, model)
        broken.update({m: drawn[m] for m, (s, _) in image_states.items()
                       if s not in ("ok", "?")})

    if not args.quiet:
        width = max((len(n) for n in names), default=10)
        # width + 1: the leading `!` on a broken row is a column of its own.
        print(f"{'model'.ljust(width + 1)}  state        used by")
        print(f"{'-' * (width + 1)}  -----------  -------")
        for name in names:
            state, detail = states[name]
            who = ", ".join(sorted(used.get(name, []))) or ""
            mark = " " if state == "ok" else "!"
            # The mark is a column of its own, so the name pads to `width` and
            # the pair pads to width + 1. `name.ljust(width - 1)` let the
            # longest name -- the one that set the width -- push the state
            # column a character right of every other row.
            line = f"{(mark + name).ljust(width + 1)}  {state.ljust(11)}  {who}"
            if state != "ok" and detail and name in used:
                line += f"   ({detail[:60]})"
            print(line)
        ok = sum(1 for s, _ in states.values() if s == "ok")
        print(f"\n{ok}/{len(names)} answered. "
              f"{len(used)} model(s) named by a role on this gateway.")

        if image_states:
            print("\npictures the assistant draws (Together):")
            for model, (state, detail) in sorted(image_states.items()):
                mark = " " if state == "ok" else "!"
                line = (f"{mark}{model.ljust(max(len(m) for m in image_states))}  "
                        f"{state.ljust(11)}  {', '.join(sorted(drawn[model]))}")
                if detail and state != "ok":
                    line += f"   ({detail[:70]})"
                print(line)

        # Roles that point elsewhere, said out loud rather than silently absent.
        # The drawing ones are excluded here because they are asked below: they
        # used to sit in this list, and "not asked" is what a household read for
        # however long both image models were returning HTTP 400 on every draw.
        other = {r: v for r, v in ((cfg.get("assistant") or {}).get("models") or {}).items()
                 if ":" in str(v or "") and r not in drawn_roles}
        if other:
            print("not asked (another provider): "
                  + ", ".join(f"{r}={v}" for r, v in sorted(other.items())))

    if broken:
        print()
        for model, who in sorted(broken.items()):
            state, detail = states[model]
            print(f"BROKEN  {', '.join(sorted(who))} -> {model} is {state}"
                  + (f": {detail[:70]}" if detail else ""))
        print("Change it on the admin page under Models, then deploy nanobot.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
