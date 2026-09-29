"""What leaves the house has been through this first.

The self-improvement pipeline (docs/self-improvement.md) hands episodes -- what
somebody asked, what Alfred answered, what went wrong -- to a model outside the
house. Those episodes are the household's own words, so they are redacted on
the house first, in four passes, and then checked:

1. **The sanitizer's map** (`deploy/sanitize.py`, with the gitignored local
   rules): the household's names, logins, domains and addresses become the
   invented cast -- Tomi, Mora, Juana... -- the same substitutions that keep
   them out of git. Consistent everywhere, so "Tomi asked twice" still reads.
2. **What the house knows about itself**, harvested from the live state at run
   time: members, logins, phones, e-mail accounts, the family directory, the
   saved places, WhatsApp contacts, the site's hosts. Each becomes a stable
   token -- `[persona-3f2a]`, `[lugar-91c0]` -- from a keyed hash, so the same
   name is the same token on every run without a table of real names being
   kept anywhere.
3. **Shapes**: e-mail addresses, phone numbers, IPs, MACs, coordinates, long
   numbers (a login id is nine digits), and the host of any URL that is not a
   public site everybody knows.
4. **A local model** (optional) names what no list knows: a friend mentioned in
   passing, a shop, a street. Local on purpose -- the step that reads the raw
   text cannot be the one that sends it out.

Then **the guard**: every harvested value is looked for again, case- and
accent-folded, in what came out. An episode where one survived is withheld,
never sent, and counted -- the count says how many, never which.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import sys
import unicodedata
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

DEPLOY = Path(__file__).resolve().parent.parent
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

# Labels a household gives places and people that identify nobody, and that
# would otherwise turn every sentence with "casa" in it into a withheld episode.
GENERIC = {
    "casa", "home", "house", "trabajo", "work", "oficina", "office", "colegio", "escuela",
    "school", "facultad", "universidad", "gym", "gimnasio", "club", "super", "supermercado",
    "mama", "mamá", "papa", "papá", "abuela", "abuelo", "tia", "tía", "tio", "tío",
    "hermano", "hermana", "hijo", "hija", "familia", "family", "living", "cocina", "kitchen",
    "dormitorio", "bedroom", "baño", "bano", "garage", "garaje", "patio", "jardin", "jardín",
    "centro", "parque", "plaza", "hospital", "clinica", "clínica", "farmacia", "banco",
    "alfred", "house", "grupo", "group", "amigos", "friends", "yo", "me", "self",
}
# Hosts that identify nobody: a link to one of these is what it says it is.
PUBLIC_HOSTS = re.compile(
    r"(^|\.)(google|youtube|wikipedia|github|openai|anthropic|opencode|nano-gpt|"
    r"openrouter|together|huggingface|apple|microsoft|mozilla|amazon|mercadolibre|"
    r"spotify|netflix|whatsapp|instagram|facebook|x|twitter|reddit|stackoverflow|"
    r"python|npmjs|pypi|docker|ollama)\.[a-z.]+$", re.I)

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL = re.compile(r"\b(https?|wss?|rtsp)://([^/\s:\"'<>)\]]+)(:\d+)?")
# A machine named without a scheme: `box.home:5010`, `nas.lan`, `hub.local`.
# The private suffixes always; any other dotted name only with a port, which
# is how a LAN service is written and a public site rarely is.
_BARE_HOST = re.compile(
    r"(?<![\w@/.-])(?:[a-z0-9-]+\.)+(?:home|lan|local|internal|localdomain|intranet)"
    r"(?::\d{2,5})?(?![\w-])|(?<![\w@/.-])(?:[a-z0-9-]+\.)+[a-z]{2,}:\d{2,5}(?![\w-])", re.I)
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b")
_MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
_COORD = re.compile(r"-?\b\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}\b|-?\b\d{1,3}\.\d{5,}\b")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{7,}\d(?![\w.])")
_LONGNUM = re.compile(r"\b\d{7,}\b")
# What the name pass offers that is code, not a name: an environment
# variable, a file, a path, a call. Redacting those costs the evaluator the one
# thing it needs to read a technical failure ("the skill reads LIGHTS_URL"),
# and none of them says who anybody is. A host is still caught, by shape.
_TECHNICAL = re.compile(r"[_/\\:=(){}`<>@#$]|^[\w.-]+\.\w{1,5}$|^[A-Z0-9]+$")
_TOKEN = re.compile(r"\[(?:persona|lugar|contacto|host|email|telefono|ip|mac|coords|numero|nombre)"
                    r"(?:-[0-9a-f]{4})?\]")


def fold(text: str) -> str:
    """Lower case, accents off: how the guard compares."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


