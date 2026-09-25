#!/usr/bin/env bash
# Stand-in certificate for the two LAN names, from a CA generated here.
#
#   ./local-cert.sh              # make one if there isn't a usable one
#   ./local-cert.sh --force      # make one regardless
#
# This exists because a publicly trusted certificate needs something we do not
# always have: DNS-01 needs the Hostinger API, HTTP-01 needs a host the
# internet can reach, and hub is neither. Without a certificate Caddy will
# not start at all, so the whole split horizon would be blocked on an outage
# somewhere else. `ensure-cert.sh` calls this only when the ACME path is
# unavailable, and `issue-cert.sh` overwrites its output the moment it is.
#
# It writes a CA, not a bare self-signed leaf, and the difference is the only
# reason it is worth the extra twenty lines: a leaf can never be made trusted
# on Android, while a CA can be installed once per device and then covers every
# name this proxy will ever serve. Install `certs/ca.pem` (written next to this
# script) on the family's machines to get rid of the warnings:
#
#   Linux/macOS browsers, iOS  → import it as a trusted root
#   Android                    → Settings → Security → Encryption & credentials
#                                → Install a certificate → CA certificate
#
# One caveat that will bite before anything else does: **the home-chat Android
# app will still refuse this certificate.** Since Android 7, apps and their
# WebViews ignore user-installed CAs unless the app ships a
# network_security_config that opts in. Browsers on the LAN will be fine, the
# app will not be, and the fix for the app is a real certificate — not another
# knob here.
set -euo pipefail
cd "$(dirname "$0")"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

CA_CN="Casa Doe local CA"
# 397 days: Apple stopped trusting longer-lived server certificates, and there
# is no reason to hand the family a certificate their phones will argue with.
# Re-running this reissues; it is meant to be temporary anyway.
DAYS_LEAF=397
DAYS_CA=3650
# Built from what the deploy passes, not written here. These were literals --
# `chat.home`, `house.home`, `*.home` and an IP on a network
# nobody is on -- so any household whose `site.domain` differed got
# a certificate for names it does not use, and the deploy's own verify
# (`{dns.portal}` served with a trusted certificate) could never pass. Nothing
# caught it: this file is run, not rendered, so a literal in it is invisible.
#
# The wildcard covers everything under the domain, which is what makes one
# certificate serve every service name at once.

# The domain is the household's, so there is no sensible value to invent for it.
# This used to default to a literal suffix, which meant a run with the
# variable unset quietly issued certificates for names the house does not have
# -- and a certificate for the wrong name fails exactly like a broken one. Read
# the setting the admin page writes; refuse if there is nothing to read.
config_domain() {
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
    # Comment and quotes stripped: `domain: home  # ours` is a legal setting,
    # and carrying the comment into SANS puts it in the certificate.
    sed -n 's/^[[:space:]]*domain:[[:space:]]*//p' "$cfg" | head -1 \
        | sed -e 's/[[:space:]]*#.*$//' -e 's/[[:space:]]*$//' | tr -d '"'"'"''
}

: "${DOMAIN:=$(config_domain || true)}"
[ -n "$DOMAIN" ] || { echo "local-cert: DOMAIN is not set and site.domain is not readable; refusing to guess a domain." >&2; exit 2; }
SANS=""
for name in $(printf '%s' "${CERT_NAMES:-}" | tr ',' ' '); do
    [ -n "$name" ] || continue
    case ",$SANS," in *",DNS:$name,"*) continue ;; esac
    SANS="${SANS:+$SANS,}DNS:$name"
done
SANS="${SANS:+$SANS,}DNS:*.$DOMAIN,DNS:$DOMAIN"
[ -n "${CERT_IP:-}" ] && SANS="$SANS,IP:$CERT_IP"

# The certs live in the compose volume, which is also where acme.sh puts the
# real ones. Everything below runs in a throwaway alpine so this does not
# depend on the host having openssl, or on which host it is run from.
# Being told beats guessing, and the deployer tells us: it exports the same
# COMPOSE_PROJECT_NAME it runs compose with. The search below is the fallback
# for running this by hand, and it is only a fallback because it cannot tell
# two candidates apart -- once a wrong-project volume exists, `head -1` picks
# by luck, and the losing guess writes a certificate Caddy never reads.
if [ -n "${COMPOSE_PROJECT_NAME:-}" ]; then
    VOLUME="${COMPOSE_PROJECT_NAME}_proxy-certs"
else
    VOLUME="$(docker volume ls -q | grep -E '_proxy-certs$' | head -1 || true)"
    # Empty on the very first run, before `docker compose up` has created it.
    # Compose names it <project>_proxy-certs, after this directory.
    [ -n "$VOLUME" ] || VOLUME="$(basename "$PWD")_proxy-certs"
fi
docker volume create "$VOLUME" >/dev/null
echo "==> certificate volume: ${VOLUME}"

