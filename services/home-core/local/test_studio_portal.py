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
import base64
import time
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
            "preview_download", "preview_rendering",
            "new_project_kind", "kind_soon", "pkind_music_video", "pkind_music_video_about", "pkind_short_film", "pkind_short_film_about", "pkind_explainer", "pkind_explainer_about", "pkind_podcast", "pkind_podcast_about", "pkind_recording", "pkind_recording_about", "pkind_free", "pkind_free_about", "storyboard", "board_make", "board_draw", "board_redraw", "board_queued", "sb_review_apply", "sb_review_applied", "sb_redrawing_review", "project_name", "more", "rail_label", "rail_song", "rail_song_none", "rail_song_bpm", "rail_song_unheard", "rail_board", "rail_board_st", "rail_weak", "rail_videos", "rail_videos_st", "rail_making", "rail_stale", "rail_film", "rail_film_st", "rail_film_none", "rail_none", "sb_video_old", "sb_has_video", "sb_review_n", "fit_button", "fit_help", "fit_confirm", "fit_done", "fit_short", "sb_review", "sb_review_help", "sb_review_all", "sb_reviewing", "sb_review_started", "sb_review_failed", "sb_review_round", "sb_review_suggests", "sb_refine", "sb_refine_help", "sb_refine_confirm", "sb_refine_started", "sb_refine_busy", "hist_button", "hist_title", "hist_help", "hist_empty", "hist_show", "hist_nothing", "hist_reordered", "hist_revert", "hist_revert_help", "hist_revert_confirm", "hist_restore", "hist_restore_help", "hist_restore_confirm", "hist_tag_now", "hist_tag_prompt", "hist_untag_confirm", "hist_done", "hist_conflicts", "score_make", "score_open", "score_running", "score_retry", "score_confirm", "score_queued", "sb_use", "sb_starts_from", "sb_video_older", "sb_to_video", "sb_to_video_off", "sb_video_stale", "sb_continues", "sb_use_frame", "board_from", "board_from_none", "ref_add", "ref_add_short", "ref_is", "ref_added", "tab_board", "sb_help", "sb_empty", "sb_redraw_changed", "sb_animatic", "sb_changed", "sb_changed_short", "sb_drawing", "sb_music_only", "mv_then", "mv_then_board", "mv_then_video", "mv_then_none", "mv_board_estimate",
            "tab_cast", "ch_none", "ch_new", "ch_edit", "ch_name", "ch_look", "ch_look_ph", "ch_personality", "ch_personality_ph", "ch_voice", "ch_voice_text", "ch_record", "ch_stop", "ch_pictures", "ch_save", "ch_pick_studio", "ch_pick_files", "ch_pick_none", "ch_portrait", "ch_speak", "ch_speak_what", "ch_speak_ph", "ch_widen_person", "ch_widen_family", "ch_scope_project", "ch_scope_person", "ch_scope_family", "ch_widen_confirm", "ch_delete_confirm", "ch_in_shot",
            "rec_title", "rec_screen", "rec_cam", "rec_mic", "rec_start", "rec_pause", "rec_resume", "rec_stop", "rec_uploading", "rec_saved", "rec_processing", "rec_failed", "rec_no_screen", "rec_need_source", "rec_default_title", "rec_denied", "delete_render_confirm",
            "rec_subs", "rec_subs_running", "rec_subs_failed", "rec_transcript", "rec_trim", "rec_trim_running", "rec_trim_done", "rec_trim_failed", "render_subs",
            "rec_describe", "rec_describing", "rec_desc_title", "rec_desc_description", "rec_desc_chapters", "rec_desc_copy", "rec_desc_copied", "rec_desc_failed", "card_paused_update", "card_paused_after", "rec_retry"):
    check(key, key in A.STUDIO_UI_KEYS and all(f"studio.{key}" in c for c in CATALOGUES.values()))

print("\na music video is planned by Alfred, shot by shot")
plans = []


chats = []


profiles = []


def _planner(username, chat_id, text, timeout, profile=None, images=None):
    asked.append(text)
    chats.append(chat_id)
    profiles.append(profile)
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
check("the storyboard is written by the Designer", profiles and profiles[-1] == "designer", profiles[-1:])
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
plans[:] = ['[{"prompt": "The pelican jumps", "continues": false, "cast": ["Pelícano"]}]']
r = client.post("/studio/api/music-video", headers=HOME, json={
    "shots": 1, "seconds": 8, "characters": [{"name": "Pelícano", "look": "a brown pelican, red helmet",
                                               "personality": "brave"}]})
check("the characters are given to Alfred by name, look and personality",
      "Pelícano: a brown pelican, red helmet Personality: brave" in (asked[0] if asked else ""), (asked or [""])[0][-400:])
check("and each planned shot comes back with who is in it",
      r.status_code == 200 and r.get_json()["shots"][0]["cast"] == ["Pelícano"], r.data[:200])

asked.clear()
plans[:] = ['[{"prompt": "one"}]', '[{"prompt": "one"}, {"prompt": "two"}]']
r = client.post("/studio/api/music-video", headers=HOME, json={"shots": 2, "seconds": 16})
check("a plan of the wrong length is asked for again, once", r.status_code == 200 and len(asked) == 2
      and len(r.get_json()["shots"]) == 2, (r.status_code, len(asked)))
