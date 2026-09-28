"""A project's words under version control: one git repository per project.

What is kept is only what a person writes -- every shot's description and
dialogue, a song's lyrics and style, a picture's prompt, the project's name
and settings, the order things are in, and the text of the project's own
characters. Never a picture, a clip or a song, and never the list of versions
the card made: those are the Studio's, and a history of them would be a
history of the queue.

    <project>/.history/            a plain git repository, `git log` works in it
        project.json               name, kind, settings
        order.json                 the ids of each section, in timeline order
        items/<id>.json            one per shot, song or picture: its words
        characters/<id>.json       one per character of this project's own

One JSON file per item, sorted and indented, so `git diff` reads as the
change it was. Each save by a person is a revision authored by them -- by
Alfred on their behalf, when it came from their assistant -- and saves close
together by the same person fold into one (the page saves while you type).

Going back never loses anything. Reverting a revision undoes only what it
changed, and only where nothing changed it again since (those are reported,
not overwritten); going back to a revision makes the project read as it did
then. Both are new revisions themselves, so they can be undone too. An item
either takes out of the timeline keeps its versions: its full record waits in
`<project>/.history-removed/<id>.json` for the revision that brings it back.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tarfile
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from .projects import EDITABLE, ID_RE, REMOVED_DIR, SECTIONS, ProjectError, Projects

HISTORY_DIR = ".history"
# A save folds into the revision before it when the same person made that one
# less than this long ago: the page saves every few keystrokes.
FOLD_S = 300
TEXT = {s: tuple(k for k in EDITABLE[s] if k != "chosen") for s in SECTIONS}
PROJECT_TEXT = ("name", "kind")
SETTINGS_TEXT = ("look", "language", "resolution", "soundtrack", "use_storyboard")
CHARACTER_TEXT = ("name", "look", "personality", "voice_text")
REV_RE = re.compile(r"^[0-9a-f]{7,40}$")

LABELS = {
    "es": {"shots": "Toma {n}", "images": "Imagen {n}", "song": "Canción", "instrumental": "Música",
           "voice": "Voz", "character": "Personaje", "project": "Proyecto", "order": "el orden",
           "added": "nueva", "removed": "quitada", "more": "y {n} más", "revert": "Revertido: {s}",
           "restore": "Vuelta a la revisión del {d}", "copy": "Copia de «{s}»", "start": "Proyecto creado", "begun": "Historial iniciado",
           "fields": {"prompt": "descripción", "soundscape": "sonido", "music": "música", "dialogue": "diálogo",
                      "seconds": "duración", "continuity": "continuidad", "refs": "imagen de inicio", "title": "título",
                      "exact": "corte", "start": "corte", "cast": "reparto", "description": "descripción",
                      "chapters": "capítulos", "kind": "tipo", "lyrics": "letra", "style": "estilo",
                      "language": "idioma", "voice": "voz", "text": "texto", "bpm": "bpm", "size": "tamaño",
                      "name": "nombre", "look": "estilo visual", "resolution": "resolución",
                      "soundtrack": "canción del video", "use_storyboard": "usar el storyboard",
                      "personality": "personalidad", "voice_text": "texto de la voz"}},
    "en": {"shots": "Shot {n}", "images": "Picture {n}", "song": "Song", "instrumental": "Music",
           "voice": "Voice", "character": "Character", "project": "Project", "order": "the order",
           "added": "added", "removed": "removed", "more": "and {n} more", "revert": "Reverted: {s}",
           "restore": "Back to the revision of {d}", "copy": "Copy of “{s}”", "start": "Project created", "begun": "History started",
           "fields": {"prompt": "description", "soundscape": "sound", "music": "music", "dialogue": "dialogue",
                      "seconds": "length", "continuity": "continuity", "refs": "start picture", "title": "title",
                      "exact": "cut", "start": "cut", "cast": "cast", "description": "description",
                      "chapters": "chapters", "kind": "kind", "lyrics": "lyrics", "style": "style",
                      "language": "language", "voice": "voice", "text": "text", "bpm": "bpm", "size": "size",
                      "name": "name", "look": "look", "resolution": "resolution", "soundtrack": "the video's song",
                      "use_storyboard": "use the storyboard", "personality": "personality",
                      "voice_text": "voice text"}},
}


class HistoryError(ProjectError):
    pass


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def snapshot(doc: dict, characters: list[dict]) -> dict[str, str]:
    """The files of a revision, from a project and its own characters."""
    files = {
        "project.json": _dump({**{k: doc.get(k) for k in PROJECT_TEXT},
                               "settings": {k: v for k, v in (doc.get("settings") or {}).items()
                                            if k in SETTINGS_TEXT}}),
        "order.json": _dump({s: [it["id"] for it in doc.get(s) or []] for s in SECTIONS}),
    }
    for s in SECTIONS:
        for it in doc.get(s) or []:
            files[f"items/{it['id']}.json"] = _dump({"section": s, **{k: it[k] for k in TEXT[s] if k in it}})
    for ch in characters:
        if ch.get("scope") == "project" and ID_RE.fullmatch(str(ch.get("id") or "")):
            files[f"characters/{ch['id']}.json"] = _dump({k: ch.get(k, "") for k in CHARACTER_TEXT})
    return files


def model(files: dict[str, str]) -> dict:
    """A revision's files read back: {project, order, items, characters}."""
    def load(name, default):
        try:
            return json.loads(files[name]) if name in files else default
        except ValueError:
            return default
    out = {"project": load("project.json", {}), "order": load("order.json", {s: [] for s in SECTIONS}),
           "items": {}, "characters": {}}
    for name in files:
        m = re.fullmatch(r"(items|characters)/([a-z0-9]+)\.json", name)
        if m:
            out[m.group(1)][m.group(2)] = load(name, {})
    return out


