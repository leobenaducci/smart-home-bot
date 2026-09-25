#!/usr/bin/env python3
"""
Find and download openly-licensed images for Alfred.
Usage: python3 find_image.py '<json>'

    {"action": "search", "query": "cordillera", "type": "photo", "limit": 6}
    {"action": "get", "url": "https://...", "filename": "cerro.jpg",
     "attribution": "..."}

Two sources, both free and neither needing an API key:

- **Openverse** (openverse.org, WordPress) aggregates the openly-licensed
  catalogues — Flickr, Wikimedia, Nappy, museums — and is the right default for
  photographs and illustrations.
- **Wikimedia Commons** is where the diagrams, maps and **SVGs** are, which
  Openverse indexes unevenly.

Everything returned carries its licence and the attribution string. That is not
decoration: most of these licences require credit, and an image used in a
document Alfred hands to the family without it is a licence breach committed on
their behalf.
"""
import base64, json, mimetypes, os, re, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

from nanobot.security.network import validate_url_target

WORKSPACE = Path(os.environ.get("NANOBOT_WORKSPACE", os.path.expanduser("~/.nanobot/workspace")))
MEDIA_DIR = WORKSPACE / "media"

OPENVERSE_API = "https://api.openverse.org/v1/images/"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Only these hosts are ever fetched from. The model chooses a URL, and a URL
# chosen by a model is input: without this, "download this image" is a request
# to fetch anything reachable from the container, including the house's own
# services. Downloads are limited to what one of the two searches returned.
ALLOWED_DOWNLOAD_SUFFIXES = (
    ".openverse.org", ".wikimedia.org", ".wikipedia.org", ".wmcloud.org",
    ".staticflickr.com", ".flickr.com", ".smithsonianmag.com", ".si.edu",
    ".metmuseum.org", ".rawpixel.com", ".nappy.co", ".stocksnap.io",
    ".pexels.com", ".unsplash.com", ".freesvg.org", ".openclipart.org",
)
# http(s) only. The allowlist is about *where*; this is about *how* — urllib
# also speaks ftp: and file:, and neither has any business here.
ALLOWED_SCHEMES = ("http", "https")
MAX_BYTES = 12 * 1024 * 1024
TIMEOUT_S = 20
# 1..20, and never a traceback: `limit` arrives inside a model-authored JSON
# blob, and the model's only feedback channel is this script's stdout, which is
# documented to be JSON.
LIMIT_MIN, LIMIT_MAX, LIMIT_DEFAULT = 1, 20, 6


def _as_limit(raw):
    try:
        return max(LIMIT_MIN, min(int(raw), LIMIT_MAX))
    except (TypeError, ValueError):
        return LIMIT_DEFAULT
# Openverse rejects requests without one, and it is the polite thing to send.
UA = "AlfredHomeAssistant/1.0 (household assistant; +https://github.com/HKUDS/nanobot)"

TYPES = {
    # (openverse category, commons search qualifier)
    "photo": ("photograph", "filetype:bitmap"),
    "illustration": ("illustration", "filetype:drawing"),
    "drawing": ("illustration", "filetype:drawing"),
    "svg": (None, "filetype:drawing filemime:image/svg+xml"),
    "diagram": (None, "filetype:drawing"),
    "any": (None, None),
}


