#!/bin/sh
# The one entrypoint. What used to be two — entrypoint.sh for a plain nanobot
# and entrypoint-multiuser.sh for this house's instances — had two copies of the
# config-copy step, and they had already drifted about where the config comes
# from.
#
# Everything an instance needs is in `config/`, mounted read-only at
# $NANOBOT_CONFIG. NANOBOT_INSTANCE picks which instance it is, and that name
# does two things:
#
#   - `instances/<name>/config.json`, if it exists, IS the config for that
#     instance — complete, not a patch. The house instance is the one that has
#     one, and it is complete on purpose: it holds fewer credentials than the
#     base, and a merge can only add. See the note in that file.
#   - `config.<name>.json`, if it exists, is deep-merged over the base. That is
#     the per-member path: an overlay says only what differs, which is what one
#     member linking a WhatsApp account actually needs.
#
# The two are exclusive and the complete file wins. Without NANOBOT_INSTANCE,
# the base config runs unchanged.
set -eu

dir="$HOME/.nanobot"
if [ -d "$dir" ] && [ ! -w "$dir" ]; then
    cat >&2 <<HELP
Error: $dir is not writable.

Fix (pick one):
  Host:   sudo chown -R 1000:1000 <the mounted directory>
  Docker: docker run --user \$(id -u):\$(id -g) ...
  Podman: podman run --userns=keep-id ...
HELP
    exit 1
fi
mkdir -p "$dir"

# Where the config directory is mounted. /etc/nanobot is the older location and
# is kept as a fallback so a compose file from before this move still boots.
CONFIG="${NANOBOT_CONFIG:-/nanobot-config}"
[ -d "$CONFIG" ] || CONFIG=/etc/nanobot
INSTANCE="${NANOBOT_INSTANCE:-}"
inst_dir="$CONFIG/instances/$INSTANCE"

# --- config.json ------------------------------------------------------------
# Copied rather than symlinked: nanobot reads it once at startup, so a symlink
# would buy nothing and an overlay has to be written somewhere anyway.
if [ -n "$INSTANCE" ] && [ -f "$inst_dir/config.json" ]; then
    cp "$inst_dir/config.json" "$dir/config.json"
    echo "config: $inst_dir/config.json" >&2
elif [ -f "$CONFIG/config.json" ]; then
    cp "$CONFIG/config.json" "$dir/config.json"
else
    echo "warning: no config.json under $CONFIG; leaving $dir/config.json as-is" >&2
fi

# --- the per-member overlay -------------------------------------------------
# Two of these now, merged the same way and in this order: the file beside the
# config, then whatever HomeWeb says about this instance. The second one is
# where a mailbox and its rules come from — those are edited on a page by an
# admin, not committed to this repo, and they carry a password, so they are
# fetched per instance over the authenticated channel rather than written into
# a file five containers can read.
merge_overlay() {
    # merge_overlay <base.json> <overlay.json> — dicts merge, everything else
    # replaces, `null` deletes. Deleting matters because an overlay is also how
    # an instance holds *fewer* credentials than the base: listing a provider
    # is the same as demanding its credential, so being unable to drop one
    # would mean every instance carries every key the base names.
    python3 - "$1" "$2" <<'MERGEPY'
import json, os, sys

def merge(base, over):
    for k, v in over.items():
        if v is None:
            base.pop(k, None)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v
    return base

base_path, over_path = sys.argv[1], sys.argv[2]
with open(base_path, encoding="utf-8") as fh:
    base = json.load(fh)
with open(over_path, encoding="utf-8") as fh:
    over = json.load(fh)
# Written to a temp and moved, so a malformed overlay cannot leave a truncated
# config behind — the container would come up as a different assistant.
tmp = base_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(merge(base, over), fh, ensure_ascii=False, indent=2)
os.replace(tmp, base_path)
MERGEPY
}

overlay="$CONFIG/config.$INSTANCE.json"
if [ -n "$INSTANCE" ] && [ ! -f "$inst_dir/config.json" ] && [ -f "$overlay" ]; then
    if merge_overlay "$dir/config.json" "$overlay"; then
        echo "config: merged overlay $overlay" >&2
    else
        # Loud, and not fatal: a broken overlay costs the difference, not the
        # assistant. The base config is already in place and valid.
        echo "warning: $overlay could not be merged; running the base config" >&2
    fi
fi

