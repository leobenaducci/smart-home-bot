#!/usr/bin/env bash
# Point chat.home and house.home at hub for everything
# inside the house, without cutting off the boxes that still have to reach the
# real hosts.
#
#   sudo HomeCore/proxy/split-horizon.sh            # apply
#   HomeCore/proxy/split-horizon.sh --dry-run       # print what it would do
#
# Run it on hub (Pi-hole lives there, and so does the reverse tunnel). Run it
# on compute too if you ever deploy the VPS from that box: the /etc/hosts half
# is what keeps `ssh root@chat.home` going to the VPS instead of
# looping back to hub. It needs root — Pi-hole refuses config edits from any
# other user, and /etc/hosts is /etc/hosts.
#
# Two halves, and the second one is the reason this is a script and not two
# lines in a runbook:
#
#   1. Pi-hole learns both names as A records for hub, so phones, laptops and
#      the WebView on the LAN reach the local Caddy (HomeCore/proxy) instead of
#      leaving the house. An A record is not enough on its own — see the
#      dnsmasq half below.
#
#   2. hub itself is exempted through /etc/hosts, which the resolver consults
#      first. It has to be: `vps-tunnel.service` opens the autossh reverse
#      tunnel to chat.home, and home-chat's VPS deploy job ssh'es to
#      the same name from the hub agent. Give those the LAN answer and the
#      box tunnels to itself — the off-LAN path dies quietly, and the only
#      symptom is that the house works fine while nothing outside it does.
#
# Idempotent: re-running refreshes the pinned VPS address from public DNS,
# which is also how you repair this after the VPS moves.
set -euo pipefail