asked.clear()
plans[:] = ["no json here", "still none"]
r = client.post("/studio/api/music-video", headers=HOME, json={"shots": 2, "seconds": 16})
check("and a second miss is an error, not a half-made video", r.status_code == 502 and len(asked) == 2, r.status_code)

print("\na recording's title, description and chapters, from its transcript")
asked.clear()
plans[:] = ['{"title": "Cómo cambiar una rueda", "description": "Paso a paso.", '
            '"chapters": [{"start": 95, "title": "Aflojar"}, {"start": 3, "title": "Intro"}]}']
r = client.post("/studio/api/describe", headers=HOME, json={"language": "es", "take": "abc123def456", "segments": [
    {"start": 0.5, "end": 4, "text": "Hoy vamos a cambiar una rueda"}, {"start": 95, "end": 99, "text": "Primero aflojamos"}]})
out = r.get_json() or {}
check("each recording version is its own conversation",
      chats and chats[-1].endswith(":stu-describe-abc123def456"), chats[-1:])
check("Alfred reads the transcript with its times", "[1:35] Primero aflojamos" in (asked[0] if asked else ""), (asked or [""])[0][:300])
check("and the chapters come back in order, the first at 0:00",
      r.status_code == 200 and [c["start"] for c in out.get("chapters", [])] == [0.0, 95.0], out)
asked.clear()
plans[:] = ["no json", ""]
check("a reply without one is an error", client.post("/studio/api/describe", headers=HOME,
      json={"segments": [{"start": 0, "text": "x"}]}).status_code == 502)
asked.clear()
check("and nothing to read is refused before asking",
      client.post("/studio/api/describe", headers=HOME, json={"segments": []}).status_code == 400 and not asked)

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

print("\nthe practice page: a version's parts, played with the song")
for key in A.PRACTICE_UI_KEYS:
    check(key, all(f"studio.{key}" in c for c in CATALOGUES.values()))
SCORE = {"state": "done", "file": "takes/s1/t1-score/score.musicxml", "midi": "takes/s1/t1-score/notes.mid",
         "minus": "takes/s1/t1-score/minus.mp3", "part": "takes/s1/t1-score/part.mp3", "tempo": 121.998,
         "score_tempo": 122, "start": 0.1156, "beat": 0.49, "fifths": 0,
         "tracks": [{"id": "acoustic_guitar", "name": "Acoustic guitar", "kind": "guitar", "notes": 10}]}
DOC = {"id": "abc123def456", "name": "Faro Zorro", "audio": [{"id": "s1", "title": "Canción </script> de Mora",
       "takes": [{"id": "t1", "file": "takes/s1/t1.mp3", "score": SCORE}, {"id": "t2", "file": "takes/s1/t2.mp3"}]}]}
got = []


class _Doc:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        return self._body


def _get(url, headers=None, timeout=None):
    got.append((url, dict(headers or {})))
    return _Doc(200, DOC) if url.endswith("/api/projects/abc123def456") else _Doc(404, {"detail": "no"})


A.requests.get = _get
r = client.get("/studio/practice?project=abc123def456&item=s1&take=t1", headers=HOME)
page = r.get_data(as_text=True)
check("the page renders, asked of the Studio as the person",
      r.status_code == 200 and got and got[-1][1].get("X-Studio-User") == USER1, (r.status_code, got[-1:]))
cfg = json.loads(page.split('id="practice-cfg">', 1)[1].split("</script>", 1)[0])
check("with the score, the song and both play-along tracks, through the proxy",
      cfg["score"] == "/studio/api/projects/abc123def456/file/takes/s1/t1-score/score.musicxml"
      and cfg["audio"] == {"song": "/studio/api/projects/abc123def456/file/takes/s1/t1.mp3",
                           "minus": "/studio/api/projects/abc123def456/file/takes/s1/t1-score/minus.mp3",
                           "part": "/studio/api/projects/abc123def456/file/takes/s1/t1-score/part.mp3"}, cfg)
check("and the tempo and start it needs to follow the song",
      cfg["meta"]["tempo"] == 121.998 and cfg["meta"]["score_tempo"] == 122 and cfg["meta"]["start"] == 0.1156)
check("a title cannot close the script it is in", "Canción </script>" not in page and page.count("</script>") == 2)
check("downloads come as attachments under the song's name", "?download=Canci%C3%B3n" in cfg["downloads"]["xml"], cfg["downloads"])
check("the renderer is the staged module build, not a CDN",
      '/static/studio/practice.js' in page and "cdn" not in page.lower())
r = client.get("/studio/practice?project=abc123def456&item=s1&take=t2", headers=HOME)
check("a version with no sheet music says how to ask for it",
      r.status_code == 200 and "practice-cfg" not in r.get_data(as_text=True))
r = client.get("/studio/practice?project=../../x&item=s1&take=t1", headers=HOME)
check("a project that is not the person's shows nothing of it",
      r.status_code == 200 and "practice-cfg" not in r.get_data(as_text=True) and got[-1][0].endswith("/api/projects/x"), got[-1:])
for f in ("alphaTab.mjs", "alphaTab.core.mjs", "alphaTab.worker.mjs", "alphaTab.worklet.mjs",
          "font/Bravura.woff2", "soundfont/sonivox.sf3", "LICENSE"):
    check(f"staged: {f}", os.path.isfile(os.path.join(SRC, "static", "vendor", "alphatab", f)))