def changes(a: dict, b: dict) -> list[dict]:
    """What changed from model `a` to model `b`, field by field."""
    out = []
    pa, pb = a.get("project") or {}, b.get("project") or {}
    for k in PROJECT_TEXT:
        if pa.get(k) != pb.get(k):
            out.append({"on": "project", "field": k, "before": pa.get(k), "after": pb.get(k)})
    sa, sb = pa.get("settings") or {}, pb.get("settings") or {}
    for k in SETTINGS_TEXT:
        if sa.get(k) != sb.get(k):
            out.append({"on": "project", "field": k, "before": sa.get(k), "after": sb.get(k)})
    ia, ib = a.get("items") or {}, b.get("items") or {}
    for iid in list(ib) + [i for i in ia if i not in ib]:
        before, after = ia.get(iid), ib.get(iid)
        sec = (after or before or {}).get("section")
        if before is None:
            out.append({"on": "item", "id": iid, "section": sec, "change": "added", "after": after})
        elif after is None:
            out.append({"on": "item", "id": iid, "section": sec, "change": "removed", "before": before})
        else:
            for k in TEXT.get(sec, ()):
                if before.get(k) != after.get(k):
                    out.append({"on": "item", "id": iid, "section": sec, "field": k,
                                "before": before.get(k), "after": after.get(k)})
    oa, ob = a.get("order") or {}, b.get("order") or {}
    for s in SECTIONS:
        ka, kb = [i for i in oa.get(s) or [] if i in ib], [i for i in ob.get(s) or [] if i in ia]
        if ka != kb:
            out.append({"on": "order", "section": s, "before": oa.get(s) or [], "after": ob.get(s) or []})
    ca, cb = a.get("characters") or {}, b.get("characters") or {}
    for cid in list(cb) + [c for c in ca if c not in cb]:
        before, after = ca.get(cid), cb.get(cid)
        if before is None or after is None:
            out.append({"on": "character", "id": cid, "change": "added" if before is None else "removed",
                        "before": before, "after": after})
        else:
            for k in CHARACTER_TEXT:
                if before.get(k) != after.get(k):
                    out.append({"on": "character", "id": cid, "field": k, "before": before.get(k), "after": after.get(k)})
    return out


