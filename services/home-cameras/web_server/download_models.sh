#!/usr/bin/env bash
# download_models.sh - fetch the YOLO weights the web server needs.
#
# The weights were deleted from the repo (commit cc0ab21, "Deleted models"):
# ~330 MB of binaries bloated every clone and every build. They are instead
# fetched from the Ultralytics asset releases on demand, into this script's
# directory (web_server/models/), which is exactly where the code reads them
# (models/yolo26x.pt relative to /app/web_server in the container, and to the
# checkout when run locally). Only missing files are downloaded, so re-runs are
# no-ops and a fresh clone needs exactly one run.
#
# deploy/manifest.yml runs this as the `pre:` step for the camera web unit,
# before `docker compose up`, so a deploy onto a fresh host fetches what it
# needs instead of starting a detector with no weights.
#
# Models referenced by the code:
#   yolo26m.pt - per-camera motion detectors, GPU (object_detector.py default)
#   yolo26s.pt - per-camera motion detectors, CPU fallback
#   yolo26x.pt - scene detection for /api/detect (SCENE_DETECT_MODEL default)

set -u

# The weights outlive the checkout, so they live at the absolute path the
# deployer bind-mounts, not beside this script. Falling back to the script's own
# directory keeps a local run working.
DIR="${CAMERAS_MODELS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/models}"
mkdir -p "$DIR"

download() {
    local name="$1"
    if [ -s "$DIR/$name" ]; then
        echo "  $name already present"
        return 0
    fi
    echo "  downloading $name ..."
    curl -fL --retry 3 -o "$DIR/$name" \
        "https://github.com/ultralytics/assets/releases/latest/download/$name" || return 1
}

fail=""
for m in yolo26m.pt yolo26s.pt yolo26x.pt; do
    if ! download "$m"; then
        fail="$fail $m"
    fi
done

echo
if [ -n "$fail" ]; then
    echo "FAILED to download:$fail"
    exit 1
fi
echo "All models present in $DIR"
