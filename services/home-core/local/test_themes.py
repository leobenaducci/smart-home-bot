"""Per-user themes: one member's version of The House.

Run: python local/test_themes.py   (needs Flask; skips loudly without it)

A theme changes colours, the shape of things, and the ground behind them —
never the type, the spacing or the layout. That is what makes it safe to let
Alfred generate one: the worst a bad theme can do is look wrong, never move a
button or hide a control.

What these pin down, in the order they can hurt:

- **An unreadable theme is refused**, with a message saying which pair failed
  and by how much, because Alfred reads that message and tries again.
- **Only the house roles can be set**, and shape is one of a named few. An open
  token list would let a theme redefine `--display` and take the pages apart;
  free radii would let it clip a button's label with its own corners.
- **The derived shades are computed, not asked for**, so `--ink-deep` is always
  darker than `--ink` however careless the theme is.
- **Nobody can set or read anybody else's.** The skill posts with a per-user
  derived token, and that token is the only thing deciding whose row it is.
- **`/theme.css` never errors.** It is a `<link>` in every page's head.
"""
import json
import re
import sqlite3
import os
import shutil
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-themes-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)

USER = "user1"
OTHER = "user2"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}, {"username": "%s", "nanobot_id": 3}]'
            % (USER, OTHER))

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_theme_db()

HOUSE = {
    'plaster': '#EFE9DC', 'paper': '#FBF8F1', 'paper-2': '#F6F1E5',
    'ink': '#2B2721', 'ink-soft': '#6E665A',
    'wall': '#3C4630', 'wall-fg': '#DAD4C2', 'wall-dim': '#ABB296',
    'olive': '#57633F', 'olive-2': '#6E7A52',
    'honey': '#C6892B', 'honey-lt': '#EBD49B',
    'clay': '#AC4B36', 'line': '#E3DACA',
}

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


# A readable theme: deep plum chrome, warm paper, berry accent.
GOOD = {
    "plaster": "#EFE4EC", "paper": "#FCF7FA", "paper-2": "#FFFFFF",
    "ink": "#2A2028", "ink-soft": "#6B5F68",
    "wall": "#40263C", "wall-fg": "#E6D8E2", "wall-dim": "#9A8795",
    "olive": "#4F6B4A", "olive-2": "#7B9475",
    "honey": "#B5477E", "honey-lt": "#F0C9DE",
    "clay": "#A33B3B", "line": "#E6D5E0",
}

A.app.config["TESTING"] = True
client = A.app.test_client()
with client.session_transaction() as s:
    s["user"] = USER
    s["csrf_token"] = "tok"
H = {"X-CSRF-Token": "tok"}


def set_theme(tokens=None, **extra):
    body = {"name": "Ciruela", "tokens": tokens if tokens is not None else GOOD, **extra}
    return client.post("/theme/api/set", headers=H, json=body)


# --- the guard ----------------------------------------------------------------
print("a theme nobody could read is refused")
dark_on_dark = dict(GOOD, ink="#7A7078")          # grey text on near-white paper
r = set_theme(dark_on_dark)
check("it is a 400", r.status_code == 400, r.status_code)
msg = r.get_json().get("error", "")
check("it names the pair that failed", '«ink»' in msg and '«paper»' in msg, msg)
check("and says by how much", ":1" in msg, msg)
check("and what to do about it", "Oscurece" in msg or "aclara" in msg, msg)

print("\nand the barely-readable one is refused too, not rounded up")
r = set_theme(dict(GOOD, wall_fg=None, **{"wall-fg": "#6E5A69"}))
check("the top bar has to be legible", r.status_code == 400, r.status_code)

print("\na colour that is not a colour is refused before it reaches a stylesheet")
for bad in ("rgb(1,2,3)", "honeydew", "#12345", "", None, 12):
    r = set_theme(dict(GOOD, honey=bad))
    check(f"  {bad!r}", r.status_code == 400, r.get_json())

print("\nand a missing role is named")
missing = {k: v for k, v in GOOD.items() if k != "clay"}
r = set_theme(missing)
check("it says which one", "clay" in r.get_json().get("error", ""), r.get_json())

print("\nonly the house roles are taken")
r = set_theme(dict(GOOD, **{"display": "Comic Sans", "r-lg": "0px"}))
check("the theme saves", r.status_code == 200, r.get_json())
css = client.get("/theme.css").get_data(as_text=True)
check("and the smuggled tokens are not in the sheet",
      "--display" not in css and "--r-lg" not in css, css[:200])


# --- what it produces ---------------------------------------------------------
print("\na good theme becomes a stylesheet")
check("it saves", set_theme().status_code == 200)
css = client.get("/theme.css").get_data(as_text=True)
check("served as CSS", client.get("/theme.css").mimetype == "text/css")
check("it sets the roles", "--wall: #40263C;" in css and "--honey: #B5477E;" in css, css[:300])
check("it is a :root block", css.strip().startswith(":root {"), css[:60])

