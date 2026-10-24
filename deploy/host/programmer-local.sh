#!/bin/sh
# The Programmer's local model: what the programmer-local units run.
#
#   programmer-local.sh pause    reserve the Studio's card, wait for its turn
#   programmer-local.sh run      serve the model on that card (exec, the unit's main process)
#   programmer-local.sh ready    wait until it answers
#   programmer-local.sh resume   end the reservation: the card goes back
#
# Settings come from programmer-local.env, which the deployer writes from
# `cloud.opencode.fallback` (deploy.py, write_programmer_local). Nothing here
# names a model, a card or a host of its own.
#
# Why the card is reserved rather than shared: the Studio owns it, and one of
# its video shots takes most of the 12 GB for ~25 minutes. A reservation
# (home-studio's studio/gpu.py) waits for the Studio's running job -- "wait
# for the job" is what the household chose (2026-09-30) -- and for anything
# else that reserved the card first, the coding benchmark included, so the
# Programmer's first turn on the fallback may wait too. While the model runs,
# a heartbeat every ten minutes keeps the reservation; fifteen without one
# and the Studio takes the card back.
set -eu

RESERVATION="${XDG_RUNTIME_DIR:-/tmp}/programmer-local.reservation"

studio() {
    # $1 = method, $2 = path, $3 = body. Prints the answer, then its status on
    # a line of its own. No Studio configured: nothing to reserve.
    [ -n "${STUDIO_URL:-}" ] && [ -n "${STUDIO_SECRET:-}" ] || return 1
    curl -sk --max-time 75 -X "$1" \
        -H "X-Studio-Secret: $STUDIO_SECRET" -H "X-Studio-User: programmer" \
        -H "X-Studio-Name: Programmer" -H "X-Studio-Admin: 1" \
        -H "Content-Type: application/json" ${3:+-d "$3"} \
        -w '\n%{http_code}\n' "$STUDIO_URL$2"
}

field() {
    # $1 = a key of the JSON answer on stdin (the status line after it ignored).
    python3 -c '
import json, sys
try:
    print(json.loads(sys.stdin.read().rsplit("\n", 1)[0]).get(sys.argv[1]) or "")
except ValueError:
    print("")' "$1"
}

status_of() { tail -n 1; }

heartbeat() {
    # Every ten minutes while the model runs. Gone (404): it expired or a
    # parent ended it -- the Studio's own check of the card's memory keeps it
    # from starting a job on top of this model, so stop beating and say so.
    rid=$(cat "$RESERVATION" 2>/dev/null) || return 0
    while sleep 600; do
        code=$(studio POST "/api/gpu/reservations/$rid/heartbeat" '{}' | status_of) || continue
        if [ "$code" = 404 ]; then
            echo "programmer-local: the card reservation is gone; the Studio may take the card back" >&2
            return 0
        fi
    done
}

case "${1:-}" in
    pause)
        # An hour is the estimate: the model stays up while turns keep coming
        # and goes after fifteen idle minutes, and that is roughly a session.
        answer=$(studio POST /api/gpu/reservations \
            '{"holder": "Programmer", "purpose": "the Programmer'"'"'s local model", "minutes": 60, "wait": 55}') \
            || { echo "programmer-local: no Studio to reserve the card from" >&2; exit 0; }
        rid=$(printf '%s' "$answer" | field id)
        if [ -z "$rid" ]; then
            echo "programmer-local: the Studio did not take the reservation ($(printf '%s' "$answer" | status_of))" >&2
            exit 0
        fi
        printf '%s' "$rid" > "$RESERVATION"
        state=$(printf '%s' "$answer" | field state)
        while [ "$state" != granted ]; do
            # Each ask waits up to 55 s for the turn and is a heartbeat too.
            # A Studio restarting keeps the reservation on disk: try again.
            answer=$(studio GET "/api/gpu/reservations/$rid?wait=55") || { sleep 10; continue; }
            case "$(printf '%s' "$answer" | status_of)" in
                200) state=$(printf '%s' "$answer" | field state) ;;
                404) echo "programmer-local: the reservation ended before its turn" >&2; exit 1 ;;
                *)   sleep 10 ;;
            esac
        done
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
        # The heartbeat lives in the unit's cgroup and is stopped with it.
        heartbeat &
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
        rid=$(cat "$RESERVATION" 2>/dev/null) || exit 0
        studio DELETE "/api/gpu/reservations/$rid" >/dev/null \
            || echo "programmer-local: the Studio did not answer; the reservation ends on its own in 15 minutes" >&2
        rm -f "$RESERVATION"
        ;;
    *)
        echo "usage: $0 pause|run|ready|resume" >&2; exit 2 ;;
esac
