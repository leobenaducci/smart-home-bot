#!/usr/bin/env python3
"""The admin page's palette, in both themes.

Run: python admin/test_theme.py

The dark theme was once three overridden values on top of a light palette, and
nothing said otherwise: it parsed, it rendered, and every page looked fine to
whoever was working in light mode. Measured, the door sat 1.21:1 from the
plate, the cards 1.10:1 from the page and the borders 1.36:1 from the cards --
the design's whole structure was gone, and error text at 2.48:1 read as
decoration.

So this reads the stylesheet the page actually ships and checks both themes.

Two different measures, because they answer two different questions:

  * **Text** is held to WCAG contrast, >= 4.5:1. That is what the ratio is for.
  * **Adjacent surfaces** are held to a perceptual lightness step, >= 4 L*.
    Contrast ratio compresses badly at low luminance -- two obviously different
    dark greys measure about 1.2 -- so using it here is what produced the mush
    in the first place.
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent / "templates" / "base.html"

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
           for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def lightness(hex_colour: str) -> float:
    """CIE L*, which is what "is this a visibly different grey" actually means."""
    y = _luminance(hex_colour)
    return 116 * (y ** (1 / 3)) - 16 if y > 0.008856 else 903.3 * y


css = BASE.read_text(encoding="utf-8")
root = css[css.index(":root {"):css.index("*, *::before")]
split = root.index("@media")
light = dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", root[:split]))
dark = dict(light)
dark.update(dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", root[split:])))

check("the light palette is complete", len(light) >= 14, sorted(light))
check("the dark theme overrides the surfaces, not just the ink",
      {"--panel", "--plate", "--plate-2", "--edge"} <= set(
          re.findall(r"(--[a-z0-9-]+):\s*#", root[split:])))

# Anything that is read as words.
TEXT = [
    ("ink on card", "--ink", "--plate-2"),
    ("ink on page", "--ink", "--plate"),
    ("ink-soft on card", "--ink-soft", "--plate-2"),
    ("live-ink on card", "--live-ink", "--plate-2"),
    ("tag-ink on card (an error message)", "--tag-ink", "--plate-2"),
    ("attn-ink on card", "--attn-ink", "--plate-2"),
    ("door-ink on the door", "--door-ink", "--panel"),
    ("door-dim on the door", "--door-dim", "--panel"),
    # Pills and badges on a tint. These were literals in models.html, light in
    # both themes, so dark mode put light ink on pale pink.
    ("tag-ink on its wash", "--tag-ink", "--tag-wash"),
    ("attn-ink on its wash", "--attn-ink", "--attn-wash"),
    ("type on the attn fill", "--on-attn", "--attn"),
]

# Anything whose job is to look like a different piece of hardware.
SURFACES = [
    ("the door reads apart from the page", "--panel", "--plate"),
    ("a card reads apart from the page", "--plate-2", "--plate"),
    ("a border reads against its card", "--edge", "--plate-2"),
    ("the login plate reads against the door", "--plate-2", "--panel"),
]

for theme_name, palette in (("light", light), ("dark", dark)):
    print(f"\n  -- {theme_name} --")
    for label, fg, bg in TEXT:
        ratio = contrast(palette[fg], palette[bg])
        check(f"{theme_name}: {label}", ratio >= 4.5, f"{ratio:.2f}:1")
    for label, a, b in SURFACES:
        step = abs(lightness(palette[a]) - lightness(palette[b]))
        check(f"{theme_name}: {label}", step >= 4.0, f"{step:.1f} L*")

# A colour defined only inside the dark block has no light value at all, and a
# foreground taken from a surface token inverts with the theme -- which is how
# the sign-out button came to be dark text on a dark door.
body = css[css.index("*, *::before"):]
on_door = re.findall(r"\.(?:door|signout|gate-langs)[^{]*\{[^}]*color:\s*var\((--plate[^)]*)\)", body)
check("nothing paints door type with a plate colour", not on_door, on_door)

# The page templates. base.html carries the palette; everything else must ask
# for it by name. Nine hardcoded values had accumulated in three of them --
# all a light-mode ink, so all of them stayed light-mode ink on a dark card --
# plus `var(--wall, #E4DACA)`, which named a token this stylesheet has never
# defined and so was always its own fallback.
#
# The deploy log is the one exception, and a real one: `.joblog` paints itself
# on `var(--panel)`, which is dark in both themes, so a terminal palette of
# light-on-dark is correct there rather than theme-dependent. It is held to the
# same 4.5:1, against that ground.
LOG_GROUND = "--panel"
for template in sorted((BASE.parent).glob("*.html")):
    if template.name == "base.html":
        continue
    body = template.read_text(encoding="utf-8")
    hexes = re.findall(r"(#[0-9A-Fa-f]{6})", body)
    log_palette = set(re.findall(r"\.joblog\s+\.\w+\s*\{[^}]*color:\s*(#[0-9A-Fa-f]{6})", body))
    stray = [h for h in hexes if h not in log_palette]
    check(f"{template.name} takes its colours from the palette",
          not stray, stray)
    for colour in log_palette:
        for theme_name, palette in (("light", light), ("dark", dark)):
            ratio = contrast(colour, palette[LOG_GROUND])
            check(f"{template.name}: log colour {colour} on the {theme_name} log ground",
                  ratio >= 4.5, f"{ratio:.2f}:1")

# Comments stripped, for the reason given at the click check below: base.html
# explains the `--line` bug in prose, and reading that prose as a use of
# `--line` failed this check on the comment that records the fix.
undefined = sorted(set(re.findall(r"var\((--[a-z0-9-]+)\)",
                                  re.sub(r"/\*.*?\*/", "", css, flags=re.S)))
                   - set(re.findall(r"^\s*(--[a-z0-9-]+):", root, re.M)))
check("every token used is defined", not undefined, undefined)

# --- a control you can actually hit ---------------------------------------
# The service toggles are a hidden checkbox under a styled span. The span is
# positioned, carries no z-index and comes after the input in source order, so
# it paints above it -- and without `pointer-events: none` it eats every click
# aimed at the switch. Nothing looked wrong: the breaker rendered correctly, it
# just never moved, and the only thing that toggled a service was clicking its
# name, which happens to be a <label for>.
#
# Checked here because there is no other check that would: it is not a colour,
# it renders identically either way, and a browser is the only thing that can
# tell the difference by looking.

# Comments stripped first. The declaration is explained by a comment that says
# `pointer-events: none` in prose, so matching the raw block passed with the
# declaration deleted -- the check was reading the reason rather than the rule.
declarations = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

for selector in (".breaker .body",):
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\}", declarations, re.S)
    check(f"{selector} exists", block is not None)
    if block:
        check(f"{selector} does not swallow the click",
              re.search(r"pointer-events\s*:\s*none", block.group(1)) is not None,
              "it paints above the input it covers, so the switch does nothing")

# And the input under it has to stay reachable: full-size and not sent behind
# anything. A z-index on the body would be the other way to break this.
inp = re.search(r"\.breaker input\s*\{(.*?)\}", declarations, re.S)
check(".breaker input covers the whole control",
      inp and "width: 100%" in inp.group(1) and "height: 100%" in inp.group(1),
      "a checkbox smaller than the switch is a switch with a dead margin")

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")
