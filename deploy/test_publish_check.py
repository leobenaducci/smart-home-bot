#!/usr/bin/env python3
"""publish_check.py: what it treats as a secret, and what it finds in a diff.

Plain script, like the other packaging suites: no arguments, run from the
root. Everything here is invented; nothing reads the live config.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_check as P  # noqa: E402

failed = []
# Built at runtime: written out whole, a fake key is exactly what the gate
# refuses to publish -- including in this file.
FAKE_KEY = "sk" + "-abcdefghijklmnopqrstuvwxyz0123"


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  <- {detail}"))
    if not ok:
        failed.append(name)


print("env values")
with tempfile.TemporaryDirectory() as d:
    env = Path(d) / "x.env"
    env.write_text("\n".join([
        "# a comment=with an equals",
        f"OPENCODE_API_KEY={FAKE_KEY}",
        "ADMIN_SECRET_KEY='q9Zr7Lm2Xv8Tn4Wp1Ks6'",
        "HOMEASSISTANT_URL=http://homeassistant.home:8123",
        "SITE_HOST=portal.home",
        "PORT=21002",
        "ENABLED=true",
        "SHORT=abc",
        "EMPTY=",
    ]))
    vals = P.env_values([env, Path(d) / "missing.env"])
check("a key and a quoted secret are values to hunt for",
      FAKE_KEY in vals and "q9Zr7Lm2Xv8Tn4Wp1Ks6" in vals, vals)
check("  labelled by their key, not their value",
      vals.get("q9Zr7Lm2Xv8Tn4Wp1Ks6") == "credential ADMIN_SECRET_KEY", vals)
check("  URLs, hostnames, numbers, flags and short values are not",
      set(vals) == {FAKE_KEY, "q9Zr7Lm2Xv8Tn4Wp1Ks6"}, vals)

print("\nuser store")
with tempfile.TemporaryDirectory() as d:
    (Path(d) / "home-core").mkdir()
    (Path(d) / "home-core" / "users.json").write_text(json.dumps(
        {"a": {"username": "900000111", "email": "someone@example.org", "phone": "+56 9 0000 0009"}}))
    users = P.user_values(d)
    check("login ids, emails and phones are hunted for",
          {"900000111", "someone@example.org", "+56 9 0000 0009"} <= set(users), users)
    check("  a missing store is nothing, not a crash", P.user_values(str(Path(d) / "none")) == {})

print("\ndiffs")
diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,2 +1,3 @@
 kept = 1
-removed = "an old line"
+added = "900000111"
+++b = 2
"""
lines = P.added_lines(diff)
check("only added lines, each with its file",
      lines == [("x.py", 'added = "900000111"'), ("x.py", "++b = 2")], lines)

print("\nkey shapes")


def shape(text):
    return [label for pattern, label in P.KEY_SHAPES if re.search(pattern, text)]


check("an API key is caught", shape(f'KEY = "{FAKE_KEY}"'))
check("a GitHub token is caught", shape("ghp_" + "a" * 36))
check("a private key with a body is caught",
      shape("-----BEGIN OPENSSH PRIVATE KEY-----\n" + "A" * 64))
check("  a fixture with only the header is not",
      not shape('"-----BEGIN OPENSSH PRIVATE KEY-----\\nsecreto\\n"'))
check("  ordinary words are not", not shape("the skill-creator task-runner"))

print("\nextensions and house commits")
PLUGINS = [
    {"name": "tomi-ledger", "root": "/srv/plugins/Ledger_Tomi",
     "doc": {"services": {"tomi-ledger": {}}, "tiles": [{"href": "/ledger/"}]}},
    {"name": "mora-backups", "root": "/srv/mora/backups",
     "doc": {"services": {"pili-reports": {}}, "tiles": [{"href": "http://{derived.hub_address}:21601/"}]}},
]
terms = P.extension_terms(PLUGINS)


def named(text):
    return P.extension_problems(terms, [("f.py", text)], {})


check("a plugin is named by its name, in either spelling, and by its directory",
      all(named(t) for t in ("# the tomi-ledger plugin", "tomi_ledger = 1", "Tomi Ledger", "see Ledger_Tomi/")))
check("  and by its service, and by the path it mounts at",
      named("route pili-reports") and named("href='/ledger/'") and named("/camaras* /ledger*"))
check("  not by a longer word, a persona under /chat, or a tile that points at an address",
      not any(named(t) for t in ("tomi-ledgers-all", "href='/chat/ledger'", "the ledgerless year",
                                 "http://{derived.hub_address}:21601/")))
check("  and the report says where, never what",
      "ledger" not in " ".join(named("tomi-ledger")).lower().replace("household extension", ""), named("tomi-ledger"))
msgs = P.extension_problems(terms, [], {"a" * 40: "house: lights", "b" * 40: "Studio: fix\n\nfor tomi-ledger",
                                        "c" * 40: "Studio: a long podcast is filed whole"})
check("a `house:` commit and a message that names an extension are refused; an ordinary one is not",
      len(msgs) == 2 and "aaaaaaaa" in msgs[0] and "bbbbbbbb" in msgs[1], msgs)
check("no extensions configured: only `house:` is left to see",
      P.extension_terms([]) == [] and not P.extension_problems([], [("f", "anything")], {"d" * 40: "x"}))

print()
if failed:
    print(f"{len(failed)} FAILED: {', '.join(failed)}")
    sys.exit(1)
print("all checks passed")
