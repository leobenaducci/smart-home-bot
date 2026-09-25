#!/usr/bin/env bash
# public-cert.sh — serve the *public* chat name from inside the house.
#
# The Android app hard-codes one URL, the public one, and keeps using it on the
# home wifi. So it leaves the house, comes back through the VPS, and is told it
# is away: the VPS copy is not the house copy, and `_at_home()` is false for
# every request the app has ever made. The family's cameras and every LAN-only
# extension were missing from its Apps menu, permanently, on the one device they
# use.
#
# The fix is the split horizon this Caddyfile was written for, applied to the
# public name as well: the house resolver answers it with hub's address, so the
# app reaches the LAN copy -- which is IS_LAN_COPY=1 and therefore at home -- and
# off the LAN the public record still points at the VPS. Nothing new is exposed;
# what changes is which copy answers a phone standing in the kitchen.
#
# Two things have to be true for that, and this does the second:
#
#   1. DNS. `<name>` must resolve to hub on the LAN and over the VPN. That is a
#      record in the household's resolver, not something a script should reach
#      in and change.
#   2. TLS. The LAN copy has to present a certificate for a *public* name, and
#      the house CA will not do -- Android does not trust it, and the app fails
#      the handshake outright rather than warning. So this takes a real
#      certificate for that one name and writes the site block that uses it.
#
# Run after issuing (see below), and again whenever the certificate renews:
#
#     ./public-cert.sh chat.example.com
#
# Issuing is deliberately not done here. The certificate for that name already
# exists somewhere in most houses that want this -- the VPS has one -- and the
# ways to get another (DNS-01 with a provider token, a copy from the VPS) differ
# per household. What this needs is the two files, named on the command line or
# found in the acme.sh store next door:
#
#     ./public-cert.sh chat.example.com /path/fullchain.pem /path/key.pem
#
# The one that issued this house's used acme.sh with `dns_hostinger`, and the
# variable the plugin reads is `HOSTINGER_Token` -- not `HOSTINGER_Api_Token`,
# which is what issue-cert.sh exports and which the plugin ignores, reporting
# "You didn't specify a Hostinger API Key yet" while the credential sits right
# there. That is the same class of failure ensure-cert.sh's comment describes.
set -euo pipefail
cd "$(dirname "$0")"

NAME="${1:-}"
FULLCHAIN="${2:-}"
KEY="${3:-}"

if [ -z "$NAME" ]; then
    echo "usage: $0 <public-name> [fullchain.pem] [key.pem]" >&2
    exit 2
fi

# The volume Caddy reads, found the same way local-cert.sh finds it: compose
# names it <project>_proxy-certs, and a wrong-project volume left over from an
# earlier layout is the reason that script does not simply guess.
if [ -n "${COMPOSE_PROJECT_NAME:-}" ]; then
    VOLUME="${COMPOSE_PROJECT_NAME}_proxy-certs"
else
    VOLUME="$(docker volume ls -q | grep -E '_proxy-certs$' | head -1 || true)"
    [ -n "$VOLUME" ] || VOLUME="$(basename "$PWD")_proxy-certs"
fi

# Caddy mounts /certs read-only -- deliberately, so the thing serving the
# certificates cannot rewrite them. The acme container in the same compose file
# mounts the same volume read-write and is how anything gets in there.
WRITER="$(docker ps --filter "name=proxy-acme" --format '{{.Names}}' | head -1)"
[ -n "$WRITER" ] || { echo "ERROR: no proxy-acme container is running to write $VOLUME" >&2; exit 1; }

# Fall back to the acme.sh store, which is where a freshly issued one lands.
if [ -z "$FULLCHAIN" ]; then
    SRC="$(docker ps --filter 'name=acme' --format '{{.Names}}' | head -1)"
    for c in $(docker ps --filter 'name=acme' --format '{{.Names}}'); do
        if docker exec "$c" test -f "/acme.sh/${NAME}_ecc/fullchain.cer" 2>/dev/null; then
            SRC="$c"; break
        fi
    done
    [ -n "${SRC:-}" ] || { echo "ERROR: no certificate given and none found for $NAME" >&2; exit 1; }
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    docker exec "$SRC" cat "/acme.sh/${NAME}_ecc/fullchain.cer" > "$TMP/fullchain.pem"
    docker exec "$SRC" cat "/acme.sh/${NAME}_ecc/${NAME}.key" > "$TMP/key.pem"
    FULLCHAIN="$TMP/fullchain.pem"
    KEY="$TMP/key.pem"
    echo "==> taking the certificate from $SRC"
fi

[ -s "$FULLCHAIN" ] && [ -s "$KEY" ] || { echo "ERROR: certificate or key is empty" >&2; exit 1; }

