#!/usr/bin/env bash
# Does the installer actually run on a machine that has nothing?
#
#   ./deploy/test_install.sh
#
# It shipped broken for four commits and nobody noticed, because every check
# was on a box where install.sh had already been run: `live_secrets_file` ended
# in an `&&` chain that returns 1 when there is no deployed copy, which under
# `set -euo pipefail` took the whole installer down at the first generated key.
# Reading the function does not show that. Running it does.
#
# So this runs the real thing against a throwaway tree with every path pointed
# at scratch, and asserts it finishes and produces what it promises.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

failures=0
check() {
    if [ "$2" = "0" ]; then printf '  PASS  %s\n' "$1"
    else printf '  FAIL  %s  <- %s\n' "$1" "${3:-}"; failures=$((failures + 1)); fi
}

# A clone, so this tests what a person would get rather than the working tree.
git -C "$ROOT" archive HEAD | (mkdir -p "$TMP/clone" && tar -x -C "$TMP/clone") 2>/dev/null \
    || { echo "  SKIP: not a git checkout"; exit 0; }
cp "$ROOT/deploy/install.sh" "$TMP/clone/deploy/install.sh"   # the one under test
cp "$ROOT/home-stack" "$TMP/clone/home-stack" 2>/dev/null || true
chmod +x "$TMP/clone/home-stack" "$TMP/clone/deploy/install.sh"

# Reuse this repo's venv rather than building one per run: the installer skips
# creating it when it exists, and pip over the network is not what is on trial.
[ -d "$ROOT/.venv" ] && cp -a "$ROOT/.venv" "$TMP/clone/.venv"

echo "the installer, on a tree that has nothing"
# `--paths-root` matters as much as the throwaway clone. Without it `paths:`
# defaults to /var/lib/home-stack, so on a machine that actually runs this
# stack the installer under test reads the household's live state -- and the
# run's result depends on what is in it. It did: a real user store made
# `have_portal` non-empty, which is a case the clone can never produce on its
# own, and the failure looked like the installer being broken for everybody.
( cd "$TMP/clone" && HOME_STACK_DEPLOY_ROOT="$TMP/deployroot" \
    HOME_STACK_NO_HOST_UNITS=1 \
    timeout 600 ./home-stack install --paths-root "$TMP/paths" \
    </dev/null >"$TMP/out" 2>&1 )
status=$?
check "it finishes" "$([ $status -eq 0 ] && echo 0 || echo 1)" \
      "exit $status; last lines: $(tail -3 "$TMP/out" | tr '\n' ' ')"

check "it created the config" \
      "$([ -f "$TMP/clone/config/home-stack.yml" ] && echo 0 || echo 1)"

# Again, over a tree that now has a portal user and still no admin password.
# That combination is what an `[ ... ] && warn` chain before a bare `return`
# gets wrong: the second test is false, the chain returns 1, the function
# returns 1 and `set -e` takes the installer down at the step that was only
# saying it had nothing to do. It is the second time that shape has done it
# here -- see the header above for the first -- so it is worth a case rather
# than a reading.
mkdir -p "$TMP/paths/state/home-core"
printf '[{"username":"someone","hash":"$2b$12$x","member":"user1","nanobot_id":1}]' \
    > "$TMP/paths/state/home-core/users.json"
( cd "$TMP/clone" && HOME_STACK_DEPLOY_ROOT="$TMP/deployroot" \
    HOME_STACK_NO_HOST_UNITS=1 \
    timeout 600 ./home-stack install --paths-root "$TMP/paths" \
    </dev/null >"$TMP/out2" 2>&1 )
status2=$?
check "it finishes again when a portal user already exists" \
      "$([ $status2 -eq 0 ] && echo 0 || echo 1)" \
      "exit $status2; last lines: $(tail -3 "$TMP/out2" | tr '\n' ' ')"
check "and says nothing about a portal user it can see" \
      "$(grep -q 'no portal user' "$TMP/out2" && echo 1 || echo 0)" \
      "it warned about a user that is right there"

generated=$(grep -cE '^[A-Z][A-Z0-9_]*=.+' "$TMP/clone/secrets/smart-home-bot.env" 2>/dev/null || echo 0)
# Ten or more: the example ships a dozen generated keys plus per-member ones.
# One means it created the file and then died, which is exactly what it did.
check "it generated the credentials it can ($generated set)" \
      "$([ "$generated" -ge 10 ] && echo 0 || echo 1)" "only $generated key(s) set"

