#!/usr/bin/env python3
"""Set the look of this member's pages in HomeCore: colours, shape and backdrop.

The palette is written by Alfred, not sampled from anything: he decides the
fourteen colours, and *those* go into the image prompt, so the backdrop is made
to match the theme rather than the theme guessed from an image. One direction
only — the other way round produces a page whose accent is whatever happened to
be bright in the corner of a picture.

Two safety rails, both here rather than only in HomeCore, so a bad palette costs
a sentence instead of a round trip and a broken house:

- **Contrast.** The same WCAG check HomeCore applies, run before the image is
  generated. Generating a picture for a theme that will be refused is a minute
  of GPU nobody gets back.
- **Colours, shape and the ground — nothing else.** A theme cannot touch the
  type, the spacing or the layout, and shape is one of four named sets rather
  than a number, so the worst it can do is look wrong.

Usage:
    theme.py '{"action":"show"}'
    theme.py '{"action":"set","name":"Plum","colors":{...},"image":"..."}'
    theme.py '{"action":"reset"}'
    theme.py '{"action":"check"}'      # why the image never appeared
"""

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HOMECORE_URL = os.environ.get("HOMECORE_URL", "https://portal.home:8443")
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")

TOGETHER_KEY = os.environ.get("TOGETHER_API_KEY", "")
TOGETHER_URL = os.environ.get("TOGETHER_API_URL", "https://api.together.xyz/v1/images/generations")
# A Jenkins credential decides the key; this decides the model, and it is an
# env var so a rename upstream is a redeploy rather than a code change.
# Chosen on the admin page (Models -> "Pictures the assistant draws") and
# exported by the deployer. Two slots: the everyday one, and a better one for
# something that will be looked at -- a theme backdrop is exactly that, so this
# script asks for the good one and falls back to the everyday one.
#
# The literal stays as a last resort, and it is worth saying why it was a
# problem: nothing ever exported TOGETHER_IMAGE_MODEL, so this default *was*
# the setting, and choosing a model on the admin page changed nothing.
#
# **The everyday slot first, and the local endpoint if there is one.** This
# asked for the *good* model on the argument above that a backdrop is looked
# at. That argument was written when both slots were a paid API and the only
# question was which one. The house draws its own pictures now, and a
# wallpaper nobody asked to be expensive is exactly what the household's own
# GPU is for -- `high` is for a person asking, or Alfred offering after they
# liked the draft (see the images skill's SKILL.md).
#
# The whole triple together, never a model from one place and a URL from
# another. `_draw_endpoint` in the images skill says why at length and it is
# the same trap here: `IMAGE_API_MODEL` is exported for *every* provider, so
# reading it beside a URL that defaults to Together posts somebody's OpenAI
# model id to api.together.xyz with the Together key. A slot is a pair.
LOCAL_MODEL_DEFAULT = "google/flash-image-3.1-lite"


def _image_endpoint():
    """(url, model, key) for the backdrop: the everyday slot, then the good one.

    `IMAGE_API_*` are what the deployer exports now; `TOGETHER_IMAGE_MODEL*`
    are read after them and only as a fallback, so an env file written before
    this stack could draw locally still works. A legacy name always means
    Together -- that is the only provider the deployer ever wrote it for.

    The key goes with the endpoint. `IMAGE_API_KEY` is whatever a custom
    endpoint wants (empty for the house's own container, which answers whoever
    asks) and the manifest declares it for exactly the case that is neither
    Together nor the house; hard-coding an empty key for every custom URL
    401'd those households on every backdrop.
    """
    for url_env, model_env, legacy in (
            ("IMAGE_API_URL", "IMAGE_API_MODEL", "TOGETHER_IMAGE_MODEL"),
            ("IMAGE_API_URL_HIGH", "IMAGE_API_MODEL_HIGH",
             "TOGETHER_IMAGE_MODEL_HIGH")):
        model = os.environ.get(model_env, "").strip()
        url = os.environ.get(url_env, "").strip()
        if not model:
            # The legacy name carries no endpoint of its own and never named
            # anything but Together, so it drops the URL beside it.
            model, url = os.environ.get(legacy, "").strip(), ""
        if not model:
            continue
        if url:
            return url, model, os.environ.get("IMAGE_API_KEY", "")
        return TOGETHER_URL, model, TOGETHER_KEY
    return TOGETHER_URL, LOCAL_MODEL_DEFAULT, TOGETHER_KEY


