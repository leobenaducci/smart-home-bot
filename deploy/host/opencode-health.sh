#!/bin/sh
# Is `opencode serve` ready to take a turn?
#
#   opencode-health.sh <port> [seconds]
#
# Exits 0 as soon as it answers, 1 if it never does. Used twice: as the unit's
# ExecStartPost, and by `./home-stack install --opencode` after it enables the
# unit, so there is one definition of "ready" rather than two that drift.
#
# A file rather than a line inside the unit, and that is the whole reason it
# exists. Written inline, systemd's own unescaping ate the quotes:
# `\"healthy\"` reached the shell as `""healthy"`, which concatenates to the
# pattern `healthy[[:space:]]*:[[:space:]]*true` -- and that cannot match
# `{"healthy":true}`, because there is a `"` between `healthy` and `:`. The
# server was fine and had been listening for a minute; the check was wrong.
#
# It asserts the **payload**, not the status code and not the port. That is the
# rule the rest of this stack's health checks follow, and it is not decoration
# here either: the port opens before the server will serve, and `curl -sf`
# passes on any 200.
set -eu

port="${1:?usage: opencode-health.sh <port> [seconds]}"
deadline="${2:-60}"
# Two opencodes answer here, and neither knows the other's door. OpenCode 1
# says `{"healthy":true}` on /global/health; OpenCode 2 has no such route and
# answers it with its web page, so it is asked for /api/info and must name its
# version. Both behind the password when the unit has one (the env file the
# unit reads is this script's environment too).
v1="http://127.0.0.1:${port}/global/health"
v2="http://127.0.0.1:${port}/api/info"
auth=""
[ -n "${OPENCODE_SERVER_PASSWORD:-}" ] && auth="opencode:${OPENCODE_SERVER_PASSWORD}"

ask() {
    if [ -n "$auth" ]; then
        curl -s -m 5 -u "$auth" "$1" 2>/dev/null
    else
        curl -s -m 5 "$1" 2>/dev/null
    fi
}

url="$v1 or $v2"
i=0
while [ "$i" -lt "$deadline" ]; do
    if ask "$v1" | grep -q '"healthy"[[:space:]]*:[[:space:]]*true' \
            || ask "$v2" | grep -q '"version"[[:space:]]*:[[:space:]]*"'; then
        exit 0
    fi
    i=$((i + 1))
    sleep 1
done

echo "opencode serve was not healthy on ${port} within ${deadline}s" >&2
echo "  tried: ${url}" >&2
exit 1
