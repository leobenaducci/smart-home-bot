#!/usr/bin/env python3
"""Fill in `members:` from a directory of USER.md profiles.

    ./deploy/import_profiles.py <dir>            what it would change
    ./deploy/import_profiles.py <dir> --apply    change it

An install carried across from an older setup arrives with an empty admin
page and five assistants that know nothing about anybody -- while the profiles
those assistants spent years being taught sit in the old machine's workspaces.
`build_member_profile` writes that file *out* from the config; this reads one
back *in*, so the round trip closes and the admin page becomes the place those
facts live.

**Matched by name, never by position.** The old directory is `user1..user5`
and so is the new one, and they are not the same people -- old `user1` is the
third member here. Copying an index across is how a child gets handed a
parent's assistant, so this joins on `**Name**` against `display_name` and
refuses a file it cannot place.

**National identity numbers are dropped.** They are in some of these profiles;
they may not be in this config, and `deploy/sanitize.py` is the record of why.
Everything else is carried over, because the point is that nothing the
household taught it is lost.

**Nothing already answered is overwritten.** A field with something in it is
left alone and reported, so this can be re-run after the old machine is frozen
without undoing an edit somebody made here in the meantime.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy as D  # noqa: E402

# `## Topics of Interest` is the one section with a field of its own; the rest
# of what a profile carries has no column on the admin page, so it is kept as
# written under `notes` rather than thrown away for want of somewhere to put
# it. Order is the order it is written back in.
HOBBY_SECTIONS = ("Topics of Interest",)
SKIP_SECTIONS = ("Basic Information",)

# A line whose whole point is an identity number -- `- **RUT**: 1.234.567-8`.
# Dropped: there is nothing else on it.
_ID_FIELD = re.compile(
    r"^\s*[-*]?\s*\*{0,2}(RUT|DNI|NIE|SSN|CI|CURP|NIF)\*{0,2}\s*:", re.I)
# One embedded in a line that says something else -- `father of X (RUT: ...)`.
# Cut out rather than taken as a reason to drop the sentence around it, which
# is how the only statement of who somebody's children are went missing the
# first time this ran.
_ID_INLINE = re.compile(
    r"\s*\(?\s*(?:RUT|DNI|NIE|SSN|CI|CURP|NIF)?\s*:?\s*"
    r"(?:[0-9]{1,2}\.[0-9]{3}\.[0-9]{3}-[0-9kK]|[0-9]{7,8}-[0-9kK])\s*\)?", re.I)

# `- **Family**: Ana (dad), Bo (mom), Cy (sister)` -- the old profiles'
# own way of saying it, and the only structured relationship in the file.
_FAMILY = re.compile(r"^\s*[-*]\s*\*\*Family\*\*\s*:\s*(.+)$", re.I | re.M)
_FAMILY_ENTRY = re.compile(r"([^,(]+?)\s*\(([^)]+)\)")

_NAME = re.compile(r"^\s*[-*]\s*\*\*Name\*\*\s*:\s*(.+)$", re.I | re.M)
_BIRTH = re.compile(r"^\s*[-*]\s*\*\*Birthdate\*\*\s*:\s*(.+)$", re.I | re.M)


def sections(text: str) -> list[tuple[str, str]]:
    """`[(heading, body), ...]` for every `##`/`###`, in order.

    Sub-headings are kept as sections of their own rather than folded into
    their parent: `### Communication Style` is where a household writes how it
    wants to be spoken to, and it is the part most worth not losing.
    """
    out, heading, body = [], None, []
    for line in text.splitlines():
        match = re.match(r"^(#{2,3})\s+(.*?)\s*$", line)
        if match:
            if heading is not None:
                out.append((heading, "\n".join(body).strip()))
            heading, body = match.group(2), []
        elif heading is not None:
            body.append(line)
    if heading is not None:
        out.append((heading, "\n".join(body).strip()))
    return out


def _clean(body: str) -> str:
    """Drop identity numbers, the template's own footer, and empty checkboxes.

    `- [ ] Intermediate` is a box nobody ticked. Carrying it over makes the
    assistant read a list of things that are *not* true about somebody, which
    is worse than saying nothing.
    """
    kept = []
    for line in body.splitlines():
        if _ID_FIELD.match(line):
            continue
        line = _ID_INLINE.sub("", line)
        if re.match(r"^\s*[-*]\s*\[\s\]\s", line):
            continue
        if line.strip().startswith("*Edit this file") or line.strip() == "---":
            continue
        kept.append(re.sub(r"^(\s*[-*]\s*)\[x\]\s*", r"\1", line, flags=re.I))
    return "\n".join(kept).strip()


def bullets(body: str) -> list[str]:
    return [re.sub(r"^\s*[-*]\s*", "", ln).strip()
            for ln in body.splitlines() if re.match(r"^\s*[-*]\s+\S", ln)]


def parse(text: str) -> dict:
    """One USER.md -> the fields `members:` has room for, plus `notes`."""
    name = _NAME.search(text)
    out = {"name": (name.group(1).strip() if name else ""),
           "hobbies": [], "notes": [], "birthdate": "", "family": {}}

    birth = _BIRTH.search(text)
    if birth:
        out["birthdate"] = normalise_date(birth.group(1).strip())

    family = _FAMILY.search(text)
    if family:
        for who, rel in _FAMILY_ENTRY.findall(family.group(1)):
            out["family"][who.strip()] = rel.strip()

    for heading, body in sections(text):
        body = _clean(body)
        if not body or heading in SKIP_SECTIONS:
            continue
        if heading in HOBBY_SECTIONS:
            out["hobbies"] += bullets(body)
        elif heading == "Special Instructions":
            # No heading: it is already what `notes` means, and
            # `build_member_profile` writes it back under that heading.
            out["notes"].append(body)
        else:
            out["notes"].append(f"## {heading}\n\n{body}")
    return out


def normalise_date(raw: str) -> str:
    """Whatever the old file wrote, as ISO. Empty when it cannot be read.

    `02/03/2000` is the form these carry. Day-first is not a guess here: it is
    what the machine that wrote them used, and a value that does not parse is
    left out rather than turned into a plausible wrong birthday.
    """
    raw = raw.strip()
    for pattern in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            import datetime
            return datetime.datetime.strptime(raw, pattern).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def match_member(cfg: dict, name: str) -> dict | None:
    """The member this profile is about, by name and nothing else."""
    if not name:
        return None
    wanted = name.strip().casefold()
    for member in (cfg.get("members") or []):
        display = str(member.get("display_name") or "").strip()
        if display.casefold() == wanted:
            return member
        # The old files use the short name the house uses; a config that
        # carries the full one still matches on its first word.
        if display.split()[:1] and display.split()[0].casefold() == wanted:
            return member
    return None


def plan(cfg: dict, folder: Path) -> tuple[list, list]:
    """(changes, complaints). Changes are `(member, field, value)`."""
    changes, complaints = [], []
    by_name = {str(m.get("display_name") or "").strip().casefold(): m
               for m in (cfg.get("members") or [])}
    for path in sorted(folder.rglob("USER.md")):
        parsed = parse(path.read_text(encoding="utf-8", errors="replace"))
        member = match_member(cfg, parsed["name"])
        if member is None:
            complaints.append(
                f"{path}: no member called {parsed['name'] or '(unnamed)'} -- "
                f"skipped, because matching these by position is how somebody "
                f"gets handed another person's assistant")
            continue
        for field, value in (("birthdate", parsed["birthdate"]),
                             ("hobbies", "\n".join(parsed["hobbies"])),
                             ("notes", "\n\n".join(parsed["notes"]))):
            if not value:
                continue
            if str(member.get(field) or "").strip():
                complaints.append(
                    f"{member['id']}: {field} already answered here, left alone")
                continue
            changes.append((member, field, value))
        wanted = {}
        for who, rel in parsed["family"].items():
            other = by_name.get(who.strip().casefold())
            if other is not None and other["id"] != member["id"]:
                wanted[other["id"]] = rel
        existing = dict(member.get("relationships") or {})
        added = {k: v for k, v in wanted.items() if not existing.get(k)}
        if added:
            changes.append((member, "relationships", {**existing, **added}))
    return changes, complaints


def _yaml():
    """The round-trip parser, so importing does not strip the config's
    comments. Plain yaml is the fallback, and it does."""
    try:
        from ruamel.yaml import YAML
    except ImportError:
        return None
    parser = YAML()
    parser.preserve_quotes = True
    return parser


def load_config() -> dict:
    parser = _yaml()
    if parser is None:
        import yaml
        return yaml.safe_load(D.CONFIG.read_text(encoding="utf-8")) or {}
    with D.CONFIG.open(encoding="utf-8") as fh:
        return parser.load(fh) or {}


def save_config(cfg: dict) -> None:
    """Serialised beside, then written into place. A half-written config is one
    the deployer refuses to run from and the portal refuses to start on -- and
    the *rename* this used to finish with was its own bug: this runs on the
    host, where a rename succeeds and hands every container a config file it
    can no longer see. See `write_in_place`."""
    tmp = D.CONFIG.with_suffix(".yml.tmp")
    parser = _yaml()
    if parser is None:
        import yaml
        tmp.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
                       encoding="utf-8")
    else:
        with tmp.open("w", encoding="utf-8") as fh:
            parser.dump(cfg, fh)
    D.write_in_place(tmp, D.CONFIG)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", help="a directory with USER.md files under it")
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default: say what they would be)")
    args = ap.parse_args(argv)

    cfg = load_config()
    changes, complaints = plan(cfg, Path(args.folder))
    for complaint in complaints:
        print(f"  skip  {complaint}")
    for member, field, value in changes:
        shown = value if isinstance(value, dict) else \
            str(value).replace("\n", " / ")
        print(f"  {member['id']:8} {field:14} {str(shown)[:96]}"
              f"{'…' if len(str(shown)) > 96 else ''}")
    if not changes:
        print("  nothing to import")
        return 0
    if not args.apply:
        print(f"\n  {len(changes)} change(s). Re-run with --apply to write them.")
        return 0
    for member, field, value in changes:
        member[field] = value
    save_config(cfg)
    print(f"\n  wrote {len(changes)} change(s) to {D.CONFIG}")
    print("  deploy nanobot to seed the assistants that have not booted yet; "
          "one that has keeps the profile it has been taught.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