IMAGE_URL, IMAGE_MODEL_NAME, IMAGE_KEY = _image_endpoint()
# "Local" means the house's own container: an endpoint of our own that wants no
# credential. A custom endpoint that *does* take a key is somebody's paid API,
# and calling that "the house's model" is the silence this flag exists to
# break.
IMAGE_IS_LOCAL = IMAGE_URL != TOGETHER_URL and not IMAGE_KEY
# What to call the far end when something goes wrong. The `check` action exists
# to tell a person where to look, and "Together answered 503" pointed at the
# wrong machine on every household drawing on its own GPU.
IMAGE_PROVIDER = "the house's image model" if IMAGE_IS_LOCAL else "Together"

# Kept under the old name because the diagnostics below print it.
TOGETHER_MODEL = IMAGE_MODEL_NAME

# Together sits behind Cloudflare, and Cloudflare bans urllib's default
# `Python-urllib/3.x` signature outright: every request comes back 403 with a
# body of `error code: 1010` and never reaches Together at all. It looks
# exactly like a dead API key — same key, same endpoint, 403 with the default
# agent and 200 with any other — so it is worth saying plainly: this header is
# load-bearing, and dropping it breaks image generation without touching a
# single line of the request that matters.
USER_AGENT = "home-stack-theme/1.0"


def _together_request(url, data=None):
    """Every call to Together goes through here, so none can forget the agent."""
    headers = {"User-Agent": USER_AGENT}
    if IMAGE_KEY:
        headers["Authorization"] = f"Bearer {IMAGE_KEY}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(url, data=data, headers=headers)


def _auth_probe():
    """Is this key accepted? Returns (ok, explanation) — costs nothing.

    `/v1/models` is a plain listing, so it answers the only question that
    matters here without generating anything."""
    # The endpoint actually configured, not Together unconditionally: a
    # custom paid endpoint gets its own key checked against its own
    # listing, and checking Together instead answers a question nobody
    # asked with a credential that was never meant for it.
    models = IMAGE_URL.split("/v1/")[0] + "/v1/models"
    try:
        with urllib.request.urlopen(_together_request(models), timeout=30):
            return True, None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200].strip()
        if _blocked_at_the_edge(e.code, detail):
            return False, ("Cloudflare refused the request (403, error code "
                           "1010). The key was never tested: what it rejects "
                           "is the client signature, not the credential.")
        return False, f"{IMAGE_PROVIDER} rejected the key: {e.code} {detail}"
    except Exception as e:
        return None, f"could not reach {IMAGE_PROVIDER}: {e}"


def _blocked_at_the_edge(code, detail):
    """True when Cloudflare refused us before Together saw the request.

    Worth telling apart from a real rejection: the answer to this one is a
    header, and reading it as an expired key sends somebody to rotate a
    credential that was never the problem."""
    return code == 403 and "1010" in detail


# The fourteen roles a theme sets, and what each one is *for*. The wording
# matters: this is what Alfred reads when he chooses the colours, and a role
# described as "a hex colour" gets a random hex colour.
ROLES = {
    "plaster": "the page ground, behind everything",
    "paper": "the sheets: cards, panels, tables",
    "paper-2": "a sheet one step lighter (text fields)",
    "ink": "the text",
    "ink-soft": "secondary text and labels",
    "wall": "the top bar, the band of strong colour",
    "wall-fg": "the text on that bar",
    "wall-dim": "the faint text on that bar",
    "olive": "the positive: money coming in, confirmations",
    "olive-2": "the second voice of the same green",
    "honey": "the accent: what can be touched, what is active",
    "honey-lt": "the same accent but soft, for backgrounds",
    "clay": "the negative: money going out, deleting, errors",
    "line": "the rules and the borders",
}