entry = open(os.path.join(SRC, "static", "vendor", "alphatab", "alphaTab.mjs"), encoding="utf-8").read()
check("the entry starts its worker beside itself, which 'self' allows",
      '"./alphaTab.worker.mjs"' in entry and '"./alphaTab.core.mjs"' in entry)

print("\na frame reviewed by the person's assistant, and refined")
FRAME = b"\xff\xd8 a frame"
PORTRAIT = b"\x89PNG portrait"
RDOC = {"id": "abc123def456", "name": "Faro", "settings": {"look": "pastel watercolour", "language": "es", "soundtrack": "s9"},
        "audio": [{"id": "s9", "chosen": 0, "takes": [{"id": "t9", "file": "takes/s9/t9.mp3", "analysis": {"file": "takes/s9/a.json"}}]}],
        "shots": [{"id": "sh1", "prompt": "Bruma climbs the lighthouse stairs", "cast": ["ch1"], "exact": True, "start": 8.0,
                   "seconds": 8.0, "board": 0, "boards": [{"id": "bd1", "file": "takes/sh1/f.jpg", "job": "jb1"}]}]}
RCHARS = {"characters": [{"id": "ch1", "name": "Bruma", "look": "a red fox in a yellow slicker",
                          "pictures": ["pictures/p.png"], "portrait": 0}]}
calls = []


class _R:
    def __init__(self, code, body=None, content=b"", ctype="application/json"):
        self.status_code, self._body, self.content, self.headers = code, body, content, {"Content-Type": ctype}

    def json(self):
        return self._body


def _studio_req(method, url, headers=None, json=None, timeout=None, **kw):
    calls.append((method, url.split("/api/", 1)[1], json, dict(headers or {})))
    path = url.split("/api/", 1)[1]
    if path == "projects/abc123def456":
        return _R(200, RDOC)
    if path == "projects/abc123def456/characters":
        return _R(200, RCHARS)
    if path.endswith("/analyze"):
        return _R(200, {"lines": [{"text": "sube la escalera", "start": 9.0, "end": 11.0},
                                  {"text": "otra cosa", "start": 30.0, "end": 32.0}]})
    return _R(200, {"ok": True, "queued": []})


def _studio_get2(url, headers=None, timeout=None, **kw):
    path = url.split("/api/", 1)[1]
    calls.append(("GET", path, None, dict(headers or {})))
    if path == "projects/abc123def456/file/takes/sh1/f.jpg":
        return _R(200, content=FRAME, ctype="image/jpeg")
    if path == "projects/abc123def456/characters/ch1/file/pictures/p.png":
        return _R(200, content=PORTRAIT, ctype="image/png")
    return _R(404)


seen = []
looked = []
vision_answers = []


def _reviewer(username, chat_id, text, timeout, profile=None, images=None):
    seen.append({"text": text, "profile": profile, "images": images or [], "chat": chat_id})
    return plans.pop(0) if plans else ""


class _V:
    ok = True
    status_code = 200

    def __init__(self, text):
        self._text = text

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


def _vision_post(url, json=None, headers=None, timeout=None, **kw):
    looked.append({"url": url, "body": json, "headers": dict(headers or {})})
    return _V(vision_answers.pop(0) if vision_answers else "")


A.requests.request, A.requests.get, A._run_nanobot_turn = _studio_req, _studio_get2, _reviewer
A.requests.post = _vision_post
A.STUDIO_VISION_URL, A.STUDIO_VISION_MODEL, A.STUDIO_VISION_KEY = "http://127.0.0.1:11437/v1/chat/completions", "qwen3.5:9b", "ollama"
vision_answers[:] = ['{"items": ["un zorro rojo", "un impermeable amarillo", "la escalera del faro"]}',
                     '{"checks": [{"item": "un zorro rojo", "shown": "yes", "why": "un zorro rojo"},'
                     ' {"item": "un impermeable amarillo", "shown": "no", "why": "Bruma no tiene el impermeable"},'
                     ' {"item": "la escalera del faro", "shown": "partly", "why": "una escalera, sin faro"}], "defects": []}']
plans[:] = ['"A red fox in a yellow slicker climbing a white lighthouse spiral stair"']
r = client.post("/studio/api/board-review", headers=HOME, json={"project": "abc123def456", "shot": "sh1"})
out = r.get_json() or {}
check("the score is counted from the checklist, not chosen: one of three shown, one partly -> 5",
      r.status_code == 200 and out.get("review", {}).get("score") == 5
      and [c["shown"] for c in out["review"]["checks"]] == ["yes", "no", "partly"]
      and out["review"]["problems"][0].startswith("un impermeable amarillo: Bruma no tiene"), out)
req_body = looked[-2]["body"] if len(looked) > 1 else {}
check("first the shot made into a checklist, by the house's model and with no picture",
      req_body.get("response_format") == {"type": "json_object"}
      and [p_["type"] for p_ in req_body["messages"][0]["content"]] == ["text"]
      and all(x in req_body["messages"][0]["content"][0]["text"] for x in (
          "Bruma climbs the lighthouse stairs", "pastel watercolour", "a red fox in a yellow slicker", "Spanish")), req_body)
parts = looked[-1]["body"]["messages"][0]["content"] if looked else []
check("looked at by the house's vision model, frame and character portrait, without thinking",
      looked and looked[-1]["url"].startswith("http://127.0.0.1:11437") and looked[-1]["body"]["model"] == "qwen3.5:9b"
      and [p_["image_url"]["url"] for p_ in parts if p_["type"] == "image_url"]
      == ["data:image/jpeg;base64," + base64.b64encode(FRAME).decode(), "data:image/png;base64," + base64.b64encode(PORTRAIT).decode()]
      and looked[-1]["body"]["reasoning_effort"] == "none", looked[-1:] and looked[-1]["url"])