# Every secret that two services compare against *each other* has to be
# generated here, because neither side can invent it alone and both refuse when
# it is empty. Counting keys does not catch one of these going missing: the
# count stays well over ten and the pair is silently unusable.
#
# PROJECTS_BROKER_TOKEN was exactly that. It was absent from the generated list,
# so on a real household it was empty at both ends and every one of the
# assistant's git verbs answered "registry said 401" -- from a broker and a
# portal that were both running and both reporting healthy.
for paired in CODE_BROKER_SECRET PROJECTS_BROKER_TOKEN PROXY_SHARED_SECRET; do
    value=$(sed -n "s/^${paired}=//p" "$TMP/clone/secrets/smart-home-bot.env" 2>/dev/null | head -1)
    check "  $paired was generated, so both sides can agree" \
          "$([ -n "$value" ] && echo 0 || echo 1)" \
          "empty: the two services that compare it will refuse each other"
done

check "and said what is still missing" \
      "$(grep -q "OPENCODE_API_KEY" "$TMP/out" && echo 0 || echo 1)"

# Non-interactive is the case a CI or a scripted install hits, and the one that
# must never block on a prompt nothing can answer.
check "it asked nothing it could not be answered" \
      "$(grep -qE 'the assistant.*not a terminal|not a terminal' "$TMP/out" && echo 0 || echo 1)" \
      "the assistant step has to skip itself without a tty"

# Whether all five got created is asserted below instead, against a scratch
# root. Checked here against the *default* paths it passed on any machine that
# had /var/lib/home-stack/backups lying around from an earlier install -- a
# check a leftover can satisfy is not one.

echo
echo "--paths-root puts the five somewhere you choose"
# All five, not the three the installer used to create. `backups:` is where
# ./home-stack backup writes and `plugins:` is where a household's own services
# live; a path declared and never created is one docker makes as an empty
# root-owned mount on first use.
( cd "$TMP/clone" && HOME_STACK_DEPLOY_ROOT="$TMP/deployroot" \
    HOME_STACK_NO_HOST_UNITS=1 \
    timeout 600 ./home-stack install --paths-root "$TMP/elsewhere" \
    </dev/null >"$TMP/out2" 2>&1 )
status=$?
check "it finishes" "$([ $status -eq 0 ] && echo 0 || echo 1)" \
      "exit $status; last lines: $(tail -3 "$TMP/out2" | tr '\n' ' ')"
for k in config state media backups plugins; do
    check "  $k is under it" "$([ -d "$TMP/elsewhere/$k" ] && echo 0 || echo 1)"
done
check "and the config says so, so the deploy agrees with the disk" \
      "$("$TMP/clone/.venv/bin/python" -c "
import yaml,sys
p=(yaml.safe_load(open(sys.argv[1])) or {})['paths']
sys.exit(0 if all(v.startswith(sys.argv[2]) for v in p.values()) else 1)" \
        "$TMP/clone/config/home-stack.yml" "$TMP/elsewhere" && echo 0 || echo 1)"
( cd "$TMP/clone" && ./home-stack install --paths-root relative/path </dev/null >/dev/null 2>&1 )
check "a relative one is refused rather than resolved against the cwd" \
      "$([ $? -ne 0 ] && echo 0 || echo 1)" \
      "paths: is read on the target host, where the cwd is not this one"

echo
echo "and when the configured place needs a root this machine will not give"
# 500: mine, and not writable. The same shape as /var/lib without needing root
# to arrange, and the case that sent the last install off to run sudo by hand.
mkdir -p "$TMP/locked" && chmod 500 "$TMP/locked"
( cd "$TMP/clone" && "$TMP/clone/.venv/bin/python" - config/home-stack.yml "$TMP/locked" <<'SETPY'
import sys
from ruamel.yaml import YAML
y = YAML(); y.preserve_quotes = True; y.width = 4096
cfg = y.load(open(sys.argv[1]))
for k in ("config", "state", "media", "backups", "plugins"):
    cfg["paths"][k] = f"{sys.argv[2]}/{k}"
y.dump(cfg, open(sys.argv[1], "w"))
SETPY
)
( cd "$TMP/clone" && HOME_STACK_DEPLOY_ROOT="$TMP/deployroot" \
    XDG_STATE_HOME="$TMP/xdg" \
    timeout 300 ./home-stack install --prepare-hosts </dev/null >"$TMP/out3" 2>&1 )
check "it does not stop and hand you a command to run" \
      "$(grep -q 'local, ready' "$TMP/out3" && echo 0 || echo 1)" \
      "$(tail -4 "$TMP/out3" | tr '\n' ' ')"
