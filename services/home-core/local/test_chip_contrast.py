"""Every chip in the household pages has to be readable on its own ground.

`_theme_css` holds a *theme's* colours to 4.5:1, and does it well. It never sees
the pages' own stylesheets, so a pale tint with the light version of the same
hue written on it passes every check in the house and still cannot be read:
`.chip.review` was honey on a honey tint at 2.47:1, and the chores review list
-- which is where a person reads someone's excuse and decides -- was a stack of
pale slabs with pale writing.

The trap is that the failure is invisible to everything else. The page renders,
the JavaScript parses, the theme validator is happy because no theme is
involved, and the only thing wrong is that nobody can read it.

So this resolves each chip rule the way a browser would -- `var()` against the
sheet's own `:root`, `rgba()` blended over the ground it is painted on -- and
asserts the pair. Run: python local/test_chip_contrast.py
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"

# The grounds a chip can sit on. A chip on an excused card is the worst case and
# the one that was actually failing, so it is checked, not assumed.
GROUNDS = ("--paper", "--plaster")
WANT = 4.5

failures = []
checked = 0
skipped = []


def _luminance(rgb):
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return .2126 * r + .7152 * g + .0722 * b


def _contrast(fg, bg):
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + .05) / (lo + .05)


def _root_vars(css):
    """The custom properties the sheet declares for itself.

    Only the `:root` blocks: a token redefined inside a media query or a
    `body[data-space=…]` is a different question and not this one.
    """
    out = {}
    for block in re.findall(r':root\s*\{(.*?)\}', css, re.S):
        for name, value in re.findall(r'(--[\w-]+)\s*:\s*([^;]+);', block):
            out.setdefault(name, value.strip())
    return out


def _resolve(value, root, seen=()):
    """A colour, as a browser would end up with it, or None if we can't tell.

    `None` is reported rather than swallowed -- a rule this cannot read is a
    rule this cannot vouch for, and saying so beats a green run that checked
    nothing.
    """
    value = value.strip()
    m = re.fullmatch(r'var\(\s*(--[\w-]+)\s*(?:,\s*(.+))?\)', value)
    if m:
        name, fallback = m.group(1), m.group(2)
        if name in seen:
            return None
        if name in root:
            return _resolve(root[name], root, seen + (name,))
        return _resolve(fallback, root, seen + (name,)) if fallback else None
    m = re.fullmatch(r'#([0-9a-fA-F]{6})', value)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)), 1.0
    m = re.fullmatch(r'rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*'
                     r'(?:,\s*([\d.]+)\s*)?\)', value)
    if m:
        r, g, b = (int(float(m.group(i))) for i in (1, 2, 3))
        return (r, g, b), float(m.group(4)) if m.group(4) else 1.0
    return None


def _over(fg, alpha, bg):
    return tuple(round(alpha * f + (1 - alpha) * b) for f, b in zip(fg, bg))


def check(page):
    global checked
    css = (TEMPLATES / page).read_text()
    root = _root_vars(css)
    grounds = {}
    for name in GROUNDS:
        got = _resolve(root.get(name, ''), root)
        if got:
            grounds[name] = got[0]
    if not grounds:
        failures.append(f"{page}: no ground colour to measure against")
        return

    for selector, body in re.findall(r'([^{}]*chip[^{}]*)\{([^{}]*)\}', css):
        selector = ' '.join(selector.split())
        bg_m = re.search(r'(?<![\w-])background(?:-color)?\s*:\s*([^;]+)', body)
        fg_m = re.search(r'(?<![\w-])color\s*:\s*([^;]+)', body)
        if not bg_m or not fg_m:
            continue
        bg = _resolve(bg_m.group(1), root)
        fg = _resolve(fg_m.group(1), root)
        if not bg or not fg:
            skipped.append(f"{page}  {selector}")
            continue
        (bg_rgb, bg_a), (fg_rgb, _) = bg, fg
        for gname, ground in grounds.items():
            painted = _over(bg_rgb, bg_a, ground) if bg_a < 1 else bg_rgb
            ratio = _contrast(fg_rgb, painted)
            checked += 1
            on = f" on {gname}" if bg_a < 1 else ""
            if ratio < WANT:
                failures.append(
                    f"{page}  {selector}{on}: {ratio:.2f}:1, needs {WANT}")
            else:
                print(f"  PASS  {page:11s} {selector}{on}  {ratio:.2f}:1")
            if bg_a >= 1:
                break   # an opaque chip looks the same on every ground


for page in ("tasks.html", "chat.html"):
    check(page)

if skipped:
    print("\nUnresolvable, so unchecked:")
    for s in skipped:
        print("  ??  " + s)

print()
if failures:
    for f in failures:
        print("FAIL  " + f)
    print(f"\n{len(failures)} unreadable chip(s)")
    sys.exit(1)
if not checked:
    print("FAIL  no chip rules found — this test stopped testing anything")
    sys.exit(1)
print(f"all checks passed ({checked} chip/ground pairs)")