@dataclass
class Known:
    """Identifiers the house knows about itself, by kind. Values only ever live
    in memory for the length of a run."""
    people: set[str] = field(default_factory=set)
    places: set[str] = field(default_factory=set)
    contacts: set[str] = field(default_factory=set)
    hosts: set[str] = field(default_factory=set)
    exact: set[str] = field(default_factory=set)      # logins, phones, emails: shapes, not words

    def add(self, kind: str, value: Any) -> None:
        # A flag (`whatsapp: true`) is not an identifier, and "true" as one
        # withheld every episode: it is in every one of them as JSON.
        if isinstance(value, (bool, dict, list)) or value is None:
            return
        v = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(v) < 3 or fold(v) in GENERIC or v.isdigit() and len(v) < 5:
            return
        getattr(self, kind).add(v)
        # "Mora" is how a person is mentioned far more often than "Mora
        # Fernández": a full name brings its parts. Capitalised ones only, so
        # "de" and "la" stay words.
        if kind in ("people", "contacts") and " " in v:
            for part in v.split():
                if part[:1].isupper() and len(part) >= 3 and fold(part) not in GENERIC:
                    getattr(self, kind).add(part)

    def all(self) -> set[str]:
        return self.people | self.places | self.contacts | self.hosts | self.exact


def _rows(db: Path, sql: str) -> list[tuple]:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


