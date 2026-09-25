"""A launch asks for a clean sheet; the page grants it once the conversation is over.

Run: python local/test_chat_new_on_launch.py

The Alfred app opens on `/chat?new=1`, and this page treats that as "start a
clean conversation" — but not unconditionally: a conversation still current
(`launchStaysWarm`, three hours of silence, the same rule that splits
conversations everywhere else) is kept instead, and the clean sheet is taken
only once that silence has ended. It was thirty minutes until 2026-08-18. A
notification tap carries the ntfy click URL instead, which never has `new`, and
therefore lands in the conversation the notification was about.

Note what this file does NOT cover: it stubs `launchContinued` directly, so the
window itself is never exercised here. That half lives in `test_catchup.js`.

**The two halves live in different repositories**, which is the whole reason
this file exists — the same gap `test_app_tiles_reachable.py` was written for.
The page could honour `?new=1` perfectly while the app stopped asking for it, or
the app could keep asking while a refactor here quietly dropped the branch, and
in both cases the symptom is the same shrug: "sometimes it opens on the old
chat". Nobody files that.

Three things are worth pinning and all three have a way of rotting:

- `?new=1` starts a fresh conversation — unless the one on screen is still warm
  (`launchContinued`, decided in `initHistory`). The app asks on every launch
  and cannot tell a new thought from the next sentence about the last one.
- A notification deep link never does, whatever else it carries.
- The parameter is consumed. A reload — rotation, a WebView process restart —
  must not start yet another conversation and throw away what was typed into
  the last one.

The page's init block is *executed*, not pattern-matched: it is pulled out of
chat.html and run in node against stubs. A test that only grepped for the string
would pass on code that no longer runs.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHAT_HTML = HERE / "templates" / "chat.html"

# The app lives in the home-chat repo, a sibling inside the home-stack
# superproject — same arrangement test_app_tiles_reachable.py relies on.
MAIN_ACTIVITY = (
    HERE.parent.parent
    / "proxy/android/app/src/main/java/com/chat/app/MainActivity.kt"
)

MARKER = "initHistory().then(function() {"
# The block calls this, and its only catch swallows everything — so a missing
# name here does not fail the test, it makes every check pass on a block that
# did nothing. Lifted from the page for the same reason the block is.
HELPER = "function forgetUrlIntent()"

failures: list[str] = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {name}")
        return
    failures.append(name)
    print(f"  FAIL  {name}\n        got  {got!r}\n        want {want!r}")


def _braced(html: str, marker: str, *, after: bool = False) -> str:
    """The `{ … }` that follows *marker*, brace-matched."""
    at = html.find(marker)
    if at < 0:
        sys.exit(
            f"Could not find `{marker}` in chat.html.\n"
            "It was renamed or restructured — this test cannot see what it is "
            "checking any more, which is worse than a failure. Re-point it."
        )
    i = html.index("{", at)
    depth = 0
    while i < len(html):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return html[at:i + 1]


def url_helper() -> str:
    """`forgetUrlIntent`, verbatim, because the block under test calls it."""
    return _braced(CHAT_HTML.read_text(encoding="utf-8"), HELPER)


def init_callback_body() -> str:
    """The real init callback out of the real template."""
    html = CHAT_HTML.read_text(encoding="utf-8")
    at = html.find(MARKER)
    if at < 0:
        sys.exit(
            f"Could not find `{MARKER}` in chat.html.\n"
            "It was renamed or restructured — this test cannot see what it is "
            "checking any more, which is worse than a failure. Re-point it."
        )
    i = at + len(MARKER) - 1
    depth = 0
    while i < len(html):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return html[at + len(MARKER): i]


HARNESS = """
const helper = %(helper)s;
const body = %(body)s;
function run(search, launchContinued) {
  const calls = { openDay: [], startNewChat: 0, replaced: null };
  const ctx = {
    TODAY: '2026-08-13',
    window: { location: { search, pathname: '/chat' } },
    history: { replaceState: (_a, _b, url) => { calls.replaced = url; } },
    openDay: (d) => calls.openDay.push(d),
    startNewChat: () => { calls.startNewChat++; },
    // initHistory has already run by the time this block does, and it says
    // here whether it kept a conversation that was still being talked in.
    launchContinued,
    URLSearchParams,
  };
  new Function(...Object.keys(ctx), helper + body)(...Object.values(ctx));
  return calls;
}
const out = {};
for (const [name, search, warm] of %(cases)s) out[name] = run(search, !!warm);
console.log(JSON.stringify(out));
"""

CASES = [
    ("launch", "?new=1&embed=1"),
    ("launch_bare", "?new=1"),
    ("launch_warm", "?new=1&embed=1", True),
    ("notification_day", "?date=2026-08-11&embed=1"),
    ("notification_prefill", "?prefill=algo&embed=1"),
    ("notification_bare", "?embed=1"),
    ("both", "?new=1&date=2026-08-11"),
    ("today", "?date=2026-08-13&new=1"),
    ("not_new", "?new=0"),
]


def main() -> int:
    if not CHAT_HTML.is_file():
        sys.exit(f"no chat.html at {CHAT_HTML}")

    node = shutil.which("node")
    if not node:
        # Loudly, not quietly. A check that silently finds nothing is how the
        # tile gap survived four times.
        print("SKIP: node is not installed, so the page half cannot be run.")
        print("      Install node, or run this where it is available.")
        # 0, like every other skip in this directory. The exit code is the
        # runner's contract -- the deploy gate runs these against the built
        # image and reads it as pass or fail, and "node is not here" is neither
        # a passing check nor a broken one. Saying so out loud is the print
        # above; saying it with a 2 just makes the deploy fail for a reason
        # that has nothing to do with the change being deployed.
        return 0

    script = HARNESS % {
        "helper": json.dumps(url_helper()),
        "body": json.dumps(init_callback_body()),
        "cases": json.dumps(CASES),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    proc = subprocess.run([node, path], capture_output=True, text=True)
    Path(path).unlink(missing_ok=True)
    if proc.returncode != 0:
        sys.exit(f"the init block threw:\n{proc.stderr[:2000]}")
    r = json.loads(proc.stdout)

    print("\nthe page, launched by the app")
    check("?new=1 starts a fresh conversation", r["launch"]["startNewChat"], 1)
    check("and does not jump to another day", r["launch"]["openDay"], [])
    check("the parameter is consumed, so a reload does not do it again",
          r["launch"]["replaced"], "/chat?embed=1")
    check("only that parameter — embed survives",
          "embed=1" in (r["launch"]["replaced"] or ""), True)
    check("with nothing else, the query goes entirely",
          r["launch_bare"]["replaced"], "/chat")

    print("\nthe page, launched onto a conversation still warm")
    # The app cannot tell "a new thought" from "the next sentence about what I
    # just asked" — it has been in the background either way. The page can, and
    # says so through launchContinued. Picking the phone back up 14 minutes
    # after "pon la luz de Noa anaranjada al 10%" to say "apágala" is the same
    # conversation, and starting a fresh one there is amnesia with a timestamp.
    check("a warm conversation is not thrown away",
          r["launch_warm"]["startNewChat"], 0)
    check("but the parameter is still consumed, so a reload is not a new chat",
          r["launch_warm"]["replaced"], "/chat?embed=1")

    print("\nthe page, opened from a notification")
    check("a dated link opens that day", r["notification_day"]["openDay"], ["2026-08-11"])
    check("...and starts nothing", r["notification_day"]["startNewChat"], 0)
    # Reported from the house on 2026-08-18: open Alfred on a new chat, say
    # hello, open «Consumo de Alfred», press back — and there is August 15th
    # again. The app was sitting on that day's URL from a notification tap; the
    # new chat happened in-page and never changed it, so the restore re-ran the
    # deep link. A day link is an instruction for an arrival, and it has to be
    # spent like `new=1` has always been.
    check("...and the day is consumed, so coming back does not open it again",
          r["notification_day"]["replaced"], "/chat?embed=1")
    check("a prefill lands in the conversation in progress",
          r["notification_prefill"]["startNewChat"], 0)
    check("and is consumed too, or it refills itself on every return",
          r["notification_prefill"]["replaced"], "/chat?embed=1")
    check("so does a bare one", r["notification_bare"]["startNewChat"], 0)

    print("\nedge cases")
    check("an explicit day beats new", r["both"]["openDay"], ["2026-08-11"])
    check("...and nothing is reset under it", r["both"]["startNewChat"], 0)
    check("today is not a day-jump, so new still applies", r["today"]["startNewChat"], 1)
    check("new=0 is not new", r["not_new"]["startNewChat"], 0)

    print("\nthe app, which has to ask for it")
    # The app ships in services/proxy/android/ here, so a missing file is a
    # stale path rather than a sibling repo nobody cloned. Asserted, because a
    # skip reported exactly that as a pass.
    check("the app source is where this expects it", MAIN_ACTIVITY.is_file(), True)
    if MAIN_ACTIVITY.is_file():
        kt = MAIN_ACTIVITY.read_text(encoding="utf-8")
        home = re.search(r'val\s+HOME_URL\s*=\s*BASE_URL\s*\+\s*"([^"]+)"', kt)
        check("MainActivity has a HOME_URL", home is not None, True)
        if home:
            check("a plain launch asks for a new chat", "new=1" in home.group(1), True)
            check("and it is the chat, not the dashboard",
                  home.group(1).startswith("chat"), True)
        # The fallback a notification with no usable URL lands on. If this ever
        # gains `new`, every notification tap starts a blank conversation —
        # which is the bug this whole arrangement replaced.
        fb = re.search(r'val\s+fallback\s*=\s*BASE_URL\s*\+\s*"([^"]+)"', kt)
        check("the deep-link fallback exists", fb is not None, True)
        if fb:
            check("and never starts a new chat", "new" in fb.group(1), False)

    if failures:
        print(f"\n{len(failures)} failed: {', '.join(failures)}\n")
        return 1
    print("\nall good\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
