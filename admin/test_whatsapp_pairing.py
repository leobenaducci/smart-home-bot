"""Pairing a WhatsApp phone from the admin page.

Run: python admin/test_whatsapp_pairing.py

The bridge draws its QR on stdout and nothing else, so pairing was a `docker
logs -f` job on the host. WhatsApp expires linked devices on its own schedule,
which makes this the one step a household has to repeat -- and the only symptom
of a dropped link is the assistant going quiet on WhatsApp.

Two things here are worth more than the rest:

`wa_linked` keys on creds.json and nothing else. A bridge that has merely *run*
writes `bridge-token`, so a directory holding just that one file is an unpaired
bridge. Reading it as paired is exactly the state that had this house's Alfred
silent while every part looked present and every container was up.

`wa_qr` takes the LAST code. The bridge prints a fresh one about every minute
and the previous ones are dead the moment the next appears, so a page showing an
older block offers something that scans and then fails -- which is worse than
showing nothing, because it looks like the phone is at fault.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Before importing app: it reads this at import time, and the suites that came
# before this one learned the hard way what happens when a test points it at the
# live tree. Nothing here writes, but nothing here needs the real tree either.
_TMP = tempfile.mkdtemp(prefix="wa-pairing-")
os.environ["HOME_STACK_STATE_DIR"] = _TMP

import app as A  # noqa: E402

if not A.wa_state_dir("user1").startswith(_TMP):
    print(f"REFUSING TO RUN: state dir is {A.wa_state_dir('user1')}, outside "
          f"the scratch tree at {_TMP}.")
    sys.exit(2)

FAILED = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))
    if not ok:
        FAILED.append(label)


def auth_dir(member, files):
    d = A.wa_state_dir(member)
    os.makedirs(d, exist_ok=True)
    for name in files:
        with open(os.path.join(d, name), "w") as fh:
            fh.write("{}")
    return d


print("a bridge that has only ever started is not a linked phone")
auth_dir("m-token", ["bridge-token"])
check("bridge-token alone is not linked", A.wa_linked("m-token") is False,
      "an unpaired bridge read as paired")

auth_dir("m-linked", ["bridge-token", "creds.json"])
check("creds.json is", A.wa_linked("m-linked") is True, "paired bridge read as unpaired")

check("and nothing at all is not", A.wa_linked("m-none") is False, "no directory matched")

print("\nthe QR shown is the current one")
# Two codes in the log, as there always are after a minute: the first is dead.
LOG = (
    "starting\n"
    "\n📱 Scan this QR code with WhatsApp (Linked Devices):\n\n"
    "\u2580\u2580\u2580\u2580\n"      # the expired block
    "\u2584\u2584\u2584\u2584\n"
    "\nConnection closed. Status: 408, Will reconnect: true\n"
    "Reconnecting in 60s (attempt 2)...\n"
    "\n📱 Scan this QR code with WhatsApp (Linked Devices):\n\n"
    "\u2588\u2588\u2588\u2588\n"      # the live one
    "\u258c\u258c\u258c\u258c\n"
)


class _Done:
    returncode = 0

    def __init__(self, out):
        self.stdout = out
        self.stderr = ""


_real_run = A.subprocess.run
A.subprocess.run = lambda *a, **k: _Done(LOG)
try:
    qr = A.wa_qr("anyone")
finally:
    A.subprocess.run = _real_run

# Real rows are block glyphs and nothing else -- a fixture with letters in it
# tests the guard rather than the choice, which is how the first version of this
# passed while proving nothing.
check("the newest code is the one taken",
      qr.splitlines() == ["\u2588\u2588\u2588\u2588", "\u258c\u258c\u258c\u258c"],
      qr.replace("\n", "|"))
check("  and the expired one is not offered",
      "\u2580\u2580\u2580\u2580" not in qr, qr.replace("\n", "|"))
check("  the block is whole", len(qr.splitlines()) == 2, qr.replace("\n", "|"))

print("\nand nothing but the code comes with it")
NOISY = (
    "\n📱 Scan this QR code with WhatsApp (Linked Devices):\n\n"
    "█▀▀█\n"
    "█▄▄█\n"
    "Connection closed. Status: 408, Will reconnect: true\n"
    "█▀▀█\n"          # a later, unrelated block-looking line
)
A.subprocess.run = lambda *a, **k: _Done(NOISY)
try:
    qr2 = A.wa_qr("anyone")
finally:
    A.subprocess.run = _real_run
check("a log line after the code ends it",
      len(qr2.splitlines()) == 2, qr2.replace("\n", "|"))
check("  so no prose is rendered as part of the QR",
      "Connection" not in qr2, qr2.replace("\n", "|"))

print("\na log with no code at all offers none")
A.subprocess.run = lambda *a, **k: _Done("starting\nlistening on ws://0.0.0.0:3002\n")
try:
    check("nothing is invented", A.wa_qr("anyone") == "", "returned something")
finally:
    A.subprocess.run = _real_run

print("\nand the container is named after the member, not the login")
# The three ids in this house are not interchangeable, and the bridge is one of
# the things the *deployer* builds -- so it is keyed on the member id, like the
# state directory beside it.
check("container follows the member id",
      A.wa_container("user4") == "whatsapp-bridge-user4", A.wa_container("user4"))
check("state dir follows the member id",
      A.wa_state_dir("user4").endswith(os.path.join("nanobot", "user4", "whatsapp-auth")),
      A.wa_state_dir("user4"))

# --- one pairing control, and it is wired ------------------------------------
#
# There were two buttons for one errand: "show the code" and "show a new code".
# The first could only ever draw a code that was already seconds from expiring
# -- which is what "couldn't link the device" turned out to be -- and the second
# asks the bridge for a fresh one. Only the second remains.
#
# The box it draws into has to sit outside the linked/unlinked branch. It did
# not, and the handler opens with `if (!box) return`, so on a bridge the page
# believed was linked the only control offered was bound to nothing: pressing
# it did nothing at all, which is exactly when a new code is wanted.
print("\nthe member page offers exactly one way to get a code")
_tpl = open(os.path.join(HERE, "templates", "member_profile.html"),
            encoding="utf-8").read()
check("  the redundant 'show the code' button is gone", 'id="wa-show"' not in _tpl)
check("  the refresh button is still there", 'id="wa-new"' in _tpl)
check("  nothing still reads the removed element",
      "getElementById('wa-show')" not in _tpl)
_area = _tpl.index('id="wa-area"')
check("  the code box is outside the linked/unlinked branch",
      "{% endif %}" in _tpl[:_area]
      and _tpl.rindex("{% endif %}", 0, _area) < _area,
      "inside it, the refresh button has nothing to draw into on a page that "
      "believes the bridge is paired")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed")