vtext = parts[0]["text"] if parts else ""
check("then the frame checked against each item, strictly, rendering defects apart",
      all(x in vtext for x in ("- un zorro rojo", "- la escalera del faro", "Be strict", "rendering defects only"))
      and looked[-1]["body"].get("response_format") == {"type": "json_object"})
check("under the bar, the Designer writes the redraw -- from the findings, with no picture sent to it",
      seen and seen[-1]["profile"] == "designer" and not seen[-1]["images"]
      and "Bruma no tiene el impermeable" in seen[-1]["text"]
      and out["review"]["prompt"] == "A red fox in a yellow slicker climbing a white lighthouse spiral stair", seen[-1:])
posted = [c for c in calls if c[1].endswith("/boards/bd1/review")]
check("the frame is marked as being looked at, then given the review",
      [c[2]["review"]["state"] for c in posted] == ["running", "done"] and posted[-1][2]["review"]["score"] == 5, posted)
check("as the person", posted and posted[-1][3].get("X-Studio-User") == USER1)
seen.clear()
vision_answers[:] = ['{"items": ["un zorro rojo", "un impermeable amarillo", "la escalera del faro"]}', '{"checks": [{"item": "a", "shown": "yes"}, {"item": "b", "shown": "yes"}], "defects": []}']
r = client.post("/studio/api/board-review", headers=HOME, json={"project": "abc123def456", "shot": "sh1"})
check("a frame that is fine costs no writing", r.status_code == 200 and not seen and not r.get_json()["review"].get("prompt"))
calls.clear()
vision_answers[:] = ['{"items": ["un zorro rojo", "un impermeable amarillo", "la escalera del faro"]}', "not json"]
r = client.post("/studio/api/board-review", headers=HOME, json={"project": "abc123def456", "shot": "sh1"})
check("a verdict that cannot be read is a failure, and the frame says so",
      r.status_code == 502 and [c[2]["review"]["state"] for c in calls if c[1].endswith("/review")] == ["running", "failed"])
saved_url = A.STUDIO_VISION_URL
A.STUDIO_VISION_URL = ""
r = client.post("/studio/api/board-review", headers=HOME, json={"project": "abc123def456", "shot": "sh1"})
check("with no vision model set, it says it cannot, rather than sending the picture elsewhere",
      r.status_code == 502 and not any(l for l in looked if l["url"] == ""))
A.STUDIO_VISION_URL = saved_url

print("\n  the Studio's hook: a refined frame, reviewed and redrawn while it scores low")
calls.clear()
vision_answers[:] = ['{"items": ["un zorro rojo", "un impermeable amarillo", "la escalera del faro"]}', '{"checks": [{"item": "a", "shown": "partly", "why": "dark"}], "defects": []}']
plans[:] = ["brighter lighthouse at dawn"]
check("the hook needs the Studio's secret",
      client.post("/studio/api/frame-review", json={"login": USER1}).status_code == 401)
r = client.post("/studio/api/frame-review", headers={"X-Studio-Secret": A.STUDIO_SECRET}, json={
    "login": USER1, "project": "abc123def456", "shot": "sh1", "job": "jb1", "refine": {"rounds": 1, "threshold": 7, "round": 1}})
deadline = time.time() + 10
while time.time() < deadline and not any(c[1] == "projects/abc123def456/storyboard" for c in calls):
    time.sleep(0.05)
redraw = [c for c in calls if c[1] == "projects/abc123def456/storyboard"]
took = [c for c in calls if c[1] == "projects/abc123def456/items/sh1/prompt"]
check("under the bar with a round left, the Designer's prompt becomes the description -- as Alfred, for the history",
      took and took[0][2] == {"prompt": "brighter lighthouse at dawn"} and took[0][3].get("X-Studio-Via") == "Alfred", took)
check("and the frame is redrawn from it, one round fewer",
      r.status_code == 200 and redraw and redraw[0][2] == {"items": ["sh1"], "refine": {"rounds": 0, "threshold": 7, "round": 2}}
      and calls.index(took[0]) < calls.index(redraw[0]), redraw)
calls.clear()
vision_answers[:] = ['{"items": ["un zorro rojo", "un impermeable amarillo", "la escalera del faro"]}', '{"checks": [{"item": "a", "shown": "partly", "why": "dark"}], "defects": []}']
plans[:] = ["x"]
client.post("/studio/api/frame-review", headers={"X-Studio-Secret": A.STUDIO_SECRET}, json={
    "login": USER1, "project": "abc123def456", "shot": "sh1", "job": "jb1", "refine": {"rounds": 0, "threshold": 7, "round": 2}})
deadline = time.time() + 10
while time.time() < deadline and not any(c[1].endswith("/review") and c[2]["review"]["state"] == "done" for c in calls):
    time.sleep(0.05)
time.sleep(0.2)
check("with no rounds left it is reviewed and left as it is",
      any(c[1].endswith("/review") and c[2]["review"]["state"] == "done" for c in calls)
      and not any(c[1] == "projects/abc123def456/storyboard" for c in calls))

