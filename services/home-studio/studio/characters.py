"""Characters: who appears, how they look, how they are, how they sound.

A character starts in a project and its scope only widens, when the person
says so:

    project   <root>/<login>/<project>/characters/<id>/   that project only
    person    <root>/<login>/.characters/<id>/            any project of theirs
    family    <root>/@family/characters/<id>/             anyone's, any project

`@` cannot be in a login, so the family's folder can never be somebody's.
Widening moves the folder -- pictures, voice and all -- and keeps the id, so a
project that already cast the character still finds it. Nothing narrows: once
the family has it, somebody else's film may be using it. The creator and a
parent can edit or delete a family character; anyone can cast it.

What is read from a character: `look` goes into every storyboard frame and
shot it is cast in, `personality` into the lines the assistant writes for it,
and `voice` (a sample) is what its lines are spoken with.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import media
from .projects import ID_RE, LOGIN_RE, ProjectError

SCOPES = ("project", "person", "family")
FAMILY_DIR = "@family"
EDITABLE = ("name", "look", "personality", "voice_text", "portrait")


class CharacterError(ProjectError):
    pass


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class Characters:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._guard = threading.Lock()

    # -- where each scope lives ---------------------------------------------
    def _base(self, scope: str, owner: str, pid: str = "") -> Path:
        if scope == "family":
            return self.root / FAMILY_DIR / "characters"
        if not LOGIN_RE.fullmatch(owner or ""):
            raise CharacterError("unknown person")
        if scope == "person":
            return self.root / owner / ".characters"
        if scope == "project" and ID_RE.fullmatch(pid or ""):
            return self.root / owner / pid / "characters"
        raise CharacterError("no such scope")

    def _find(self, cid: str, owner: str, pid: str = "") -> tuple[Path, dict] | None:
        """A character this person may see: the project's, their own, the
        family's -- in that order."""
        if not ID_RE.fullmatch(cid or ""):
            return None
        for scope in SCOPES:
            try:
                d = self._base(scope, owner, pid) / cid
            except CharacterError:
                continue
            f = d / "character.json"
            if f.is_file():
                try:
                    return d, json.loads(f.read_text())
                except ValueError:
                    return None
        return None

    def _write(self, d: Path, doc: dict) -> None:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "character.json.tmp"
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
        tmp.replace(d / "character.json")

    @staticmethod
    def may_edit(doc: dict, login: str, admin: bool) -> bool:
        return doc.get("creator") == login or (doc.get("scope") == "family" and admin)

    # -- the documents --------------------------------------------------------
    def get(self, cid: str, owner: str, pid: str = "") -> dict:
        found = self._find(cid, owner, pid)
        if not found:
            raise CharacterError("no such character")
        return found[1]

    def list(self, owner: str, pid: str = "") -> list[dict]:
        """Every character this person can cast in this project: its own,
        theirs, the family's."""
        out = []
        for scope in SCOPES:
            try:
                base = self._base(scope, owner, pid)
            except CharacterError:
                continue
            for f in sorted(base.glob("*/character.json")):
                try:
                    out.append(json.loads(f.read_text()))
                except ValueError:
                    continue
        return out

    def create(self, owner: str, pid: str, fields: dict) -> dict:
        now = time.time()
        doc = {"id": _new_id(), "scope": "project", "creator": owner, "project": pid,
               "name": "", "look": "", "personality": "", "voice_text": "", "portrait": -1,
               "pictures": [], "voice": "", "created": now, "updated": now}
        doc.update(self._clean(fields, doc))
        if not doc["name"]:
            raise CharacterError("a character needs a name")
        self._write(self._base("project", owner, pid) / doc["id"], doc)
        return doc

    def update(self, cid: str, owner: str, pid: str, fields: dict, admin: bool = False) -> dict:
        with self._guard:
            found = self._find(cid, owner, pid)
            if not found:
                raise CharacterError("no such character")
            d, doc = found
            if not self.may_edit(doc, owner, admin):
                raise CharacterError("only whoever made this character can change it")
            doc.update(self._clean(fields, doc))
            doc["updated"] = time.time()
            self._write(d, doc)
            return doc

    def widen(self, cid: str, owner: str, pid: str, admin: bool = False) -> dict:
        """One step wider: project -> person -> family. The folder moves and
        the id stays, so every project that cast it keeps finding it."""
        with self._guard:
            found = self._find(cid, owner, pid)
            if not found:
                raise CharacterError("no such character")
            d, doc = found
            if not self.may_edit(doc, owner, admin):
                raise CharacterError("only whoever made this character can share it")
            nxt = {"project": "person", "person": "family"}.get(doc["scope"])
            if not nxt:
                raise CharacterError("the whole family has it already")
            target = self._base(nxt, doc["creator"]) / cid
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(target))
            doc.update(scope=nxt, updated=time.time())
            doc.pop("project", None)
            self._write(target, doc)
            return doc

    def delete(self, cid: str, owner: str, pid: str, admin: bool = False) -> None:
        with self._guard:
            found = self._find(cid, owner, pid)
            if not found:
                raise CharacterError("no such character")
            d, doc = found
            if not self.may_edit(doc, owner, admin):
                raise CharacterError("only whoever made this character can delete it")
            shutil.rmtree(d, ignore_errors=True)

    # -- its files ------------------------------------------------------------
    def file(self, cid: str, owner: str, pid: str, rel: str) -> Path:
        found = self._find(cid, owner, pid)
        if not found:
            raise CharacterError("no such character")
        base = found[0].resolve()
        path = (base / rel).resolve()
        if base not in path.parents or not path.is_file():
            raise CharacterError("no such file")
        return path

    def add_file(self, cid: str, owner: str, pid: str, name: str, data: bytes, kind: str,
                 admin: bool = False) -> dict:
        """A picture of the character, or the sample its voice is cloned from
        (one voice: a new sample replaces the old).

        Pictures are opened and saved again as PNG: a family character is
        seen by everybody, so a "picture" that is really a page or a script
        must never be stored as one. Samples are kept as WAV -- one recorded
        in the page is WebM/Opus, which the voice cloner does not read -- and
        converted before the lock is taken, so a long one holds nobody up."""
        found = self._find(cid, owner, pid)
        if not found:
            raise CharacterError("no such character")
        if not self.may_edit(found[1], owner, admin):
            raise CharacterError("only whoever made this character can change it")
        d, stem = found[0], _new_id()
        if kind == "voice":
            rel = f"voice/{stem}.wav"
            raw = d / f"voice/{stem}.upload"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(data)
            try:
                media.to_wav(raw, d / rel, rate=44100, channels=1)
            except media.MediaError as exc:
                raise CharacterError(f"that recording could not be read: {exc}") from None
            finally:
                raw.unlink(missing_ok=True)
        else:
            rel = f"pictures/{stem}.png"
            try:
                from PIL import Image  # noqa: PLC0415
                import io  # noqa: PLC0415
                img = Image.open(io.BytesIO(data))
                img.load()
                (d / rel).parent.mkdir(parents=True, exist_ok=True)
                img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB").save(d / rel, "PNG")
            except Exception:                                  # noqa: BLE001 -- not a picture is the answer
                raise CharacterError("that is not a picture") from None
        with self._guard:
            found = self._find(cid, owner, pid)
            if not found:
                (d / rel).unlink(missing_ok=True)
                raise CharacterError("no such character")
            d, doc = found
            if kind == "voice":
                old = doc.get("voice")
                if old:
                    (d / old).unlink(missing_ok=True)
                doc["voice"] = rel
            else:
                doc.setdefault("pictures", []).append(rel)
                if doc.get("portrait", -1) < 0:
                    doc["portrait"] = len(doc["pictures"]) - 1
            doc["updated"] = time.time()
            self._write(d, doc)
            return doc

    def add_picture_file(self, cid: str, owner: str, pid: str, src: Path, admin: bool = False) -> dict:
        """A picture the card drew for it (a portrait), filed as the person who
        asked for it -- who must still be allowed to change the character."""
        return self.add_file(cid, owner, pid, src.name, src.read_bytes(), "picture", admin)

    def store_voice_test(self, cid: str, owner: str, pid: str, src: Path) -> str:
        """A line spoken in the character's voice, kept in its folder -- one per
        person who tried it, so it plays from any project and nobody's try
        replaces anybody else's."""
        with self._guard:
            found = self._find(cid, owner, pid)
            if not found:
                raise CharacterError("no such character")
            d, doc = found
            rel = f"voice/test-{owner}{src.suffix.lower()}"
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(d / rel))
            doc.setdefault("voice_tests", {})[owner] = rel
            self._write(d, doc)
            return rel

    def set_field(self, cid: str, owner: str, pid: str, key: str, value) -> None:
        """A field the studio itself sets (the last voice test), no rights
        check: the job that sets it was the owner's."""
        with self._guard:
            found = self._find(cid, owner, pid)
            if found:
                found[1][key] = value
                self._write(found[0], found[1])

    # -- what the rest of the studio reads -------------------------------------
    def describe(self, ids: list[str], owner: str, pid: str) -> str:
        """The cast of a shot, as a prompt reads it: each name and look."""
        parts = []
        for cid in ids or []:
            found = self._find(str(cid), owner, pid)
            if found and found[1].get("look"):
                parts.append(f"{found[1]['name']}: {found[1]['look']}")
        return "; ".join(parts)

    @staticmethod
    def _clean(fields: dict, doc: dict) -> dict:
        out = {}
        for key in EDITABLE:
            if key not in fields:
                continue
            value = fields[key]
            if key == "portrait":
                n = len(doc.get("pictures") or [])
                out[key] = value if isinstance(value, int) and -1 <= value < n else doc.get("portrait", -1)
            else:
                limit = {"name": 60, "look": 800, "personality": 1200, "voice_text": 600}[key]
                out[key] = str(value or "").strip()[:limit]
        return out
