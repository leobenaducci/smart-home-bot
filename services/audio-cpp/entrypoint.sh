#!/usr/bin/env bash
# Install the model packages if they are not cached, write server.json from the
# environment, and start audiocpp_server.
#
# Generated rather than committed, for the reason the deployer applies
# everywhere else: which model this house runs is a *setting*, and a
# server.json in the repo would be one household's answer baked into
# everyone's copy. The packages below are the bench's starting point, not a
# claim that they are the right ones -- `python3 tools/model_manager_v2.py
# list` inside this container is the authority on what exists.
set -euo pipefail

MODELS="${AUDIOCPP_MODELS:-/models}"
PORT="${AUDIOCPP_PORT:-8080}"
THREADS="${AUDIOCPP_THREADS:-4}"
# cpu unless the GPU overlay says otherwise. This has to match what the image
# was *built* with: a cuda backend in a cpu-built image fails at startup, which
# is the better of the two failures -- the reverse runs, on the CPU, at CPU
# speed, and looks like a disappointing GPU result.
BACKEND="${AUDIOCPP_BACKEND:-cpu}"

# **Lists, comma-separated.** audio.cpp ships 33 TTS families and one of them
# has to be better than the first one tried, so the server registers as many
# as are asked for and `test/profile_tts.py` walks them. `lazy_load` below is
# what makes that affordable: a registered model costs a path until something
# calls it.
#
# Sizes are measured with `model_manager_v2.py sizes <id>`, never estimated
# from the parameter count -- a package carries a codec and a tokenizer too,
# and the estimate was wrong by 3x in both directions. The two defaults are
# 1.99 GB and 1.15 GB, and none of it is VRAM, which is the whole reason this
# is worth measuring.
#
# These are **package ids**, not the Hugging Face directory names. A package is
# not one file: the .gguf listed under the HF repo is an *index* -- about a
# kilobyte of `audiocpp.model_spec.json` pointing at the real weights -- so
# fetching it with curl yields a valid GGUF containing no model. That is how
# this was first written, and the server came up holding a 1058-byte "model".
# `model_manager_v2.py` resolves a package to the files it is made of.
TTS_PKGS="${AUDIOCPP_TTS_PACKAGES:-${AUDIOCPP_TTS_PACKAGE:-qwen3_tts_0_6b_base_q8_0}}"
ASR_PKGS="${AUDIOCPP_ASR_PACKAGES:-${AUDIOCPP_ASR_PACKAGE:-qwen3_asr_0_6b_q8_0}}"
# `none` is a deliberate empty list. A blank is not: compose's `${VAR:-default}`
# fills a blank back in, so the deployer sends the word and it is dropped here.
# A household whose voice gateway transcribes with faster-whisper has no ASR to
# serve, and until 2026-09-21 the only way to say so was to keep a 1.15 GB
# model on the card.
[ "$TTS_PKGS" = "none" ] && TTS_PKGS=""
[ "$ASR_PKGS" = "none" ] && ASR_PKGS=""

# The family used to be an environment variable next to the package, and that
# was a second place to get it wrong: `qwen3_tts_0_6b_base_q8_0` with
# `AUDIOCPP_TTS_FAMILY=qwen3_asr` is a config the server accepts and then
# fails on. The spec knows -- `model_manager_v2.py info <pkg>` prints the
# family it belongs to -- so it is read, not declared. Say so rather than
# ignoring a variable somebody set on purpose.
if [ -n "${AUDIOCPP_TTS_FAMILY:-}${AUDIOCPP_ASR_FAMILY:-}" ]; then
    echo "models: AUDIOCPP_{TTS,ASR}_FAMILY are no longer read -- the family" >&2
    echo "        now comes from the package spec, which cannot disagree" >&2
    echo "        with the weights. Ignoring them." >&2
fi

# A package id is NOT the directory it lands in: `qwen3_tts_0_6b_base_q8_0`
# installs into `Qwen3-TTS-12Hz-0.6B-Base-GGUF`, the Hugging Face repo path.
# Guessing that the two match downloaded 3.1 GB successfully and then exited 1
# on `find: /models/qwen3_tts_0_6b_base_q8_0: No such file or directory`.
#
# So ask the tool. `--dry-run` resolves the package and prints `target <dir>`
# without fetching anything, which is the same resolution `install` uses.
pkg_dir() {   # pkg_dir <package-id> -> the directory it installs into
    ( cd /app && python3 tools/model_manager_v2.py install "$1" \
        --models-root "$MODELS" --dry-run 2>/dev/null ) \
        | awk '/^target /{print $2; exit}'
}

pkg_family() {   # pkg_family <package-id> -> the family the spec puts it in
    ( cd /app && python3 tools/model_manager_v2.py info "$1" 2>/dev/null ) \
        | awk '/^family:/{print $2; exit}'
}

install_pkg() {   # install_pkg <package-id> -> echoes the .gguf path
    local dir gguf
    dir="$(pkg_dir "$1")"
    if [ -z "$dir" ]; then
        echo "models: '$1' does not resolve to anything." >&2
        echo "        'python3 /app/tools/model_manager_v2.py list' inside" >&2
        echo "        this container is the authority on what exists." >&2
        return 1
    fi
    gguf="$(find "$dir" -name '*.gguf' 2>/dev/null | head -1)"
    if [ -n "$gguf" ]; then
        echo "models: $1 already present in $dir" >&2
    else
        echo "models: installing $1 into $dir ..." >&2
        ( cd /app && python3 tools/model_manager_v2.py install "$1" \
            --models-root "$MODELS" ) || return 1
        gguf="$(find "$dir" -name '*.gguf' 2>/dev/null | head -1)"
    fi
    if [ -z "$gguf" ]; then
        echo "models: no .gguf under $dir after installing $1" >&2
        return 1
    fi
    printf '%s' "$gguf"
}

# One JSON object per package. **The id is the package id**, not `tts`/`asr`:
# with several of each registered, a generic id names whichever one the
# entrypoint happened to write first, and a profile row headed `tts` says
# nothing about which model produced it.
entries=""
add_entries() {   # add_entries <task> <comma-separated package ids>
    local task="$1" list="$2" pkg path family
    while IFS= read -r pkg; do
        [ -n "$pkg" ] || continue
        path="$(install_pkg "$pkg")" || return 1
        family="$(pkg_family "$pkg")"
        if [ -z "$family" ]; then
            echo "models: no family in the spec for '$pkg'" >&2
            return 1
        fi
        [ -n "$entries" ] && entries="${entries},"
        entries="${entries}
    {
      \"id\": \"${pkg}\",
      \"family\": \"${family}\",
      \"path\": \"${path}\",
      \"task\": \"${task}\"
    }"
    done <<< "$(printf '%s' "$list" | tr ',' '\n' | tr -d ' ')"
}

add_entries tts "$TTS_PKGS" || exit 1
add_entries asr "$ASR_PKGS" || exit 1

cat > /app/server.json <<JSON
{
  "host": "0.0.0.0",
  "port": ${PORT},
  "backend": "${BACKEND}",
  "threads": ${THREADS},
  "lazy_load": true,
  "models": [${entries}
  ]
}
JSON

echo "config: $(cat /app/server.json)" >&2
exec audiocpp_server --config /app/server.json