def _get(url, params=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _attribution(title, creator, license_name, license_url, source_url):
    """A credit line that satisfies CC-BY-style attribution: what, by whom,
    under which licence, and where it came from."""
    bits = [f'"{title}"' if title else "Imagen"]
    if creator:
        bits.append(f"de {creator}")
    if license_name:
        lic = license_name.upper().replace("_", "-")
        bits.append(f"({lic}{f' — {license_url}' if license_url else ''})")
    if source_url:
        bits.append(f"via {source_url}")
    return " ".join(bits)


def _search_openverse(query, kind, limit):
    category = TYPES.get(kind, TYPES["any"])[0]
    params = {"q": query, "page_size": limit}
    if category:
        params["category"] = category
    if kind == "svg":
        params["extension"] = "svg"
    try:
        data = _get(OPENVERSE_API, params)
    except Exception as e:
        return [], f"openverse: {e}"
    out = []
    for r in data.get("results", []):
        out.append({
            "source": "openverse",
            "title": r.get("title") or "",
            "creator": r.get("creator") or "",
            "license": r.get("license") or "",
            "license_url": r.get("license_url") or "",
            "url": r.get("url") or "",
            "thumbnail": r.get("thumbnail") or "",
            "page": r.get("foreign_landing_url") or "",
            "filetype": r.get("filetype") or "",
            "width": r.get("width"), "height": r.get("height"),
            "attribution": r.get("attribution") or _attribution(
                r.get("title"), r.get("creator"), r.get("license"),
                r.get("license_url"), r.get("foreign_landing_url")),
        })
    return out, None


def _search_commons(query, kind, limit):
    qualifier = TYPES.get(kind, TYPES["any"])[1]
    search = f"{qualifier} {query}".strip() if qualifier else query
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": search, "gsrnamespace": "6",
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": "640",
    }
    try:
        data = _get(COMMONS_API, params)
    except Exception as e:
        return [], f"commons: {e}"
    out = []
    for page in (data.get("query", {}).get("pages", {}) or {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}

        title = (page.get("title") or "").removeprefix("File:")
        artist = _meta_text(meta, "Artist")
        licence = _meta_text(meta, "LicenseShortName")
        licence_url = _meta_text(meta, "LicenseUrl")
        out.append({
            "source": "wikimedia commons",
            "title": title,
            "creator": artist,
            "license": licence,
            "license_url": licence_url,
            "url": info.get("url") or "",
            "thumbnail": info.get("thumburl") or "",
            "page": info.get("descriptionurl") or "",
            "filetype": (info.get("mime") or "").split("/")[-1],
            "width": info.get("width"), "height": info.get("height"),
            "attribution": _attribution(title, artist, licence, licence_url,
                                        info.get("descriptionurl")),
        })
    return out, None


def _meta_text(meta, key):
    """One `extmetadata` field as plain text. Commons returns small HTML
    fragments in these (an `<a>` around the author, mostly)."""
    v = (meta.get(key) or {}).get("value") or ""
    return _strip_tags(v) if "<" in v else " ".join(v.split())


def _strip_tags(html):
    out, depth = [], 0
    for ch in html:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return " ".join("".join(out).split())


def _host_allowed(url):
    try:
        parts = urllib.parse.urlparse(url)
        host = (parts.hostname or "").lower()
    except Exception:
        return False
    if not host or parts.scheme.lower() not in ALLOWED_SCHEMES:
        return False
    return any(host == s.lstrip(".") or host.endswith(s) for s in ALLOWED_DOWNLOAD_SUFFIXES)


class _AllowlistedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-check the allowlist on every hop.

    Checking only the URL the model handed us constrains the *first* request
    and nothing after it: urllib follows 30x by default, and some allowlisted
    domains host third-party services (`*.wmcloud.org` is Wikimedia Cloud, one
    web service per user). One `Location: http://192.168.1.11:5001/snapshot`
    and the allowlist has bought nothing — a camera frame comes back as
    `image/jpeg` and passes the content-type check on the way in.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _host_allowed(newurl):
            raise urllib.error.URLError(
                f"redirect a un host no permitido: "
                f"{urllib.parse.urlparse(newurl).hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_AllowlistedRedirects())


def search(data):
    query = str(data.get("query") or "").strip()
    if not query:
        return {"error": "falta 'query'"}
    kind = str(data.get("type") or "any").lower()
    limit = _as_limit(data.get("limit"))
    source = str(data.get("source") or "auto").lower()

    results, errors = [], []
    # SVGs and diagrams live on Commons; photographs are Openverse's strength.
    order = ["commons", "openverse"] if kind in ("svg", "diagram") else ["openverse", "commons"]
    if source in ("openverse", "commons"):
        order = [source]

    for src in order:
        found, err = (_search_openverse if src == "openverse" else _search_commons)(query, kind, limit)
        if err:
            errors.append(err)
        results += found
        # Only reach for the second source when the first came back thin —
        # two round-trips for a question already answered is wasted time.
        if len(results) >= limit:
            break

    top = results[:limit]
    return {"query": query, "type": kind, "count": len(top), "results": top,
            "errors": errors or None}


def get(data):
    url = str(data.get("url") or "").strip()
    if not url:
        return {"error": "'url' is missing"}
    if not _host_allowed(url):
        return {"error": "that URL is not from a known image source; "
                         "use one that `search` returned"}
    filename = os.path.basename(str(data.get("filename") or "").replace("\\", "/")).strip()
    if not filename:
        filename = os.path.basename(urllib.parse.urlparse(url).path) or "image"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = MEDIA_DIR / filename

    # Accept matters: urllib sends none by default, and some of these CDNs
    # (Flickr's, measured) answer a bare request with 502 rather than the file.
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "image/*,*/*;q=0.8",
    })
    try:
        # `_OPENER`, not urlopen: it re-checks the allowlist on every redirect.
        with _OPENER.open(req, timeout=TIMEOUT_S) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype and not ctype.startswith("image/"):
                return {"error": f"eso no es una imagen ({ctype})"}
            if not dest.suffix:
                dest = dest.with_suffix(mimetypes.guess_extension(ctype) or ".jpg")
            payload = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code} al descargar"}
    except Exception as e:
        return {"error": str(e)}
    if len(payload) > MAX_BYTES:
        return {"error": f"la imagen supera los {MAX_BYTES // (1024 * 1024)} MB"}
    dest.write_bytes(payload)

    rel = str(dest.relative_to(WORKSPACE))
    return {
        "file": rel,
        "saved_to": str(dest),
        "bytes": len(payload),
        "attribution": str(data.get("attribution") or ""),
        "message": f"Imagen guardada en {rel}",
    }


