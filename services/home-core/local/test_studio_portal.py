"""The Studio's two portal-side pieces: what "lyrics with Alfred" asks the
assistant for, and how a file is handed over as a download.

Run: python local/test_studio_portal.py   (needs Flask; skips loudly without it)

Downloads: the page's `?download=<name>` makes the portal answer with an
attachment. That header is the whole feature on a phone -- the Android app's
WebView ignores the `download` attribute and only hands a response to the
phone's downloads when it says `attachment` -- so it is checked, along with the
name it carries and that the parameter never reaches the studio.

Lyrics:

The page asks before calling, and says which of two things it wants: a new
song from the theme, or the lyrics already in the box kept and changed only
where the person says. The second is the one that must not quietly become the
first -- a person who typed their own verse and asked for a shorter chorus
should get their verse back. So this pins what reaches the model in each mode,
with the model stubbed out.
"""
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

tmp = tempfile.mkdtemp(prefix="homecore-lyrics-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
PROXY_SECRET = "p" * 32
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET=PROXY_SECRET,
                  DEBUG_API_KEY="d" * 32)

USER1 = "user1"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}]' % USER1)

sys.path.insert(0, dst)
import app as A  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


HOME = {"X-Proxy-Secret": PROXY_SECRET, "X-Proxy-User": USER1, "X-Proxy-Lan": "1"}
client = A.app.test_client()
asked = []
A._run_nanobot_turn = lambda username, chat_id, text, timeout, profile=None: (
    asked.append(text) or "[Verse]\nla la la")

MINE = "[Verse]\nMora canta en la cocina\n\n[Chorus]\nun estribillo muy muy largo"


def lyrics(**body):
    asked.clear()
    r = client.post("/studio/api/lyrics", json=body, headers=HOME)
    return r, (asked[0] if asked else "")


print("a new song needs a theme, and says so")
r, _ = lyrics(mode="new", theme="")
check("no theme -> 400 before anything is asked", r.status_code == 400 and not asked, r.status_code)

print("\nkeeping the lyrics and changing only what was asked")
r, prompt = lyrics(mode="edit", lyrics=MINE, notes="un estribillo más corto", theme="la cocina")
check("answers with what the assistant wrote", r.status_code == 200 and r.get_json()["lyrics"].startswith("[Verse]"), r.data[:80])
check("the person's own lyrics reach the model", MINE in prompt, prompt[-200:])
check("with only their change asked for", "Change only this: un estribillo más corto" in prompt, prompt[:200])
check("and everything else kept as it is", "Leave every other line exactly as it is" in prompt)
check("rather than a new song", "Write original song lyrics" not in prompt)
check("a theme is not required to edit", lyrics(mode="edit", lyrics=MINE)[0].status_code == 200)

print("\nimproving them with no instructions")
r, prompt = lyrics(mode="edit", lyrics=MINE)
check("polishes rather than rewrites", "Improve them without rewriting them" in prompt, prompt[:200])
check("and still sends them", MINE in prompt)

print("\nwriting a new one")
r, prompt = lyrics(mode="new", theme="el mar", notes="que rime", seconds=60)
check("from the theme", "about: el mar" in prompt, prompt[:120])
check("with the extra instructions", "Also: que rime" in prompt)
check("seconds that arrive as text or a fraction still work",
      lyrics(mode="new", theme="x", seconds="45.5")[0].status_code == 200)

print("\nan edit with nothing to edit is a new song, and needs its theme")
check("empty lyrics + no theme -> 400", lyrics(mode="edit", lyrics="  ")[0].status_code == 400)

print("\nand every string the dialog shows is handed to the page, in both catalogues")
# Staged beside the app in the image; from a checkout, the repository's own.
import json  # noqa: E402
I18N = next(d for d in (os.path.join(SRC, "i18n"), os.path.join(SRC, "..", "..", "..", "i18n"))
            if os.path.isfile(os.path.join(d, "en.json")))
CATALOGUES = {loc: json.load(open(os.path.join(I18N, f"{loc}.json"), encoding="utf-8"))
              for loc in ("en", "es")}
for key in ("lyrics_mode_edit", "lyrics_mode_new", "lyrics_confirm_new", "lyrics_notes",
            "lyrics_notes_ph", "lyrics_go"):
    check(key, key in A.STUDIO_UI_KEYS and all(f"studio.{key}" in c for c in CATALOGUES.values()))

print("\na file asked for as a download comes back as an attachment")
A.STUDIO_URL, A.STUDIO_SECRET = "http://studio.invalid", "s" * 32
sent = []


class _Up:
    status_code = 200
    headers = {"Content-Type": "video/mp4", "Content-Length": "3"}

    def iter_content(self, chunk_size=None):
        yield b"abc"


def _fake(method, url, params=None, **kw):
    sent.append((url, dict(params or {})))
    return _Up()


A.requests.request = _fake
r = client.get("/studio/api/projects/abc123def456/file/takes/x1.mp4?download=Canción de Mora - Toma 1", headers=HOME)
r.get_data()  # drain the stream, or its context is popped out of order
cd = r.headers.get("Content-Disposition", "")
check("attachment", cd.startswith("attachment;"), cd)
check("with the asked-for name and the file's own extension",
      "filename*=UTF-8''Canci%C3%B3n%20de%20Mora%20-%20Toma%201.mp4" in cd, cd)
check("and a plain-ASCII name for older handlers", 'filename="Cancion de Mora - Toma 1.mp4"' in cd, cd)
check("the parameter is not forwarded to the studio", sent and "download" not in sent[-1][1], sent[-1:])
r = client.get("/studio/api/projects/abc123def456/file/takes/x1.mp4", headers=HOME)
r.get_data()
check("without it, the file plays inline as before", "Content-Disposition" not in r.headers, r.headers)
check("a name cannot smuggle a path or a quote",
      '/' not in A._studio_attachment('../../etc/"passwd', "takes/a.png").split("filename*")[0].split('filename="')[1])

shutil.rmtree(tmp, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