print("\nthe derived shades are computed, so a theme cannot get them wrong")
for role in ("ink-deep", "olive-deep", "clay-deep", "honey-deep", "rule", "tint"):
    check(f"  --{role} is there", f"--{role}:" in css, css)
deep = A._hex_rgb(css.split("--ink-deep:")[1].split(";")[0].strip())
check("and --ink-deep really is darker than --ink",
      A._luminance(deep) < A._luminance(A._hex_rgb(GOOD["ink"])), deep)


# --- whose theme is it --------------------------------------------------------
print("\na theme belongs to one member")
other = A.app.test_client()
with other.session_transaction() as s2:
    s2["user"] = OTHER
    s2["csrf_token"] = "tok"
check("somebody else's page gets no theme",
      other.get("/theme.css").get_data(as_text=True).strip() == "")
check("and cannot read mine through the API",
      other.get("/theme/api/current").get_json()["theme"] is None)

print("\nAlfred sets a theme with its own derived token, and only its own")
token = A._proxy_user_token(OTHER)
fresh = A.app.test_client()
r = fresh.post("/theme/api/set", json={"name": "de Sam", "tokens": GOOD},
               headers={"X-Proxy-User": OTHER, "X-Proxy-Secret": token})
check("the skill's own member: accepted", r.status_code == 200, r.get_json())
r = fresh.post("/theme/api/set", json={"name": "robada", "tokens": GOOD},
               headers={"X-Proxy-User": USER, "X-Proxy-Secret": token})
check("somebody else's member: refused", r.status_code in (401, 403), r.status_code)


# --- the backdrop -------------------------------------------------------------
print("\nthe backdrop")
PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
       "IQAAAABJRU5ErkJggg==")
check("saving one is fine", set_theme(backdrop_png=PNG).status_code == 200)
css = client.get("/theme.css").get_data(as_text=True)
check("the sheet paints it behind everything", "body::before" in css and "z-index: -1" in css, css)
check("faintly, so the text stays readable", "opacity: 0.18;" in css, css)
check("and it is served", client.get("/theme/backdrop").status_code == 200)
check("cache-busted by the theme's own timestamp", "/theme/backdrop?v=" in css, css)
r = set_theme(backdrop_png="not base64 at all!!")
check("junk is refused", r.status_code == 400, r.get_json())
check("dropping it works", set_theme(drop_backdrop=True).status_code == 200)
check("and then there is nothing to serve", client.get("/theme/backdrop").status_code == 404)


# --- the link in every page ---------------------------------------------------
print("\n/theme.css is in every page's head, so it must never fail")
anon = A.app.test_client()
r = anon.get("/theme.css")
check("no session: 200 and empty, not a redirect to the login page",
      r.status_code == 200 and r.get_data(as_text=True).strip() == "", r.status_code)
check("still CSS", r.mimetype == "text/css")

print("\nreset puts The House back")
check("it resets", client.post("/theme/api/reset", headers=H).status_code == 200)
check("the sheet is empty again", client.get("/theme.css").get_data(as_text=True).strip() == "")
check("and the API says there is no theme",
      client.get("/theme/api/current").get_json()["theme"] is None)



# --- shape --------------------------------------------------------------------
# A theme changes colours and the shape of things, and nothing else. Shape is a
# named set rather than free numbers: "border-radius: 400px" is a button whose
# label is clipped by its own corners, and a theme is written by a model.
print("\nshape is one of a named few")
r = set_theme(shape="square")
check("a known shape is accepted", r.status_code == 200, r.get_json())
check("and reported back", r.get_json().get("shape") == "square", r.get_json())
css = client.get("/theme.css").get_data(as_text=True)
check("the sheet carries it", "border-radius: 3px !important" in css, css[-400:])
check("buttons get the control size", "border-radius: 2px !important" in css, css[-400:])

r = set_theme(shape="400px")
check("a number is refused", r.status_code == 400, r.get_json())
check("and the message lists the real ones",
      "rounded" in r.get_json().get("error", ""), r.get_json())

print("\nthe house shape adds nothing to the sheet")
set_theme(shape="rounded")
css = client.get("/theme.css").get_data(as_text=True)
check("no radius rules at all", "border-radius" not in css, css[-200:])

print("\nno shape given is the house shape, not an error")
r = set_theme()
check("accepted", r.status_code == 200, r.get_json())
check("and recorded as the default", r.get_json().get("shape") == "rounded", r.get_json())

print("\nwithout a backdrop the theme owns the background wash too")
css = client.get("/theme.css").get_data(as_text=True)
check("the page ground is restated in the theme's colours",
      "radial-gradient" in css, css)