# --- drawing one that does not exist ------------------------------------------
#
# The two catalogues above hold what somebody has already photographed. A
# birthday invitation needs balloons *in this palette, on this ground*, and no
# search returns that -- which is why the Designer, having only search and
# hand-written SVG, produced type on a flat field and nothing else.
#
# The household already pays for this: `assistant.models.image_normal` and
# `image_high` are set on the admin page ("Pictures the assistant draws") and
# exported by the deployer. Until now only the `theme` skill read them, so the
# models were wired to wallpapers and reachable from nowhere else.
TOGETHER_KEY = os.environ.get("TOGETHER_API_KEY", "")
TOGETHER_URL = os.environ.get(
    "TOGETHER_API_URL", "https://api.together.xyz/v1/images/generations")
# Together sits behind Cloudflare, and Cloudflare rejects urllib's default
# `Python-urllib/3.x` outright: 403 with a body of `error code: 1010`, which
# never reaches Together and reads exactly like a dead key. This header is
# load-bearing -- the same trap `theme.py` documents, and this stack has now
# walked into it twice.
DRAW_UA = "home-stack-images/1.0"
# Every size a Together image model is known to accept, with the square one
# last because it is the only one all of them take.
_SIZES = {
    "square": (1024, 1024),
    "portrait": (896, 1152),
    "landscape": (1264, 848),
}


def _wants_high(quality):
    return str(quality).lower() in ("high", "alta", "good")