# --- the WhatsApp channel, for the member who has one -------------------------
# `WHATSAPP_ENABLED=1` is set by the deployer for exactly the members whose
# `members[].whatsapp` is true, and a `whatsapp-bridge-<member>` container is
# started beside each of them. What was missing is the only thing that makes
# the assistant *read* it: a `channels.whatsapp` block. The comment beside that
# variable says the overlay is `config/config.<member>.json` and that with "no
# file, no effect" -- and no file was ever generated, because the source
# household committed one by hand for the single member who was linked there.
#
# So a household that linked a phone got: a paired bridge, a healthy container,
# `✅ Connected to WhatsApp` in its log, and an assistant with no WhatsApp
# channel at all. Nothing failed. Messages simply arrived at a bridge nobody
# was listening to.
#
# Generated here rather than shipped, because which member is linked is a
# setting and not a fact about the package. A committed `config.user1.json`
# would be this household's answer baked into everyone's copy -- and it is the
# shared config that must never carry this block, since that would link every
# member to one person's account.
if [ "${WHATSAPP_ENABLED:-}" = "1" ] && [ -n "$INSTANCE" ]; then
    if python3 -c 'import json,sys; sys.exit(0 if (json.load(open(sys.argv[1])).get("channels") or {}).get("whatsapp") else 1)' "$dir/config.json" 2>/dev/null; then
        echo "config: whatsapp channel already present" >&2
    else
        gen="$dir/whatsapp-overlay.json"
        # allowFrom must be non-empty whenever enabled is true: nanobot exits at
        # startup on an enabled channel with an empty list, which restart-loops
        # the instance and reads like a bad token.
        cat > "$gen" <<WAJSON
{
  "channels": {
    "whatsapp": {
      "enabled": true,
      "bridgeUrl": "ws://whatsapp-bridge-$INSTANCE:3002",
      "allowFrom": ["*"],
      "addressTrigger": "alfred",
      "groupPolicy": "open"
    }
  }
}
WAJSON
        if merge_overlay "$dir/config.json" "$gen"; then
            echo "config: whatsapp channel generated for $INSTANCE" >&2
        else
            echo "warning: could not add the whatsapp channel for $INSTANCE" >&2
        fi
        rm -f "$gen"
    fi
fi

# --- what HomeWeb says about this instance ----------------------------------
# The mailboxes this Alfred may read and the rules about what he may do with
# them. Same authenticated fetch as FAMILY.md below, and the same posture:
# --fail so an error page never becomes the config, and a miss leaves the
# container running the config it already has. Losing the overlay costs the
# email channel; treating a 502 as an empty config would hand him a mailbox
# with no rules on it, and «no rules» is the one state that must never arrive
# by accident.
sync_profile_overlay() {
    base="${TASKS_API_URL:-}"
    [ -n "$base" ] || return 0
    base="$(printf '%s' "$base" | sed 's#/tasks/api#/profiles/api#')"
    [ -n "${HOMECORE_USER_ID:-}" ] && [ -n "${HOMECORE_PROXY_TOKEN:-}" ] || return 0
    tmp="$dir/profile-overlay.json"
    if curl -skf --max-time 10 \
            -H "X-Proxy-Secret: $HOMECORE_PROXY_TOKEN" \
            -H "X-Proxy-User: $HOMECORE_USER_ID" \
            "$base/export" -o "$tmp" && [ -s "$tmp" ]; then
        if merge_overlay "$dir/config.json" "$tmp"; then
            echo "config: merged the profile HomeWeb serves for $INSTANCE" >&2
        else
            echo "warning: HomeWeb's profile could not be merged; running without it" >&2
        fi
    fi
    rm -f "$tmp"
}
sync_profile_overlay

# --- the prompt files -------------------------------------------------------
# Symlinked rather than copied: a symlink resolves when the agent reads it, so
# editing SOUL.md on the host applies on the next turn with no restart. They
# used to be bind-mounted one file at a time, which pinned each to the inode it
# had at container start and silently ignored every later edit.
#
# The instance's own copy wins where it has one. That is the whole reason this
# directory is shared: TOOLS.md and HEARTBEAT.md were byte-identical in two
# places, and the pair drifts the first time somebody edits one of them.
#
# $CONFIG is mounted read-only, so these are readable and not writable by the
# agent — an attempt to edit one fails loudly with EROFS rather than silently
# rewriting the persona for the whole household.
ws="$dir/workspace"
mkdir -p "$ws"
for f in SOUL.md AGENTS.md TOOLS.md HEARTBEAT.md MORNING.md; do
    if [ -n "$INSTANCE" ] && [ -f "$inst_dir/$f" ]; then
        ln -sfn "$inst_dir/$f" "$ws/$f"
    elif [ -f "$CONFIG/$f" ]; then
        ln -sfn "$CONFIG/$f" "$ws/$f"
    else
        echo "warning: $f is missing under $CONFIG; leaving $ws/$f as-is" >&2
    fi