check("and no cream is left in it", "#F6F1E5" not in css and "#E7DFCC" not in css, css)

print("\nwith a backdrop the wash gets out of its way")
set_theme(backdrop_png=PNG)
css = client.get("/theme.css").get_data(as_text=True)
check("no competing gradient", "radial-gradient" not in css, css)
check("the image is the ground", "body::before" in css, css)
# Every other page shows its ground between the cards. The chat fills the
# viewport with one opaque surface, so unless that surface gets out of the way
# too the backdrop is invisible on the page people spend all day looking at.
check("and the chat surface stops hiding it",
      "#messages { background-color: transparent; }" in css, css)
# Every page paints two cream washes of its own, literal and older than the
# themes. Clearing only the colour left them on top of the backdrop — a plum
# house with a cream glow in the corner, which is the very thing the
# no-backdrop branch exists to prevent.
check("and the page's own washes are cleared too",
      "background-image: none" in css, css)
check("so no cream survives anywhere in the sheet",
      "#F6F1E5" not in css and "#E7DFCC" not in css, css)
client.post("/theme/api/reset", headers=H)

print("\nwithout a backdrop the chat surface is left alone")
set_theme()
css = client.get("/theme.css").get_data(as_text=True)
check("nothing said about #messages", "#messages" not in css, css)
client.post("/theme/api/reset", headers=H)


# --- backdrop strength --------------------------------------------------------
# The default is tuned for a wash. A drawn motif at .18 is a field of grey
# smudges, so somebody who asks for a wallpaper of kittens can ask for the
# strength that makes them kittens — and no further, because past `strong`
# the text sitting straight on the ground stops meeting its contrast floor.
print("\nthe backdrop's strength is one of a named few")
r = set_theme(backdrop_png=PNG, strength="strong")
check("a known strength is accepted", r.status_code == 200, r.get_json())
check("and reported back", r.get_json().get("strength") == "strong", r.get_json())
css = client.get("/theme.css").get_data(as_text=True)
check("the sheet carries it", "opacity: 0.45;" in css, css[-300:])

r = set_theme(backdrop_png=PNG, strength="0.9")
check("a number is refused", r.status_code == 400, r.get_json())
check("and the message lists the real ones",
      "strong" in r.get_json().get("error", ""), r.get_json())

print("\nno strength given keeps the house default")
r = set_theme(backdrop_png=PNG)
check("accepted", r.status_code == 200, r.get_json())
check("recorded as normal", r.get_json().get("strength") == "normal", r.get_json())
css = client.get("/theme.css").get_data(as_text=True)
check("and the sheet still says .18", "opacity: 0.18;" in css, css[-300:])

print("\nstrength survives a round trip")
set_theme(backdrop_png=PNG, strength="visible")
cur = client.get("/theme/api/current").get_json()
check("the API reports it", cur["theme"].get("strength") == "visible", cur)
check("and offers the vocabulary", "strong" in (cur.get("strengths") or []), cur)
client.post("/theme/api/reset", headers=H)

# --- the vocabulary rename ----------------------------------------------------
# Shapes and strengths used to be named in Spanish, and they are *stored* per
# member rather than recomputed, so a house that themed itself before the
# rename has 'redondeado' sitting in its themes table. Accepted on the way in
# and normalised on the way out: an un-aliased value reaching _theme_css is a
# KeyError on a stylesheet every single page links in its head.
print("\nthe old Spanish vocabulary still resolves")
r = set_theme(shape="pastilla", strength="tenue", backdrop_png=PNG)
check("an old shape is accepted", r.status_code == 200, r.get_json())
check("and comes back as the English name",
      r.get_json().get("shape") == "pill", r.get_json())
check("an old strength comes back translated too",
      r.get_json().get("strength") == "faint", r.get_json())
css = client.get("/theme.css").get_data(as_text=True)
check("and the sheet is the one the old name meant",
      "opacity: 0.12;" in css, css[-300:])

# The stored-row path, which is the one that actually breaks: written straight
# into the table the way the old code would have left it.
conn = sqlite3.connect(A.THEME_DB_PATH)
conn.execute("UPDATE themes SET shape=?, strength=? WHERE username=?",
             ("redondeado", "marcado", USER))
conn.commit()
conn.close()
cur = client.get("/theme/api/current").get_json()
check("a row written before the rename reads back in English",
      cur["theme"].get("shape") == "rounded"
      and cur["theme"].get("strength") == "strong", cur)
check("and its stylesheet renders instead of raising",
      client.get("/theme.css").status_code == 200)
client.post("/theme/api/reset", headers=H)

