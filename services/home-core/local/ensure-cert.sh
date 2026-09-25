#!/usr/bin/env bash
# Make sure the portal has a certificate to start with.
#
# app.py serves HTTPS and nothing else: `make_server(..., ssl_context=(...))`.
# Without the two files it does not fail gracefully — it raises
# FileNotFoundError before the first request and the container crash-loops,
# which reads as a broken build rather than a missing file.
#
# There used to be a cert.pem and a key.pem committed to this directory and
# baked into the image. Two things were wrong with that. The mount at /certs is
# a bind mount from the state directory, so it *shadowed* the baked pair with
# an empty host directory and the app could not start anyway — a fresh install
# never came up. And a TLS private key in a git repository is a private key
# every clone has.
#
# So the pair is generated here, on the machine, on first deploy, and kept in
# the state directory where it survives a redeploy and is never pushed
# anywhere. Regenerating is deleting the two files and deploying again.
#
# This is the portal's own listener. The certificate a browser is asked to
# trust is the proxy's, from proxy/ensure-cert.sh, and that one is separate on
# purpose: the proxy terminates the connection a person makes, this one only
# has to satisfy the proxy talking to the app behind it.
set -euo pipefail

CERTS="${HOMECORE_CERTS_DIR:?HOMECORE_CERTS_DIR is not set — the manifest supplies it}"
HOST="${SITE_HOST:-localhost}"
DAYS="${CERT_DAYS:-3650}"

mkdir -p "$CERTS"

# Present and not expiring within the month: leave it alone. Regenerating on
# every deploy would hand the proxy a new key each time for no reason.
if [ -s "$CERTS/cert.pem" ] && [ -s "$CERTS/key.pem" ]; then
    if openssl x509 -in "$CERTS/cert.pem" -noout -checkend $((30 * 86400)) >/dev/null 2>&1; then
        echo "certificate: keeping the one in $CERTS"
        openssl x509 -in "$CERTS/cert.pem" -noout -subject -enddate
        exit 0
    fi
    echo "certificate: the one in $CERTS expires within 30 days — reissuing"
fi

echo "certificate: issuing a self-signed pair for $HOST in $CERTS"
openssl req -x509 -newkey rsa:2048 -nodes -sha256 \
    -days "$DAYS" \
    -keyout "$CERTS/key.pem" \
    -out "$CERTS/cert.pem" \
    -subj "/CN=$HOST" \
    -addext "subjectAltName=DNS:$HOST,DNS:localhost,IP:127.0.0.1" \
    2>/dev/null
# The app runs as the deploying user, not root, and reads the key on every
# start.
chmod 600 "$CERTS/key.pem"
chmod 644 "$CERTS/cert.pem"
openssl x509 -in "$CERTS/cert.pem" -noout -subject -enddate