def label_of(ch: dict, a: dict, b: dict, lang: str) -> str:
    """Who a change is about, as a person reads it: "Toma 3", "Canción «X»"."""
    L = LABELS.get(lang, LABELS["es"])
    if ch["on"] == "project":
        return L["project"]
    if ch["on"] == "order":
        return L["order"]
    if ch["on"] == "character":
        rec = ((ch.get("after") if ch.get("change") == "added" else ch.get("before") if ch.get("change") == "removed" else None)
               or b["characters"].get(ch["id"]) or a["characters"].get(ch["id"]) or {})
        return f"{L['character']} «{rec.get('name') or '?'}»"
    rec = (b["items"].get(ch["id"]) or a["items"].get(ch["id"]) or {})
    sec = ch.get("section") or rec.get("section")
    if sec == "audio":
        name = L.get(rec.get("kind") or "song", L["song"])
        return f"{name} «{rec['title']}»" if rec.get("title") else name
    order = (b["order"].get(sec) or []) if ch["id"] in (b["order"].get(sec) or []) else (a["order"].get(sec) or [])
    n = order.index(ch["id"]) + 1 if ch["id"] in order else "?"
    return L.get(sec, "{n}").format(n=n)


def summary(a: dict, b: dict, lang: str) -> str:
    """A revision's subject: what was touched, a few at most."""
    L = LABELS.get(lang, LABELS["es"])
    groups: dict[str, list[str]] = {}
    for ch in changes(a, b):
        who = label_of(ch, a, b, lang)
        what = L[ch["change"]] if ch.get("change") else L["fields"].get(ch.get("field"), ch.get("field") or "")
        if what and what not in groups.setdefault(who, []):
            groups[who].append(what)
    parts = [f"{who}: {', '.join(w for w in whats if w)}" if any(whats) else who for who, whats in groups.items()]
    if len(parts) > 3:
        parts = parts[:3] + [L["more"].format(n=len(parts) - 3)]
    return " · ".join(parts)[:200]


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()[:40] or "tag"


