#!/usr/bin/env python3
"""The Studio's card: who has it, and reserving it from this machine.

    ./home-stack card                       who has the card, who waits
    ./home-stack card --token               the card token, for a program on the LAN
    ./home-stack card run -m 30 -- CMD ...  reserve it, run CMD on it, give it back

`run` waits its turn (the Studio's running job finishes first), sends the
heartbeat every ten minutes while CMD runs, and releases the card when CMD
exits, however it exits. CMD sees CUDA_VISIBLE_DEVICES set to the card, with
CUDA_DEVICE_ORDER=PCI_BUS_ID so the number means what nvidia-smi says.

The API is home-studio's /api/gpu (docs/home-studio.md, "Reserving the
card"); this is one client of it, the same as any other.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy  # noqa: E402
import models  # noqa: E402


def _where() -> tuple[str, str]:
    url = deploy.studio_url(models._config())
    shared = (models._secrets().get("PROXY_SHARED_SECRET") or "").strip()
    if not url or not shared:
        sys.exit("There is no Studio to reserve the card from (services.home-studio is off, "
                 "or PROXY_SHARED_SECRET is not set).")
    return url, deploy.derive_studio_gpu_token(shared)


def _api(method: str, path: str, body: dict | None = None, timeout: float = 75) -> tuple[int, dict]:
    url, token = _where()
    req = urllib.request.Request(url + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except ValueError:
            return exc.code, {}


def _mins(seconds) -> str:
    return f"~{max(1, round((seconds or 0) / 60))} min"


def show() -> int:
    code, st = _api("GET", "/api/gpu")
    if code != 200:
        print(f"the Studio answered {code}: {st.get('detail', '')}", file=sys.stderr)
        return 1
    studio = st["studio"]
    print(f"card {st['device'] or '?'}: " + (
        "the Studio is working" + (f", {_mins(studio['ends_in'])} to go" if studio["ends_in"] else "")
        if studio["running"] else "the Studio is idle") + f", {studio['queued']} queued")
    for r in st["reservations"]:
        what = f" ({r['purpose']})" if r["purpose"] else ""
        if r["state"] == "granted":
            print(f"  has it   {r['holder']}{what}: " +
                  ("over its estimate" if r["overdue"] else f"{_mins(r['ends_in'])} left of {r['minutes']}"))
        else:
            print(f"  waits #{r['position']} {r['holder']}{what}: {r['minutes']} min, starts in {_mins(r['starts_in'])}")
    if not st["reservations"]:
        print("  nobody has reserved it")
    return 0


def run(name: str, minutes: int, purpose: str, cmd: list[str]) -> int:
    code, r = _api("POST", "/api/gpu/reservations",
                   {"holder": name, "purpose": purpose, "minutes": minutes, "wait": 55})
    if code != 201:
        print(f"the Studio refused the reservation ({code}): {r.get('detail', '')}", file=sys.stderr)
        return 1
    rid = r["id"]
    try:
        while r.get("state") != "granted":
            print(f"waiting for the card: #{r.get('position')} in line, starts in {_mins(r.get('starts_in'))}",
                  file=sys.stderr)
            code, r = _api("GET", f"/api/gpu/reservations/{rid}?wait=55")
            if code == 404:
                print("the reservation ended before its turn", file=sys.stderr)
                return 1
        print(f"card {r['device']} is yours; running {' '.join(cmd)}", file=sys.stderr)
        done = threading.Event()

        def beat():
            while not done.wait(r.get("heartbeat_every", 600)):
                if _api("POST", f"/api/gpu/reservations/{rid}/heartbeat", {})[0] == 404:
                    print("the card reservation is gone (expired, or a parent ended it)", file=sys.stderr)
                    return
        threading.Thread(target=beat, daemon=True).start()
        env = {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": str(r["device"])}
        proc = subprocess.Popen(cmd, env=env)
        signal.signal(signal.SIGTERM, lambda *_: proc.terminate())
        try:
            return proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            return proc.wait()
        finally:
            done.set()
    finally:
        _api("DELETE", f"/api/gpu/reservations/{rid}", timeout=20)
        print("card given back", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--token", action="store_true", help="print the card token for a program on the LAN")
    sub = ap.add_subparsers(dest="verb")
    rp = sub.add_parser("run", help="reserve the card, run a command on it, give it back")
    rp.add_argument("-m", "--minutes", type=int, required=True, help="roughly how long it needs the card")
    rp.add_argument("-n", "--name", default="", help="who is asking, for the Studio's page (default: the command)")
    rp.add_argument("-p", "--purpose", default="", help="what for")
    rp.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    if args.token:
        url, token = _where()
        print(token)
        print(f"# {url}/api/gpu  with  Authorization: Bearer <that>", file=sys.stderr)
        return 0
    if args.verb == "run":
        cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
        if not cmd:
            ap.error("run needs a command after --")
        return run(args.name or Path(cmd[0]).name, args.minutes, args.purpose, cmd)
    return show()


if __name__ == "__main__":
    sys.exit(main())