calls.clear()
saved_url = A.STUDIO_VISION_URL
A.STUDIO_VISION_URL = ""
r = client.post("/studio/api/frame-review", headers={"X-Studio-Secret": A.STUDIO_SECRET}, json={
    "login": USER1, "project": "abc123def456", "shot": "sh1", "job": "jb1", "refine": {"rounds": 0, "threshold": 7}})
time.sleep(0.3)
check("every drawn frame is sent, and a house with no vision model leaves them unreviewed rather than failed",
      r.status_code == 200 and r.get_json().get("reviewed") is False and not calls, calls)
A.STUDIO_VISION_URL = saved_url


print("\n  one style, every frame")
OTHER = b"\xff\xd8 another frame"
_saved_shots = RDOC["shots"]
RDOC["shots"] = _saved_shots + [
    {"id": "sh2", "prompt": "the sea at night", "board": 0, "boards": [
        {"id": "bd2", "file": "takes/sh2/g.jpg", "review": {"state": "done", "score": 8, "style": "yes"}}]},
    {"id": "sh3", "prompt": "a gull", "board": 0, "boards": [
        {"id": "bd3", "file": "takes/sh3/h.jpg", "review": {"state": "done", "score": 9, "style": "no"}}]}]
_get_before = A.requests.get
def _studio_get3(url, headers=None, timeout=None, **kw):
    if url.endswith("/file/takes/sh2/g.jpg"):
        return _R(200, content=OTHER, ctype="image/jpeg")
    if url.endswith("/file/takes/sh3/h.jpg"):
        return _R(200, content=b"\xff\xd8 off-style", ctype="image/jpeg")
    return _studio_get2(url, headers=headers, timeout=timeout, **kw)
A.requests.get = _studio_get3
refs = A._studio_style_refs(USER1, "abc123def456", RDOC, "sh1")
check("a frame is held to the storyboard's frames that passed and kept the style -- never one found off-style",
      [n for n, _ in refs] == ["shot 2"] and refs[0][1].endswith(base64.b64encode(OTHER).decode()), [n for n, _ in refs])
vision_answers[:] = ['{"items": ["un zorro rojo", "la escalera del faro"]}',
                     '{"checks": [{"item": "El estilo visual", "shown": "no", "why": "es una foto, no acuarela"},'
                     ' {"item": "El mismo estilo", "shown": "partly", "why": "más oscuro"},'
                     ' {"item": "un zorro rojo", "shown": "yes"}, {"item": "la escalera del faro", "shown": "yes"}],'
                     ' "defects": []}']
plans[:] = ["a red fox on lighthouse stairs, in pastel watercolour"]
seen.clear()
r = client.post("/studio/api/board-review", headers=HOME, json={"project": "abc123def456", "shot": "sh1"})
rv = (r.get_json() or {}).get("review") or {}
vparts = looked[-1]["body"]["messages"][0]["content"]
vt = vparts[0]["text"]
check("the look and the other frames' style are items of the checklist, first and in the project's language",
      "- " + A._studio_t("es", "studio.review_item_look", "The film's look: {look}", look="pastel watercolour") in vt
      and "- " + A._studio_t("es", "studio.review_item_style", "The same style as the storyboard's other frames") in vt
      and vt.index("Requirements:") < vt.index("pastel watercolour", vt.index("Requirements:"))
      < vt.index("- un zorro rojo"), vt[-500:])
check("  with the other frame sent as a picture, after the portraits, and named as style, not content",
      [x["image_url"]["url"] for x in vparts if x["type"] == "image_url"][-1].endswith(base64.b64encode(OTHER).decode())
      and "not the same content" in vt and "not the style" in vt, vt[:600])
check("a frame in the wrong style cannot pass on the rest: two of four shown would be 6, capped at 4",
      rv.get("score") == 4 and rv.get("style") == "no", rv)
check("  its style problems lead the list the redraw is written from",
      (rv.get("problems") or [""])[0].endswith("es una foto, no acuarela"), rv.get("problems"))
check("  and the Designer is told the look fixes it -- not to write style words of its own",
      seen and A.STUDIO_STYLE_RULE in seen[-1]["text"] and "not by words of yours" in seen[-1]["text"]
      and "restat" not in seen[-1]["text"], seen[-1:])
check("  and the review keeps whether it held the style, for the next frame's references",
      any(c[1].endswith("/boards/bd1/review") and c[2]["review"].get("style") == "no" for c in calls))
vision_answers[:] = ['{"items": ["un zorro rojo"]}',
                     '{"checks": [{"item": "a", "shown": "yes"}, {"item": "b", "shown": "yes"},'
                     ' {"item": "c", "shown": "yes"}], "defects": []}']
r = client.post("/studio/api/board-review", headers=HOME, json={"project": "abc123def456", "shot": "sh1"})
check("a frame in the style passes as before", (r.get_json() or {})["review"]["score"] == 10
      and r.get_json()["review"]["style"] == "yes", r.get_json())
plans[:] = ['[{"prompt": "x"}, {"prompt": "y"}, {"prompt": "z"}]']
seen.clear()
A._studio_correct_shots(USER1, RDOC, RDOC["shots"], "make it night")
check("a correction keeps to the look as well", seen and A.STUDIO_STYLE_RULE in seen[0]["text"])
RDOC["shots"] = _saved_shots
A.requests.get = _get_before
refs = A._studio_style_refs(USER1, "abc123def456", RDOC, "sh1")
check("a storyboard of one frame has nothing to compare it with", refs == [])
RDOC["shots"] = _saved_shots + [
    {"id": "sh2", "prompt": "the sea", "board": 0, "boards": [
        {"id": "bd2", "file": "takes/sh2/g.jpg", "review": {"state": "done", "score": 9}}]},
    {"id": "sh4", "prompt": "a boat", "board": 0, "boards": [{"id": "bd4", "file": "takes/sh2/g.jpg"}]}]