class History:
    def __init__(self, projects: Projects, characters=None):
        self.projects = projects
        self.characters = characters
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # -- git ------------------------------------------------------------------------
    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _dir(self, owner: str, pid: str) -> Path:
        return self.projects.dir(owner, pid) / HISTORY_DIR

    @staticmethod
    def _git(d: Path, *args: str, check: bool = True, env: dict | None = None, data: bytes | None = None):
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(d), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8",
                "GIT_COMMITTER_NAME": "Studio", "GIT_COMMITTER_EMAIL": "studio@localhost"}
        r = subprocess.run(["git", "-C", str(d), *args], capture_output=True, input=data, env={**base, **(env or {})},
                           timeout=60)
        if check and r.returncode:
            raise HistoryError(r.stderr.decode("utf-8", "replace").strip()[:300] or f"git {args[0]} failed")
        return r

    def _ensure(self, d: Path) -> None:
        if not (d / ".git").is_dir():
            d.mkdir(parents=True, exist_ok=True)
            self._git(d, "init", "-q", "-b", "main")
            self._git(d, "config", "core.quotepath", "off")

    def _has_head(self, d: Path) -> bool:
        return self._git(d, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode == 0

    def _files_at(self, d: Path, rev: str) -> dict[str, str]:
        """A revision's files, all of them, in one call."""
        if not rev:
            return {}
        r = self._git(d, "archive", "--format=tar", rev, check=False)
        if r.returncode:
            return {}
        out = {}
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tar:
            for m in tar.getmembers():
                if m.isfile():
                    out[m.name] = tar.extractfile(m).read().decode("utf-8", "replace")
        return out

    def _resolve(self, d: Path, rev: str) -> str:
        if not REV_RE.fullmatch(rev or ""):
            raise HistoryError("no such revision")
        r = self._git(d, "rev-parse", "-q", "--verify", f"{rev}^{{commit}}", check=False)
        if r.returncode:
            raise HistoryError("no such revision")
        return r.stdout.decode().strip()

    def _parent(self, d: Path, rev: str) -> str:
        r = self._git(d, "rev-parse", "-q", "--verify", f"{rev}^", check=False)
        return r.stdout.decode().strip() if r.returncode == 0 else ""

    # -- the current state --------------------------------------------------------------
    def _characters(self, owner: str, pid: str) -> list[dict]:
        if not self.characters:
            return []
        try:
            return [c for c in self.characters.list(owner, pid) if c.get("scope") == "project"]
        except ProjectError:
            return []

    def _now(self, owner: str, pid: str) -> tuple[dict, dict[str, str]]:
        doc = self.projects.load(owner, pid)
        return doc, snapshot(doc, self._characters(owner, pid))

    @staticmethod
    def _lang(doc: dict) -> str:
        return "en" if str((doc.get("settings") or {}).get("language") or "es").startswith("en") else "es"

    # -- recording ----------------------------------------------------------------------
    def capture(self, owner: str, pid: str) -> tuple[dict, dict[str, str]] | None:
        """The project's words as they are this moment, for a revision written
        later: taken when the save is made, so a revision written behind
        others still holds what that save saved, under who saved it."""
        try:
            return self._now(owner, pid)
        except ProjectError:
            return None

    def record(self, owner: str, pid: str, login: str, name: str, via: str = "", kind: str = "edit",
               message: str = "", captured: tuple[dict, dict[str, str]] | None = None) -> str | None:
        """The project as it is now (or as `captured`), as a revision -- or
        folded into the last one when it is the same person's edit, minutes
        ago. None when nothing a person writes has changed."""
        d = self._dir(owner, pid)
        with self._lock(f"{owner}/{pid}"):
            captured = captured or self.capture(owner, pid)
            if not captured:
                return None
            doc, files = captured
            self._ensure(d)
            for sub in ("items", "characters"):
                for f in (d / sub).glob("*.json"):
                    if f"{sub}/{f.name}" not in files:
                        f.unlink()
            for rel, text in files.items():
                path = d / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.is_file() or path.read_text(encoding="utf-8") != text:
                    path.write_text(text, encoding="utf-8")
            self._git(d, "add", "-A")
            head = self._has_head(d)
            if head and self._git(d, "diff", "--cached", "--quiet", check=False).returncode == 0:
                return None
            author = f"{name or login}{f' ({via})' if via else ''}"
            fold = False
            if head and kind == "edit":
                last = self._git(d, "log", "-1", "--format=%at%x1f%(trailers:key=Kind,valueonly,separator=)"
                                              "%x1f%(trailers:key=Login,valueonly,separator=)%x1f%an").stdout.decode()
                at, lkind, llogin, lname = (last.strip().split("\x1f") + ["", "", "", ""])[:4]
                tagged = self._git(d, "tag", "--points-at", "HEAD").stdout.strip()
                fold = (lkind.strip() == "edit" and llogin.strip() == login and lname == author and not tagged
                        and time.time() - float(at or 0) < FOLD_S)
            lang = self._lang(doc)
            if not head:
                # The first revision is the project as it was made, and never
                # folds into the next: going back to it is going back to the start.
                kind, subject = "start", message or LABELS[lang]["start"]
            else:
                base = self._files_at(d, self._parent(d, "HEAD") if fold else "HEAD")
                subject = message or summary(model(base), model(files), lang) or LABELS[lang]["start"]
            body = f"{subject}\n\nKind: {kind}\nLogin: {login}\n"
            args = ["commit", "-q", "--no-verify", "-m", body, f"--author={author} <{_slug(login)}@studio>"]
            if fold:
                # Typed and then undone within the fold: the revision it would
                # have amended is simply gone, as the edit is.
                if self._git(d, "diff", "--cached", "--quiet", "HEAD^", check=False).returncode == 0:
                    self._git(d, "reset", "-q", "--soft", "HEAD^")
                    return self._git(d, "rev-parse", "HEAD").stdout.decode().strip()
                args.append("--amend")
            self._git(d, *args)
            return self._git(d, "rev-parse", "HEAD").stdout.decode().strip()

    def begin_all(self) -> int:
        """A first revision for every project that has none -- those from
        before the history -- so the first edit after it has a before to go
        back to. Once each; returns how many were begun."""
        n = 0
        for f in sorted(self.projects.root.glob("*/*/project.json")):
            owner, pid = f.parent.parent.name, f.parent.name
            if owner.startswith(".") or (f.parent / HISTORY_DIR / ".git").is_dir() or not ID_RE.fullmatch(pid):
                continue
            try:
                doc = self.projects.load(owner, pid)
                if self.record(owner, pid, owner, "Studio", kind="start",
                               message=LABELS[self._lang(doc)]["begun"]):
                    n += 1
            except (ProjectError, OSError):
                continue
        return n

    # -- reading --------------------------------------------------------------------------
    def log(self, owner: str, pid: str, limit: int = 300) -> dict:
        d = self._dir(owner, pid)
        if not (d / ".git").is_dir() or not self._has_head(d):
            return {"revisions": [], "tags": []}
        raw = self._git(d, "log", f"-n{int(limit)}", "--format=%H%x1f%an%x1f%at%x1f%s%x1f"
                                                   "%(trailers:key=Kind,valueonly,separator=)%x1e").stdout.decode("utf-8", "replace")
        revs = []
        for rec in raw.split("\x1e"):
            parts = rec.strip("\n").split("\x1f")
            if len(parts) >= 5 and parts[0]:
                revs.append({"rev": parts[0], "author": parts[1], "at": int(parts[2] or 0),
                             "subject": parts[3], "kind": parts[4].strip() or "edit"})
        tags = []
        raw = self._git(d, "for-each-ref", "refs/tags", "--format=%(refname:short)%1f%(*objectname)%1f%(objectname)"
                                                        "%1f%(contents:subject)%1f%(taggername)%1f%(taggerdate:unix)").stdout
        for line in raw.decode("utf-8", "replace").splitlines():
            ref, peeled, obj, label, who, at = (line.split("\x1f") + [""] * 6)[:6]
            tags.append({"ref": ref, "rev": peeled or obj, "name": label or ref, "author": who, "at": int(at or 0)})
        return {"revisions": revs, "tags": tags}

    def show(self, owner: str, pid: str, rev: str) -> dict:
        """What one revision changed, field by field, before and after."""
        d = self._dir(owner, pid)
        full = self._resolve(d, rev)
        a, b = model(self._files_at(d, self._parent(d, full))), model(self._files_at(d, full))
        lang = self._lang(self.projects.load(owner, pid))
        L = LABELS[lang]
        out = []
        for ch in changes(a, b):
            out.append({**ch, "label": label_of(ch, a, b, lang),
                        "field_label": L[ch["change"]] if ch.get("change") else L["fields"].get(ch.get("field"), ch.get("field"))})
        return {"rev": full, "changes": out}

    # -- going back ------------------------------------------------------------------------
    def _revive(self, owner: str, pid: str, section: str, iid: str, text: dict) -> dict:
        """An item back in the timeline: its full record when the history has
        it (versions and all, those whose files still exist), else its words."""
        base = self.projects.dir(owner, pid)
        item = {"id": iid, "takes": [], "chosen": -1}
        try:
            kept = json.loads((base / REMOVED_DIR / f"{iid}.json").read_text(encoding="utf-8"))
            if isinstance(kept, dict) and kept.get("id") == iid:
                item = kept
                item["takes"] = [t for t in item.get("takes") or [] if (base / str(t.get("file") or "")).is_file()]
                if not -1 <= int(item.get("chosen", -1)) < len(item["takes"]):
                    item["chosen"] = -1
        except (OSError, ValueError):
            pass
        for k in TEXT[section]:
            if k in text:
                item[k] = text[k]
        return item

    def _apply(self, owner: str, pid: str, change) -> list[dict]:
        """Run `change(doc, chars, conflicts)` on the project under its lock;
        the characters' text is written through Characters."""
        conflicts: list[dict] = []
        pending_chars: dict[str, dict] = {}
        with self.projects._lock(f"{owner}/{pid}"):
            doc = self.projects.load(owner, pid)
            before = {s: {it["id"]: it for it in doc.get(s) or []} for s in SECTIONS}
            chars = {c["id"]: c for c in self._characters(owner, pid)}
            change(doc, chars, conflicts, pending_chars)
            for s in SECTIONS:
                now = {it["id"] for it in doc.get(s) or []}
                for iid, it in before[s].items():
                    if iid not in now:
                        self.projects.stash_removed(owner, pid, it)
            doc["updated"] = time.time()
            self.projects._write(owner, pid, doc)
        for cid, fields in pending_chars.items():
            try:
                self.characters.update(cid, owner, pid, fields, admin=True)
            except (ProjectError, AttributeError) as exc:
                conflicts.append({"on": "character", "id": cid, "reason": str(exc)})
        return conflicts

    def revert(self, owner: str, pid: str, rev: str, login: str, name: str, via: str = "") -> dict:
        """Undo what one revision changed -- only that, and only where it has
        not changed again since. The rest is reported."""
        d = self._dir(owner, pid)
        full = self._resolve(d, rev)
        self.record(owner, pid, login, name, via)
        a_files, b_files = self._files_at(d, self._parent(d, full)), self._files_at(d, full)
        a, b = model(a_files), model(b_files)
        todo = changes(a, b)
        subject = self._git(d, "log", "-1", "--format=%s", full).stdout.decode("utf-8", "replace").strip()

        def change(doc, chars, conflicts, pending_chars):
            items = {s: {it["id"]: it for it in doc.get(s) or []} for s in SECTIONS}
            where = {iid: s for s in SECTIONS for iid in items[s]}
            for ch in todo:
                if ch["on"] == "project":
                    k = ch["field"]
                    holder = doc if k in PROJECT_TEXT else doc.setdefault("settings", {})
                    if holder.get(k) == ch["after"]:
                        if ch["before"] is None:
                            holder.pop(k, None)
                        else:
                            holder[k] = ch["before"]
                    else:
                        conflicts.append({**ch, "now": holder.get(k)})
                elif ch["on"] == "item" and ch.get("field"):
                    s = where.get(ch["id"])
                    it = items[s][ch["id"]] if s else None
                    if it is not None and it.get(ch["field"]) == ch["after"]:
                        if ch["before"] is None:
                            it.pop(ch["field"], None)
                        else:
                            it[ch["field"]] = ch["before"]
                    else:
                        conflicts.append({**ch, "now": it.get(ch["field"]) if it else None})
                elif ch["on"] == "item" and ch["change"] == "added":
                    s = where.get(ch["id"])
                    if s:
                        doc[s] = [it for it in doc[s] if it["id"] != ch["id"]]
                elif ch["on"] == "item" and ch["change"] == "removed":
                    s = ch["section"]
                    if s in SECTIONS and ch["id"] not in where:
                        old = a["order"].get(s) or []
                        pos = old.index(ch["id"]) if ch["id"] in old else len(doc.get(s) or [])
                        doc.setdefault(s, []).insert(min(pos, len(doc[s])),
                                                     self._revive(owner, pid, s, ch["id"], ch["before"]))
                elif ch["on"] == "order":
                    s = ch["section"]
                    cur = [it["id"] for it in doc.get(s) or []]
                    if [i for i in cur if i in ch["after"]] == [i for i in ch["after"] if i in cur]:
                        rank = {iid: n for n, iid in enumerate(ch["before"])}
                        doc[s] = sorted(doc.get(s) or [], key=lambda it: rank.get(it["id"], len(rank) + cur.index(it["id"])))
                    else:
                        conflicts.append(ch)
                elif ch["on"] == "character" and ch.get("field"):
                    c = chars.get(ch["id"])
                    if c is not None and c.get(ch["field"]) == ch["after"]:
                        pending_chars.setdefault(ch["id"], {})[ch["field"]] = ch["before"] or ""
                    else:
                        conflicts.append(ch)
                else:
                    conflicts.append(ch)                         # a character made or deleted: not redone
        conflicts = self._apply(owner, pid, change)
        doc = self.projects.load(owner, pid)
        new = self.record(owner, pid, login, name, via, kind="revert",
                          message=LABELS[self._lang(doc)]["revert"].format(s=subject)[:200])
        return {"rev": new, "conflicts": conflicts, "applied": len(todo) - len(conflicts)}

    def restore(self, owner: str, pid: str, rev: str, login: str, name: str, via: str = "") -> dict:
        """The project as it read at a revision: its words, its order, what
        was in it. Versions made since stay with the items that keep them."""
        d = self._dir(owner, pid)
        full = self._resolve(d, rev)
        self.record(owner, pid, login, name, via)
        target = model(self._files_at(d, full))
        at = int(self._git(d, "log", "-1", "--format=%at", full).stdout.decode().strip() or 0)

        def change(doc, chars, conflicts, pending_chars):
            proj = target["project"]
            for k in PROJECT_TEXT:
                if proj.get(k) is not None:
                    doc[k] = proj[k]
            settings = doc.setdefault("settings", {})
            for k in SETTINGS_TEXT:
                if k in (proj.get("settings") or {}):
                    settings[k] = proj["settings"][k]
                else:
                    settings.pop(k, None)
            for s in SECTIONS:
                cur = {it["id"]: it for it in doc.get(s) or []}
                out = []
                for iid in target["order"].get(s) or []:
                    text = target["items"].get(iid) or {}
                    if iid in cur:
                        it = cur[iid]
                        for k in TEXT[s]:
                            if k in text:
                                it[k] = text[k]
                            else:
                                it.pop(k, None)
                        out.append(it)
                    else:
                        out.append(self._revive(owner, pid, s, iid, text))
                doc[s] = out
            for cid, text in target["characters"].items():
                c = chars.get(cid)
                if c is None:
                    conflicts.append({"on": "character", "id": cid, "change": "removed", "before": text})
                elif any(c.get(k) != text.get(k) for k in CHARACTER_TEXT):
                    pending_chars[cid] = {k: text.get(k, "") for k in CHARACTER_TEXT}
        conflicts = self._apply(owner, pid, change)
        doc = self.projects.load(owner, pid)
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(at))
        new = self.record(owner, pid, login, name, via, kind="restore",
                          message=LABELS[self._lang(doc)]["restore"].format(d=when + " UTC"))
        return {"rev": new, "conflicts": conflicts}

    # -- tags ---------------------------------------------------------------------------------
    def tag(self, owner: str, pid: str, rev: str, label: str, login: str, name: str) -> dict:
        d = self._dir(owner, pid)
        full = self._resolve(d, rev)
        label = " ".join(str(label or "").split())[:80]
        if not label:
            raise HistoryError("a tag needs a name")
        ref = f"{_slug(label)}-{uuid.uuid4().hex[:6]}"
        self._git(d, "tag", "-a", ref, "-m", label, full,
                  env={"GIT_COMMITTER_NAME": name or login, "GIT_COMMITTER_EMAIL": f"{_slug(login)}@studio"})
        return {"ref": ref, "rev": full, "name": label}

    def untag(self, owner: str, pid: str, ref: str) -> None:
        d = self._dir(owner, pid)
        if not re.fullmatch(r"[a-z0-9-]{1,60}", ref or ""):
            raise HistoryError("no such tag")
        if self._git(d, "tag", "-d", ref, check=False).returncode:
            raise HistoryError("no such tag")
