#!/usr/bin/env bash
# house-dns-record.sh — point a public name at this machine, on the house network only.
#
# The other half of the split horizon. public-cert.sh made this host able to
# *serve* the public chat name; this makes the house resolver hand it out. Off
# the LAN the public record is untouched and still points at the VPS, so nothing
# becomes reachable from outside that was not already.
#
# Why it matters: the Android app hard-codes the public URL and keeps using it on
# the home wifi, so without this it leaves the house, comes back through the VPS,
# and is told it is away -- no cameras and no LAN-only extension in its Apps
# menu, on the one device the family actually uses.
#
#     RESOLVER_HOST=<pihole> ./house-dns-record.sh --check                 what it would do, changes nothing
#     RESOLVER_HOST=<pihole> ./house-dns-record.sh                         chat name -> this host
#     RESOLVER_HOST=<pihole> ./house-dns-record.sh some.name 192.168.88.9  anything else
#
# The record lives on the Pi-hole, which is on another machine, so this reaches
# it over ssh and uses sudo *there*. It will ask for that machine's password.
#
# Run it as yourself, not with sudo: the ssh key it needs is in your home
# directory, and root's is not the same key. If you do run it with sudo it drops
# back to the invoking user rather than failing at the ssh, because "permission
# denied (publickey)" from inside a script reads as a broken key rather than as
# the wrong account.
set -euo pipefail

# --- run as the invoking user, whatever the invocation ---
if [ "${EUID:-$(id -u)}" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    echo "==> this does not need root here; re-running as $SUDO_USER"
    exec sudo -u "$SUDO_USER" -H -- "$0" "$@"
fi

# The machine running Pi-hole. No default: a name written here is some
# household's resolver, and rarely the one running this.
RESOLVER_HOST="${RESOLVER_HOST:-}"
if [ -z "$RESOLVER_HOST" ]; then
    echo "RESOLVER_HOST is not set: the machine running Pi-hole, e.g." >&2
    echo "    RESOLVER_HOST=pihole.home $0 --check" >&2
    exit 2
fi
RESOLVER_USER="${RESOLVER_USER:-$(id -un)}"
TOML="/etc/pihole/pihole.toml"

CHECK=0
ARGS=()
for a in "$@"; do
    case "$a" in
        --check) CHECK=1 ;;
        -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) ARGS+=("$a") ;;
    esac
done

NAME="${ARGS[0]:-}"
ADDR="${ARGS[1]:-}"

# Not guessed from config: `dns.chat` there is the *house* name, and this wants
# the public one the app hard-codes, which the config does not carry.
[ -n "$NAME" ] || NAME="${PUBLIC_CHAT_NAME:-}"
if [ -z "$NAME" ]; then
    echo "usage: $0 [--check] <public-name> [address]" >&2
    echo "  e.g. $0 chat.example.com" >&2
    exit 2
fi
# The address of the interface that reaches the LAN -- `route get`, not
# `hostname -I`, whose first field can be 127.0.0.1 or a docker bridge. A record
# pointing every phone in the house at itself is worse than no record.
if [ -z "$ADDR" ]; then
    ADDR="$(ip -o -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)"
fi
[ -n "$ADDR" ] || { echo "ERROR: could not work out this host's LAN address; pass it" >&2; exit 1; }

RECORD="$ADDR $NAME"
echo "Record:   $RECORD"
echo "Resolver: $RESOLVER_USER@$RESOLVER_HOST"
echo

ssh_ro() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$RESOLVER_USER@$RESOLVER_HOST" "$@"; }

# The resolver's own address, resolved once, before anything restarts it.
#
# `dig @$RESOLVER_HOST` needs DNS to find the resolver -- from the resolver that is
# restarting. For the whole restart window nothing answers, so the check saw no
# answer, called it a wrong answer, and rolled back a change that was correct.
# It undid itself every time and reported the resolver's fault.
RESOLVER_IP="$(getent hosts "$RESOLVER_HOST" 2>/dev/null | awk '{print $1; exit}')"
[ -n "$RESOLVER_IP" ] || RESOLVER_IP="$(ssh_ro "echo \$SSH_CONNECTION" 2>/dev/null | awk '{print $3}')"
[ -n "$RESOLVER_IP" ] || { echo "ERROR: could not resolve $RESOLVER_HOST to an address" >&2; exit 1; }
echo "Resolver address: $RESOLVER_IP (pinned before the restart)"

if ! ssh_ro true 2>/dev/null; then
    echo "ERROR: cannot reach $RESOLVER_USER@$RESOLVER_HOST over ssh without a password." >&2
    echo "       That key is what this needs to get in; sudo happens at the far end." >&2
    exit 1
fi

# --- what is true now ---
before="$(dig +short +time=3 +tries=1 "@$RESOLVER_IP" "$NAME" 2>/dev/null | tr '\n' ' ' || true)"
echo "==> $NAME currently resolves to: ${before:-(nothing)}"