def _draw_endpoint(quality):
    """(model, url, key) for the quality asked for.

    Two roles, and they can sit on different providers -- a cheap everyday
    model and a dearer one for something somebody keeps. So the endpoint is
    chosen *with* the model rather than assumed: picking the model from one
    role and the URL from a module constant is how "high quality" quietly
    started meaning "some other role's endpoint, addressed as Together".

    This mattered most when `image_normal` was a container on this machine
    (`services/z-image`, removed 2026-09-08 -- see docs/local-generation.md).
    The per-role resolution is kept because the failure it prevents is about
    two roles disagreeing, which has nothing to do with either being local.

    `IMAGE_API_*` are what the deployer exports now. `TOGETHER_*` are read
    after them and only as a fallback: an env file written before this stack
    could draw locally still works, which is the same courtesy every renamed
    key here gets.
    """
    high = _wants_high(quality)
    model = (os.environ.get("IMAGE_API_MODEL_HIGH" if high else "IMAGE_API_MODEL", "")
             or os.environ.get("TOGETHER_IMAGE_MODEL_HIGH" if high
                               else "TOGETHER_IMAGE_MODEL", ""))
    url = os.environ.get("IMAGE_API_URL_HIGH" if high else "IMAGE_API_URL", "")
    if not model:
        # Fall back to the other role rather than refusing: a household that
        # has configured one of the two should get pictures.
        other_high = not high
        model = (os.environ.get("IMAGE_API_MODEL_HIGH" if other_high
                                else "IMAGE_API_MODEL", "")
                 or os.environ.get("TOGETHER_IMAGE_MODEL_HIGH" if other_high
                                   else "TOGETHER_IMAGE_MODEL", ""))
        url = os.environ.get("IMAGE_API_URL_HIGH" if other_high
                             else "IMAGE_API_URL", "")
    # No URL means the role is on a hosted provider, which is Together here.
    # The key goes with the endpoint: `IMAGE_API_KEY` is whatever that endpoint
    # wants (empty for the house's own container, which answers whoever asks),
    # and sending Together's to a container on the LAN would be handing a paid
    # credential to something that never needed it.
    if url:
        return model, url, os.environ.get("IMAGE_API_KEY", "")
    return model, TOGETHER_URL, TOGETHER_KEY