# The shape axis. Named, not numeric: a free radius is a button whose label is
# clipped by its own corners.
SHAPES = {
    "rounded": "like The House: soft-cornered sheets, rounded buttons",
    "soft": "a little less round, more sober",
    "square": "almost sharp, architectural",
    "pill": "fully rounded controls, pill-shaped",
}

# How far the backdrop shows through the plaster. The scale exists because one
# number cannot serve both things a backdrop can be: a wash wants to disappear,
# and a drawing wants to be recognised. At the usual strength, drawn kittens
# look like smudges.
STRENGTHS = {
    "faint": "barely sensed",
    "normal": "the usual one: right for washes, gradients and grain",
    "visible": "the drawing reads without fighting the text",
    "strong": "for drawings: you can tell what they are. The most that is safe",
}

CONTRAST = (
    ("ink", "paper", 4.5, "text on sheets"),
    ("ink-soft", "paper", 4.0, "secondary text on sheets"),
    ("ink", "plaster", 4.5, "text on the ground"),
    ("wall-fg", "wall", 4.5, "the top bar text"),
    ("honey", "paper", 2.5, "the accent on sheets"),
)


def _rgb(value):
    if not isinstance(value, str):
        return None
    s = value.strip().lstrip("#")
    if len(s) == 3 and all(c in "0123456789abcdefABCDEF" for c in s):
        s = "".join(c * 2 for c in s)
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        return None
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def _luminance(rgb):
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    lo, hi = sorted((_luminance(a), _luminance(b)))
    return (hi + 0.05) / (lo + 0.05)


def _check(colors):
    """The same rules HomeCore enforces, so a refusal happens here — before a
    minute of image generation — and says what to change."""
    if not isinstance(colors, dict):
        return '"colors" has to be an object with all fourteen colours'
    for role in ROLES:
        if role not in colors:
            return f'"{role}" is missing ({ROLES[role]})'
        if _rgb(colors[role]) is None:
            return f'"{role}" is not a #RRGGBB hex colour: {colors[role]!r}'
    for fg, bg, want, what in CONTRAST:
        got = _contrast(_rgb(colors[fg]), _rgb(colors[bg]))
        if got < want:
            return (f'{what} would not be legible: "{fg}" on "{bg}" is '
                    f'{got:.1f}:1 and needs {want}:1. Darken "{fg}" or '
                    f'lighten "{bg}" and try again.')
    return None