A.requests.get = _studio_get3
check("only frames that passed in the style are references: not unreviewed ones, nor ones passed before "
      "style was checked", A._studio_style_refs(USER1, "abc123def456", RDOC, "sh1") == [])
RDOC["settings"] = {**RDOC["settings"], "look": ""}
check("  (with no look, a passing frame is the only style there is)",
      [n for n, _ in A._studio_style_refs(USER1, "abc123def456", RDOC, "sh1")] == ["shot 2"])
RDOC["settings"]["look"] = "pastel watercolour"
RDOC["shots"] = _saved_shots
A.requests.get = _get_before

print("\na character described from its picture")
_saved_d = (A._studio_call, A._studio_data_url, A.requests.post)
puts_d, looked_d = [], []
def _scd(username, method, path, body=None, timeout=30, via=""):
    if path == "projects/p9/characters":
        return {"characters": [{"id": "c1", "name": "Bruma", "pictures": ["pictures/a.png", "pictures/b.png"],
                                "portrait": 1}, {"id": "c2", "name": "Nadie", "pictures": []}]}
    if method == "PUT":
        puts_d.append((path, body, via))
        return {"ok": True}
    return None
A._studio_call = _scd
A._studio_data_url = lambda u, path: "data:image/png;base64,QUJD" if path.endswith("pictures/b.png") else None
def _vd(url, json=None, headers=None, timeout=None, **kw):
    looked_d.append((url, json))
    return _V('{"look": "a toddler of about two, round face, tight dark curls, yellow shirt and blue overalls"}')
A.requests.post = _vd
r = client.post("/studio/api/char-describe", headers=HOME, json={"project": "p9", "character": "c1"})
check("the chosen picture is looked at by the house's own vision model, and nowhere else",
      r.status_code == 200 and looked_d and looked_d[0][0] == A.STUDIO_VISION_URL
      and looked_d[0][1]["messages"][0]["content"][1]["image_url"]["url"] == "data:image/png;base64,QUJD", r.get_json())
check("  asked for what an illustrator needs, and no guess at who it is",
      "no guess at who they are" in looked_d[0][1]["messages"][0]["content"][0]["text"])
check("  and the look is saved on the character, as the assistant",
      puts_d == [("projects/p9/characters/c1", {"look": "a toddler of about two, round face, tight dark curls, "
                                                         "yellow shirt and blue overalls"}, "Alfred")], puts_d)
r = client.post("/studio/api/char-describe", headers=HOME, json={"project": "p9", "character": "c2"})
check("a character with no picture is told to get one", r.status_code == 400)
A._studio_call, A._studio_data_url, A.requests.post = _saved_d
page = open(os.path.join(os.path.dirname(os.path.abspath(A.__file__)), "templates", "studio.html"), encoding="utf-8").read()
check("the page has the describe button, the style flag and the style upload",
      "data-ch-describe" in page and "data-style-ref" in page and 'data-style="1"' in page
      and "api('char-describe'" in page)


print("\nreference pictures, in words, for the Designer")
_saved_r = (A._studio_call, A._studio_data_url, A.requests.post, A._run_nanobot_turn, A._studio_background,
            A.requests.request)
posted_r, looked_r, asked_r = [], [], []
RDOC2 = {"id": "p8", "settings": {"look": "3D cartoon"},
         "uploads": [{"file": "uploads/a-style.png", "kind": "reference", "style": True, "style_at": 1},
                     {"file": "uploads/b-track.png", "kind": "reference"},
                     {"file": "uploads/c-old.png", "kind": "reference", "description": "Shows: a kept description"}],
         "shots": [{"id": "s1", "prompt": "The race starts.", "refs": ["uploads/b-track.png"]},
                   {"id": "s2", "prompt": "The finish line.", "refs": ["uploads/c-old.png"]}]}
def _scr(username, method, path, body=None, timeout=30, via=""):
    if path == "projects/p8":
        return RDOC2
    if path == "projects/p8/characters":
        return {"characters": [{"id": "c1", "name": "Bruma", "look": "Un bebe", "pictures": ["pictures/a.png"],
                                "portrait": 0}]}
    posted_r.append((method, path, body, via))
    return {"ok": True}
A._studio_call = _scr
A._studio_data_url = lambda u, path: "data:image/png;base64,QUJD"
def _vr(url, json=None, headers=None, timeout=None, **kw):
    looked_r.append(json)
    if "character for a film" in json["messages"][0]["content"][0]["text"]:
        return _V('{"look": "a baby of about one, round face, dark curls"}')
    return _V('{"shows": "a red go-kart track by a lake", "style": "flat 2D vector drawing"}')
A.requests.post = _vr
words, style = A._studio_ref_words(USER1, RDOC2)
check("each reference picture is put into words once, by the house's model, what it shows and its style",
      words["uploads/b-track.png"] == "Shows: a red go-kart track by a lake Style: flat 2D vector drawing"
      and style == ["uploads/a-style.png"] and len(looked_r) == 2, (words, len(looked_r)))