LAN_TARGET="${LAN_TARGET:-192.168.1.10}"   # hub
PUBLIC_RESOLVER="${PUBLIC_RESOLVER:-1.1.1.1}"
# The names come from `dns:` -- the same settings the admin page writes and the
# deployer exports as PORTAL_NAME / CHAT_NAME. They used to be two literals
# that no household here uses, so a run edited
# the resolver for names nobody types and left the real ones alone.
config_dns() {
    # HOME_STACK_CONFIG is a *file* -- deploy.py's _config_path() and the admin
    # container both use it that way (`HOME_STACK_CONFIG=/state/home-stack.yml`).
    # The directory is HOME_STACK_CONFIG_DIR. Reading the first as a directory
    # built `/state/home-stack.yml/home-stack.yml`, which is unreadable, so this
    # refused to run anywhere the variable was exported at all.
    local cfg="${HOME_STACK_CONFIG:-}"
    if [ -z "$cfg" ]; then
        cfg="${HOME_STACK_CONFIG_DIR:-/var/lib/home-stack/config}/home-stack.yml"
    fi
    [ -r "$cfg" ] || return 1
    awk -v k="$1" '
        /^dns:/            { in_dns = 1; next }
        /^[^[:space:]#]/   { in_dns = 0 }
        in_dns && $1 == k":" { gsub(/^[^:]*:[[:space:]]*/, ""); sub(/[[:space:]]*#.*/, "");
                               gsub(/"/, ""); print; exit }
    ' "$cfg"
}
PORTAL_NAME="${PORTAL_NAME:-$(config_dns portal || true)}"
CHAT_NAME="${CHAT_NAME:-$(config_dns chat || true)}"
if [ -z "$PORTAL_NAME" ] || [ -z "$CHAT_NAME" ]; then
    echo "split-horizon: dns.portal / dns.chat are not set and the config is not" >&2
    echo "readable. Refusing to guess names for somebody's resolver." >&2
    exit 2
fi
# Overridable one name at a time, because the two are not equally ready to be
# moved. While the proxy is on a locally generated CA (see local-cert.sh), the
# portal only has to satisfy browsers, which can be taught to trust it — but
# the home-chat Android app cannot be, and pointing the chat name here would
# turn a working app into a TLS error. Take that one when the real certificate
# is back:  sudo NAMES="$PORTAL_NAME" ./split-horizon.sh
MANAGED_NAMES=("$CHAT_NAME" "$PORTAL_NAME")
read -ra LOCAL_NAMES <<< "${NAMES:-${MANAGED_NAMES[*]}}"
PIN_NAMES=("$CHAT_NAME")                 # names this box must still resolve publicly
MARKER='# split-horizon: real address, keep Pi-hole out of it'

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

run() {
    if [ "$DRY_RUN" = 1 ]; then
        echo "would run: $*"
    else
        "$@"
    fi
}

# pihole-FTL takes list settings as a whole JSON array, so every write below is
# read-modify-write. Nothing we put in these lists contains a quote or a
# backslash (they are host records and dnsmasq directives), so this stays a
# join rather than a real encoder.
to_json() {
    local out="[" i=0
    for v in "$@"; do
        [ "$i" -gt 0 ] && out+=","
        out+="\"${v}\""
        i=$((i + 1))
    done
    echo "${out}]"
}

if [ "$DRY_RUN" = 0 ] && [ "$(id -u)" != 0 ]; then
    echo "FAIL: run me with sudo (Pi-hole will not take config edits from $(id -un))" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. /etc/hosts first, on purpose.
#
# Asking a public resolver directly is what makes this survive re-runs: once
# Pi-hole holds the LAN record, the system resolver would answer with hub and
# a naive re-run would pin the wrong address forever.
# ---------------------------------------------------------------------------
for name in "${PIN_NAMES[@]}"; do
    real=$(dig +short A "$name" "@${PUBLIC_RESOLVER}" | grep -E '^[0-9.]+$' | head -1 || true)
    if [ -z "$real" ]; then
        echo "FAIL: ${PUBLIC_RESOLVER} gave no A record for ${name}; refusing to guess" >&2
        exit 1
    fi
    # Drop any line we (or a past run) put there for this name, then append the
    # current answer. Rewriting rather than editing in place keeps one code
    # path for "first run" and "the VPS moved", which is the case that matters:
    # a stale pin is worse than no pin, because it looks configured.
    name_re="${name//./\\.}"
    echo "==> /etc/hosts: ${name} -> ${real}"
    if [ "$DRY_RUN" = 1 ]; then
        printf 'would write: %s\t%s\t%s\n' "$real" "$name" "$MARKER"
    else
        tmp=$(mktemp)
        grep -vE "^[0-9.]+[[:space:]]+${name_re}([[:space:]]|\$)" /etc/hosts > "$tmp" || true
        printf '%s\t%s\t%s\n' "$real" "$name" "$MARKER" >> "$tmp"
        # cat, not mv: /etc/hosts keeps its inode, owner and mode, and nothing
        # ever observes the file as missing.
        cat "$tmp" > /etc/hosts
        rm -f "$tmp"
    fi
done

# ---------------------------------------------------------------------------
# 2. Pi-hole's local DNS records.
#
# dns.hosts is a list and pihole-FTL takes the whole list at once, so this
# reads the current one, drops any entry for the names we manage, appends ours
# and writes it back. Anything else in there is preserved verbatim — that list
# is every .home name in the house.
# ---------------------------------------------------------------------------
if ! command -v pihole-FTL >/dev/null; then
    echo "==> no pihole-FTL here; skipping the DNS half (run this on hub for that)"
    exit 0
fi

current=$(pihole-FTL --config dns.hosts)
# Printed as `[ a b, c d ]`; strip the brackets and split on ", ".
entries=$(printf '%s' "$current" | sed -e 's/^\[[[:space:]]*//' -e 's/[[:space:]]*\]$//')

wanted=()
IFS=',' read -ra existing <<< "$entries"
for e in "${existing[@]}"; do
    e="$(echo "$e" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -z "$e" ] && continue
    host="${e##* }"
    # Strip every name we manage, not just the ones being pointed at the LAN
    # this run: NAMES= is "these go local, the rest go public", so a name left
    # out of it has to lose its record rather than keep a stale one. Anything
    # outside MANAGED_NAMES is somebody else's and is copied through untouched.
    skip=0
    for name in "${MANAGED_NAMES[@]}"; do
        [ "$host" = "$name" ] && skip=1
    done
    [ "$skip" = 0 ] && wanted+=("$e")
done
for name in "${LOCAL_NAMES[@]}"; do
    wanted+=("${LAN_TARGET} ${name}")
done

echo "==> pihole dns.hosts: ${#wanted[@]} entries, including:"
for name in "${LOCAL_NAMES[@]}"; do echo "      ${LAN_TARGET} ${name}"; done
run pihole-FTL --config dns.hosts "$(to_json "${wanted[@]}")"

# ---------------------------------------------------------------------------
# 3. Suppress AAAA for the names we take over.
#
# A host record only answers the family it is written in. Pi-hole stays a
# forwarder for every other type, so an AAAA for house.home is
# still fetched from Hostinger and handed out happily — leaving a dual-stack
# client with a LAN A record and a public AAAA, and told by RFC 6724 to prefer
# the AAAA. The whole redirect is then bypassed by exactly the devices most
# likely to have working IPv6.
#
# `local=/name/` is dnsmasq's "answer this name from local data only, never
# forward it". The A keeps coming from the host record above; AAAA becomes
# NODATA, and the client uses the A. Verified against dnsmasq 2.92 (the leak
# reproduces without this line and stops with it), and it is scoped to the one
# name — everything else still forwards.
#
# This has to track LOCAL_NAMES exactly in both directions: a `local=` line for
# a name with no host record is an NXDOMAIN, so dropping a name from NAMES has
# to drop its line too. We own `local=/<anything>.home/` and nothing
# else in this list.
# ---------------------------------------------------------------------------
current_lines=$(pihole-FTL --config misc.dnsmasq_lines)
lines_entries=$(printf '%s' "$current_lines" | sed -e 's/^\[[[:space:]]*//' -e 's/[[:space:]]*\]$//')

lines=()
IFS=',' read -ra existing_lines <<< "$lines_entries"
for e in "${existing_lines[@]}"; do
    e="$(echo "$e" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -z "$e" ] && continue
    # Any entry for a name this script manages, whatever the suffix. The
    # pattern here used to spell one suffix twice, so it matched nothing
    # and stale entries were carried forward on every run.
    #
    # MANAGED_NAMES, not LOCAL_NAMES, and the same rule as the host records
    # above: `NAMES=` is "these go local, the rest go public". Dropping a name
    # from NAMES drops its host record, so a `local=` line matched only against
    # LOCAL_NAMES would survive it -- and `local=/name/` with no host record is
    # an NXDOMAIN, which is the exact failure the comment above warns about,
    # produced by the one override this script documents.
    skip=0
    for n in "${MANAGED_NAMES[@]}"; do
        [[ "$e" == "local=/$n/" ]] && skip=1 && break
    done
    [ "$skip" = 1 ] && continue
    lines+=("$e")
done
for name in "${LOCAL_NAMES[@]}"; do
    lines+=("local=/${name}/")
done

echo "==> pihole misc.dnsmasq_lines: ${#lines[@]} lines, including:"
for name in "${LOCAL_NAMES[@]}"; do echo "      local=/${name}/"; done
run pihole-FTL --config misc.dnsmasq_lines "$(to_json "${lines[@]}")"

# ---------------------------------------------------------------------------
# Verify.
#
# Two things this has to get right or it lies about a run that worked:
#
#   - Ask ${LAN_TARGET}, not 127.0.0.1. On hub FTL answers on ::1 and on the
#     LAN address but times out on IPv4 loopback, so a loopback check reports a
#     dead resolver every time. ${LAN_TARGET} is also what the LAN itself asks.
#
#   - Wait for the reload. Writing either setting makes FTL restart dnsmasq, so
#     for a second or two afterwards the resolver is either down (timeout) or
#     still answering from the configuration we just replaced. Checking
#     immediately reported a leak that had already been fixed.
# ---------------------------------------------------------------------------
answers() { dig +short "$2" "$1" "@${LAN_TARGET}" +time=2 +tries=1 | tr '\n' ' '; }

if [ "$DRY_RUN" = 0 ]; then
    first="${LOCAL_NAMES[0]}"
    for _ in $(seq 1 15); do
        # Both halves are live once the A is ours and the AAAA has stopped
        # coming back — that is exactly what the two writes above did.
        if [[ "$(answers "$first" A)" == *"${LAN_TARGET}"* ]] \
            && [ -z "$(answers "$first" AAAA)" ]; then
            break
        fi
        sleep 1
    done

    echo "==> now answering (asked at ${LAN_TARGET}):"
    for name in "${LOCAL_NAMES[@]}"; do
        a=$(answers "$name" A)
        aaaa=$(answers "$name" AAAA)
        printf '      %-28s A %s%s\n' "$name" "${a:-<none>}" \
            "${aaaa:+ / AAAA ${aaaa}<- still leaking}"
    done
    for name in "${PIN_NAMES[@]}"; do
        printf '      %-28s %s (this box, via /etc/hosts)\n' "$name" "$(getent hosts "$name" | awk '{print $1}' | head -1)"
    done
fi