def draw(data):
    """Generate an image from a description and save it into the workspace."""
    prompt = str(data.get("prompt") or data.get("query") or "").strip()
    if not prompt:
        return {"error": "'prompt' is missing: say what the picture shows"}
    # `normal` by default, which is the house's own GPU. It used to default to
    # `high`, and `high` is a paid API -- so every drawing anybody asked for,
    # including the incidental ones, was billed. That made sense when the
    # house had no image model of its own; it does not now.
    #
    # `high` is still there and still better. It is reached two ways and both
    # of them involve a person: they ask for it, or Alfred offers it once they
    # like the draft. See SKILL.md.
    model, url, key = _draw_endpoint(data.get("quality") or "normal")
    if not model:
        return {"error": "no image model is configured: set one on the admin "
                         "page under Models -> pictures the assistant draws"}
    # A key is required of a hosted endpoint and meaningless for the house's
    # own container, which is reachable on the container network and answers
    # whoever asks. Checking `TOGETHER_KEY` unconditionally -- which is what
    # this did -- would refuse to draw locally on a household that had never
    # signed up to Together, which is exactly the household most likely to be
    # running its own model.
    if not key and url == TOGETHER_URL:
        return {"error": "TOGETHER_API_KEY is not set, so nothing can be drawn; "
                         "search for an existing image instead"}

    shape = str(data.get("shape") or "square").lower()
    width, height = _SIZES.get(shape, _SIZES["square"])
    # Bytes inline from the house's own container, a URL from a hosted one.
    #
    # Not a preference. `_save_drawn` refuses to fetch a private address --
    # correctly, because a hosted provider handing back an internal URL is an
    # SSRF and the guard is the whole reason that check exists. But the local
    # model *is* an internal address, so asking it for a URL means asking for
    # a link this skill must then refuse to follow. Inline bytes skip the
    # second request entirely and leave the guard exactly as strict.
    inline = url != TOGETHER_URL
    body = {"model": model, "prompt": prompt, "n": 1, "width": width,
            "height": height,
            "response_format": "b64_json" if inline else "url"}

    def _post(fields):
        headers = {"User-Agent": DRAW_UA, "Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(
            url, data=json.dumps(fields).encode(), headers=headers)
        # Longer than the hosted default: the house's own model takes ~33 s a
        # picture on the card in this box, and its first call after an idle
        # unload reloads the weights first.
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode())

    # These models disagree about their own request shape and only say so by
    # rejecting one. Read what the 400 objects to and drop it, rather than
    # encoding one model's opinions here -- which is what broke the theme skill
    # the day its model changed. Bounded, each adjustment tried once.
    payload, last, fell_back = None, None, False
    for _ in range(3):
        try:
            payload = _post(body)
            break
        except urllib.error.HTTPError as e:
            last = e.read().decode()[:300]
            if e.code == 403 and "1010" in last:
                return {"error": "Cloudflare refused the request (error 1010): "
                                 "the User-Agent was rejected, not the key"}
            # The house's own model could not get the card. Not a broken
            # request -- the same request will work later -- so retrying it
            # here three times just spends the attempts on a queue nobody is
            # draining. If the other quality sits on a hosted provider, draw
            # there instead and say so, because a picture from the paid model
            # is a better answer than no picture with an explanation about
            # VRAM.
            #
            # 503 is "the card is busy, ask again later" and 507 is "loaded
            # but no room to run". Both mean the same thing to whoever asked
            # for a drawing: the GPU is busy with Ollama's models and the
            # camera detectors.
            #
            # 502 is deliberately NOT in this list. The server answers 502 for
            # a failure that will not clear on its own -- a wrong model id, a
            # gated repo, a corrupt cache -- and treating those as "busy"
            # would bill the hosted provider for every drawing, forever,
            # while the broken container sat there needing a person.
            if e.code in (503, 507) and url != TOGETHER_URL:
                alt_model, alt_url, alt_key = _draw_endpoint(
                    "normal" if _wants_high(data.get("quality") or "normal")
                    else "high")
                if alt_url == TOGETHER_URL and alt_model and alt_key:
                    model, url, key = alt_model, alt_url, alt_key
                    body["model"] = alt_model
                    # And back to a URL: the hosted provider is not an
                    # internal address, and this is the shape the download
                    # path below was written for.
                    body["response_format"] = "url"
                    fell_back = True
                    inline = False
                    continue
                return {"error": "the house's own image model could not get "
                                 "the GPU, and there is no hosted model "
                                 "configured to fall back to. Try again in a "
                                 "minute, or search for an existing image."}
            dropped = False
            for field in ("width", "height", "response_format", "n"):
                # Whole word, not substring. `"n" in last` is true of very
                # nearly every error body ever written, so a plain `in` test
                # dropped `n` on the first 400 whatever the complaint was --
                # marking the attempt "adjusted", spending one of only three,
                # and hiding the square-size fallback below behind it.
                if field in body and re.search(rf"\b{field}\b", last):
                    body.pop(field, None)
                    dropped = True
            if not dropped and (width, height) != _SIZES["square"]:
                body["width"], body["height"] = _SIZES["square"]
                dropped = True
            if not dropped:
                return {"error": f"HTTP {e.code}: {last}"}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
    if payload is None:
        return {"error": f"the image model kept refusing: {last}"}

    # Whether a person was billed for this, from the credential rather than
    # from the address. `url != TOGETHER_URL` looks like the same question and
    # is not: `IMAGE_API_URL` + `IMAGE_API_KEY` is the third case the manifest
    # declares that key for -- an endpoint that is neither Together nor the
    # house -- and reporting that one as the house's own model is exactly the
    # silence SKILL.md makes Alfred break. The house's container is the one
    # that wants no key; `key` is also reassigned by the fall-back above, so
    # this stays right after a substitution without being maintained twice.
    local = not key
    entries = payload.get("data") or []
    url = ""
    for entry in entries:
        if isinstance(entry, dict):
            url = entry.get("url") or ""
            if url:
                break
    if not url:
        b64 = next((e.get("b64_json") for e in entries
                    if isinstance(e, dict) and e.get("b64_json")), "")
        if not b64:
            return {"error": "the model answered without an image"}
        try:
            raw = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            return {"error": "the model's image did not decode"}
        # The same cap the download path applies. Nothing about the bytes
        # arriving inline instead of over a URL makes them safe to write
        # unbounded into the workspace.
        if len(raw) > MAX_BYTES:
            return {"error": f"la imagen supera los {MAX_BYTES // (1024 * 1024)} MB"}
        filename = os.path.basename(
            str(data.get("filename") or "").replace("\\", "/")).strip() or "dibujo.png"
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        dest = MEDIA_DIR / filename
        if not dest.suffix:
            dest = dest.with_suffix(".png")
        dest.write_bytes(raw)
        rel = str(dest.relative_to(WORKSPACE))
        # The same sentence the download path writes, and for the same reason:
        # the household chose which model draws, so a substitution has to be
        # said out loud. A flag nobody reads back is not saying it -- the model
        # summarising this for a person reads `message`.
        msg = f"Imagen dibujada y guardada en {rel}"
        if fell_back:
            msg += (f" (la GPU de la casa estaba ocupada, así que la dibujó "
                    f"{model} en la nube)")
        return {"file": rel, "saved_to": str(dest), "model": model,
                "fell_back": fell_back, "local": local, "paid": not local, "message": msg}

    return _save_drawn(url, data.get("filename"), model, fell_back, local)