check("  and the words are kept on the picture, so the next call reads them",
      ("POST", "projects/p8/uploads/b-track.png/description",
       {"description": "Shows: a red go-kart track by a lake Style: flat 2D vector drawing"}, "") in posted_r, posted_r)
check("  a picture already described is not looked at again",
      words["uploads/c-old.png"] == "Shows: a kept description" and len(looked_r) == 2)
A._run_nanobot_turn = lambda u, c, text, timeout, profile=None: asked_r.append(text) or json.dumps(
    {"look": None, "shots": [{"prompt": "a"}, {"prompt": "b"}]})
A._studio_correct_shots(USER1, RDOC2, RDOC2["shots"], "closer shots")
check("the Designer writes each shot knowing what its reference picture shows, and the style pictures",
      asked_r and "Shot 1 [drawn from its reference picture -- Shows: a red go-kart track by a lake" in asked_r[0]
      and "The film's style pictures" in asked_r[0], asked_r[0][:1500] if asked_r else "")
looked_r.clear(); posted_r.clear()
look, why = A._studio_describe_character(USER1, "p8", "c1")
check("a character's look is written from its picture, keeping the person's own note as facts",
      look == "a baby of about one, round face, dark curls"
      and 'already wrote this about them: "Un bebe"' in looked_r[0]["messages"][0]["content"][0]["text"]
      and "describe only the main one" in looked_r[0]["messages"][0]["content"][0]["text"]
      and ("PUT", "projects/p8/characters/c1", {"look": look}, "Alfred") in posted_r, (look, why, posted_r))
_cs = A._studio_call
A._studio_call = lambda u, m, path, body=None, timeout=30, via="": (
    {"characters": [{"id": "c1", "name": "Bruma", "look": "x" * 120, "pictures": ["pictures/a.png"], "portrait": 0}]}
    if path == "projects/p8/characters" else _cs(u, m, path, body, timeout, via))
looked_r.clear()
A._studio_describe_character(USER1, "p8", "c1")
check("  a look longer than a note is an earlier description, and is not carried into the new one",
      "already wrote" not in looked_r[0]["messages"][0]["content"][0]["text"])
check("  and an automatic one leaves it alone", A._studio_describe_character(USER1, "p8", "c1", True) == (None, None))
A._studio_call = _cs
bg = []
A._studio_background = lambda fn, *a: bg.append((fn.__name__, a))
class _Up:
    status_code = 200
    headers = {}
    def iter_content(self, chunk_size=1):
        return iter([b"{}"])
A.requests.request = lambda *a, **kw: _Up()
client.post("/studio/api/projects/p8/characters/c1/upload", headers=HOME,
            data=b'--x\r\nContent-Disposition: form-data; name="kind"\r\n\r\npicture\r\n--x--',
            content_type="multipart/form-data; boundary=x")
check("a picture given to a character starts its description, in the background, only if its look is a note",
      bg == [("_studio_describe_character", (USER1, "p8", "c1", True))], bg)
bg.clear()
client.post("/studio/api/projects/p8/characters/c1/upload", headers=HOME,
            data=b'--x\r\nContent-Disposition: form-data; name="kind"\r\n\r\nvoice\r\n--x--',
            content_type="multipart/form-data; boundary=x")
check("  a voice sample starts nothing", bg == [], bg)
asked_r.clear()
A._run_nanobot_turn = lambda u, c, text, timeout, profile=None, images=None: asked_r.append(text) or json.dumps(
    [{"prompt": "a"}, {"prompt": "b"}])
client.post("/studio/api/music-video", headers=HOME, json={"shots": 2, "seconds": 10, "kind": "song", "lyrics": "la",
                                                            "project": "p8", "ref": "uploads/b-track.png"})
check("the music-video plan is written knowing the style pictures and the first shot's reference",
      asked_r and "The film's style pictures show" in asked_r[0]
      and "The first shot is drawn from a reference picture, which shows: Shows: a red go-kart track" in asked_r[0]
      and A.STUDIO_STYLE_RULE in asked_r[0], asked_r[0][:900] if asked_r else "")
(A._studio_call, A._studio_data_url, A.requests.post, A._run_nanobot_turn, A._studio_background,
 A.requests.request) = _saved_r


print("\none correction for the whole storyboard")
_saved = (A._studio_call, A._run_nanobot_turn, A._studio_configured, A._studio_reachable)
A._studio_configured = A._studio_reachable = lambda: True
BOARD = {"id": "p1", "settings": {"look": "watercolour, warm"},
         "shots": [{"id": "s1", "prompt": "Mora walks in a sunny park.", "cast": ["c1"]},
                   {"id": "s2", "prompt": "A dog runs on the beach.", "continuity": True},
                   {"id": "s3", "prompt": "Real footage.", "recorded": True},
                   {"id": "s4", "prompt": "Mora waves from a window.", "cast": ["c1"]}]}
calls, asked = [], []
def _sc(username, method, path, body=None, timeout=30, via=""):
    calls.append((method, path, body, via))
    if path == "projects/p1":
        return BOARD
    if path == "projects/p1/characters":
        return {"characters": [{"id": "c1", "name": "Mora", "look": "short dark hair"}]}
    return {"ok": True}
A._studio_call = _sc
answer = json.dumps([{"prompt": "Mora walks in a park at night."}, {"prompt": "A dog runs on the beach."},
                     {"prompt": "Mora waves from a window at night."}])
