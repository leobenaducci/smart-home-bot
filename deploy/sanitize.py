#!/usr/bin/env python3
"""Replace every household-specific identifier with a neutral placeholder.

Run once when the package was extracted from a live stack, and again as a check
afterwards. Running --apply twice is a no-op: every rule is written so it cannot
match its own output. That is a property worth testing rather than assuming --
an earlier version appended a DNS suffix to `mqtt.home` as a plain literal,
which matched again on the next run and left the suffix on three times over.
`--check` covers it.

    ./deploy/sanitize.py --check     report what is left, change nothing
    ./deploy/sanitize.py --apply     rewrite the tree

What it does NOT do is remove a document whose whole content is personal -- a
household member's profile is not a search-and-replace problem, and pretending
otherwise leaves birthdates in place with the names swapped. Those files are
replaced with templates by hand; --check lists anything that still looks like
one.

**Plugins are out of scope, deliberately.** This walks ROOT and nothing above
it, and a plugin lives outside the package by construction -- the deployer
refuses to load one from inside the tree. That is the whole arrangement that
lets this package stay redistributable while a real house runs on it: the
household's own services, with the household's own names, addresses and
credentials in them, are somewhere this script never looks.

It is a boundary, not a loophole. The same file that passes unnoticed in a
plugin beside the tree produces six hits the moment it is copied inside one,
and the deployer will not deploy it from there either.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- the household's own terms, if this is the household's own checkout -----
#
# Every rule in this file names nobody. What a household needs *in addition* --
# its domains, machines, LAN ranges, members' names and login ids -- is exactly
# the data this package exists to keep out, so it lives beside the credentials
# in a file git ignores, and is folded in below when it is there.
#
# It used to be here, in plaintext, in a tracked file: a full legal name, five
# national identity numbers, personal email addresses, every nickname. And
# `--check` could never scan it, because a file holding its own search terms
# rewrites them the moment it runs over itself -- so the one file guaranteed to
# contain household data was the one file never checked, and it reported clean.
#
# A redistributed copy has no such file and needs none: its tree is already
# clean, and the shape rules below still refuse to let a real identity back in.
RULES_FILE = ROOT / "deploy" / "sanitize-rules.local.py"
HAVE_LOCAL_RULES = RULES_FILE.is_file()
_LOCAL: dict = {}
if HAVE_LOCAL_RULES:
    exec(compile(RULES_FILE.read_text(encoding="utf-8"), str(RULES_FILE), "exec"),
         _LOCAL)


SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "build", "dist",
             ".gradle", ".idea", "vendor"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2",
                 ".ttf", ".otf", ".zip", ".pdf", ".jar", ".so", ".bin",
                 ".mp3", ".wav", ".onnx", ".tflite", ".webp", ".svg"}

# Ordered: longer, more specific patterns first, so `chat.<domain>` is rewritten
# before the bare domain would swallow it.
REPLACEMENTS: list[tuple[str, str]] = _LOCAL.get("REPLACEMENTS", [])

# Applied after the literal table. Each pattern is written so it cannot match
# its own output -- that is what makes a second run a no-op.
# Every LAN alias the stack ever used. Kept because other rules and the
# --selftest fixtures refer to it, and because it is the list of names worth
# looking at when a new one appears.
LAN_ALIASES = ["hub", "compute", "storage", "mqtt", "assistant", "ollama",
               "paperless", "cameras", "homeassistant", "dns", "n8n", "ci", "whisper",
               "chat", "casa", "ntfy", "media", "horde"]

# There used to be two rules here, and both appended a DNS suffix to
# `<alias>.home`. That is normalisation, not redaction: `mqtt.home` names the
# same box either way and identifies nobody, so the rules bought no privacy and
# cost a suffix that has to resolve somewhere. It resolved nowhere -- not on the
# household this was extracted from and not on the one running it -- so every
# name they produced was a lookup that failed, and a container waiting on a
# resolver looks exactly like a service that is down. The mobile app shipped a
# build pointed at one of them and was found by somebody holding the phone.
#
# Redaction is what the literal rules above do. Names stay as the household
# writes them, and `dns:` is where they are configured.
REGEX_REPLACEMENTS: list[tuple[str, str]] = []

# Household members. Neutral placeholder names rather than "Member 1": these
# appear inside prose in skill descriptions and personas, and a slot number
# reads as a bug there. Longest first, so a longer name is not left half
# replaced by the shorter one it contains.
PEOPLE: list[tuple[str, str]] = _LOCAL.get("PEOPLE", [])

# The member *keys*. These are identifiers, not prose -- SMB share names, ntfy
# topics, room owners, login ids, test fixtures -- but they were derived from
# real first names, so they carry the same identity the display names did.
# They map onto the ids in config/home-stack.yml.
# One of those keys is also an ordinary first-person Spanish verb. The
# word-boundary rule cannot tell the login apart from the verb, so the verb
# contexts are re-protected by VERB_CONTEXTS below after the pass.
MEMBER_KEYS: list[tuple[str, str]] = _LOCAL.get("MEMBER_KEYS", [])

# Two short nicknames are deliberately NOT in any table above. Both are real,
# and neither can be replaced by rule: each is also an ordinary token elsewhere
# in the tree -- a weekday abbreviation and a printf format, a shell function
# and a German article. They were edited out of the two alias tables that held
# them by hand. RESIDUAL below cannot hunt for them either, for the same reason.

# What --check hunts for afterwards. Anything matching here is either a missed
# replacement or a file that needs replacing wholesale rather than editing.
# What `--check` looks for, and none of it names anybody.
#
# The household's own identifiers -- its domains, machine names, LAN ranges and
# every member's name and login id -- used to be listed here in plaintext, in a
# tracked file, because you cannot search for what you cannot name. That made
# this script the largest concentration of household data in the package, and
# `--check` could never scan it: a file holding its own search terms rewrites
# them the moment it runs over itself.
#
# They live in `sanitize-rules.local.py` now, which git ignores, and are folded
# in below when it is present. What stays here is shape: things that are
# personal whoever they belong to. A redistributed copy carries these and
# nothing else, and they still catch a household identifier walking back in.
RESIDUAL = list(_LOCAL.get("RESIDUAL", [])) + [
    # 01/01/2000 is the placeholder documentation examples use; anything else
    # shaped like a date of birth is a real one until proven otherwise.
    #
    # 02/03/2000 and its mirror are placeholders too, and they exist for one
    # reason: a symmetric date cannot illustrate the thing that actually
    # matters about dates here, which is that 02/03 is two different days
    # depending on who wrote it. The code that refuses to guess between them
    # has to be able to say so in a comment and prove it in a test.
    (r"(?!01/01/2000|02/03/2000|03/02/2000)"
     r"[0-3][0-9]/[0-1][0-9]/(19|20)[0-9]{2}", "a birthdate"),
    (r"\b[0-9]{7,8}-[0-9kK]\b", "a national identity number"),
    # A firmware Wi-Fi password with something in it. The package's convention
    # is an empty literal that whoever flashes the board fills in
    # (services/proxy/esp32/*/secrets.h.example), and two smart-lights sketches
    # plus a README shipped a working one anyway -- a credential nobody can
    # rotate, in a file whose whole job is to be copied onto hardware. The
    # placeholders below are the documented ones and stay.
    (r"(?!WIFI_PASSWORD[^\"\n]*\"(YOUR_WIFI_PASSWORD|CHANGEME|your-password|)\")"
     r"WIFI_PASSWORD[^\"\n]*\"[^\"]+\"", "a Wi-Fi password"),
    (r"[a-zA-Z0-9._%+-]+@(gmail|hotmail|outlook|yahoo)\.[a-z]+", "a personal email address"),
    # Device credentials. Camera RTSP logins are stored in the clear because the
    # stream URL needs them, so a committed config file is a committed password
    # -- and the first pass of this script did not look for one.
    # The placeholder list stays case-SENSITIVE even though --check now folds
    # case for the name patterns: under IGNORECASE the lookahead also swallows
    # `Secret...`, `Test...`, `Example...` and `Your_...`, which are password
    # prefixes, not placeholders. A real `"password": "Secretstuff99"` went from
    # flagged to silently skipped.
    (r'"password"\s*:\s*"(?!(?-i:CHANGE_ME|your_|test|secret|example|changeme|\.\.\.))'
     r'[^"]{6,}"', "a device password"),
    (r'"password_hash"\s*:\s*"[0-9a-f]{32,}"', "a stored password hash"),
    (r"rtsp://[^\s\"']*:[^\s\"'@/{]+@", "credentials embedded in an RTSP URL"),
]


def _rut_check_digit(body: str) -> str:
    """The verifier for a Chilean RUT, which is what makes this checkable.

    The enumerated form -- five literal numbers in a tracked file -- was there
    because "ports, timestamps and DB ids are all 8-9 digit numbers too", and
    that reasoning was sound. But a RUT carries its own checksum, so the shape
    can be validated instead of the values listed: this catches every
    member's id, including the ones this package was never told about, and
    names none of them.
    """
    total, factor = 0, 2
    for digit in reversed(body):
        total += int(digit) * factor
        factor = 2 if factor == 7 else factor + 1
    rest = 11 - (total % 11)
    return {11: "0", 10: "k"}.get(rest, str(rest))


def looks_like_a_national_id(token: str) -> bool:
    """An 8-9 digit run whose last character is its own valid check digit."""
    token = token.lower()
    if not (8 <= len(token) <= 9) or not token[:-1].isdigit():
        return False
    if not (token[-1].isdigit() or token[-1] == "k"):
        return False
    # 11111111-1 and friends: a repeated digit validates but is a placeholder.
    if len(set(token[:-1])) == 1:
        return False
    return _rut_check_digit(token[:-1]) == token[-1]


# Compiled once, for the same reason PEOPLE_PATTERNS is: cmd_check() runs every
# one of these against every line of every candidate file -- ~235k lines here,
# so a per-line re.search(str, ...) pays the pattern-cache lookup a few million
# times per run.
RESIDUAL_PATTERNS = [(re.compile(pattern, re.IGNORECASE), label)
                     for pattern, label in RESIDUAL]


UPSTREAM = {
    "services/nanobot/SECURITY.md",
    "services/nanobot/CONTRIBUTING.md",
    "services/nanobot/docs/chat-apps.md",
}


# Third-party prose that this package vendors but does not own: upstream nanobot
# docs and any licence or attribution file. A word-boundary surname pass does not
# belong in someone else's copyright line -- "Copyright (c) 2024 J. Muller" is a
# legal attribution, not a household member, and rewriting it to "J. Roe" is a
# compliance problem rather than a redaction. --check still reads them.
def _is_vendored(rel: Path) -> bool:
    if str(rel) in UPSTREAM:
        return True
    stem = rel.name.upper()
    return stem.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")) or \
        "THIRD_PARTY" in stem


def _git_ignored_set() -> set:
    """Everything `git check-ignore` claims, resolved once.

    Shelling out per file made a full check take minutes; one call with
    --stdin over the candidate list is a single process. Outside a git
    checkout there is nothing to ignore, and the empty set is the right
    answer rather than an error.
    """
    try:
        listing = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--others", "--ignored",
             "--exclude-standard", "--directory"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    if listing.returncode != 0:
        return set()
    out = set()
    for line in listing.stdout.splitlines():
        line = line.strip()
        if line:
            out.add((ROOT / line).resolve())
    return out


_IGNORED = None


def is_git_ignored(path: Path) -> bool:
    global _IGNORED
    if _IGNORED is None:
        _IGNORED = _git_ignored_set()
    if not _IGNORED:
        return False
    resolved = path.resolve()
    if resolved in _IGNORED:
        return True
    # `--directory` collapses an ignored directory to one entry, so a file
    # inside one is ignored without being listed itself.
    return any(parent in _IGNORED for parent in resolved.parents)


def candidate_files(rewriting: bool = False) -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if is_git_ignored(path):
            # Nothing git ignores can leave this machine, and some of it cannot
            # help containing local paths: the deploy log records every rsync
            # target, so a single deploy made `--check` report the deploying
            # user's home directory as a residual hit for ever after. The point
            # of this check is what a clone would carry.
            continue
        # This script names every value it replaces, so rewriting it would
        # destroy the mapping it exists to document.
        if path.resolve() == Path(__file__).resolve():
            continue
        # The translation catalogues are prose in seven languages and some of it
        # collides with the names above -- "Luci" is Italian for "lights", and
        # one run shipped `"portal.tiles.lights": "Controllo delle kai"`.
        #
        # Skipped for --apply only. --check must still read them: a catalogue
        # can hold an email address or an IP just as easily as code can, and
        # making the whole directory invisible to the checker trades one bug for
        # a blind spot. Matched at any depth, because each service also carries
        # its own copy (services/nanobot/webui/src/i18n).
        if rewriting and "i18n" in path.parts:
            continue
        if rewriting and _is_vendored(path.relative_to(ROOT)):
            continue
        files.append(path)
    return files


# Sentence shapes where that word is the verb, not the login. Applied after
# the member-key pass to undo exactly these; one of them shipped as "No user1
# ni escribo" in a persona before this existed.
#
# Every entry repairs `user1`, never the placeholder name. The name is in
# NEVER_LOWER, so no pass in this file can turn the verb into the placeholder
# -- a repair keyed on the placeholder is unreachable, and it is not harmless:
# `text.replace` is unanchored, so it also rewrites a sentence like "No Alex ni
# Sam pudieron venir" ("neither Alex nor Sam could come").
# Alex is a person in this package's own prose, so that shape is expected to
# occur. `user1` cannot appear in a Spanish sentence by accident; `Alex` can.
VERB_CONTEXTS: list[tuple[str, str]] = _LOCAL.get("VERB_CONTEXTS",
                                                  _LOCAL.get("LEO_VERB_CONTEXTS", []))

# Two names are never lower-cased, because the lowercase form is an ordinary
# Spanish word and the replacement is not reversible once it lands: a
# first-person verb, and an adjective ("fue ___ casualidad") that this once
# turned into nonsense.
# Both still get replaced in their capitalised and upper-case forms, which is
# how the surname and the login actually appear; the lowercase e-mail local part
# A personal email is handled by the literal table, which now runs first.
# Everything else gets all three cases, because a name
# shouts in an MQTT topic or a rule example and
# whispers in the alias tables that map what somebody is *called* to a member id
# whispers in an address. A table keyed on one casing saw neither.
NEVER_LOWER: set[str] = _LOCAL.get("NEVER_LOWER", set())


def _name_variants(old: str, new: str) -> list[tuple[str, str]]:
    """The spellings of one name to replace, deduplicated, exact form first."""
    pairs = [(old, new), (old.upper(), new.upper())]
    if old not in NEVER_LOWER:
        pairs.append((old.lower(), new.lower()))
    seen: set[str] = set()
    out = []
    for variant, replacement in pairs:
        if variant not in seen:
            seen.add(variant)
            out.append((variant, replacement))
    return out


# Compiled once rather than per file. apply_to() runs over every file in the
# tree, and these are ~90 patterns.
# Word-boundary only: a short name is letters that appear inside other words, and
# an unbounded replace turned a name into a half-substituted one, and
# "completar" into
# "compAlextar" the first time this ran.
PEOPLE_PATTERNS = [
    (re.compile(rf"\b{re.escape(variant)}\b"), replacement)
    for old, new in PEOPLE
    for variant, replacement in _name_variants(old, new)
]
# Three cases, because these appear as lowercase identifiers, as uppercase test
# constants, and occasionally capitalised mid-sentence.
MEMBER_KEY_PATTERNS = [
    (re.compile(rf"\b{re.escape(variant)}\b"), replacement)
    for old, new in MEMBER_KEYS
    for variant, replacement in ((old, new), (old.upper(), new.upper()),
                                 (old.capitalize(), new.capitalize()))
]


def apply_to(text: str) -> tuple[str, int]:
    changes = 0
    for old, new in REPLACEMENTS:
        if old in text:
            changes += text.count(old)
            text = text.replace(old, new)
    for pattern, replacement in REGEX_REPLACEMENTS:
        text, n = re.subn(pattern, replacement, text)
        changes += n
    # The name passes run AFTER the literal table, never before. Several
    # literals contain a household name -- a personal address, the household
    # domains -- and a name pass that goes first rewrites them token by token
    # into a half-placeholder address and
    # `chat.doe.com`, which no later rule matches. The addresses survive with a
    # plausible-looking new spelling, the five domain rules and the email rule
    # go dead, and the result is stable across runs, so --selftest's idempotency
    # check still passes while every one of those rules does nothing.
    for pattern, replacement in PEOPLE_PATTERNS:
        text, n = pattern.subn(replacement, text)
        changes += n
    for pattern, replacement in MEMBER_KEY_PATTERNS:
        text, n = pattern.subn(replacement, text)
        changes += n
    for wrong, right in VERB_CONTEXTS:
        # Counted like every other pass. cmd_apply() writes on `updated !=
        # original`, so a file that only needed a verb repair was written while
        # contributing nothing to the total -- "rewrote 0 occurrence(s) across
        # 1 file(s)" is a report that argues with itself.
        n = text.count(wrong)
        if n:
            text = text.replace(wrong, right)
            changes += n
    return text, changes


def cmd_apply() -> int:
    # Nothing to replace *with*: the terms are the household's own and live in
    # a file git ignores. Refusing is the only honest answer -- rewriting with
    # an empty table changes nothing and reports success, which would read as
    # "the tree is already sanitised" on a tree full of somebody's name.
    if not HAVE_LOCAL_RULES:
        out = (f"--apply needs {RULES_FILE.relative_to(ROOT)}, which is not "
               f"here.\n"
               f"    That file holds the identifiers to replace -- names, "
               f"domains, login ids -- and\n"
               f"    git ignores it on purpose. A redistributed copy has none "
               f"and needs none: its\n"
               f"    tree is already clean, and --check still works on the "
               f"shapes that name nobody.")
        print(out, file=sys.stderr)
        return 2
    touched = 0
    total = 0
    for path in candidate_files(rewriting=True):
        try:
            original = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        updated, changes = apply_to(original)
        # Compare the text, not the counter. A rule can "match" and substitute
        # identical bytes -- a name whose placeholder shares its casing, say --
        # which rewrote the file, counted a change, and made a second run look
        # non-idempotent when the tree had not moved at all.
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            touched += 1
            total += changes
    print(f"rewrote {total} occurrence(s) across {touched} file(s)")
    return 0




def tracked_databases() -> list[Path]:
    """Every SQLite file git is carrying, as absolute paths.

    Tracked only. A developer's scratch copy in the working tree is their
    business; what ships is not.
    """
    try:
        out = subprocess.run(["git", "ls-files", "-z", "*.db"], cwd=ROOT,
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [ROOT / rel for rel in out.stdout.split("\0") if rel]


def database_findings() -> list[tuple[Path, str, int]]:
    """Rows in a database this package ships. There should be none.

    The rest of this file reads text and gives up on anything that is not
    UTF-8 -- which is every SQLite file, so the shipped seed databases were
    invisible to the one tool whose job is finding household data in them.
    They were not empty. `geo.db` carried two real place names with their
    latitude and longitude and four geofence reminders written against them,
    and `family.db` carried five profile rows that would have appeared in a
    fresh install as phantom members.

    The schema is the seed and the rows are not: `_FAMILY_SEED` in
    `services/home-core/local/app.py` is `{}` with a comment explaining why,
    and this is the same rule enforced one layer down.
    """
    findings = []
    for path in tracked_databases():
        if not path.is_file():
            continue
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                n = db.execute(f"SELECT COUNT(*) FROM '{table}'").fetchone()[0]
                if n:
                    findings.append((path.relative_to(ROOT), table, n))
            db.close()
        except sqlite3.Error:
            continue          # not a database, or one this account cannot read
    return findings


def cmd_check() -> int:
    findings: dict[str, list[tuple[Path, int, str]]] = {}
    # No `rewriting=True` here: --check reads the translation catalogues that
    # --apply refuses to rewrite. Skipping them in both directions is how a
    # corrupted `"Controllo delle kai"` became invisible to the tool that exists
    # to find exactly that.
    for path in candidate_files():
        if str(path.relative_to(ROOT)) in UPSTREAM:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, PermissionError):
            continue
        for lineno, line in enumerate(lines, 1):
            for pattern, label in RESIDUAL_PATTERNS:
                if pattern.search(line):
                    findings.setdefault(label, []).append(
                        (path.relative_to(ROOT), lineno, line.strip()[:100])
                    )

    rows = database_findings()
    if not findings and not rows:
        print("clean: no household identifiers found, and the shipped "
              "databases are empty")
        return 0

    if rows:
        print(f"\nrows in a shipped database — {len(rows)} table(s)")
        for rel, table, n in rows[:12]:
            print(f"  {rel}: {table} has {n} row(s)")
        if len(rows) > 12:
            print(f"  … and {len(rows) - 12} more")
        print("  the schema is the seed; the rows are somebody's household.")
    if not findings:
        return 1

    for label, hits in findings.items():
        print(f"\n{label} — {len(hits)} hit(s)")
        for rel, lineno, snippet in hits[:12]:
            print(f"  {rel}:{lineno}: {snippet}")
        if len(hits) > 12:
            print(f"  … and {len(hits) - 12} more")
    print(f"\n{sum(len(h) for h in findings.values())} residual hit(s)")
    return 1


# Strings that must survive a pass untouched. Every one of these is a real bug
# this script shipped: an unanchored `\.home\b` rewrote a pathlib call, a JVM
# system property, this package's own config keys and a translation key, all of
# which merely happen to end in `.home`.
SELFTEST_UNCHANGED = [
    "No leo ni escribo nada de nadie",
    # "No X ni Y" is "neither X nor Y", and Alex and Sam are people in this
    # package's own prose. A repair rule keyed on "No alex ni" rewrote this
    # sentence into "No leo ni Sam pudieron venir".
    "No Alex ni Sam pudieron venir",
    "Path.home()",
    'System.getProperty("user.home")',
    "{services.home-core.port}",
    '"common.home": "Home"',
    "mqtt.home",
    "/home/homestack",
    "homestack",
]

# And the replacements that must still happen.
# The domain and email rules are here because they are the ones that broke: a
# name pass that ran before the literal table turned the household domain and
# a personal address into half-substituted placeholders, and
# nothing noticed, because the old samples covered only login ids, hosts and IPs
# and the wrong output was perfectly idempotent.
SELFTEST_CHANGED: list[tuple[str, str]] = _LOCAL.get("SELFTEST_CHANGED", [])


def cmd_selftest() -> int:
    failures = []

    for sample in SELFTEST_UNCHANGED:
        result, _ = apply_to(sample)
        if result != sample:
            failures.append(f"rewrote something it should not: {sample!r} -> {result!r}")

    for sample, expected in SELFTEST_CHANGED:
        result, _ = apply_to(sample)
        if result != expected:
            failures.append(f"{sample!r} -> {result!r}, expected {expected!r}")

    # Idempotency, as a property rather than a claim.
    for sample, _ in SELFTEST_CHANGED:
        once, _ = apply_to(sample)
        twice, _ = apply_to(once)
        if once != twice:
            failures.append(f"not idempotent: {sample!r} -> {once!r} -> {twice!r}")

    # The i18n guard, as a property rather than a comment. `Luci` is a real
    # nickname AND the Italian for "lights", so apply_to() cannot tell them
    # apart -- the protection is that --apply never opens those files, and the
    # protection is worth nothing if a later refactor quietly drops it. This is
    # the check that would have caught `"Controllo delle kai"` before it shipped.
    rewritable = {p.relative_to(ROOT) for p in candidate_files(rewriting=True)}
    readable = {p.relative_to(ROOT) for p in candidate_files()}
    leaked = sorted(p for p in rewritable if "i18n" in p.parts)
    if leaked:
        failures.append(f"--apply can rewrite translation catalogues: {leaked[:3]}")
    unread = sorted(p for p in readable if "i18n" in p.parts)
    if not unread:
        failures.append("--check cannot see the translation catalogues at all")
    # The skip is not there for one collision -- it is there because the
    # catalogues are translated prose in seven languages and the placeholder
    # names are ordinary words somewhere. `Luci` was Italian for "lights" and
    # got corrupted once; the rule that did it has been retired, and the skip
    # stays for the next one. Asserting a *specific* collision would tie this
    # test to whichever word happens to collide today, so assert the property:
    # a rule aimed at household prose must not fire on a catalogue value.
    sample = "Controllo delle luci"          # the collision that taught us this
    if apply_to(sample)[0] != sample:
        failures.append(f"a rule still rewrites catalogue prose: {sample!r}")

    if failures:
        for line in failures:
            print(f"  FAIL {line}")
        return 1
    print(f"selftest ok: {len(SELFTEST_UNCHANGED)} guarded, "
          f"{len(SELFTEST_CHANGED)} replaced, idempotent, "
          f"{len(unread)} catalogue file(s) read-only")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="rewrite the tree")
    parser.add_argument("--check", action="store_true", help="report only")
    parser.add_argument("--selftest", action="store_true",
                        help="verify the rules themselves")
    args = parser.parse_args()
    if args.selftest:
        return cmd_selftest()
    if args.apply:
        return cmd_apply()
    return cmd_check()


if __name__ == "__main__":
    sys.exit(main())
