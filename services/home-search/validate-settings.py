#!/usr/bin/env python3
"""Parse the deployed settings.yml BEFORE anything is replaced, and seed it.

Runs as the unit's `pre:` hook, which is the whole point of the timing. The
stack this came from validated by *printing* the value and installing the file
anyway: a config with `limiter: true` passed, the good host file was
overwritten, the containers were recreated onto it, and only then did the check
spend four minutes failing on 429 — with the working config already gone.

Two jobs, in this order:

1. **Seed the secret.** The example ships the image's own placeholder, so a
   first deploy would otherwise stand up an instance whose secret_key is the
   string every SearXNG on the internet ships with. If the file still says
   `ultrasecretkey` and SEARXNG_SECRET_KEY is set, it is substituted in place.
   An operator's own key is never touched.

2. **Refuse a config that would deploy green and answer nothing.** A missing
   `json` format, a placeholder secret with no key to replace it, and a
   `server.limiter` that is not explicitly false are each a named failure here
   rather than a 403 or a 429 in front of the family later.

Absent counts as wrong for the limiter. Not because the image's default is on
-- it ships `limiter: false` -- but because this is the setting that decides
whether the instance answers the assistant at all, and inheriting it from an
upstream nobody here controls is how it changes without anybody noticing.
"""
import os
import re
import sys
from pathlib import Path

PLACEHOLDER = "ultrasecretkey"


def die(msg: str) -> None:
    sys.exit("home-search: " + msg)


def main() -> None:
    config_dir = os.environ.get("SEARCH_CONFIG_DIR", "").strip()
    if not config_dir:
        die("SEARCH_CONFIG_DIR is unset; the manifest's state: entry supplies it")
    path = Path(config_dir) / "searxng" / "settings.yml"
    if not path.exists():
        # The deployer seeds it from settings.example.yml through `seed_from`,
        # which runs before this. Reaching here means that did not happen.
        die(f"{path} does not exist; it is seeded from settings.example.yml")

    raw = path.read_text(encoding="utf-8")

    # Substitution on the text, not a YAML round-trip: this file is mostly
    # comments explaining why each setting is what it is, and re-emitting it
    # through a dumper would throw all of them away.
    if PLACEHOLDER in raw:
        key = os.environ.get("SEARXNG_SECRET_KEY", "").strip()
        if not key:
            die(
                f"{path} still carries the placeholder secret and no\n"
                f"    SEARXNG_SECRET_KEY is set to replace it. Run\n"
                f"    `./home-stack install --generate-secrets`, or put an\n"
                f"    `openssl rand -hex 32` into that file by hand."
            )
        if key == PLACEHOLDER:
            die("SEARXNG_SECRET_KEY is the placeholder itself")
        raw = raw.replace(PLACEHOLDER, key)
        # Written through a temp file in the same directory and renamed, which
        # is atomic on the same filesystem. This is the single live copy of a
        # config whose loss is not loud: SearXNG writes the image template in
        # its place, which serves HTML only, so the container comes up healthy
        # and every skill call gets a 403. A truncated write here -- a full
        # disk, a killed deploy -- would produce exactly that.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(raw, encoding="utf-8")
        os.replace(tmp, path)
        print(f"home-search: seeded server.secret_key in {path}")

    try:
        import yaml
    except ImportError:
        die("python3-yaml is not installed on this target")
    try:
        cfg = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        die(f"{path} is not valid YAML:\n    {exc}")
    if not isinstance(cfg, dict):
        die(f"{path} parsed as {type(cfg).__name__}, not a mapping")

    server = cfg.get("server") or {}
    search = cfg.get("search") or {}

    secret = str(server.get("secret_key") or "")
    if not secret or secret == PLACEHOLDER:
        die(f"server.secret_key in {path} is {secret!r}")

    formats = search.get("formats") or []
    if "json" not in formats:
        die(
            f"search.formats in {path} does not include 'json' (found: {formats!r}).\n"
            f"    Without it /search?format=json answers 403 with an HTML body,\n"
            f"    which is the whole contract the searxng skill is built on."
        )

    limiter = server.get("limiter")
    if limiter is not False:
        die(
            f"server.limiter is {'absent' if limiter is None else repr(limiter)} in {path};\n"
            f"    it must be explicitly false. The limiter answers 429 to anything\n"
            f"    whose User-Agent looks like a script, which is exactly what the\n"
            f"    assistant is."
        )

    print(
        "home-search: settings ok — formats=%r, limiter=%r, secret set (%d chars)"
        % (formats, limiter, len(secret))
    )


if __name__ == "__main__":
    main()