check "it says where the state went instead" \
      "$(grep -q "$TMP/xdg/home-stack" "$TMP/out3" && echo 0 || echo 1)" \
      "a relocation nobody is told about is a deploy writing somewhere the operator does not know"
check "and wrote it into the config" \
      "$("$TMP/clone/.venv/bin/python" -c "
import yaml,sys
p=(yaml.safe_load(open(sys.argv[1])) or {})['paths']
sys.exit(0 if all(v.startswith(sys.argv[2]) for v in p.values()) else 1)" \
        "$TMP/clone/config/home-stack.yml" "$TMP/xdg/home-stack" && echo 0 || echo 1)"

echo
echo "and when they are already there, but not writable"
# The case `mkdir -p` cannot see. It returns 0 for a directory that exists
# whatever owns it, so the version of this that only ran mkdir reported
# "local, ready" for a root-owned tree and left the first deploy to fail on
# Permission denied several minutes in -- which is how the last install here
# went. All five exist below; none can be written.
for k in config state media backups plugins; do mkdir -p "$TMP/there/$k"; done
chmod 500 "$TMP/there"/*
( cd "$TMP/clone" && "$TMP/clone/.venv/bin/python" - config/home-stack.yml "$TMP/there" <<'SETPY3'
import sys
from ruamel.yaml import YAML
y = YAML(); y.preserve_quotes = True; y.width = 4096
cfg = y.load(open(sys.argv[1]))
for k in ("config", "state", "media", "backups", "plugins"):
    cfg["paths"][k] = f"{sys.argv[2]}/{k}"
y.dump(cfg, open(sys.argv[1], "w"))
SETPY3
)
( cd "$TMP/clone" && HOME_STACK_DEPLOY_ROOT="$TMP/deployroot" \
    XDG_STATE_HOME="$TMP/xdg3" \
    timeout 300 ./home-stack install --prepare-hosts </dev/null >"$TMP/out5" 2>&1 )
check "existing is not reported as ready" \
      "$(grep -q "$TMP/xdg3/home-stack" "$TMP/out5" && echo 0 || echo 1)" \
      "mkdir -p returns 0 on a directory it cannot write; only writing a file answers this: $(tail -3 "$TMP/out5" | tr '\n' ' ')"
chmod -R 700 "$TMP/there" 2>/dev/null || true

echo
echo "but never over state somebody already has"
# The one case where relocating is the wrong answer. Moving `paths:` while a
# deploy has written under them orphans the camera registry, the user store and
# the databases in a single silent step -- the same loss guard_state_paths()
# exists to prevent, arrived at from the other end.
mkdir -p "$TMP/held/state/home-core" && printf '[]' > "$TMP/held/state/home-core/users.json"
chmod -R 500 "$TMP/held"
( cd "$TMP/clone" && "$TMP/clone/.venv/bin/python" - config/home-stack.yml "$TMP/held" <<'SETPY2'
import sys
from ruamel.yaml import YAML
y = YAML(); y.preserve_quotes = True; y.width = 4096
cfg = y.load(open(sys.argv[1]))
for k in ("config", "state", "media", "backups", "plugins"):
    cfg["paths"][k] = f"{sys.argv[2]}/{k}"
y.dump(cfg, open(sys.argv[1], "w"))
SETPY2
)
( cd "$TMP/clone" && HOME_STACK_DEPLOY_ROOT="$TMP/deployroot" \
    XDG_STATE_HOME="$TMP/xdg2" \
    timeout 300 ./home-stack install --prepare-hosts </dev/null >"$TMP/out4" 2>&1 )
check "it refuses to move them" \
      "$(grep -q 'not writable' "$TMP/out4" && echo 0 || echo 1)" \
      "$(tail -4 "$TMP/out4" | tr '\n' ' ')"
check "and leaves the config pointing at the state that exists" \
      "$("$TMP/clone/.venv/bin/python" -c "
import yaml,sys
p=(yaml.safe_load(open(sys.argv[1])) or {})['paths']
sys.exit(0 if p['state'].startswith(sys.argv[2]) else 1)" \
        "$TMP/clone/config/home-stack.yml" "$TMP/held" && echo 0 || echo 1)"
chmod -R 700 "$TMP/held" "$TMP/locked" 2>/dev/null || true

echo
if [ "$failures" -gt 0 ]; then echo "$failures FAILED"; exit 1; fi
echo "all checks passed"
