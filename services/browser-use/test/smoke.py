#!/usr/bin/env python3
"""Does the browser actually open a page and read it back?

Run: python services/browser-use/test/smoke.py [--base http://127.0.0.1:21034]

The check is on the *content*, not the status code, and that is the whole point.
A browser that starts, navigates nowhere and hands back an empty string answers
200 for as long as you leave it running -- which is the failure this service is
most likely to have, because a missing Chromium binary and a working one differ
only in what comes back.

`example.com` because it is the one page on the internet whose text is stable,
short, and served without a cookie wall. A browser that read it has the page's
heading; one that returned an error page, a captcha or nothing does not.

The assertion is the *heading* and not a sentence from the body, because the
body is not as stable as this file first assumed: it used to read "illustrative
examples", which is what this test checked, and by 2026-09 it said "for use in
documentation examples" instead -- so the check had quietly become one that a
working browser fails. `<h1>Example Domain</h1>` has outlasted every rewrite of
the prose under it.

It also checks that the browser still **refuses the house**, which is the same
thing `deploy/units/crawl4ai/test/smoke.py` checks next door and for the same
reason: this service opens a URL a model picked after reading somebody else's
page, so the guard is the difference between a browser and a way into the LAN.
That check runs first, because it is fast and deterministic where the read
above depends on whichever model is configured -- so it still means something
on a day the model is the thing that is broken.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

PAGE = "https://example.com/"
EXPECT = "example domain"

# Refused before a browser is launched, so each of these answers in
# milliseconds. The loopback entry is deliberately *this service's own
# /healthz*: it is a URL that certainly answers, so a pass proves the guard
# turned something reachable away rather than merely failing to reach a thing
# that was down. 169.254.169.254 is the cloud metadata address, which is what
# an SSRF is usually pointed at; file:// is the container's own disk.
#
# All three are checked at the door by `_guard`, which is the half a
# BROWSER_USE_ALLOW_CIDRS value does not widen unless somebody has whitelisted
# loopback or link-local -- which is not a thing to do. So this passing does
# *not* say the second-hop guard is intact: a household that has named a CIDR
# has turned the browser profile's IP-literal block off entirely, and nothing
# reachable from out here can see that.
BLOCKED = [
    ("http://169.254.169.254/", "private network"),
    ("file:///etc/passwd", "scheme"),
]


def check_blocked(base: str) -> int:
    """0 if every internal target was refused, 1 if any got through."""
    # `base` rather than a fixed port, so the loopback case stays "a URL that
    # certainly answers" when this is pointed somewhere other than 21034.
    targets = [(f"{base.rstrip('/')}/healthz", "private network")] + BLOCKED
    for url, expect in targets:
        # Short on purpose: the guard answers without opening anything, so a
        # request that takes seconds has already told you it went to the
        # browser instead.
        try:
            out = post(base, "/read", {"url": url}, 20.0)
        except Exception as exc:                                  # noqa: BLE001
            print(f"FAIL  asking for {url} did not come back cleanly: {exc}")
            return 1
        if out.get("ok"):
            print(f"FAIL  it opened {url}. The SSRF guard is not refusing the "
                  f"house -- see `_guard` and the browser profile in server.py")
            return 1
        if expect not in str(out.get("error") or "").lower():
            print(f"FAIL  {url} was refused, but not by the guard: "
                  f"{out.get('error')!r}. Expected {expect!r} -- something else "
                  f"is failing and the guard is untested")
            return 1
    print(f"  ok    refused all {len(targets)} internal target(s)")
    return 0


def post(base: str, path: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:21034")
    # Longer than the service's own task timeout, so a task that times out is
    # reported by the service as a sentence rather than by this as a socket
    # error -- the first tells you which of the two is wrong.
    ap.add_argument("--timeout", type=float, default=200.0)
    args = ap.parse_args()

    try:
        with urllib.request.urlopen(f"{args.base}/healthz", timeout=15) as r:
            health = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"FAIL  /healthz says it is not ready: {exc.read().decode()[:200]}")
        return 1
    except Exception as exc:                                      # noqa: BLE001
        print(f"FAIL  nothing is listening on {args.base}: {exc}")
        return 1
    print(f"  ok    healthy, model={health.get('model')}")

    rc = check_blocked(args.base)
    if rc:
        return rc

    out = post(args.base, "/read", {"url": PAGE}, args.timeout)
    if not out.get("ok"):
        print(f"FAIL  it could not read {PAGE}: {out.get('error')}")
        return 1

    text = (out.get("text") or "")
    if not text.strip():
        print("FAIL  it answered ok with no text — a browser that opened nothing "
              "looks exactly like this")
        return 1
    if EXPECT not in text.lower():
        print(f"FAIL  read {len(text)} chars but not the page: {EXPECT!r} is "
              f"missing. Got: {text[:160]!r}")
        return 1

    print(f"  ok    read {PAGE} in {out.get('seconds')}s "
          f"({out.get('steps')} step(s)), and it is the right page")
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
