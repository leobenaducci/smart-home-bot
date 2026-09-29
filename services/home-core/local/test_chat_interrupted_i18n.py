"""The "interrupted" marker in the chat must be translatable.

Run: python local/test_chat_interrupted_i18n.py

The marker was hard-coded as the Spanish word "interrumpido", so users whose
interface was set to another locale saw Spanish text mixed into the chat.
"""
import json
import os
import re
import sys

SRC = os.path.dirname(os.path.abspath(__file__))

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


CHAT_HTML = os.path.join(SRC, "templates", "chat.html")

# Find the repository root from wherever this test was copied/executed.
ROOT = SRC
while ROOT != "/" and not os.path.isdir(os.path.join(ROOT, "i18n")):
    ROOT = os.path.dirname(ROOT)
I18N = os.path.join(ROOT, "i18n")

page = open(CHAT_HTML, encoding="utf-8").read()

# The source must ask for the translated string, not spell it out.
check("chat.html uses t('chat.interrupted')",
      "t('chat.interrupted')" in page,
      "translation key not found")
check("chat.html no longer hard-codes 'interrumpido'",
      "'interrumpido'" not in page and '"interrumpido"' not in page,
      "literal Spanish string still present")

# en.json is the reference catalogue; es.json is the only complete translation
# the stack promises. The rest fall back to English by design (CLAUDE.md, i18n).
for locale in ("en", "es"):
    data = json.loads(open(os.path.join(I18N, f"{locale}.json"), encoding="utf-8").read())
    check(f"{locale}.json defines chat.interrupted",
          data.get("chat.interrupted"),
          data.get("chat.interrupted"))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
