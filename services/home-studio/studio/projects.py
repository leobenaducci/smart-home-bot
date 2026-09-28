"""Projects: one folder per person per project, on the big disk.

    <root>/<login>/<project id>/
        project.json      the timeline, the tracks, the pictures, which take is chosen
        takes/            every result the card produced for this project
        uploads/          what the person brought: reference pictures, voice samples
        renders/          the stitched films

The page edits what a person decides -- prompts, order, durations, which take
is chosen. What the card produced is recorded here, by the server, and never
taken from the page: a saved project names its takes by id, and the files are
whatever this module recorded for those ids. That is what keeps one person's
project from pointing at another person's files.

A retake never deletes the take before it; a deleted project moves to
`.trash` and is only removed after a grace period.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

LOGIN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
ID_RE = re.compile(r"^[a-z0-9]{6,32}$")
SECTIONS = ("shots", "audio", "images")
AUDIO_KINDS = ("song", "instrumental", "voice")
# What the page may set on an item. Everything else on it is the server's.
EDITABLE = {
    # `exact`: a shot cut to the music -- generated a little long and trimmed
    # to `seconds` when the film is made. `start` is where the cut falls in
    # the song, for the page to show.
    # `board`: which storyboard frame of the shot is the chosen one.
    # `cast`: the characters in the shot, by id (studio/characters.py).
    "shots": ("prompt", "soundscape", "music", "dialogue", "seconds", "continuity", "chosen", "refs", "title",
              "exact", "start", "board", "cast"),
    "audio": ("kind", "title", "lyrics", "style", "language", "seconds", "voice", "text", "chosen", "bpm"),
    "images": ("prompt", "size", "chosen", "title"),
}
TRASH_DAYS = 14
# What a project is for. It decides the page's starting shape and the
# planning flow Alfred runs; every tool stays available in every kind.
PROJECT_KINDS = ("free", "music_video", "short_film", "explainer", "podcast", "recording")


class ProjectError(ValueError):
    pass


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class Projects:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # -- paths ----------------------------------------------------------------
    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def dir(self, owner: str, pid: str) -> Path:
        if not LOGIN_RE.fullmatch(owner or "") or not ID_RE.fullmatch(pid or ""):
            raise ProjectError("no such project")
        return self.root / owner / pid

    def file(self, owner: str, pid: str, rel: str) -> Path:
        """A file inside a project, and never outside it."""
        base = self.dir(owner, pid).resolve()
        path = (base / rel).resolve()
        if base not in path.parents or not path.is_file():
            raise ProjectError("no such file")
        return path

    # -- the documents --------------------------------------------------------
    def list(self, owner: str) -> list[dict]:
        if not LOGIN_RE.fullmatch(owner or ""):
            return []
        out = []
        for p in sorted((self.root / owner).glob("*/project.json")):
            try:
                doc = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            out.append({"id": doc["id"], "name": doc.get("name", ""), "updated": doc.get("updated", 0),
                        "kind": doc.get("kind") or "free",
                        "shots": len(doc.get("shots") or []), "audio": len(doc.get("audio") or []),
                        "images": len(doc.get("images") or []),
                        "default": bool(doc.get("default")),
                        "cover": self._cover(doc)})
        # The default project first: it is where anything asked of the
        # assistant went, which is what somebody is usually looking for.
        return sorted(out, key=lambda d: (not d["default"], -d["updated"]))

    @staticmethod
    def _cover(doc: dict) -> str:
        for section, key in (("shots", "first"), ("images", "file")):
            for item in doc.get(section) or []:
                take = Projects.chosen_take(item)
                if take and take.get(key):
                    return take[key]
        return ""

    def create(self, owner: str, name: str, kind: str = "free") -> dict:
        if not LOGIN_RE.fullmatch(owner or ""):
            raise ProjectError("unknown person")
        kind = kind if kind in PROJECT_KINDS else "free"
        pid = _new_id()
        d = self.root / owner / pid
        for sub in ("takes", "uploads", "renders"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        now = time.time()
        doc = {"id": pid, "owner": owner, "name": (name or "Sin título").strip()[:80], "kind": kind,
               "created": now, "updated": now,
               "settings": {"resolution": "832x480", "fps": 24, "language": "es"},
               "shots": [], "audio": [], "images": [], "renders": [], "uploads": []}
        self._write(owner, pid, doc)
        return doc

    def load(self, owner: str, pid: str) -> dict:
        try:
            return json.loads((self.dir(owner, pid) / "project.json").read_text())
        except OSError:
            raise ProjectError("no such project") from None

    def _write(self, owner: str, pid: str, doc: dict) -> None:
        path = self.dir(owner, pid) / "project.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
        tmp.replace(path)

    def save(self, owner: str, pid: str, incoming: dict) -> dict:
        """What the page sends, merged onto what the server knows.

        The page's order of items wins (that is the timeline), its editable
        fields win, and everything else -- takes, files, renders -- is kept
        from the stored copy. An item the page does not send is removed from
        the timeline; its takes stay on disk until the project is deleted.
        """
        with self._lock(f"{owner}/{pid}"):
            doc = self.load(owner, pid)
            if "name" in incoming:
                doc["name"] = str(incoming["name"] or doc["name"]).strip()[:80]
            if incoming.get("kind") in PROJECT_KINDS:
                doc["kind"] = incoming["kind"]
            if isinstance(incoming.get("settings"), dict):
                s = incoming["settings"]
                if re.fullmatch(r"\d{3,4}x\d{3,4}", str(s.get("resolution", ""))):
                    doc["settings"]["resolution"] = s["resolution"]
                if str(s.get("language", "")) and len(str(s["language"])) <= 5:
                    doc["settings"]["language"] = str(s["language"])
                # The song a music video was made for: the preview plays it
                # and the film is laid over it. An item id or nothing.
                # The project's look: what every storyboard frame and shot is
                # asked to share, so the pictures read as one film.
                if "look" in s:
                    doc["settings"]["look"] = str(s.get("look") or "")[:600]
                if "soundtrack" in s:
                    st = str(s.get("soundtrack") or "")
                    doc["settings"]["soundtrack"] = st if ID_RE.fullmatch(st) else ""
            for section in SECTIONS:
                if not isinstance(incoming.get(section), list):
                    continue
                known = {item["id"]: item for item in doc.get(section) or []}
                merged = []
                for item in incoming[section][:200]:
                    if not isinstance(item, dict):
                        continue
                    base = known.get(item.get("id")) or {"id": _new_id(), "takes": [], "chosen": -1}
                    for key in EDITABLE[section]:
                        if key in item:
                            base[key] = _clean(key, item[key], base)
                    merged.append(base)
                doc[section] = merged
            doc["updated"] = time.time()
            self._write(owner, pid, doc)
            return doc

    def default(self, owner: str, name: str = "Alfred") -> dict:
        """The person's default project: where whatever the assistant is asked
        for lands when nobody named a project -- one place to find loose
        requests, instead of a project per picture. Made on first use; its id
        is remembered in `<owner>/.default`, and a deleted one is made again.
        """
        if not LOGIN_RE.fullmatch(owner or ""):
            raise ProjectError("unknown person")
        pointer = self.root / owner / ".default"
        with self._lock(f"{owner}/.default"):
            try:
                pid = pointer.read_text().strip()
                doc = self.load(owner, pid)
                if not doc.get("default"):
                    raise ProjectError("not the default any more")
                return doc
            except (OSError, ProjectError):
                pass
            doc = self.create(owner, name)
            doc["default"] = True
            self._write(owner, doc["id"], doc)
            pointer.write_text(doc["id"])
            return doc

    def add_recording(self, owner: str, pid: str, title: str) -> dict:
        """A clip the person is recording (the Recording kind): a shot with no
        description, whose take is what they recorded rather than what the
        card made. `recording` says where it is: recording, processing, done
        or failed."""
        with self._lock(f"{owner}/{pid}"):
            doc = self.load(owner, pid)
            item = {"id": _new_id(), "title": str(title or "")[:80], "prompt": "", "seconds": 0.0,
                    "takes": [], "chosen": -1, "recorded": True,
                    "recording": {"state": "recording", "started": time.time()}}
            doc.setdefault("shots", []).append(item)
            doc["updated"] = time.time()
            self._write(owner, pid, doc)
            return item

    def set_item_field(self, owner: str, pid: str, item_id: str, key: str, value) -> None:
        """One server-side field of one item (a recording's state)."""
        with self._lock(f"{owner}/{pid}"):
            doc = self.load(owner, pid)
            found = self.find(doc, item_id)
            if not found:
                raise ProjectError("no such item")
            found[2][key] = value
            doc["updated"] = time.time()
            self._write(owner, pid, doc)

    def append(self, owner: str, pid: str, section: str, items: list[dict]) -> list[dict]:
        """Add items to the end of a section, server-side, and return them with
        their ids. Unlike `save` it leaves every other item as it is -- which is
        what lets two requests land in one project without either replacing
        the other's."""
        if section not in SECTIONS:
            raise ProjectError(f"no section {section}")
        with self._lock(f"{owner}/{pid}"):
            doc = self.load(owner, pid)
            added = []
            for item in items[:50]:
                base = {"id": _new_id(), "takes": [], "chosen": -1}
                for key in EDITABLE[section]:
                    if key in item:
                        base[key] = _clean(key, item[key], base)
                added.append(base)
            doc.setdefault(section, []).extend(added)
            doc["updated"] = time.time()
            self._write(owner, pid, doc)
            return added

    def compress_audio_takes(self, compress) -> int:
        """Every lossless audio take already filed, through `compress`
        (media.compress_audio), with the project pointing at the result.

        Takes are compressed as they are filed now; this is for the ones made
        before that, and a no-op once there are none. One project at a time,
        under its lock, so a save from the page cannot land in between. A take
        that will not compress keeps its WAV and is tried again next start.
        """
        done = 0
        for doc_path in sorted(self.root.glob("*/*/project.json")):
            owner, pid = doc_path.parent.parent.name, doc_path.parent.name
            if not LOGIN_RE.fullmatch(owner) or not ID_RE.fullmatch(pid):
                continue
            with self._lock(f"{owner}/{pid}"):
                try:
                    doc = self.load(owner, pid)
                except ProjectError:
                    continue
                base, changed = self.dir(owner, pid), False
                for section in SECTIONS:
                    for item in doc.get(section) or []:
                        for take in item.get("takes") or []:
                            rel = str(take.get("file") or "")
                            if not rel.lower().endswith((".wav", ".flac")) or not (base / rel).is_file():
                                continue
                            try:
                                new = compress(base / rel)
                            except Exception:                  # noqa: BLE001
                                continue
                            take["file"] = str(Path(new).relative_to(base))
                            changed, done = True, done + 1
                if changed:
                    self._write(owner, pid, doc)
        return done

    def delete(self, owner: str, pid: str) -> None:
        src = self.dir(owner, pid)
        if not src.is_dir():
            raise ProjectError("no such project")
        trash = self.root / owner / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        src.rename(trash / f"{pid}-{int(time.time())}")

    def duplicate(self, owner: str, pid: str, name: str = "") -> dict:
        doc = self.load(owner, pid)
        new = self.create(owner, name or f"{doc['name']} (copia)")
        src, dst = self.dir(owner, pid), self.dir(owner, new["id"])
        for sub in ("takes", "uploads", "renders"):
            shutil.copytree(src / sub, dst / sub, dirs_exist_ok=True)
        doc.update(id=new["id"], name=new["name"], created=new["created"], updated=time.time())
        doc.pop("default", None)                        # a copy is an ordinary project
        self._write(owner, new["id"], doc)
        return doc

    def purge_trash(self) -> None:
        cutoff = time.time() - TRASH_DAYS * 86400
        for d in self.root.glob("*/.trash/*"):
            try:
                if int(d.name.rsplit("-", 1)[-1]) < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except ValueError:
                continue

    # -- items and takes ------------------------------------------------------
    @staticmethod
    def chosen_take(item: dict) -> dict | None:
        takes = item.get("takes") or []
        i = item.get("chosen", -1)
        if isinstance(i, int) and 0 <= i < len(takes):
            return takes[i]
        return takes[-1] if takes else None

    @staticmethod
    def find(doc: dict, item_id: str) -> tuple[str, int, dict] | None:
        for section in SECTIONS:
            for i, item in enumerate(doc.get(section) or []):
                if item["id"] == item_id:
                    return section, i, item
        return None

    def add_take(self, owner: str, pid: str, item_id: str, take: dict, choose: bool = True) -> dict:
        """Record what the card made for one item. `take` holds paths
        relative to the project (the manager moved the files in)."""
        with self._lock(f"{owner}/{pid}"):
            doc = self.load(owner, pid)
            found = self.find(doc, item_id)
            if not found:
                raise ProjectError("the item this take was for is gone")
            _section, _i, item = found
            take = {"id": _new_id(), "created": time.time(), **take}
            item.setdefault("takes", []).append(take)
            # A favourite keeps its place: a new version is only chosen when
            # the person has not said which one they want.
            favourite = next((i for i, t in enumerate(item["takes"]) if t.get("favorite")), None)
            if favourite is not None:
                item["chosen"] = favourite
            elif choose:
                item["chosen"] = len(item["takes"]) - 1
            doc["updated"] = time.time()
            self._write(owner, pid, doc)
            return take

    def _unlink_inside(self, base: Path, rel: str) -> None:
        """Remove a file of this project's, and only one inside it."""
        if not rel:
            return
        path = (base / rel).resolve()
        if base.resolve() in path.parents and path.is_file():
            path.unlink()

    def delete_take(self, owner: str, pid: str, item_id: str, take_id: str) -> dict:
        """One version of an item, gone from the project *and* the disk -- the
        clip or picture and the frames taken from it. The one way a take's
        files leave before the project does, and it is asked for by id: a
        save never deletes a file, because a page with an older copy of the
        project sends fewer items than there are."""
        with self._lock(f"{owner}/{pid}"):
            doc = self.load(owner, pid)
            found = self.find(doc, item_id)
            if not found:
                raise ProjectError("no such item")
            item = found[2]
            takes = item.get("takes") or []
            idx = next((i for i, t in enumerate(takes) if t.get("id") == take_id), None)
            if idx is None:
                raise ProjectError("no such version")
            take = takes.pop(idx)
            chosen = item.get("chosen", -1)
            if isinstance(chosen, int) and chosen >= 0:
                item["chosen"] = -1 if chosen == idx else chosen - (1 if chosen > idx else 0)
            base = self.dir(owner, pid)
            for key in ("file", "first", "last"):
                self._unlink_inside(base, str(take.get(key) or ""))
            self._unlink_inside(base, str((take.get("analysis") or {}).get("file") or ""))
            doc["updated"] = time.time()
            self._write(owner, pid, doc)
            return item

    def set_favorite(self, owner: str, pid: str, item_id: str, take_id: str) -> dict:
        """One version marked the favourite, the others unmarked, and it made
        the chosen one. An empty `take_id` unmarks them all."""
        with self._lock(f"{owner}/{pid}"):
            doc = self.load(owner, pid)
            found = self.find(doc, item_id)
            if not found:
                raise ProjectError("no such item")
            item = found[2]
            takes = item.get("takes") or []
            if take_id and not any(t.get("id") == take_id for t in takes):
                raise ProjectError("no such version")
            for i, t in enumerate(takes):
                t.pop("favorite", None)
                if take_id and t.get("id") == take_id:
                    t["favorite"] = True
                    item["chosen"] = i
            doc["updated"] = time.time()
            self._write(owner, pid, doc)
            return item

    @staticmethod
    def chosen_board(item: dict) -> dict | None:
        boards = item.get("boards") or []
        i = item.get("board", -1)
        if isinstance(i, int) and 0 <= i < len(boards):
            return boards[i]
        return boards[-1] if boards else None

    def add_board(self, owner: str, pid: str, item_id: str, board: dict) -> dict:
        """A storyboard frame for a shot: a still, cheap (a picture, not a
        video), to look at before the card spends half an hour on the shot --
        and, approved, the frame the shot's video starts from. The newest is
        chosen; the earlier ones are kept to go back to."""
        with self._lock(f"{owner}/{pid}"):
            doc = self.load(owner, pid)
            found = self.find(doc, item_id)
            if not found or found[0] != "shots":
                raise ProjectError("the shot this frame was for is gone")
            item = found[2]
            board = {"id": _new_id(), "created": time.time(), **board}
            item.setdefault("boards", []).append(board)
            item["board"] = len(item["boards"]) - 1
            doc["updated"] = time.time()
            self._write(owner, pid, doc)
            return board

    def set_take_field(self, owner: str, pid: str, item_id: str, take_id: str, key: str, value) -> None:
        """One server-side field of one version -- its analysis, its favourite
        mark. Never from the page's save, which cannot set a take's fields."""
        with self._lock(f"{owner}/{pid}"):
            doc = self.load(owner, pid)
            found = self.find(doc, item_id)
            take = next((t for t in (found[2].get("takes") or []) if t.get("id") == take_id), None) if found else None
            if take is None:
                raise ProjectError("no such version")
            take[key] = value
            doc["updated"] = time.time()
            self._write(owner, pid, doc)

    def delete_upload(self, owner: str, pid: str, rel: str) -> None:
        """A file the person brought, gone from the disk, and from whatever
        pointed at it: a shot's start picture, a voice's sample. A job already
        queued with it fails with "no such file" when its turn comes."""
        if not re.fullmatch(r"uploads/[A-Za-z0-9._-]+", rel or ""):
            raise ProjectError("no such file")
        with self._lock(f"{owner}/{pid}"):
            doc = self.load(owner, pid)
            before = len(doc.get("uploads") or [])
            doc["uploads"] = [u for u in doc.get("uploads") or [] if u.get("file") != rel]
            if len(doc["uploads"]) == before:
                raise ProjectError("no such file")
            for shot in doc.get("shots") or []:
                if rel in (shot.get("refs") or []):
                    shot["refs"] = [r for r in shot["refs"] if r != rel]
            for audio in doc.get("audio") or []:
                if audio.get("voice") == rel:
                    audio["voice"] = ""
            self._unlink_inside(self.dir(owner, pid), rel)
            doc["updated"] = time.time()
            self._write(owner, pid, doc)

    def add_upload(self, owner: str, pid: str, name: str, data: bytes, kind: str) -> dict:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[-60:] or "file"
        rel = f"uploads/{_new_id()}-{safe}"
        with self._lock(f"{owner}/{pid}"):
            doc = self.load(owner, pid)
            (self.dir(owner, pid) / rel).write_bytes(data)
            entry = {"file": rel, "name": name[:80], "kind": kind, "created": time.time()}
            doc.setdefault("uploads", []).append(entry)
            doc["updated"] = time.time()
            self._write(owner, pid, doc)
            return entry

    def add_render(self, owner: str, pid: str, render: dict) -> None:
        with self._lock(f"{owner}/{pid}"):
            doc = self.load(owner, pid)
            doc.setdefault("renders", []).append({"created": time.time(), **render})
            doc["updated"] = time.time()
            self._write(owner, pid, doc)


def _clean(key: str, value: Any, item: dict) -> Any:
    """One editable field, shaped: text trimmed, numbers bounded."""
    if key == "seconds":
        try:
            return max(1.0, min(600.0, round(float(value), 4)))
        except (TypeError, ValueError):
            return item.get("seconds", 5.0)
    if key == "chosen":
        n = len(item.get("takes") or [])
        return value if isinstance(value, int) and -1 <= value < n else item.get("chosen", -1)
    if key == "board":
        n = len(item.get("boards") or [])
        return value if isinstance(value, int) and -1 <= value < n else item.get("board", -1)
    if key in ("continuity", "exact"):
        return bool(value)
    if key == "start":
        try:
            return max(0.0, min(3600.0, round(float(value), 4)))
        except (TypeError, ValueError):
            return None
    if key == "bpm":
        try:
            return max(30, min(300, int(value)))
        except (TypeError, ValueError):
            return None
    if key == "kind":
        return value if value in AUDIO_KINDS else item.get("kind", "song")
    if key == "cast":
        return [str(v) for v in (value or [])[:8] if ID_RE.fullmatch(str(v))]
    if key == "refs":
        # Uploads by their relative path only; anything else is dropped.
        return [str(v) for v in (value or [])[:9] if re.fullmatch(r"uploads/[A-Za-z0-9._-]+", str(v))]
    limit = 4000 if key in ("lyrics", "text", "prompt") else 400
    return str(value or "")[:limit]