done

# USER.md is NOT in that loop, and must not be: memory consolidation writes to
# it, so a symlink into the read-only mount would fail with EROFS every time
# Dream tried to record something. It is seeded once and then belongs to the
# instance — which is also why an existing one is never overwritten, or every
# restart would throw away everything the agent had learned.
if [ -n "$INSTANCE" ] && [ -f "$inst_dir/USER.md" ] && [ ! -f "$ws/USER.md" ]; then
    cp "$inst_dir/USER.md" "$ws/USER.md"
fi

# --- the instance's own skills ----------------------------------------------
# Copied, not symlinked, and replaced wholesale every start: nothing but this
# repo edits them, and seed-if-missing would silently pin the first version
# forever the way a stale bind mount does. The loop refuses an unnamed entry
# rather than letting `rm -rf` see an empty name.
if [ -n "$INSTANCE" ] && [ -d "$inst_dir/skills" ]; then
    mkdir -p "$ws/skills"
    for skill in "$inst_dir"/skills/*/; do
        [ -d "$skill" ] || continue
        name="$(basename "$skill")"
        [ -n "$name" ] || { echo "refusing to copy an unnamed skill" >&2; exit 1; }
        rm -rf "$ws/skills/$name"
        cp -r "$skill" "$ws/skills/$name"
    done
fi

# --- shared household memory ------------------------------------------------
# Pull the shared household directory from HomeCore into workspace/FAMILY.md so
# this (isolated) Alfred always has everybody in context. HomeCore is the source
# of truth; live reads and writes go through the `family` skill. Refreshed on
# start and every 30 min so another member's edits show up without a restart.
# Skips silently when this instance has no proxy credentials — the house
# instance is exactly that case.
sync_family_md() {
    base="${TASKS_API_URL:-}"
    [ -n "$base" ] || return 0
    base="$(printf '%s' "$base" | sed 's#/tasks/api#/family/api#')"
    [ -n "${HOMECORE_USER_ID:-}" ] && [ -n "${HOMECORE_PROXY_TOKEN:-}" ] || return 0
    tmp="$ws/.FAMILY.md.tmp"
    # --fail matters: without it curl exits 0 on 401/403/500 and writes the
    # error BODY to $tmp — non-empty, so it would replace a good FAMILY.md with
    # an error page and feed that to the prompt.
    if curl -skf --max-time 10 \
            -H "X-Proxy-Secret: $HOMECORE_PROXY_TOKEN" \
            -H "X-Proxy-User: $HOMECORE_USER_ID" \
            "$base/markdown" -o "$tmp" && [ -s "$tmp" ]; then
        mv "$tmp" "$ws/FAMILY.md"
    else
        rm -f "$tmp"
    fi
}
sync_family_md
# The watcher has to outlive this shell — `exec` below replaces it — but not
# the process that takes its place. `exec` keeps the PID, so the backgrounded
# subshell's parent *is* nanobot; when nanobot goes the subshell is reparented
# to init and nothing ever kills it. Run outside a container that is forever:
# 262 orphaned watchers had accumulated on the host, one per start going back
# months, each still curling FAMILY.md every half hour on behalf of an instance
# that no longer exists. In a container teardown hid it, which is why it took
# this long to see.
#
# So the watcher checks the PID it was forked from and stops when that dies.
# Cheap, and it does the right thing in both places.
parent=$$
( while sleep 1800; do
      kill -0 "$parent" 2>/dev/null || exit 0
      sync_family_md
  done ) &

# Gateway and API server in one process so they share a MessageBus: without
# that, subagent and cron results never reach the WebSocket subscribers.
if [ "$#" -gt 0 ]; then
    exec nanobot "$@"
fi
exec nanobot gateway --api-port "${NANOBOT_API_PORT:-8900}"