# The pair has to match, and the certificate has to be for the name being
# served. Serving a valid certificate for the wrong name fails in the app as a
# refused connection with nothing logged on this side, which is indistinguishable
# from the tunnel being down.
subject="$(openssl x509 -in "$FULLCHAIN" -noout -subject 2>/dev/null || true)"
case "$subject" in
    *"$NAME"*) ;;
    *) echo "ERROR: that certificate is not for $NAME ($subject)" >&2; exit 1 ;;
esac
if ! openssl x509 -in "$FULLCHAIN" -noout -checkend 0 >/dev/null 2>&1; then
    echo "ERROR: that certificate has already expired" >&2; exit 1
fi
cert_pub="$(openssl x509 -in "$FULLCHAIN" -noout -pubkey 2>/dev/null | openssl md5)"
key_pub="$(openssl ec -in "$KEY" -pubout 2>/dev/null | openssl md5 \
           || openssl rsa -in "$KEY" -pubout 2>/dev/null | openssl md5)"
[ "$cert_pub" = "$key_pub" ] || { echo "ERROR: that key does not match that certificate" >&2; exit 1; }

# Nothing to do if the volume already holds this exact certificate. This is
# meant to be run from cron after a renewal, and a cron job that reloads Caddy
# every week whether or not anything changed is a weekly chance to break the
# proxy for no reason.
installed="$(docker exec "$WRITER" sh -c 'cat /certs/public-fullchain.pem 2>/dev/null' | openssl md5 2>/dev/null || true)"
offered="$(openssl md5 < "$FULLCHAIN" 2>/dev/null || true)"
if [ -n "$installed" ] && [ "$installed" = "$offered" ] \
   && docker exec "$WRITER" test -s /certs/sites/public-chat.caddy 2>/dev/null; then
    exp="$(openssl x509 -in "$FULLCHAIN" -noout -enddate 2>/dev/null | cut -d= -f2)"
    echo "==> $NAME is already installed and served (expires $exp); nothing to do"
    exit 0
fi

echo "==> installing $NAME into $VOLUME via $WRITER"
docker cp "$FULLCHAIN" "$WRITER:/certs/public-fullchain.pem"
docker cp "$KEY" "$WRITER:/certs/public-key.pem"
docker exec "$WRITER" sh -c 'chmod 644 /certs/public-fullchain.pem && chmod 600 /certs/public-key.pem'

# The site block. Same upstream and the same flush_interval as the house name's
# block -- this is the same proxy answering to a second name, not a second
# service. Written into the volume rather than the Caddyfile so the Caddyfile
# stays a file the deployer bind-mounts verbatim, and so a house without a
# public certificate has no block naming files it does not have.
# `-i`, or the heredoc goes nowhere: `docker exec` without it does not forward
# stdin and the file lands empty. Caddy then imports an empty file, reloads
# without complaint and serves nothing for the name.
docker exec -i "$WRITER" sh -c "mkdir -p /certs/sites && cat > /certs/sites/public-chat.caddy" <<EOF
# Written by public-cert.sh. The public chat name, answered from inside the
# house so the Android app reaches the LAN copy instead of hairpinning out to
# the VPS and being told it is away.
${NAME} {
	tls /certs/public-fullchain.pem /certs/public-key.pem
	encode gzip
	reverse_proxy 127.0.0.1:{\$LOCAL_PROXY_PORT:21003} {
		flush_interval -1
	}
}
EOF

# The failure this catches is the one that happened here: an empty block is
# valid Caddyfile, imports with only a warning, reloads fine, and serves
# nothing at all for the name.
if [ "$(docker exec "$WRITER" sh -c 'wc -c < /certs/sites/public-chat.caddy' 2>/dev/null || echo 0)" -lt 40 ]; then
    echo "ERROR: the site block was not written" >&2
    docker exec "$WRITER" rm -f /certs/sites/public-chat.caddy
    exit 1
fi

CADDY="$(docker ps --filter 'name=proxy-caddy' --format '{{.Names}}' | head -1)"
[ -n "$CADDY" ] || { echo "ERROR: no proxy-caddy container is running" >&2; exit 1; }

if ! docker exec "$CADDY" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    echo "ERROR: the config would not validate; removing the block and leaving Caddy alone" >&2
    docker exec "$WRITER" rm -f /certs/sites/public-chat.caddy
    exit 1
fi
docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
echo "==> $NAME is served from this host"
echo
echo "Still needed, and not this script's to do:"
echo "  * a record on the house resolver: $NAME -> this host's LAN address"
echo "  * re-run this when the certificate renews"
