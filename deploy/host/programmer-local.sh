#!/bin/sh
# The Programmer's local model: what the programmer-local units run.
#
#   programmer-local.sh pause    lend the card: pause the Studio, wait for its job
#   programmer-local.sh run      serve the model on that card (exec, the unit's main process)
#   programmer-local.sh ready    wait until it answers
#   programmer-local.sh resume   give the card back to the Studio
#
# Settings come from programmer-local.env, which the deployer writes from
# `cloud.opencode.fallback` (deploy.py, write_programmer_local). Nothing here
# names a model, a card or a host of its own.
#
# Why the Studio is paused rather than shared: it owns its card, and one of its
# video shots takes most of the 12 GB for ~25 minutes. A pause stops it taking
# the next job; the running one finishes, then its worker unloads. This waits
# for that -- "wait for the job" is what the household chose (2026-09-30) --
# so the Programmer's first turn on the fallback may wait too.
set -eu

studio() {
    # $1 = pause | resume. No Studio configured: nothing to lend, nothing to give back.
    [ -n "${STUDIO_URL:-}" ] && [ -n "${STUDIO_SECRET:-}" ] || return 0
    curl -sk --max-time 20 -X POST \
        -H "X-Studio-Secret: $STUDIO_SECRET" -H "X-Studio-User: programmer" \
        -H "X-Studio-Name: Programmer" -H "X-Studio-Admin: 1" \
        -H "Content-Type: application/json" -d '{"reason": "programmer"}' \
        "$STUDIO_URL/api/admin/$1" >/dev/null
}

studio_busy() {
    # True while the Studio still has a worker on the card or a job running.
    [ -n "${STUDIO_URL:-}" ] && [ -n "${STUDIO_SECRET:-}" ] || return 1
    curl -sk --max-time 20 -H "X-Studio-Secret: $STUDIO_SECRET" \
        -H "X-Studio-User: programmer" -H "X-Studio-Name: Programmer" -H "X-Studio-Admin: 1" \
        "$STUDIO_URL/api/queue" | python3 -c '
import json, sys
try:
    s = json.load(sys.stdin).get("status") or {}
except ValueError:
    sys.exit(1)      # an unreadable answer is not proof of a busy card
sys.exit(0 if s.get("worker") or s.get("running") else 1)'
}

case "${1:-}" in
    pause)
        studio pause || echo "programmer-local: the Studio did not answer the pause" >&2
        while studio_busy; do sleep 10; done
        ;;
    run)
        model="$MODEL"
        case "$model" in
            /*) path="$model" ;;
            *)  path=$(ollama show --modelfile "$model" | awk '/^FROM /{print $2; exit}') ;;
        esac
        [ -r "$path" ] || { echo "programmer-local: no model file for $model" >&2; exit 1; }
        export CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES="$GPU"
        export LD_LIBRARY_PATH="$LLAMA_BIN:$LLAMA_BIN/../lib"
        exec "$LLAMA_BIN/llama-server" -m "$path" --alias "$MODEL" \
            --host 127.0.0.1 --port "$BACKEND_PORT" -c "$CONTEXT" -np 1 -ngl 999 \
            -fa on -ctk q8_0 -ctv q8_0 --jinja
        ;;
    ready)
        i=0
        until curl -sf --max-time 5 "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null; do
            i=$((i + 1)); [ "$i" -lt 120 ] || exit 1; sleep 1
        done
        ;;
    resume)
        studio resume || echo "programmer-local: the Studio did not answer the resume" >&2
        ;;
    *)
        echo "usage: $0 pause|run|ready|resume" >&2; exit 2 ;;
esac
