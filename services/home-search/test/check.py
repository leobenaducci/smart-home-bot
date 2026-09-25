#!/usr/bin/env python3
"""Ask both services something a working one can answer, and fail otherwise.

This is the file that keeps `home-search` inside the stack's rule that a check
must be able to fail. Two ways the deploy it came from passed while the stack
was broken, and each has a test here:

* **SearXNG answered 200 with an empty `results` array.** An instance with no
  outbound DNS, or whose upstreams are refusing a new IP, does exactly that:
  the key is present, a check that asserted the key succeeded, the deploy went
  green, and every assistant call afterwards got "nothing came back" -- which
  the skill's own troubleshooting reads as an instance problem. So this
  requires a *result*, and prints `unresponsive_engines` when there is none.

* **Vane answered on its port with no model behind it.** It comes up long
  before it has one, and with no chat model or no embedding model every
  /api/search returns 400 while the container looks healthy. So this reads
  /api/providers and names the missing half.

Usage:
    check.py --searxng http://127.0.0.1:8888 --vane http://127.0.0.1:3000
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# The instance rejects a curl-looking User-Agent on /search when the limiter is
# on. The limiter is off here -- validate-settings.py refuses to deploy
# otherwise -- but the skill sends a browser's headers, so this asks the way the
# skill does rather than the way that happens to work.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Accept-Language": "en",
}


def die(msg: str) -> None:
    sys.exit("home-search: " + msg)


def fetch(url: str, timeout: int):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def poll(url: str, deadline: float, what: str, accept=None) -> str:
    """Wait for a body the caller will *accept*, not merely for a body.

    At least one request, always. The loop used to test the clock before its
    first attempt, so a service asked with no budget left was declared dead
    without ever being contacted -- and `last` was still the empty string it
    was initialised to, which is the one field written to explain the failure.

    *accept* is handed the decoded body and returns None when it is good, or a
    string saying what is wrong. Retrying only the *connection* reintroduces
    this file's own bug one level up: SearXNG answers 200 with an empty
    `results` array while its engines are still warming, and Vane answers 200
    on /api/providers with no models while Ollama is still pulling one. Both
    happen on the first ask, so a retry that stops at "a body arrived" gives up
    at once on the two conditions that resolve by themselves, well inside the
    budget the manifest deliberately raised.
    """
    last = ""
    first = True
    while first or time.time() < deadline:
        first = False
        try:
            body = fetch(url, 20)
            if accept is None:
                return body
            complaint = accept(body)
            if complaint is None:
                return body
            last = complaint
            time.sleep(5)
            continue
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            last = f"HTTP {exc.code}: {body}"
            if exc.code == 403:
                last += ("\n    A 403 with an HTML body means search.formats is "
                         "missing 'json'.")
            elif exc.code == 429:
                last += ("\n    A 429 means the limiter is on, which "
                         "validate-settings.py rejects -- so the deployed "
                         "settings.yml is not the one this stack installed.")
        except Exception as exc:                     # noqa: BLE001 - reported
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(5)
    die(f"{what} never answered within the timeout.\n    last: {last}")


def _searxng_complaint(raw: str):
    """None when this body is a working answer, else what is wrong with it."""
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        return f"not JSON ({exc}): {raw[:200]}"
    if not isinstance(doc, dict):
        return f"returned {type(doc).__name__}, not an object"
    results = doc.get("results")
    if not isinstance(results, list):
        return f"no `results` list; keys were: {sorted(doc)[:20]!r}"
    if not results:
        return ("0 results for a query that always has some -- engines not "
                f"answering yet. unresponsive_engines: "
                f"{doc.get('unresponsive_engines') or 'none reported'}")
    return None


def check_searxng(base: str, deadline: float) -> None:
    q = urllib.parse.urlencode({"q": "wikipedia", "format": "json"})
    raw = poll(f"{base.rstrip('/')}/search?{q}", deadline, "SearXNG",
               accept=_searxng_complaint)
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        die(f"/search?format=json did not return JSON ({exc}):\n    {raw[:300]}")
    if not isinstance(doc, dict):
        die(f"/search?format=json returned {type(doc).__name__}, not an object")
    results = doc.get("results")
    if not isinstance(results, list):
        die(f"/search?format=json has no `results` list; keys were: {sorted(doc)[:20]!r}")
    if not results:
        die(
            "SearXNG answers JSON but returned 0 results for a query that always\n"
            "    has some. The contract is fine; the engines are not.\n"
            "    unresponsive_engines: %s\n"
            "    Check outbound DNS and egress from this box."
            % (doc.get("unresponsive_engines") or "none reported")
        )
    print(f"home-search: SearXNG is serving JSON ({len(results)} results)")


def _vane_complaint(raw: str):
    """None when Vane can actually be asked something, else what is missing."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return f"not JSON: {raw[:200]}"
    if not isinstance(doc, dict):
        return f"returned {type(doc).__name__}, not an object"
    provs = doc.get("providers")
    if not isinstance(provs, list) or not all(isinstance(p, dict) for p in provs):
        return f"not the shape the skill reads: {raw[:200]}"
    def models(field):
        return [m for p in provs for m in (p.get(field) or [])
                if isinstance(m, dict) and m.get("key")]
    if not models("chatModels"):
        return "no chat model yet (Ollama may still be pulling one)"
    if not models("embeddingModels"):
        return "no embedding model yet (Ollama may still be pulling one)"
    return None


