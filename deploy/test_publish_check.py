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

print()
if failed:
    print(f"{len(failed)} FAILED: {', '.join(failed)}")
    sys.exit(1)
print("all checks passed")
