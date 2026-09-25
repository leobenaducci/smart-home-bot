#!/usr/bin/env bash
# Make sure /certs holds something Caddy can start with, by the best means
# available right now. This is what the Jenkins stage calls; the two scripts it
# delegates to are also fine to run by hand.
#
#   ACME_DNS_API_TOKEN set  → issue-cert.sh: the real *.home from
#                              Let's Encrypt, renewed unattended from then on.
#   not set, or it failed    → local-cert.sh: a certificate from a CA generated
#                              on this box. Everything works; browsers complain
#                              until the CA is installed, and the Android app
#                              complains regardless (see local-cert.sh).
#
# The fallback exists because the ACME path depends on a third party being
# reachable — on 2026-08-14 the Hostinger API was not — and Caddy will not
# start without certificate files, which would have made an outage over there
# an outage in here.
#
# "or it failed" is the part worth reading twice. Having the token is not the
# same as the token working: on 2026-08-14 the credential was present and
# correct and issuance still failed, because compose was handing it to acme.sh
# under a name the dnsapi plugin does not read. Treating that as fatal made a
# *broken* ACME path strictly worse than a *missing* one — no certificate, so
# no Caddy, so no chat and no residencia — which inverts the reason the
# fallback was written. So a failed issue drops through to the local CA and the
# stage keeps going, loudly.
#
# Going back is one run of issue-cert.sh: it writes the same two filenames, so
# nothing about Caddy's configuration changes, and local-cert.sh refuses to
# overwrite a certificate it did not issue.
set -euo pipefail
cd "$(dirname "$0")"

if [ "${ACME_MODE:-local-ca}" = "acme-dns" ] && [ -n "${ACME_DNS_API_TOKEN:-}" ]; then
    if ./issue-cert.sh; then
        exit 0
    fi
    echo "WARN: ------------------------------------------------------------"
    echo "WARN: the token IS set and issuance failed anyway. Read the acme.sh"
    echo "WARN: output above rather than assuming a missing credential."
    echo "WARN: falling back to the local CA so the proxy still comes up."
    echo "WARN: ------------------------------------------------------------"
else
    echo "==> certificate mode: local CA (nothing outbound)"
fi

echo "WARN: browsers on the LAN will warn until proxy/certs/ca.pem is installed,"
echo "WARN: and the home-chat Android app will refuse the connection outright."
echo "WARN: re-run ./issue-cert.sh with the token to replace this."
exec ./local-cert.sh