def _homeweb(method, path, data=None):
    if not USER_ID or not TOKEN:
        return {"error": "HOMECORE_PROXY_TOKEN / HOMECORE_USER_ID are missing"}
    cmd = ["curl", "-sk", "--max-time", "30", "-X", method,
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(f"{HOMECORE_URL}{path}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired:
        return {"error": f"HomeCore did not answer at {HOMECORE_URL}"}
    if r.returncode != 0:
        return {"error": f"HomeCore (curl {r.returncode}): {r.stderr.strip()}"}
    try:
        return json.loads(r.stdout or "{}")
    except ValueError:
        return {"error": f"HomeCore returned something unreadable: {r.stdout[:200]}"}


def _backdrop_prompt(brief, colors):
    """The image prompt, with the palette written into it.

    The colours come first and as hex, because that is the part that has to
    survive: a backdrop in colours the page is not using is worse than no
    backdrop.

    Asking for a **wallpaper**, not a picture, is what lets somebody have the
    kittens they asked for. The earlier version banned subjects outright, which
    was the wrong lever: what makes a backdrop unusable is a focal point — one
    big thing in the middle, behind a form. A motif repeated evenly across the
    frame has no middle to compete with, so the subject can stay. Readability
    is not resting on this prompt anyway; the sheet paints it at 18% under
    solid sheets of paper.
    """
    swatches = ", ".join(f"{colors[r]}" for r in
                         ("plaster", "paper", "wall", "olive", "honey", "clay"))
    return (
        f"Seamless decorative wallpaper. {brief.strip()}. "
        f"Use strictly this colour palette: {swatches}. "
        "Drawn as a soft flat illustration and repeated as an evenly scattered "
        "motif across the entire frame, small, with generous empty space "
        "between the shapes and no single focal point, nothing large in the "
        "centre. Muted and low contrast, gentle shapes, paper grain, "
        "no text, no letters, no logos, no borders, no frame. "
        "Evenly lit across the whole frame so text can be laid over it. "
        "Wide desktop wallpaper."
    )


def _image_kind(raw):
    """"png" / "jpeg" / "webp" from the file's own first bytes, or None.

    By magic number rather than by the Content-Type header: the header is
    whatever a CDN felt like sending, and this decides what gets painted behind
    every page in the house.
    """
    if raw[:4] == b"\x89PNG":
        return "png"
    if raw[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def _generate(brief, colors):
    """(base64 png/jpeg, kind, error)."""
    if IMAGE_URL == TOGETHER_URL and not IMAGE_KEY:
        # Naming both places on purpose. The key reaches the *container* from
        # the secrets file, but a skill runs as a subprocess of the agent and
        # only sees what `tools.exec.allowedEnvKeys` lets through — so "the key
        # is missing" sent somebody to look at the secrets, which was correct
        # all along.
        return None, None, (
            "TOGETHER_API_KEY never reached this script. If it is in the "
            "secrets file, what is missing is adding it to "
            "`tools.exec.allowedEnvKeys` in config/config.json and "
            "deploying again. The theme is saved either way, it just ends up "
            "without a backdrop image.")
    # `url` because that is what these models are documented to return — the
    # base64 branch below stays for the ones that answer with bytes instead.
    #
    # `steps` is deliberately absent unless asked for. Together's models split
    # on it: the diffusion ones (FLUX and friends) want it, and the newer ones
    # — including the default here — reject the request outright with "this
    # parameter is not supported for the selected model". Sending it by default
    # broke image generation for everyone the day the model changed, so it is
    # opt-in, and the retry below covers the other half of the split.
    #
    # 1264x848 is the widest landscape this model accepts, and a backdrop wants
    # to be wide. Models disagree about which sizes exist at all, so the retry
    # below falls back to 1024x1024 — the one size every one of them takes.
    size = os.environ.get("TOGETHER_IMAGE_SIZE", "1264x848").lower().split("x")
    payload_body = {
        "model": IMAGE_MODEL_NAME,
        "prompt": _backdrop_prompt(brief, colors),
        "width": int(size[0]), "height": int(size[1]), "n": 1,
        # Bytes inline from the house's own container, a URL from a hosted
        # one -- the same split the images skill makes and for the same
        # reasons. Asking the local model for a link means it writes a PNG
        # into its own volume that nothing ever deletes, and then a second
        # HTTP round trip to fetch back bytes it already had.
        "response_format": "b64_json" if IMAGE_IS_LOCAL else "url",
    }
    steps = os.environ.get("TOGETHER_IMAGE_STEPS", "").strip()
    if steps:
        payload_body["steps"] = int(steps)

    # Longer for the house's own model than a hosted one would need: it draws
    # in ~33 s on the card in this box, and its first call after an idle
    # unload loads ~12 GB of weights before it starts. 180 s times out on a
    # cold backdrop, which reads as the model being broken.
    _timeout = 300 if IMAGE_IS_LOCAL else 180

    def _post(fields):
        req = _together_request(IMAGE_URL, json.dumps(fields).encode())
        with urllib.request.urlopen(req, timeout=_timeout) as r:
            return json.loads(r.read().decode())

    # Together's image models disagree about their own request shape, and they
    # only say so by rejecting one. Rather than encode one model's opinions
    # here — which is what broke this the day the model changed — read what the
    # 400 complains about and try again without that parameter. Bounded, and
    # each adjustment is tried once.
    payload, last = None, None
    for _ in range(3):
        try:
            payload = _post(payload_body)
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if _blocked_at_the_edge(e.code, detail):
                return None, None, (
                    "Cloudflare blocked the request before it reached Together "
                    "(403, error code 1010). It is not the key: it is the client "
                    "signature. This script sends a User-Agent of its own for "
                    "exactly that reason, so if you see this the header was lost.")
            last = (f"{IMAGE_PROVIDER} answered {e.code} for "
                    f"{IMAGE_MODEL_NAME} at {IMAGE_URL}: {detail}")
            if e.code == 400 and "content_policy" in detail:
                # The moderator rejected this particular prompt. Everything
                # else — key, endpoint, model, network — is demonstrably fine,
                # so say that: it is a brief to rewrite, not a thing to fix.
                return None, None, (
                    "the image moderator rejected this request. The key and the "
                    "connection are fine; what did not pass is the backdrop "
                    "description. Ask for something else, simpler and without "
                    "characters.")
            if e.code != 400:
                return None, None, last
            if "steps" in detail and "steps" in payload_body:
                payload_body.pop("steps")
            elif "steps" in detail:
                payload_body["steps"] = 28
            elif ("width" in detail or "height" in detail) and \
                    payload_body["width"] != 1024:
                payload_body["width"] = payload_body["height"] = 1024
            else:
                return None, None, last
        except Exception as e:
            return None, None, f"could not talk to {IMAGE_PROVIDER}: {e}"
    if payload is None:
        return None, None, last
    items = payload.get("data") or []
    if not items:
        return None, None, (f"{IMAGE_PROVIDER} returned no image: "
                            f"{json.dumps(payload)[:200]}")
    first = items[0]
    if first.get("b64_json"):
        return first["b64_json"], "png", None
    url = first.get("url")
    if not url:
        return None, None, f"{IMAGE_PROVIDER} returned neither b64_json nor url"
    try:                      # this model answers with a link, not the bytes
        # The agent, because whatever serves this link may screen signatures
        # the same way — but deliberately *not* the Authorization header: the
        # link points somewhere else, and the key has no business going there.
        dl = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(dl, timeout=120) as r:
            raw = r.read()
    except Exception as e:
        return None, None, (f"could not download the image "
                            f"{IMAGE_PROVIDER} returned: {e}")
    kind = _image_kind(raw)
    if not kind:
        # The link Together hands back is a short redirect, and what comes out
        # the other end is not guaranteed to be a picture: an expired link, a
        # rate-limit page or an error body all arrive as bytes and would
        # otherwise be stored as the backdrop and painted behind every page.
        head = raw[:80].decode("utf-8", "replace").strip()
        return None, None, (f"what came back from {IMAGE_PROVIDER} is not an "
                            f"image ({len(raw)} bytes, starts with {head!r})")
    return base64.b64encode(raw).decode(), kind, None


def main():
    try:
        args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    except ValueError:
        print(json.dumps({"error": "the argument has to be JSON"}, ensure_ascii=False))
        return
    action = (args.get("action") or "show").lower()

    if action == "show":
        out = _homeweb("GET", "/theme/api/current")
        out["roles"] = ROLES
        out["formas"] = SHAPES
        out["intensidades"] = STRENGTHS
        print(json.dumps(out, ensure_ascii=False)); return

    if action == "check":
        # For when the backdrop does not appear and nobody knows which of the
        # three links is broken: the key reaching this script, the model name,
        # or the call itself. Answers all three in one run, with whatever
        # Together actually said rather than a summary of it.
        out = {
            "key_arrived": bool(IMAGE_KEY),
            "model": TOGETHER_MODEL,
            "endpoint": IMAGE_URL,
            "local": IMAGE_IS_LOCAL,
            "homeweb": {"url": HOMECORE_URL, "user": USER_ID or None,
                        "token_arrived": bool(TOKEN)},
        }
        if IMAGE_IS_LOCAL:
            # No key is missing here and none is meant to arrive: the house's
            # own container answers whoever asks. Probing Together's `/v1/models`
            # would report a rejected credential on a household that has no
            # Together account and does not need one.
            out["diagnosis"] = ("drawing on the house's own model, which takes "
                                "no key. If the backdrop is missing, look at "
                                f"{IMAGE_URL} rather than at a credential.")
            print(json.dumps(out, ensure_ascii=False)); return
        if not IMAGE_KEY:
            out["diagnosis"] = (
                "the key never reached this script. If it is in the secrets "
                "file, what is missing is adding it to "
                "`tools.exec.allowedEnvKeys` in config/config.json and "
                "deploying the assistant again.")
            print(json.dumps(out, ensure_ascii=False)); return
        # Ask the free endpoint whether the key is any good *before* paying for
        # a picture. It separates the two failures that look identical from the
        # outside — a key Together rejects (401, JSON) and a client Cloudflare
        # refuses to forward (403, `error code: 1010`) — and only the first one
        # is a reason to go rotate a credential.
        out["key_works"], out["diagnosis"] = _auth_probe()
        if out["key_works"] is False:
            out["test"] = "not attempted"
            print(json.dumps(out, ensure_ascii=False)); return
        out.pop("diagnosis", None)
        # A real brief and a real palette, not "test" and fourteen greys: the
        # moderator judges what it is actually sent, and a degenerate prompt
        # gets flagged often enough to report a healthy setup as broken.
        b64, kind, error = _generate(
            "cotton paper with a soft grain",
            {"plaster": "#F2EDE2", "paper": "#FBF8F1", "paper-2": "#FFFFFF",
             "ink": "#2B2A24", "ink-soft": "#5F5C52", "wall": "#3D4A2E",
             "wall-fg": "#F3EEDF", "wall-dim": "#8E937C", "olive": "#5C6B45",
             "olive-2": "#7B8B5E", "honey": "#C6892B", "honey-lt": "#EBD9B4",
             "clay": "#A6462F", "line": "#E2DACA"})
        out["test"] = "ok" if b64 else "failed"
        if error:
            out["diagnosis"] = error
        elif b64:
            out["bytes_received"] = len(b64)
        print(json.dumps(out, ensure_ascii=False)); return

    if action == "reset":
        print(json.dumps(_homeweb("POST", "/theme/api/reset"), ensure_ascii=False)); return

    if action != "set":
        print(json.dumps({"error": f"unknown action: {action}",
                          "actions": ["show", "set", "reset", "check"]}, ensure_ascii=False)); return

    colors = args.get("colors") or args.get("tokens")
    problem = _check(colors)
    if problem:
        print(json.dumps({"error": problem, "roles": ROLES}, ensure_ascii=False)); return

    shape = (args.get("shape") or "").strip().lower()
    if shape and shape not in SHAPES:
        print(json.dumps({"error": f'"{shape}" is not a shape',
                          "shapes": SHAPES}, ensure_ascii=False)); return

    strength = (args.get("image_strength") or "").strip().lower()
    if strength and strength not in STRENGTHS:
        print(json.dumps({"error": f'"{strength}" is not a strength',
                          "strengths": STRENGTHS}, ensure_ascii=False)); return

    payload = {"name": (args.get("name") or "").strip(), "tokens": colors}
    if shape:
        payload["shape"] = shape
    if strength:
        payload["strength"] = strength
    warning = None
    brief = (args.get("image") or "").strip()
    if brief:
        b64, kind, error = _generate(brief, colors)
        if error:
            warning = error          # the theme is still worth saving
        else:
            # Named by what the bytes actually are: HomeCore saves it with the
            # matching extension, and that extension is the Content-Type it is
            # served with afterwards.
            payload[f"backdrop_{kind}"] = b64
    elif args.get("drop_image"):
        payload["drop_backdrop"] = True

    out = _homeweb("POST", "/theme/api/set", payload)
    if warning:
        out["warning"] = warning
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
