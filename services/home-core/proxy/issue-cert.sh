#!/usr/bin/env bash
# Issue (or renew, or just re-install) the *.home certificate the
# proxy serves, and leave acme.sh configured to keep doing it unattended.
#
# Run this before `docker compose up -d`: Caddy names the certificate files
# explicitly, so it will not start at all while /certs is empty. The Deploy
# proxy stage gets there through `ensure-cert.sh`, which calls this when the
# token is available and falls back to `local-cert.sh` when it is not. Run this
# one by hand the moment the token exists again — it overwrites the local CA's
# output in place, and nothing about Caddy's configuration changes.
#
# Requires ACME_DNS_API_TOKEN in the environment, and ACME_DNS_PROVIDER
# naming the acme.sh dnsapi plugin to drive (dns_cf, dns_hostinger, ...).
# The provider used to be hardcoded, which made the documented `acme-dns`
# certificate mode reachable only for one registrar. (Formerly the
# same name). The challenge is DNS-01, so nothing has to be reachable from the
# internet — which is the point, since hub is not.
set -euo pipefail

# acme.sh looks for a variable named after the plugin, so map the one generic
# name this package documents onto whatever the chosen plugin expects.
: "${ACME_DNS_PROVIDER:=dns_cf}"
: "${ACME_DNS_API_TOKEN:?ACME_DNS_API_TOKEN is required for the acme-dns certificate mode}"
case "$ACME_DNS_PROVIDER" in
    dns_cf)        export CF_Token="$ACME_DNS_API_TOKEN" ;;
    # `HOSTINGER_Token`, which is the name dnsapi/dns_hostinger.sh reads. It was
    # `HOSTINGER_Api_Token`, which that plugin does not look at -- so issuance
    # failed with "You didn't specify a Hostinger API Key yet" while the
    # credential sat right there in the environment. That is the failure
    # ensure-cert.sh's comment describes as a name the plugin does not read, and
    # it was still this line. `HOSTINGER_Api` in that plugin is the endpoint URL,
    # not a credential, which is what makes the wrong name look plausible.
    dns_hostinger) export HOSTINGER_Token="$ACME_DNS_API_TOKEN" ;;
    dns_gd)        export GD_Key="$ACME_DNS_API_TOKEN" ;;
    dns_namecheap) export NAMECHEAP_API_KEY="$ACME_DNS_API_TOKEN" ;;
    *)  # Pass it through under the conventional name and let acme.sh decide.
        export ACME_DNS_API_TOKEN ;;
esac
cd "$(dirname "$0")"


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

DOMAIN="${DOMAIN:-$(config_domain || true)}"
[ -n "$DOMAIN" ] || { echo "issue-cert: DOMAIN is not set and site.domain is not readable; refusing to guess a domain." >&2; exit 2; }

# Caddy reads certificates when it loads its config and not afterwards, so a
# renewal that only replaces the files changes nothing until it restarts. This
# runs inside the acme container, which is why it talks to the Docker API over
# the socket rather than calling the docker CLI it does not have. `|| true`:
# on the very first run the container does not exist yet, and a renewal that
# succeeded should not be reported as a failure because the restart 404ed.
RELOAD_CMD="curl -fsS --unix-socket /var/run/docker.sock -X POST http://localhost/containers/home-proxy-caddy/restart >/dev/null || true"

echo "==> issuing ${DOMAIN} + *.${DOMAIN} (${ACME_DNS_PROVIDER})"
# acme.sh exits 2 when the certificate exists and is not due for renewal. That
# is the normal outcome of every deploy after the first, not an error.
rc=0
docker compose run --rm acme \
    --issue \
    --server letsencrypt \
    --dns "${ACME_DNS_PROVIDER}" \
    -d "${DOMAIN}" \
    -d "*.${DOMAIN}" \
    --keylength ec-256 \
    --fullchain-file /certs/fullchain.pem \
    --key-file /certs/key.pem \
    --reloadcmd "${RELOAD_CMD}" || rc=$?
if [ "${rc}" -ne 0 ] && [ "${rc}" -ne 2 ]; then
    echo "FAIL: acme.sh --issue exited ${rc}" >&2
    exit "${rc}"
fi

# Unconditional, and not redundant: when --issue skips as up-to-date it writes
# nothing, so a recreated (or newly empty) certs volume would still be empty
# and Caddy would refuse to start. --install-cert re-writes the files from the
# stored certificate every time, and re-records the reload command.
echo "==> installing certificate files into the shared volume"
docker compose run --rm acme \
    --install-cert \
    -d "${DOMAIN}" \
    --ecc \
    --fullchain-file /certs/fullchain.pem \
    --key-file /certs/key.pem \
    --reloadcmd "${RELOAD_CMD}"

echo "==> certificate in place:"
docker compose run --rm --entrypoint sh acme -c \
    'openssl x509 -in /certs/fullchain.pem -noout -subject -ext subjectAltName -enddate'
