#!/usr/bin/env bash
# Stop preview servers the Programmer left behind.
#
# The agent is told to offer a local preview for anything with a page, which
# means starting a server in the background and stopping it when the person is
# done. A turn that fails between those two never reaches the second half, and
# what it leaves is a web server on the house network that nobody knows is
# running and nothing will ever close. One survived four hours here, serving a
# checkout, and was found by accident.
#
# The safety property is the working directory, not the command line. A process
# is a candidate only if its cwd is inside the checkout root -- which is a
# directory the broker owns and only agent work runs in -- so no amount of
# pattern-matching a command can make this reach a person's own processes.
# Nothing outside that tree is looked at, whatever it is called.
#
# Run by opencode-preview-reaper.timer. Safe to run by hand, and `--dry-run`
# says what it would stop without stopping anything.
set -uo pipefail

ROOT="${OPENCODE_WORKSPACE_ROOT:-}"
MAX_AGE_MIN="${OPENCODE_PREVIEW_MAX_AGE_MIN:-120}"
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY=1

if [ -z "$ROOT" ]; then
    echo "opencode-preview-reaper: OPENCODE_WORKSPACE_ROOT is empty; nothing to do" >&2
    # Not an error: a household that never enabled the Programmer has no
    # checkout root, and a timer that fails every quarter of an hour on a
    # machine with nothing to reap is noise somebody has to learn to ignore.
    exit 0
fi
# Resolved once, so the prefix test compares two real paths. A symlinked state
# directory would otherwise never match and this would silently reap nothing.
ROOT="$(readlink -f "$ROOT" 2>/dev/null || echo "$ROOT")"
MAX_AGE_S=$(( MAX_AGE_MIN * 60 ))
me=$$
killed=0

for d in /proc/[0-9]*; do
    pid="${d#/proc/}"
    [ "$pid" = "$me" ] && continue
    # Only this account's processes: readlink on somebody else's cwd fails, so
    # this is already true, but saying it costs nothing.
    cwd="$(readlink "$d/cwd" 2>/dev/null)" || continue
    case "$cwd" in
        "$ROOT"|"$ROOT"/*) ;;
        *) continue ;;
    esac

    age="$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')"
    [ -n "$age" ] || continue
    [ "$age" -ge "$MAX_AGE_S" ] || continue

    # Read for the log only. What it is called has no part in the decision --
    # see the note at the top about why the directory is the test.
    cmd="$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null | cut -c1-120)"
    if [ -n "$DRY" ]; then
        echo "would stop pid $pid (${age}s, cwd $cwd): $cmd"
        continue
    fi
    # TERM, then KILL if it is still there. A dev server with a child process
    # group ignores a plain TERM often enough to be worth the second pass, and
    # a preview nobody can stop is the thing being fixed.
    kill -TERM "$pid" 2>/dev/null || continue
    for _ in 1 2 3 4 5; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
    echo "opencode-preview-reaper: stopped pid $pid after ${age}s in $cwd: $cmd"
    killed=$(( killed + 1 ))
done

[ "$killed" -gt 0 ] && echo "opencode-preview-reaper: stopped $killed process(es)"
exit 0