# Whether the record is already there cannot be answered from here: pihole-FTL
# rewrites pihole.toml mode 640, so an unprivileged grep gets "permission
# denied", returns non-zero, and reads as "absent" -- and `sed` is not
# idempotent, so a second run would add a duplicate record.
#
# So the check happens inside the privileged block below, where it can actually
# read the file, rather than being guessed out here.
if [ "${before% }" = "$ADDR" ]; then
    echo "==> it already resolves here. Nothing to do."
    exit 0
fi

# A `local=/<name>/` line without a record for that name is a loaded gun.
# dnsmasq reads it as "answer this domain from local data only, never forward",
# so with nothing local the answer is NXDOMAIN -- not a fall-back to the public
# record, a failure to resolve. It stays harmless only while pihole-FTL has not
# restarted since the line was added, which is a thing that happens on its own.
#
# Found exactly that here: the line added 2026-08-26, FTL running since
# 2026-08-14. Adding the record is what defuses it, and this says so rather than
# quietly fixing something nobody knew was broken.
# Same reason as above: this can only be checked with privileges, so it is
# reported from inside the block that has them. Kept here as a best effort for
# the --check path, where nothing is changed either way.
if ssh_ro "grep -qF 'local=/$NAME/' $TOML" 2>/dev/null; then
    echo
    echo "NOTE: $TOML already carries \"local=/$NAME/\", which tells dnsmasq to"
    echo "      answer that name from local records and never forward it. There is"
    echo "      no local record yet, so the next pihole-FTL restart -- a reboot, an"
    echo "      update, this script -- would make the name NXDOMAIN on the house"
    echo "      network and the app would stop resolving at home."
    echo "      Adding the record is what makes that line correct."
    echo
fi

if [ "$CHECK" = 1 ]; then
    echo
    echo "--check: would add   $RECORD"
    echo "         to          $TOML on $RESOLVER_HOST"
    echo "         then restart pihole-FTL and verify the answer."
    exit 0
fi

# --- change it, with the previous file kept at the far end ---
echo "==> adding the record (sudo on $RESOLVER_HOST will ask for a password)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ssh -t -o ConnectTimeout=10 "$RESOLVER_USER@$RESOLVER_HOST" "
    set -e
    if sudo grep -qF '\"$RECORD\"' $TOML; then
        echo '    the record is already in the file; not adding it twice'
    else
        sudo cp -p $TOML $TOML.$STAMP.bak
        echo '    backup kept at $TOML.$STAMP.bak'
        # Anchored at end of line on purpose. There are two 'hosts =' keys: the
        # [dns] one, which ends the line at '[', and [dhcp]'s static leases,
        # which is '[]' and holds a completely different format. Without the
        # anchor this inserts a DNS record into the DHCP reservations.
        sudo sed -i '/^  hosts = \[\$/a\\    \"$RECORD\",' $TOML
    fi
    if sudo grep -qF 'local=/$NAME/' $TOML; then
        echo '    note: local=/$NAME/ is set, so this record is what keeps that name resolving here'
    fi
    sudo systemctl restart pihole-FTL
"

# --- did it take? ---
# Wait for the answer we asked for, not for any answer at all. pihole-FTL takes
# a while to start serving, and during that gap a query gets nothing -- which the
# old loop accepted as the final answer and treated as failure.
echo "==> waiting for the resolver to come back and answer"
after=""
for i in $(seq 1 30); do
    sleep 2
    after="$(dig +short +time=3 +tries=1 "@$RESOLVER_IP" "$NAME" 2>/dev/null | head -1 || true)"
    [ "$after" = "$ADDR" ] && break
    [ $((i % 5)) -eq 0 ] && echo "    still waiting (${after:-no answer yet})"
done

if [ "$after" != "$ADDR" ]; then
    echo "ERROR: $NAME resolves to '${after:-nothing}', expected $ADDR." >&2
    echo "       Restoring $TOML from the backup and restarting." >&2
    ssh -t "$RESOLVER_USER@$RESOLVER_HOST" \
        "sudo cp -p $TOML.$STAMP.bak $TOML && sudo systemctl restart pihole-FTL"
    exit 1
fi
echo "    $NAME -> $after"

# --- and does the thing it exists for actually work ---
# The record is only half of it: this host has to answer that name with a
# certificate the phone trusts. Checked against the real CA store, never with
# -k, because -k answers no question at all -- see the proxy's own verify.
echo "==> checking that this host serves $NAME"
code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 \
        --resolve "$NAME:443:$ADDR" "https://$NAME/" 2>/dev/null || echo 000)"
verify="$(curl -s -o /dev/null -w '%{ssl_verify_result}' -m 15 \
        --resolve "$NAME:443:$ADDR" "https://$NAME/" 2>/dev/null || echo 1)"
if [ "$code" = "000" ] || [ "$verify" != "0" ]; then
    echo "WARN: the record is in place, but https://$NAME on this host answered" >&2
    echo "      '$code' with tls verify result '$verify'. The name resolves here" >&2
    echo "      and nothing serves it yet -- run ./public-cert.sh $NAME." >&2
    exit 1
fi
echo "    https://$NAME -> $code, certificate verified"
echo
echo "Done. On the house network that name is this host; off it, the public"
echo "record is untouched and still points wherever it did."
