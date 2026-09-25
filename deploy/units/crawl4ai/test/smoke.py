#!/usr/bin/env python3
"""Does the crawler actually read a page, and does it still refuse the house?

Run: python deploy/units/crawl4ai/test/smoke.py [--base http://127.0.0.1:11235]

Two things are checked here that no health endpoint can tell you, and the
second is the reason this file exists at all.

**It read the page.** `/health` answers 200 from a server whose Chromium never
started, and `/md` on a broken crawler returns `success: true` with an empty
string. So the assertion is on the text: example.com's body carries "Example
Domain" as its heading, and a crawler that fetched an error page, a captcha or
nothing does not have it.

**It still refuses to fetch the house.** crawl4ai runs on the container network
with an LLM choosing its URLs, which makes it a fetch-anything primitive
pointed at everything the household runs -- the admin page, Grafana, Jenkins,
the camera registry, and the cloud-metadata address on any VM. GHSA-365w-hqf6-vxfg
(CVSS 9.8) is exactly that, fixed in 0.8.7. The guard is upstream's, which
means it can leave in an upgrade the same way it arrived, and nothing else in
this package would notice: every check in the manifest would still pass. So it
is asserted here, against the addresses that actually matter on this machine.

The token is not optional. Without CRAWL4AI_API_TOKEN the image binds the
container's own loopback and this script cannot reach it at all -- see the
comment in deploy/units/crawl4ai/docker-compose.yml.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PAGE = "https://example.com/"
EXPECT = "example domain"

# Addresses a crawler on this network must refuse. The first is cloud metadata
# (credentials, on any VM); the second is this stack's own admin page, which is
# equivalent to root on the machine; the last two are the ranges a household
# LAN actually uses.
BLOCKED = [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:21002/",
    "http://192.168.1.1/",
    "http://10.0.0.1/",
]


def post(base: str, path: str, body: dict, token: str, timeout: float):
    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"detail": body[:200]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:11235")
    ap.add_argument("--token", default=os.environ.get("CRAWL4AI_API_TOKEN", ""))
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    if not args.token:
        print("FAIL  no token. Pass --token or set CRAWL4AI_API_TOKEN — without "
              "one the server binds its own loopback and is unreachable.")
        return 1

    # /health is deliberately unauthenticated; the monitor routes are not.
    try:
        with urllib.request.urlopen(f"{args.base}/health", timeout=15) as r:
            health = json.loads(r.read().decode())
    except Exception as exc:                                      # noqa: BLE001
        print(f"FAIL  nothing is answering on {args.base}: {exc}")
        return 1
    if health.get("status") != "ok":
        print(f"FAIL  /health does not say ok: {health}")
        return 1
    print(f"  ok    healthy, version={health.get('version')}")

    status, out = post(args.base, "/md", {"url": PAGE}, args.token, args.timeout)
    if status != 200:
        print(f"FAIL  /md answered {status}: {str(out)[:200]}")
        return 1
    text = (out.get("markdown") or "")
    if not text.strip():
        print("FAIL  it answered with no markdown — a crawler that fetched "
              "nothing looks exactly like this")
        return 1
    if EXPECT not in text.lower():
        print(f"FAIL  read {len(text)} chars but not the page: {EXPECT!r} is "
              f"missing. Got: {text[:160]!r}")
        return 1
    print(f"  ok    read {PAGE} and it is the right page ({len(text)} chars)")

    leaked = []
    for url in BLOCKED:
        status, out = post(args.base, "/md", {"url": url}, args.token, 45.0)
        blocked = status >= 400 and "ssrf" in str(out).lower()
        print(f"  {'ok  ' if blocked else 'FAIL'}  refuses {url} "
              f"({status}: {str(out.get('detail') or out)[:60]})")
        if not blocked:
            leaked.append(url)
    if leaked:
        print(f"\nFAIL  {len(leaked)} address(es) this must not reach were not "
              f"refused. The SSRF guard is upstream's and it has regressed, or "
              f"the pin moved to a version without it. Do not deploy this.")
        return 1

    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