def harvest(cfg: dict, state: Path) -> Known:
    """Everything identifying this house can name about itself, from the live
    config and state. A source that is missing is skipped: a house without
    WhatsApp simply has no contacts to hide."""
    k = Known()
    for m in cfg.get("members") or []:
        for f in ("display_name", "id", "ntfy_topic"):
            k.add("people", m.get(f))
        for f in ("phone", "whatsapp"):
            k.add("exact", m.get(f))
        # Who is whose: keys and labels both, since either may be a name. A
        # label that is only a relationship ("hermano") is dropped as generic.
        rels = m.get("relationships")
        for key, label in (rels.items() if isinstance(rels, dict) else []):
            k.add("people", key)
            k.add("people", label)
    site = cfg.get("site") or {}
    for f in ("name", "domain", "host"):
        k.add("hosts", site.get(f))
    for v in (cfg.get("dns") or {}).values():
        k.add("hosts", v)
    k.add("hosts", ((cfg.get("cloud") or {}).get("vps") or {}).get("host"))
    try:
        users = json.loads((state / "home-core" / "users.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        users = []
    for u in (users.values() if isinstance(users, dict) else users):
        if isinstance(u, dict):
            k.add("exact", u.get("username"))
            k.add("people", u.get("member"))
    data = state / "home-core" / "data"
    for name, full, person in _rows(data / "family.db",
                                    "SELECT display_name, full_name, person FROM family_profiles"):
        for v in (name, full, person):
            k.add("people", v)
        for part in str(full or "").split():
            if part[:1].isupper():
                k.add("people", part)
    for (other,) in _rows(data / "family.db", "SELECT other FROM family_relations"):
        k.add("people", other)
    for (name,) in _rows(data / "geo.db", "SELECT name FROM places"):
        k.add("places", name)
    for (name,) in _rows(data / "whatsapp.db", "SELECT name FROM wa_chats"):
        k.add("contacts", name)
    for addr, user in _rows(data / "profiles.db",
                            "SELECT address, imap_username FROM email_accounts"):
        k.add("exact", addr)
        k.add("exact", user)
    for (name,) in _rows(data / "profiles.db", "SELECT display_name FROM nanobot_profiles"):
        k.add("people", name)
    # Every credential in the env file, the way the publish gate reads them: a
    # reply that echoed a token must not leave either.
    conf = (cfg.get("paths") or {}).get("config")
    if conf:
        try:
            import publish_check  # noqa: PLC0415 -- deploy/publish_check.py
            for value in publish_check.env_values([Path(conf) / "smart-home-bot.env"]):
                k.add("exact", value)
        except Exception:  # noqa: BLE001
            pass
    return k


_VARIANTS = {"a": "aáàäâã", "e": "eéèëê", "i": "iíìïî", "o": "oóòöôõ", "u": "uúùüû",
             "n": "nñ", "c": "cç"}


def _bounded(value: str) -> re.Pattern:
    """*value* as a whole word, in any case and with or without its accents:
    people write "Sofia" for "Sofía" and "nautico" for "Náutico"."""
    parts = []
    for ch in fold(value):
        v = _VARIANTS.get(ch)
        parts.append(f"[{v}]" if v else re.escape(ch))
    return re.compile(rf"(?<![\w]){''.join(parts)}(?![\w])", re.IGNORECASE)


class Redactor:
    """One run's redaction: built once, applied to every episode."""

    def __init__(self, known: Known, salt: bytes,
                 sanitize: Callable[[str], str] | None = None,
                 names_model: Callable[[str], list[str]] | None = None):
        self.known = known
        self.salt = salt
        self.sanitize = sanitize
        self.names_model = names_model
        self.withheld = 0
        # Longest first, so "Luz Paula" goes before a "Paula" it contains.
        table = [(v, "persona") for v in known.people] + [(v, "lugar") for v in known.places] \
            + [(v, "contacto") for v in known.contacts] + [(v, "host") for v in known.hosts] \
            + [(v, "numero") for v in known.exact]
        self._table = [(_bounded(v), kind, v) for v, kind in
                       sorted(table, key=lambda t: -len(t[0]))]
        self._guard = sorted({fold(v) for v in known.all()}, key=len, reverse=True)

    def token(self, kind: str, value: str) -> str:
        h = hmac.new(self.salt, fold(value).encode(), hashlib.sha256).hexdigest()[:4]
        return f"[{kind}-{h}]"

    def text(self, text: str) -> str:
        """One piece of text, all four passes. The guard is separate: it judges
        a whole episode, not a field."""
        if not text:
            return text or ""
        if self.sanitize:
            text = self.sanitize(text)
        # Addresses and links before names: a name replaced inside an e-mail
        # address or a host leaves something neither pass recognises.
        text = _EMAIL.sub(lambda m: self.token("email", m.group(0)), text)
        text = _URL.sub(lambda m: m.group(0) if PUBLIC_HOSTS.search(m.group(2))
                        else f"{m.group(1)}://{self.token('host', m.group(2))}", text)
        text = _BARE_HOST.sub(lambda m: m.group(0) if PUBLIC_HOSTS.search(m.group(0).split(":")[0])
                              else self.token("host", m.group(0)), text)
        for rx, kind, value in self._table:
            text = rx.sub(self.token(kind, value), text)
        text = _MAC.sub(lambda m: self.token("mac", m.group(0)), text)
        text = _IP.sub(lambda m: self.token("ip", m.group(0)), text)
        text = _COORD.sub(lambda m: self.token("coords", m.group(0)), text)
        text = _PHONE.sub(lambda m: self.token("telefono", m.group(0))
                          if sum(c.isdigit() for c in m.group(0)) >= 8 else m.group(0), text)
        text = _LONGNUM.sub(lambda m: self.token("numero", m.group(0)), text)
        if self.names_model:
            for name in self.names_model(_TOKEN.sub(" ", text)):
                name = str(name).strip()
                if (len(name) >= 3 and fold(name) not in GENERIC and not _TOKEN.fullmatch(name)
                        and not _TECHNICAL.search(name)):
                    text = _bounded(name).sub(self.token("nombre", name), text)
        return text

    def survivors(self, blob: str) -> int:
        """How many known identifiers are still in *blob*. Folded both sides,
        word-bounded, so "Sofía" is found as "sofia" and "Ana" is not found
        inside "banana"."""
        folded = fold(blob)
        return sum(1 for v in self._guard
                   if re.search(rf"(?<![\w]){re.escape(v)}(?![\w])", folded))

    def episode(self, ep: dict, fields: Iterable[str]) -> dict | None:
        """*ep* with each text field redacted, or None when the guard finds
        anything left -- the episode is withheld and counted."""
        out = dict(ep)
        for f in fields:
            v = out.get(f)
            if isinstance(v, str):
                out[f] = self.text(v)
            elif isinstance(v, list):
                out[f] = [dict(x, text=self.text(x.get("text", ""))) if isinstance(x, dict)
                          else self.text(str(x)) for x in v]
            elif isinstance(v, dict):
                out[f] = {kk: self.text(vv) if isinstance(vv, str) else vv for kk, vv in v.items()}
        if self.survivors(json.dumps(out, ensure_ascii=False)):
            self.withheld += 1
            return None
        return out


def sanitizer() -> Callable[[str], str] | None:
    """The sanitizer's substitutions, with the household's local rules. None
    when they are missing, and the caller says so: without them the invented
    cast is not applied and the harvested pass is doing all the work."""
    try:
        import sanitize  # noqa: PLC0415 -- deploy/sanitize.py
    except Exception:  # noqa: BLE001
        return None
    if not getattr(sanitize, "HAVE_LOCAL_RULES", False):
        return None
    return lambda text: sanitize.apply_to(text)[0]


def ollama_names(url: str, model: str, timeout: float = 60.0) -> Callable[[str], list[str]]:
    """The local model's pass: the proper names in a text, verbatim, as JSON.
    Asked for names, never for a verdict, and any failure is an empty list --
    the guard after it is what makes the output safe, not this."""
    prompt = ("List every proper name in the text below that identifies a real person, "
              "a pet, a street or address, a business, a school or a specific place. "
              "Copy each exactly as written. Skip generic words, product and app names, "
              "anything in square brackets, and everything technical: code, variable and "
              "environment-variable names, file names, paths, commands, model names. "
              "Answer as JSON: {\"names\": [...]}.\n\nText:\n")

    def call(text: str) -> list[str]:
        if not text.strip():
            return []
        body = json.dumps({"model": model, "stream": False, "format": "json",
                           "think": False,
                           "messages": [{"role": "user", "content": prompt + text[:6000]}],
                           "options": {"temperature": 0}}).encode()
        try:
            req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                content = json.loads(r.read())["message"]["content"]
            names = json.loads(content).get("names") or []
            return [n for n in names if isinstance(n, str)][:50]
        except Exception:  # noqa: BLE001
            return []
    return call