A._run_nanobot_turn = lambda u, c, text, timeout, profile=None: asked.append((text, profile)) or answer
r = client.post("/studio/api/board-correct", headers=HOME,
                json={"project": "p1", "feedback": "it is night in every shot"})
d = r.get_json() or {}
check("the Designer is asked once, for the whole storyboard", len(asked) == 1 and asked[0][1] == "designer", asked)
text = asked[0][0] if asked else ""
check("with every shot in order, the look, the characters and the person's correction",
      "Shot 1 [on screen: Mora]: Mora walks" in text and "Shot 3 [on screen: Mora]: Mora waves" in text and "watercolour" in text
      and "Mora: short dark hair" in text and "it is night in every shot" in text, text[:600])
check("a recorded shot is not a description to rewrite", "Real footage" not in text)
check("the shot that continues the one before says so", "Shot 2 (continues the shot before)" in text)
saved = [(p, b, v) for m, p, b, v in calls if p.endswith("/prompt")]
check("only the shots that changed are saved, under the assistant's name",
      [p for p, _, _ in saved] == ["projects/p1/items/s1/prompt", "projects/p1/items/s4/prompt"]
      and all(v == "Alfred" for _, _, v in saved) and saved[0][1] == {"prompt": "Mora walks in a park at night."},
      saved)
redrawn = [b for m, p, b, v in calls if p == "projects/p1/storyboard"]
check("and those, and only those, are redrawn", redrawn == [{"items": ["s1", "s4"]}], redrawn)
check("the page is told how many of how many", d == {"changed": 2, "total": 3, "redrawn": True, "look": ""}, d)
check("  with style left to the look: none named, none repeated, no photo mentioned",
      A.STUDIO_STYLE_RULE in text and "photo" in A.STUDIO_STYLE_RULE and "do not repeat the look" in text, text[-900:])
calls.clear(); asked.clear()
r = client.post("/studio/api/board-correct", headers=HOME,
                json={"project": "p1", "feedback": "night", "redraw": False})
check("asked not to redraw, it only rewrites",
      not any(p == "projects/p1/storyboard" for _, p, _, _ in calls) and r.get_json().get("redrawn") is False)
check("no correction written is refused before any model is asked",
      client.post("/studio/api/board-correct", headers=HOME, json={"project": "p1", "feedback": "  "}).status_code == 400)
asked.clear(); calls.clear()
A._run_nanobot_turn = lambda u, c, text, timeout, profile=None: asked.append(text) or "[{\"prompt\": \"only one\"}]"
r = client.post("/studio/api/board-correct", headers=HOME, json={"project": "p1", "feedback": "night"})
check("an answer with the wrong number of shots is asked for once more, then refused, and nothing is saved",
      len(asked) == 2 and r.status_code == 502 and not any(p.endswith("/prompt") for _, p, _, _ in calls),
      (len(asked), r.status_code))
calls.clear(); asked.clear()
answer = json.dumps({"look": "3D animated cartoon for children, bright and clean", "shots": [
    {"prompt": "Mora walks in a sunny park."}, {"prompt": "A dog runs on the beach."},
    {"prompt": "Mora waves from a window."}]})
A._run_nanobot_turn = lambda u, c, text, timeout, profile=None: asked.append(text) or answer
r = client.post("/studio/api/board-correct", headers=HOME, json={"project": "p1", "feedback": "make it a cartoon"})
d = r.get_json() or {}
puts = [(b, v) for m, p_, b, v in calls if m == "PUT" and p_ == "projects/p1"]
check("a correction about the style changes the film's look, as the assistant -- not every shot",
      puts == [({"settings": {"look": "3D animated cartoon for children, bright and clean"}}, "Alfred")]
      and not any(p_.endswith("/prompt") for _, p_, _, _ in calls), (puts, calls))
check("  and every frame is redrawn, since the look is in each",
      [b for m, p_, b, v in calls if p_ == "projects/p1/storyboard"] == [{"items": ["s1", "s2", "s4"]}])
check("  and the page is told the new look", d.get("look") == "3D animated cartoon for children, bright and clean"
      and d.get("changed") == 0, d)
check("  the Designer is asked to say 2D or 3D, and to take style words out of the shots",
      "2D or 3D" in asked[0] and "Take out of the descriptions any style words" in asked[0], asked[0][-700:])
calls.clear()
answer = json.dumps({"look": "watercolour, warm", "shots": [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}]})
client.post("/studio/api/board-correct", headers=HOME, json={"project": "p1", "feedback": "x"})
check("  a look that comes back the same is not a change", not any(m == "PUT" for m, *_ in calls), calls)
check("an old-style answer, a bare array, still reads as the shots",
      A._studio_parse_correction('[{"prompt": "a"}]', 1) == (None, ["a"])
      and A._studio_parse_correction('{"look": null, "shots": [{"prompt": "a"}]}', 1) == (None, ["a"]))
A._studio_call, A._run_nanobot_turn, A._studio_configured, A._studio_reachable = _saved
page = open(os.path.join(os.path.dirname(os.path.abspath(A.__file__)), "templates", "studio.html"), encoding="utf-8").read()
check("the page has the box, keeps what is typed across redraws, and sends it",
      'id="sb-correct-text"' in page and "correctDraft = el.value" in page
      and "api('board-correct'" in page and "esc(correctDraft)" in page)

shutil.rmtree(tmp, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
