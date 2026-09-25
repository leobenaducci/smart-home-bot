#!/usr/bin/env bash
# Re-fetch the house faces from Google Fonts and vendor them.
#
# The pages used to link fonts.googleapis.com directly. That is a network
# dependency on machines that are mostly LAN-only, and a render-blocking one:
# a router that DROPs outbound 443 does not fail the request, it waits out the
# TCP timeout, which is tens of seconds of blank page for a decorative font.
# So the files live in the repos and each app serves its own copy — nothing
# here depends on another box, or on the internet, being reachable.
#
# Four copies on purpose, for the same reason each page carries its own copy of
# The House block: HomeCameras is on another machine, and the MQTT dashboard
# is another origin on this one. A font is a CORS-restricted subresource, and
# an http:// page cannot pull one from an https:// origin with a self-signed
# certificate — so "one provider" would have meant three apps silently falling
# back. 544 KB each, next to the vendored KaTeX that is here for the same
# reason.
#
# Usage:  HomeCore/tools/fetch-fonts.sh [destination ...]
# With no destination it writes to all four apps, relative to this checkout.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK="$(cd "$HERE/../.." && pwd)"

# latin and latin-ext only. This house writes Spanish and every accent it needs
# — á é í ó ú ñ ü ¿ ¡ — is in latin; latin-ext is cheap insurance for a name.
# Cyrillic, Greek and Vietnamese were two thirds of the download and would
# never have been drawn.
SUBSETS="latin latin-ext"
API="https://fonts.googleapis.com/css2"
QUERY="family=Fraunces:opsz,wght@9..144,600..900&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap"
# Without a browser UA, Google serves the ttf stylesheet instead of woff2.
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

DESTS=("$@")
if [ ${#DESTS[@]} -eq 0 ]; then
    DESTS=(
        "$STACK/HomeCore/local/static/vendor/fonts"
        "$STACK/HomeCameras/web_server/static/fonts"
        "$STACK/mqtt-dashboard/dashboard/public/fonts"
        "$STACK/finance_helper/src/fonts"
    )
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "Fetching the stylesheet…"
curl -fsS -A "$UA" "$API?$QUERY" -o "$WORK/google.css"

python3 - "$WORK" "$SUBSETS" <<'PY'
import pathlib, re, sys, urllib.request

work, subsets = pathlib.Path(sys.argv[1]), sys.argv[2].split()
css = (work / "google.css").read_text(encoding="utf-8")
# Google labels each @font-face with a /* subset */ comment on the line above.
faces = re.findall(r"/\* (\S+) \*/\s*(@font-face \{.*?\})", css, re.S)
kept = []
for subset, block in faces:
    if subset not in subsets:
        continue
    family = re.search(r"font-family: '([^']+)'", block).group(1)
    weight = re.search(r"font-weight: ([^;]+);", block).group(1).strip()
    url = re.search(r"url\((https://[^)]+\.woff2)\)", block).group(1)
    name = f"{family.replace(' ', '')}-{weight.replace(' ', '_')}-{subset}.woff2"
    print(f"  {name}")
    (work / name).write_bytes(urllib.request.urlopen(url).read())
    kept.append((subset, block.replace(url, name)))

if not kept:
    raise SystemExit("no faces matched — did the stylesheet format change?")

header = """/* The House — the house faces, served from here.

   Fraunces names the page, IBM Plex Sans is what you read, IBM Plex Mono is
   anything you count. They used to come from fonts.googleapis.com, which is a
   network dependency on machines that are mostly LAN-only — and a
   render-blocking one, since a router that DROPs outbound 443 does not fail
   the request, it waits out the TCP timeout.

   Now each app serves its own copy: same origin as the pages, no third party
   sees the family browsing, and the house works with the internet unplugged.

   latin and latin-ext only — every accent Spanish needs is in latin.

   Do not edit by hand. Regenerate with HomeCore/tools/fetch-fonts.sh. */
"""
out = header + "\n" + "\n".join(f"/* {s} */\n{b}" for s, b in kept) + "\n"
(work / "house.css").write_text(out, encoding="utf-8")
print(f"  house.css ({len(kept)} faces)")
PY

for dest in "${DESTS[@]}"; do
    mkdir -p "$dest"
    rm -f "$dest"/*.woff2 "$dest"/house.css
    cp "$WORK"/*.woff2 "$WORK/house.css" "$dest/"
    echo "→ $dest ($(du -sh "$dest" | cut -f1))"
done

echo "Done. The pages link it as house.css; nothing else needs changing."