def check_vane(base: str, deadline: float) -> None:
    raw = poll(f"{base.rstrip('/')}/api/providers", deadline, "Vane",
               accept=_vane_complaint)
    try:
        doc = json.loads(raw)
    except ValueError:
        die(f"/api/providers did not return JSON:\n    {raw[:300]}")
    if not isinstance(doc, dict):
        die(f"/api/providers returned {type(doc).__name__}, not an object")
    provs = doc.get("providers")
    if not isinstance(provs, list) or not all(isinstance(p, dict) for p in provs):
        die(
            "/api/providers is not the shape the vane skill reads. Its pick()\n"
            "    does `for m in (p.get(field) or []) if m.get('key')`, so an entry\n"
            "    that is not an object is an uncaught AttributeError in front of\n"
            "    the family. Body was:\n    " + raw[:300]
        )

    # Both halves, named separately. Vane needs a chat model AND an embedding
    # model; with either missing every /api/search answers 400 while the
    # container stays healthy, and "Vane is up" is the answer that hides it.
    def models(field: str) -> list:
        found = []
        for p in provs:
            for m in (p.get(field) or []):
                if isinstance(m, dict) and m.get("key"):
                    found.append(f"{p.get('key') or '?'}/{m['key']}")
        return found

    chat = models("chatModels")
    embed = models("embeddingModels")
    if not chat or not embed:
        die(
            "Vane answers but cannot be asked anything: it has %s.\n"
            "    Every /api/search returns 400 in this state. Pull both on the\n"
            "    Ollama host, e.g. `ollama pull llama3.1` and\n"
            "    `ollama pull nomic-embed-text`.\n"
            "    chat models: %s\n    embedding models: %s"
            % ("no chat model" if not chat else "no embedding model",
               chat or "none", embed or "none")
        )
    print(f"home-search: Vane has {len(chat)} chat and {len(embed)} embedding model(s)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--searxng", required=True)
    ap.add_argument("--vane", required=True)
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args()
    # A deadline each, not one shared. They are independent services warming up
    # at the same time, and a single budget spent them in series: a SearXNG
    # that took most of it left Vane with none and the failure named Vane,
    # which was never asked.
    check_searxng(a.searxng, time.time() + a.timeout)
    check_vane(a.vane, time.time() + a.timeout)


if __name__ == "__main__":
    main()