def _save_drawn(url, filename, model, fell_back=False, local=False):
    """Download a picture the image model just made.

    Not through `get`, and the difference is the point. That path enforces
    ALLOWED_DOWNLOAD_SUFFIXES because its URL comes from the *model*, and a
    model-chosen URL is an SSRF waiting to happen. This URL comes from
    Together's own API response, so the catalogue allowlist does not apply --
    and Together's CDN host is not in it, which would refuse every drawing.

    What must still hold is that nothing here reaches the house: the same size
    cap, the same image/* check, and `validate_url_target`, which resolves the
    name and refuses a private address. An answer that pointed at 192.168.x
    would otherwise be fetched with the household's own network position.
    """
    ok, why = validate_url_target(url)
    if not ok:
        return {"error": f"the image model returned an unusable URL: {why}"}
    filename = os.path.basename(str(filename or "").replace("\\", "/")).strip() or "dibujo.png"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = MEDIA_DIR / filename
    req = urllib.request.Request(url, headers={
        "User-Agent": DRAW_UA, "Accept": "image/*,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            final_ok, final_why = validate_url_target(r.geturl())
            if not final_ok:
                return {"error": f"the download was redirected somewhere unusable: {final_why}"}
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype and not ctype.startswith("image/"):
                return {"error": f"eso no es una imagen ({ctype})"}
            if not dest.suffix:
                dest = dest.with_suffix(mimetypes.guess_extension(ctype) or ".png")
            payload = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code} al descargar el dibujo"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    if len(payload) > MAX_BYTES:
        return {"error": f"la imagen supera los {MAX_BYTES // (1024 * 1024)} MB"}
    dest.write_bytes(payload)
    rel = str(dest.relative_to(WORKSPACE))
    out = {"file": rel, "saved_to": str(dest), "bytes": len(payload),
           "model": model, "attribution": f"generada con {model}",
           # A flag rather than "work it out from the model name". SKILL.md
           # requires Alfred to say which model drew a picture in every reply,
           # and a rule that depends on recognising a model id is a rule that
           # will be got wrong the first time the id changes.
           "local": local, "paid": not local,
           "message": f"Imagen dibujada y guardada en {rel}"}
    # Said out loud, because the household chose which model draws and a
    # silent substitution makes that choice a lie. It also names the cause:
    # the card is shared with Ollama and the camera detectors, and "busy" is
    # a state that passes rather than a fault to chase.
    if fell_back:
        out["fell_back"] = True
        out["message"] += (f" (la GPU de la casa estaba ocupada, así que la "
                           f"dibujó {model} en la nube)")
    return out


ACTIONS = {"search": search, "get": get, "draw": draw}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: find_image.py '<json>'"}))
        sys.exit(1)
    try:
        payload = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON: {e}"}))
        sys.exit(1)
    fn = ACTIONS.get(str(payload.get("action") or "search").lower())
    if fn is None:
        print(json.dumps({"error": f"unknown action: {payload.get('action')}"}))
        sys.exit(1)
    try:
        result = fn(payload)
    except Exception as e:  # noqa: BLE001
        # The caller is a model whose only feedback channel is this stdout, and
        # every other exit here is JSON. A traceback on stderr with nothing on
        # stdout reads as "the skill is broken" and gets retried unchanged.
        result = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(1 if "error" in result else 0)
