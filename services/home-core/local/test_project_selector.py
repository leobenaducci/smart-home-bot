"""The Programmer's project selector: reachable, and right before it is asked.

Two failures, one visible and one not, and they compounded.

The selector is populated by a fetch. Until it resolved, the only option was
the placeholder, so `activeProject()` answered null and a message typed in that
window went with **no project at all** -- and the Programmer replied "necesito
saber a qué proyecto te refieres" about the project the person could see
selected on screen, because by the time they read the reply the list had
arrived. Nothing logged, nothing failed; the selector simply looked decorative.

The other half is why they were typing blind: on a phone the header did not fit
and did not wrap, so it was cut off at the right edge -- the selector half
visible, "Projects" sliced down the middle, "Credentials" gone.

Both are structural, so this reads the template source. Run:
python local/test_project_selector.py
"""
import re
import sys
from pathlib import Path

CHAT = Path(__file__).resolve().parent / "templates" / "chat.html"
src = CHAT.read_text(encoding="utf-8")

failures = []


def check(name, ok, why=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else "  -- " + why))
    if not ok:
        failures.append(name)


# --- the choice is known before the list arrives ----------------------------

setup = re.search(r'if \(projectSel\) \{(.*?)\n  \}', src, re.S)
check("the selector has a setup block", bool(setup),
      "if (projectSel) { ... } is gone or reshaped; the checks below are blind")

if setup:
    body = setup.group(1)
    read_at = body.find("localStorage.getItem('programador-project')")
    load_at = body.find("loadProjects()")
    check("the remembered project is read in the setup block", read_at >= 0,
          "nothing restores it before the fetch, so the first message after a "
          "page load is sent with no project")
    check("and read before loadProjects() is called",
          read_at >= 0 and load_at >= 0 and read_at < load_at,
          "restoring it inside or after the fetch is the race this fixes")

# The list handler still has to survive a slug whose project is gone, and still
# has to pick the only project there is.
check("a remembered slug that matched nothing is forgotten",
      "removeItem('programador-project')" in src and re.search(
          r'saved && !matched', src) is not None,
      "a deleted project stays selected and is sent on every turn")
check("the sole project is chosen when nothing is remembered",
      re.search(r'!saved && list\.length === 1', src) is not None,
      "a person with one project still has to pick it before being understood")


# --- and the controls are actually on screen --------------------------------

check("the Programmer controls are wrapped", 'id="project-bar"' in src,
      "without a wrapper the phone cannot move them off the title's row")

bar = re.search(r'<div id="project-bar">(.*?)</div>', src, re.S)
check("the wrapper holds all three controls",
      bool(bar) and all(x in bar.group(1) for x in
                        ('id="project-sel"', 'id="project-btn"',
                         'id="credential-btn"')),
      "one left outside the wrapper is the one that gets clipped")

check("the wrapper is transparent on wide screens",
      re.search(r'#project-bar \{ display: contents; \}', src) is not None,
      "without display:contents the wrapper changes the desktop layout too")

phone = re.search(r'@media \(max-width:580px\) \{(.*?)\n    \}', src, re.S)
check("there is a phone media query", bool(phone))
if phone:
    rules = phone.group(1)
    check("the header may wrap on a phone", "flex-wrap: wrap" in rules,
          "no wrap means the overflow is clipped rather than moved")
    check("the title can give way", re.search(r'header h1 \{[^}]*min-width: 0', rules) is not None,
          "a flex item's min-width is its content: the h1 refuses to shrink and "
          "pushes the controls off the right edge")
    check("the controls get a row of their own",
          re.search(r'#project-bar \{[^}]*flex-basis: 100%', rules) is not None,
          "sharing the title's row is what did not fit in the first place")

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("all checks passed")