# --- the camera wall's dialect ------------------------------------------------
# The cameras use their own token names and live in the dark, so the theme is
# translated rather than copied. The translation used to read the light colour
# off `paper` — which is only the light one in a *light* theme. A dark theme
# passes the contrast check above perfectly and inverted it completely: 1.15:1
# text on ground, i.e. invisible, with the accents still bright enough to look
# like the sheet had worked.
print("\nthe night sheet is readable whichever way the theme is lit")


def night(**over):
    tokens = dict(GOOD, **over)
    assert set_theme(tokens).status_code == 200, "fixture theme was refused"
    css = client.get("/theme/night.css").get_data(as_text=True)
    return dict(l.strip().rstrip(";").split(": ") for l in css.splitlines()
                if l.startswith("  --"))


def contrast(a, b):
    return A._contrast(A._hex_rgb(a), A._hex_rgb(b))


LIT = {}                                    # light theme: GOOD as it stands
DARK = {"plaster": "#141A28", "paper": "#1B2233", "paper-2": "#1B2233",
        "ink": "#E6ECF8", "ink-soft": "#C3CBDD",
        "wall": "#0F1526", "wall-fg": "#DCE4F5", "honey": "#E0A94E",
        "olive": "#6FA36B", "clay": "#D2705A", "line": "#2A3145"}

for label, over in (("a light theme", LIT), ("a dark theme", DARK)):
    v = night(**over)
    check(f"  {label}: text on the ground is readable",
          contrast(v["--text"], v["--bg"]) >= 4.5,
          f'{v["--text"]} on {v["--bg"]} = {contrast(v["--text"], v["--bg"]):.1f}:1')
    check(f"  {label}: muted text on a panel is readable",
          contrast(v["--text-muted"], v["--surface"]) >= 4.5,
          f'{contrast(v["--text-muted"], v["--surface"]):.1f}:1')
    check(f"  {label}: the accent can be seen",
          contrast(v["--accent"], v["--surface"]) >= 3.0,
          f'{contrast(v["--accent"], v["--surface"]):.1f}:1')
    # The ground is pinned, not shaded, so every theme is equally dark.
    check(f"  {label}: the ground really is dark",
          A._luminance(A._hex_rgb(v["--bg"])) < 0.05, v["--bg"])

client.post("/theme/api/reset", headers=H)


print("\nthe derived colours are readable on the surface they are painted on")
# The enforcer above only ever sees the roles a theme *sets*. Everything a
# page writes in `--clay-deep` or `--honey-deep` is derived afterwards, by
# darkening — which adds contrast on a pale house and removes it on a dark
# one. That is the whole «letras oscuras sobre fondo oscuro» report: on a dark
# theme the back-link came out at 2.7:1, the tags at 2.4:1 and the warnings at
# 2.1:1, and every one of them passed validation.
DARK = {
    'plaster': '#16181C', 'paper': '#1E2126', 'paper-2': '#262A31',
    'ink': '#E6E2D8', 'ink-soft': '#A29C90',
    'wall': '#0F1114', 'wall-fg': '#DAD4C2', 'wall-dim': '#8E8878',
    'olive': '#7C8C5A', 'olive-2': '#5E6B45',
    'honey': '#C6892B', 'honey-lt': '#3A2E14',
    'clay': '#AC4B36', 'line': '#2E333A',
}
# fg token, bg token, where the two pages paint it, floor
PAINTED = (
    ('olive-ink', 'plaster', 'the «Panel principal» back-link', 4.5),
    ('clay-ink', 'paper', 'a warning on a sheet', 4.5),
    ('honey-ink', 'honey-lt', 'a .tag and the warn-sheet body', 4.5),
    ('clay-loud', 'honey-lt', 'the warn-sheet heading', 4.5),
    ('toast-bad-fg', 'clay-deep', 'an error toast', 4.5),
    ('danger-fg', 'clay', 'the «Borrar» button', 4.5),
    ('ink-soft', 'paper', 'the <select> caret, drawn in ink-soft', 3.0),
)
for lit, tokens in (('a dark theme', DARK), ('the house as it ships', HOUSE)):
    css = A._theme_css(tokens)
    tok = dict(re.findall(r'--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})', css))
    for fg, bg, where, want in PAINTED:
        got = A._contrast(A._hex_rgb(tok[fg]), A._hex_rgb(tok[bg]))
        check(f"  {lit}: {where}", got >= want, f"{got:.2f}:1, needs {want}")
    check(f"  {lit}: the toast borrows the chrome rather than deriving a ground",
          '--toast-bg: var(--wall);' in css and '--toast-fg: var(--wall-fg);' in css)
    caret = re.search(r"--caret:.*?fill='%23([0-9A-Fa-f]{6})'", css)
    check(f"  {lit}: the caret is drawn in the theme's own ink, not a literal",
          bool(caret) and caret.group(1).upper() == tok['ink-soft'].lstrip('#').upper(),
          caret.group(1) if caret else None)

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