docker run --rm -v "${VOLUME}:/certs" -e FORCE="$FORCE" -e DAYS_LEAF="$DAYS_LEAF" \
    -e DAYS_CA="$DAYS_CA" -e SANS="$SANS" -e CA_CN="$CA_CN" \
    -e DOMAIN="$DOMAIN" alpine:3 sh -euc '
apk add --no-cache openssl >/dev/null

# Refuse to stand on top of a real certificate. Re-running this after the
# Hostinger outage clears would otherwise quietly downgrade every device on the
# LAN from a trusted certificate to one that warns.
if [ -s /certs/fullchain.pem ]; then
    issuer=$(openssl x509 -in /certs/fullchain.pem -noout -issuer)
    case "$issuer" in
        *"$CA_CN"*) ;;
        *) echo "REFUSING: /certs/fullchain.pem was issued by${issuer#*=} - not replacing a real certificate" >&2
           echo "          (delete it by hand if that is really what you want)" >&2
           exit 1 ;;
    esac
    # Still valid is not the same as still correct. This checked the expiry
    # alone, so renaming the house -- a change to `site.domain` --
    # left a certificate for the old names in place for a year, and the deploy
    # failed its own verify with no way to get past it short of deleting the
    # file by hand. Reissue when the names have changed too.
    # No single quotes anywhere in here: this whole block is the body of
    # `sh -euc \x27...\x27` below, so one would close that string and the file
    # stops parsing. Double quotes and $\x27..\x27 instead.
    have=$(openssl x509 -in /certs/fullchain.pem -noout -ext subjectAltName 2>/dev/null \
           | tr -d " " | tr "," "\n" | grep -E "^(DNS|IPAddress):" \
           | sed "s/IPAddress:/IP:/" | sort | tr "\n" ",")
    want=$(printf "%s" "$SANS" | tr -d " " | tr "," "\n" | sort | tr "\n" ",")
    if [ "$FORCE" = 0 ] && [ "$have" = "$want" ] \
       && openssl x509 -in /certs/fullchain.pem -noout -checkend $((30*86400)); then
        echo "==> existing local certificate is good for another 30+ days and covers the same names; nothing to do"
        openssl x509 -in /certs/fullchain.pem -noout -subject -enddate
        exit 0
    fi
    [ "$have" = "$want" ] || echo "==> the names changed; reissuing"
fi

# The CA is generated once and kept: reissuing the leaf under the same CA means
# the devices that already trust it stay trusting it.
if [ ! -s /certs/ca.key ]; then
    echo "==> generating the CA (once; devices trust this, not the leaf)"
    openssl ecparam -name prime256v1 -genkey -noout -out /certs/ca.key
    chmod 600 /certs/ca.key
    openssl req -x509 -new -key /certs/ca.key -sha256 -days "$DAYS_CA" \
        -out /certs/ca.pem -subj "/O=$DOMAIN/CN=$CA_CN" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign"
else
    echo "==> reusing the existing CA"
fi

echo "==> issuing the leaf"
openssl ecparam -name prime256v1 -genkey -noout -out /certs/key.pem
chmod 600 /certs/key.pem
# The CN is the first name in SANS, not a literal. It was `chat.home`,
# which is why a certificate issued for this household still announced itself
# as somebody else\x27s host. Modern clients read subjectAltName and ignore the
# CN, but it is the line a person sees first when they inspect the thing.
LEAF_CN=$(printf "%s" "$SANS" | tr "," "\n" | sed -n "s/^DNS://p" | head -1)
openssl req -new -key /certs/key.pem -subj "/CN=${LEAF_CN:-$DOMAIN}" -out /tmp/leaf.csr
# busybox sh has no process substitution, hence the temp file.
printf "subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n" \
    "$SANS" > /tmp/leaf.ext
openssl x509 -req -in /tmp/leaf.csr -CA /certs/ca.pem -CAkey /certs/ca.key \
    -CAcreateserial -days "$DAYS_LEAF" -sha256 -extfile /tmp/leaf.ext -out /tmp/leaf.pem

# Caddy is pointed at fullchain.pem, so the CA has to be in it: a client that
# trusts the CA still needs the chain to get from the leaf to it.
cat /tmp/leaf.pem /certs/ca.pem > /certs/fullchain.pem
openssl x509 -in /certs/fullchain.pem -noout -subject -ext subjectAltName -enddate
'

# A copy outside the volume, because "install this on your phone" is the whole
# point and `docker run` is a silly way to ask for a file.
mkdir -p certs
docker run --rm -v "${VOLUME}:/certs" alpine:3 cat /certs/ca.pem > certs/ca.pem
echo "==> CA for the family's devices: $(pwd)/certs/ca.pem"

# Only matters on a re-issue; on first run Caddy is not up yet.
if docker ps --format '{{.Names}}' | grep -qx home-proxy-caddy; then
    echo "==> restarting Caddy to pick it up"
    docker restart home-proxy-caddy >/dev/null
fi
