"""The Studio's portal-side pieces: what "lyrics with Alfred" and the music
video ask the assistant for, and how a file is handed over as a download.

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
            "lyrics_notes_ph", "lyrics_go", "music_video", "mv_help", "mv_idea", "mv_idea_ph",
            "mv_shot_len", "mv_ref", "mv_generate_now", "mv_estimate", "mv_existing", "mv_go",
            "mv_planning", "mv_failed", "mv_added", "preview", "preview_missing", "preview_close",
            "mute_shots", "soundtrack", "live_preview", "cancel_all", "cancel_all_confirm",
            "delete_take", "delete_take_confirm", "delete_upload_confirm", "remove_confirm",
            "mv_listening", "mv_heard", "mv_heard_nowords", "mv_listen_failed", "shot_cut",
            "favorite_set", "favorite_clear", "rs_button", "rs_title", "rs_mode_part", "rs_mode_all",
            "rs_pick", "rs_range", "rs_no_lines", "rs_similar", "rs_keep_voice", "rs_go",
            "preview_download", "preview_rendering"):
    check(key, key in A.STUDIO_UI_KEYS and all(f"studio.{key}" in c for c in CATALOGUES.values()))

print("\na music video is planned by Alfred, shot by shot")
plans = []


def _planner(username, chat_id, text, timeout, profile=None):
    asked.append(text)
    return plans.pop(0) if plans else ""


A._run_nanobot_turn = _planner
asked.clear()
plans[:] = ['Here you go:\n[{"prompt": "A girl on a bike at night", "continues": true},'
            ' {"prompt": "Close-up of her smile", "continues": true}, "Neon street, wide shot"]']
r = client.post("/studio/api/music-video", headers=HOME, json={
    "shots": 3, "seconds": 24, "kind": "song", "lyrics": MINE, "style": "synthpop",
    "title": "Noche", "idea": "80s film look"})
shots = (r.get_json() or {}).get("shots") or []
check("three shots come back, read out of the prose around them", r.status_code == 200 and len(shots) == 3, r.data[:200])
check("the first never continues anything", shots and shots[0]["continues"] is False, shots[:1])
check("a later one keeps what Alfred said", len(shots) > 1 and shots[1]["continues"] is True, shots)
check("a bare string is a shot too", len(shots) > 2 and shots[2]["prompt"] == "Neon street, wide shot", shots)
prompt = asked[0] if asked else ""
check("Alfred is given the words, the length, the count and the look",
      MINE in prompt and "24 seconds" in prompt and "exactly 3" in prompt and "80s film look" in prompt, prompt[:300])

asked.clear()
plans[:] = ['[{"prompt": "Intro wide shot"}, {"prompt": "Pelican close-up"}]']
r = client.post("/studio/api/music-video", headers=HOME, json={
    "shots": 9, "seconds": 16, "kind": "song", "lyrics": MINE,
    "plan": [{"start": 0, "end": 5.5, "sung": False, "words": "", "section": "Instrumental"},
             {"start": 5.5, "end": 16, "sung": True, "words": "Mora canta en la cocina", "section": "Verso"}]})
prompt = asked[0] if asked else ""
check("with the Studio's cuts, the count is the plan's, not the page's",
      r.status_code == 200 and len(r.get_json()["shots"]) == 2 and "exactly 2 shots" in prompt, (r.status_code, prompt[:200]))
check("each shot is given its time and the words sung in it",
      "Shot 2 (0:05.5-0:16.0, Verso): sung: \"Mora canta en la cocina\"" in prompt, prompt[-400:])
check("and a shot with nobody singing is said to be music only",
      "Shot 1 (0:00.0-0:05.5, Instrumental): no singing" in prompt, prompt[-400:])

asked.clear()
plans[:] = ['[{"prompt": "one"}]', '[{"prompt": "one"}, {"prompt": "two"}]']
r = client.post("/studio/api/music-video", headers=HOME, json={"shots": 2, "seconds": 16})
check("a plan of the wrong length is asked for again, once", r.status_code == 200 and len(asked) == 2
      and len(r.get_json()["shots"]) == 2, (r.status_code, len(asked)))
asked.clear()
plans[:] = ["no json here", "still none"]
r = client.post("/studio/api/music-video", headers=HOME, json={"shots": 2, "seconds": 16})
check("and a second miss is an error, not a half-made video", r.status_code == 502 and len(asked) == 2, r.status_code)

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
