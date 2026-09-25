#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# home-stack installer
# ---------------------------------------------------------------------------
# Takes a machine from nothing to a stack you can deploy to. It does not deploy
# anything itself -- that is deploy/deploy.py, and keeping the two apart means
# you can re-run this without touching a running house.
#
# One PC by default: every role in config/home-stack.yml points at 127.0.0.1,
# so nothing here needs ssh, keys, or a second machine. Give a role a real
# address later and only that role moves.
#
#   ./home-stack install                    interactive first-time setup
#   ./home-stack install --generate-secrets fill in every generated key
#   ./home-stack install --check            prerequisites only, change nothing
#   ./home-stack install --prepare-hosts    create state dirs on the targets
#   ./home-stack install --create-user      admin password and the first login
#
# The only credential you have to supply is the assistant's model API key, plus
# the two passwords the last step asks for -- those are deliberately not
# generated, because they are the ones a person types. Everything else this
# generates, and nothing here reaches the internet.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/config/home-stack.yml"
CONFIG_EXAMPLE="$ROOT/config/home-stack.example.yml"
SECRETS="$ROOT/secrets/smart-home-bot.env"
SECRETS_EXAMPLE="$ROOT/secrets/smart-home-bot.env.example"
VENV="$ROOT/.venv"

if [ -t 1 ]; then
    C_STEP=$'\033[36m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'
    C_ERR=$'\033[31m';  C_OFF=$'\033[0m'
else
    C_STEP=""; C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi
step() { printf '%s==> %s%s\n' "$C_STEP" "$1" "$C_OFF"; }
ok()   { printf '%s    ok  %s%s\n' "$C_OK" "$1" "$C_OFF"; }
warn() { printf '%s    warn %s%s\n' "$C_WARN" "$1" "$C_OFF"; }
# Neither ok nor warn: something the installer decided for you and you should
# know about, on a step that succeeded. `warn` for these read as a failure and
# sent people looking for what had gone wrong.
note() { printf '    %s\n' "$1"; }
die()  { printf '%s    FAIL %s%s\n' "$C_ERR" "$1" "$C_OFF" >&2; exit 1; }

# ---------------------------------------------------------------------------

check_prerequisites() {
    step "checking prerequisites"
    local missing=()

    for tool in docker rsync ssh python3 openssl curl; do
        if command -v "$tool" >/dev/null 2>&1; then
            ok "$tool"
        else
            missing+=("$tool")
        fi
    done

    if docker compose version >/dev/null 2>&1; then
        ok "docker compose"
    else
        missing+=("the docker compose plugin")
    fi

    if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        ok "python $(python3 -c 'import platform; print(platform.python_version())')"
    else
        missing+=("python3 >= 3.9")
    fi

    if [ ${#missing[@]} -gt 0 ]; then
        die "missing: ${missing[*]}
    On Debian/Ubuntu:
      sudo apt install docker.io docker-compose-v2 rsync openssh-client python3 python3-venv openssl curl
    On older Debian/Ubuntu the compose package is called docker-compose-plugin
    instead; 26.04 ships it as docker-compose-v2 and does not carry the old
    name at all, so `apt install docker-compose-plugin` fails outright."
    fi

    if ! docker info >/dev/null 2>&1; then
        die "docker is installed but not usable by $(whoami).
    Add yourself to the docker group and log back in:
      sudo usermod -aG docker $(whoami)"
    fi
    ok "docker daemon reachable"
}

setup_venv() {
    step "python environment"
    if [ ! -d "$VENV" ]; then
        python3 -m venv "$VENV" || die "could not create a venv (install python3-venv)"
        ok "created .venv"
    fi
    "$VENV/bin/pip" install --quiet --upgrade pip
    # bcrypt: the installer hashes the passwords it asks for, so the plaintext
    # never reaches a file. It is the same library the portal and the admin
    # page verify with, so a hash written here is one they can both read.
    "$VENV/bin/pip" install --quiet pyyaml flask "ruamel.yaml>=0.18" "bcrypt>=4.0"
    ok "pyyaml, flask, bcrypt installed"
}

# ---------------------------------------------------------------------------

gen() { openssl rand -hex 32; }
# Keys whose shape is decided by whatever reads them. PROJECTS_KEY goes to
# Fernet, which wants exactly 32 bytes base64url-encoded -- `openssl rand -hex
# 32` is 64 characters that decode to 48, and Fernet refuses it at import. The
# portal then logs "set but unusable" once and the credentials page asks the
# household for a key it already has.
gen_fernet() { openssl rand 32 | base64 | tr '+/' '-_'; }
gen_for() { case "$1" in PROJECTS_KEY) gen_fernet ;; *) gen ;; esac; }

# The portal computes this same value at request time. deploy.py derives it too,
# from the shared secret, so the three can never drift apart.
derive_token() {
    printf '%s:%s' "$1" "$2" | openssl dgst -sha256 -r | cut -d' ' -f1
}

set_key() {
    local key="$1" value="$2" file="$3"
    if grep -qE "^${key}=" "$file"; then
        # `|` as the delimiter and an escaped value: a generated secret is hex,
        # but a supplied one is not, and a `/` in it silently truncated the line.
        #
        # The value goes in through the environment, not argv. stdin is taken
        # by the heredoc, and /proc/<pid>/environ is readable only by this user
        # and root while /proc/<pid>/cmdline is readable by everyone -- so a
        # freshly generated secret spent the length of this call in a file any
        # local account could cat.
        HOME_STACK_SET_VALUE="$value" python3 - "$file" "$key" <<'PY'
import os
import sys
path, key = sys.argv[1], sys.argv[2]
value = os.environ["HOME_STACK_SET_VALUE"]
lines = open(path).read().splitlines(keepends=True)
# Written beside and renamed over, never truncated in place: this runs once per
# generated key, and a crash between the truncating open and the writes used to
# leave the whole secrets file -- including the user's hand-typed API key --
# empty, with nothing able to restore it.
tmp = path + ".tmp"
# 0600 before a single credential reaches it, not after. This scratch copy holds
# every secret in the house, it is written into a directory other local accounts
# can list, and it outlives the call if anything here raises. `os.replace` also
# carries this mode onto the secrets file itself, so the file is never briefly
# world-readable between the swap and the `chmod 600` below.
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    for line in lines:
        if line.startswith(key + "="):
            fh.write(f"{key}={value}\n")
        else:
            fh.write(line)
os.replace(tmp, path)
PY
        chmod 600 "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

# Strips surrounding quotes, because load_secrets in deploy.py does too.
# Without it a quoted value reads back as set here but with different
# bytes, and the two disagree about what still needs generating.
get_key() {
    grep -E "^$1=" "$2" 2>/dev/null | head -1 | cut -d= -f2- \
        | sed -e "s/^['\"]//" -e "s/['\"]\$//" || true
}

# The model key under whichever name this env file spells it. The credential
# did not change when the stack moved from the flat Go plan to Zen, only what
# it is called, and deploy.py's SECRET_ALIASES reads the old name -- so the
# installer must not tell a household with a working key that it has none.
get_model_key() {
    local v
    v="$(get_key OPENCODE_API_KEY "$1")"
    [ -n "$v" ] || v="$(get_key OPENCODE_GO_API_KEY "$1")"
    printf '%s' "$v"
}

# The deployed copy of the credentials, if there is one.
#
# The admin page's config and secrets are seeded from this repository on the
# *first* deploy and are state afterwards -- the page edits them, so a deploy
# deliberately never re-seeds, or it would throw away whatever you changed
# there. The consequence is the one that cost somebody half an hour: setting
# the admin password here wrote a file the running page does not read, and no
# amount of redeploying applied it.
#
# So anything set here is written to both. Which of the two wins is decided per
# key, below.
live_secrets_file() {
    [ -f "$CONFIG" ] || return 0
    local dir
    dir="$("$VENV/bin/python" - "$CONFIG" <<'LIVE_PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print((cfg.get("paths") or {}).get("config", "/var/lib/home-stack/config"))
LIVE_PY
)"
    if [ -n "$dir" ] && [ -f "$dir/smart-home-bot.env" ]; then
        printf '%s' "$dir/smart-home-bot.env"
    fi
    # Explicit, and not the `&& printf` chain this was: with no deployed copy
    # that chain is the last command and returns 1, so under `set -euo pipefail`
    # `live="$(live_secrets_file)"` took the installer down with it -- on every
    # fresh install, at the first generated key. "There is no deployed copy" is
    # the ordinary answer here, not a failure.
    return 0
}

# Write KEY to the repository's secrets file and to the deployed copy.
#
#   replace  this is a deliberate new value and must take effect -- the admin
#            password. Overwrites whatever the deployed copy holds.
#   fill     a generated value that only ever filled a blank. Left alone if the
#            deployed copy already has one, because that one is in use by a
#            running container and the page may have set it.
set_key_both() {
    local key="$1" value="$2" mode="${3:-fill}" live
    set_key "$key" "$value" "$SECRETS"
    live="$(live_secrets_file)"
    [ -n "$live" ] || return 0
    if [ "$mode" = "replace" ] || [ -z "$(get_key "$key" "$live")" ]; then
        set_key "$key" "$value" "$live"
        ok "$key also written to the deployed copy ($live)"
    else
        warn "$key differs in the deployed copy; left as it is"
        warn "  the admin page owns $live -- change it there, or delete the key"
    fi
}

members_from_config() {
    if [ -f "$CONFIG" ]; then
        "$VENV/bin/python" - "$CONFIG" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print(" ".join(cfg.get("services", {}).get("nanobot", {}).get("members", [])))
PY
    else
        echo "user1 user2"
    fi
}

generate_secrets() {
    step "generating secrets"

    if [ ! -f "$SECRETS" ]; then
        cp "$SECRETS_EXAMPLE" "$SECRETS"
        chmod 600 "$SECRETS"
        ok "created secrets/smart-home-bot.env from the example"
    fi
    chmod 600 "$SECRETS"

    # PROJECTS_BROKER_TOKEN is one secret two services compare against each
    # other: the code broker presents it to the portal's registry, and the
    # portal refuses outright when either side is empty. Generating it here is
    # what makes both ends hold the same value -- it was missing from this
    # list, so on a real household it was empty at both ends and every one of
    # the assistant's git verbs came back "registry said 401", from two
    # services that were both running and both reporting healthy.
    local generated=0
    for key in HOMECORE_SECRET_KEY HOMECORE_DEBUG_API_KEY ADMIN_SECRET_KEY \
               NANOBOT_DEBUG_SECRET NANOBOT_API_SECRET_HOUSE \
               PROXY_SHARED_SECRET PROXY_SESSION_SIGNING_KEY VOICE_GATEWAY_TOKEN \
               SHARE_SMB_PASSWORD PAPERLESS_SECRET_KEY \
               SEARXNG_SECRET_KEY CODE_BROKER_SECRET PROJECTS_KEY \
               ALFRED_MCP_SECRET \
               PROJECTS_BROKER_TOKEN \
               BACKUP_ENCRYPTION_KEY; do
        if [ -z "$(get_key "$key" "$SECRETS")" ]; then
            set_key_both "$key" "$(gen_for "$key")" fill
            generated=$((generated + 1))
        fi
    done

    # Per-member keys, exactly as many as the config asks for.
    local shared members
    shared="$(get_key PROXY_SHARED_SECRET "$SECRETS")"
    members="$(members_from_config)"
    for member in $members; do
        # user1 -> USER_1, matching member_env_suffix in deploy.py.
        local upper; upper="$(printf '%s' "$member" \
            | sed -E 's/^([a-zA-Z]+)([0-9]+)$/\1_\2/' \
            | tr '[:lower:]' '[:upper:]')"
        if [ -z "$(get_key "NANOBOT_API_SECRET_$upper" "$SECRETS")" ]; then
            set_key_both "NANOBOT_API_SECRET_$upper" "$(gen)" fill
            generated=$((generated + 1))
        fi
        # Deliberately NOT written to the env file. deploy.py derives this
        # from PROXY_SHARED_SECRET at deploy time; persisting a copy meant
        # its re-derivation guard never fired, so rotating the shared
        # secret shipped the stale token and task notifications 401'd.
        # Remove any copy an older installer left behind.
        sed -i "/^HOMECORE_PROXY_TOKEN_$upper=/d" "$SECRETS"
    done

    ok "$generated key(s) generated, $(printf '%s' "$members" | wc -w) member(s) provisioned"

    if [ -z "$(get_model_key "$SECRETS")" ]; then
        warn "OPENCODE_API_KEY is empty - the assistant will not answer."
        warn "It is the one credential this stack cannot generate. Put it in"
        warn "secrets/smart-home-bot.env, or set it from the admin page later."
    else
        ok "model API key present"
    fi
}

# ---------------------------------------------------------------------------
# The assistant: what it is called, and what answers for it
# ---------------------------------------------------------------------------
# Two questions the stack cannot guess and cannot run without. The name is the
# cheap one -- it reaches the prompts, the pages and the words the voice panels
# say out loud, and a default nobody chose is a house talking to a stranger.
#
# The model is the one that decides whether any of this works. Three ways to
# answer it and at least one has to be true, so this asks rather than warning
# afterwards that the assistant will not answer. `--assistant` re-runs it.

# ---------------------------------------------------------------------------
# Where the assistant instances run
# ---------------------------------------------------------------------------
# Compose is the default and needs nothing. Kubernetes is worth it for two
# things and it is worth being narrow about which: a probe that restarts an
# instance that has stopped answering without anybody noticing, and a scheduler
# that can put an instance on a machine with room for it.
#
# Asked rather than assumed in either direction. Standing up a cluster behind
# somebody's back on a single-PC install is the wrong default; so is requiring
# one from a household that has one machine and is happy.

choose_runtime() {
    step "where the assistant runs"
    [ -f "$CONFIG" ] || die "config/home-stack.yml does not exist; run without --runtime first"
    if [ ! -t 0 ]; then
        warn "not a terminal; leaving services.nanobot.runtime as it is"
        return 0
    fi

    local current
    current="$("$VENV/bin/python" - "$CONFIG" <<'CUR_PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print(((cfg.get("services") or {}).get("nanobot") or {}).get("runtime", "compose"))
CUR_PY
)"
    echo
    echo "    One assistant instance per member. Today nothing watches them:"
    echo "    an instance can be wedged for a day and the only sign is somebody"
    echo "    saying it stopped answering."
    echo
    echo "      1  Docker Compose on this machine        (default, nothing to install)"
    echo "      2  Kubernetes -- install k3s here"
    echo "      3  Kubernetes -- I already have a cluster"
    echo
    printf '      which? [1/2/3, currently %s] ' "$current"
    local pick; read -r pick </dev/tty || pick=""

    case "${pick:-1}" in
        1|"") _set_runtime compose
              ok "the assistant runs on Docker Compose"
              return 0 ;;
        2)    _install_k3s || return 1
              _set_runtime kubernetes
              _enable_registry
              ok "k3s installed; the assistant runs on Kubernetes" ;;
        3)    local kubeconfig
              printf '      path to your kubeconfig [%s]: ' "$HOME/.kube/config"
              read -r kubeconfig </dev/tty || kubeconfig=""
              kubeconfig="${kubeconfig:-$HOME/.kube/config}"
              [ -f "$kubeconfig" ] || { warn "no file at $kubeconfig; leaving this alone"; return 1; }
              # Prove it reaches a cluster before writing anything that says it
              # does. A kubeconfig that parses is not a cluster that answers.
              if ! KUBECONFIG="$kubeconfig" kubectl get nodes >/dev/null 2>&1; then
                  warn "that kubeconfig does not reach a working cluster"
                  warn "  KUBECONFIG=$kubeconfig kubectl get nodes"
                  return 1
              fi
              "$VENV/bin/python" - "$CONFIG" "$kubeconfig" <<'KC_PY'
import sys
from ruamel.yaml import YAML
y = YAML(); y.preserve_quotes = True
# 100, matching _round_trip_yaml() in admin/app.py, which owns this file.
# ruamel defaults to 80 and silently re-wraps every long scalar it did not
# write -- a three-line change arriving as a hundred lines of reflowed
# member notes, which is indistinguishable from something having gone wrong.
y.width = 100
cfg = y.load(open(sys.argv[1]))
cfg.setdefault("services", {}).setdefault("nanobot", {})["kubeconfig"] = sys.argv[2]
y.dump(cfg, open(sys.argv[1], "w"))
KC_PY
              _set_runtime kubernetes
              _enable_registry
              ok "using the cluster at $kubeconfig" ;;
        *)    warn "not one of the options; leaving it as $current"
              return 0 ;;
    esac

    warn "saved, not applied. Render and apply the instances with:"
    warn "  ./home-stack k8s apply"
}

_set_runtime() {
    "$VENV/bin/python" - "$CONFIG" "$1" <<'RT_PY'
import sys
from ruamel.yaml import YAML
y = YAML(); y.preserve_quotes = True
# 100, matching _round_trip_yaml() in admin/app.py, which owns this file.
# ruamel defaults to 80 and silently re-wraps every long scalar it did not
# write -- a three-line change arriving as a hundred lines of reflowed
# member notes, which is indistinguishable from something having gone wrong.
y.width = 100
cfg = y.load(open(sys.argv[1]))
cfg.setdefault("services", {}).setdefault("nanobot", {})["runtime"] = sys.argv[2]
y.dump(cfg, open(sys.argv[1], "w"))
RT_PY
}

# A node can only start a pod whose image it can pull, and the assistant image
# is built rather than published. On one node that is survivable; the moment
# there are two it is the whole problem, so the registry goes on with it.
_enable_registry() {
    "$VENV/bin/python" - "$CONFIG" <<'REG_PY'
import sys
from ruamel.yaml import YAML
y = YAML(); y.preserve_quotes = True
# 100, matching _round_trip_yaml() in admin/app.py, which owns this file.
# ruamel defaults to 80 and silently re-wraps every long scalar it did not
# write -- a three-line change arriving as a hundred lines of reflowed
# member notes, which is indistinguishable from something having gone wrong.
y.width = 100
cfg = y.load(open(sys.argv[1]))
reg = cfg.setdefault("services", {}).setdefault("registry", {})
reg["enabled"] = True
reg.setdefault("host", "hub")
reg.setdefault("port", 21040)
y.dump(cfg, open(sys.argv[1], "w"))
REG_PY
    ok "the image registry is switched on -- deploy it with ./home-stack deploy registry"
}

_install_k3s() {
    if command -v k3s >/dev/null 2>&1; then
        ok "k3s is already installed"
        return 0
    fi
    echo
    echo "    This installs k3s on this machine. Two things it will not do:"
    echo "      * Traefik is disabled -- it wants ports 80 and 443, and the"
    echo "        house proxy already has them."
    echo "      * the bundled load balancer is disabled, for the same reason."
    printf '      go ahead? [y/N] '
    local ok_go; read -r ok_go </dev/tty || ok_go=""
    case "$ok_go" in [yY]*) ;; *) warn "not installing k3s"; return 1 ;; esac

    # Memory is the one that bites: a control plane wants ~512 MB, and a house
    # hub is usually already full. Say so rather than letting the OOM killer
    # explain it by stopping something else.
    local avail; avail="$(free -m | awk '/Mem:/ {print $7}')"
    if [ "${avail:-0}" -lt 700 ]; then
        warn "only ${avail}M of memory available; k3s wants ~512M for itself"
        warn "  add swap or stop something before this is a good idea"
        printf '      continue anyway? [y/N] '
        local anyway; read -r anyway </dev/tty || anyway=""
        case "$anyway" in [yY]*) ;; *) return 1 ;; esac
    fi

    curl -sfL https://get.k3s.io | \
        INSTALL_K3S_EXEC="--disable traefik --disable servicelb --write-kubeconfig-mode 644" \
        sh - >/dev/null 2>&1 || { warn "k3s install failed"; return 1; }

    local i
    for i in $(seq 1 30); do
        k3s kubectl get nodes 2>/dev/null | grep -q " Ready " && break
        sleep 5
    done
    k3s kubectl get nodes 2>/dev/null | grep -q " Ready " \
        || { warn "k3s installed but the node never became Ready"; return 1; }
    ok "k3s is up"
}

choose_assistant() {
    step "the assistant"
    [ -f "$CONFIG" ] || die "config/home-stack.yml does not exist; run without --assistant first"
    if [ ! -t 0 ]; then
        warn "not a terminal; leaving the assistant's name and model as they are"
        return 0
    fi

    # --- the name ----------------------------------------------------------
    local current name
    current="$("$VENV/bin/python" - "$CONFIG" <<'NAME_PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print((cfg.get("site") or {}).get("assistant_name", "Alfred"))
NAME_PY
)"
    echo
    echo "    What should the household call its assistant?"
    echo "    It goes in its own prompts, on the pages, and into the sentences"
    echo "    the voice panels say out loud. Alfred is the butler this shipped"
    echo "    with; anything you would actually say works."
    printf '      name [%s]: ' "${current:-Alfred}"
    read -r name </dev/tty || name=""
    name="${name:-${current:-Alfred}}"
    "$VENV/bin/python" - "$CONFIG" "$name" <<'SETNAME_PY'
import sys
from ruamel.yaml import YAML
y = YAML(); y.preserve_quotes = True
# 100, matching _round_trip_yaml() in admin/app.py, which owns this file.
# ruamel defaults to 80 and silently re-wraps every long scalar it did not
# write -- a three-line change arriving as a hundred lines of reflowed
# member notes, which is indistinguishable from something having gone wrong.
y.width = 100
cfg = y.load(open(sys.argv[1]))
cfg.setdefault("site", {})["assistant_name"] = sys.argv[2]
y.dump(cfg, open(sys.argv[1], "w"))
SETNAME_PY
    ok "the assistant is called $name"

    # --- what answers for it ------------------------------------------------
    local have_go have_cloud have_local
    have_go="$(get_model_key "$SECRETS")"
    have_cloud="$(get_key OLLAMA_API_KEY "$SECRETS")"
    have_local="$("$VENV/bin/python" - "$CONFIG" <<'LOCAL_PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
o = ((cfg.get("cloud") or {}).get("ollama") or {})
print("yes" if (o.get("local") or {}).get("enabled") else "")
LOCAL_PY
)"

    echo
    echo "    Something has to answer for it. Any one of these is enough, and"
    echo "    they can be combined later -- a model names which one it wants."
    echo
    echo "      1  OpenCode Zen           one key, billed per token, nothing to run"
    echo "      2  Ollama on your own PC  costs electricity, no key, needs a GPU"
    echo "      3  ollama.com             a key, and frames leave the house"
    echo
    [ -n "$have_go" ]    && ok "OpenCode Zen key already set"
    [ -n "$have_cloud" ] && ok "ollama.com key already set"
    [ -n "$have_local" ] && ok "a local Ollama is already enabled"

    if [ -n "$have_go$have_cloud$have_local" ]; then
        printf '      change it? [y/N] '
        local again; read -r again </dev/tty || again=""
        case "$again" in [yY]*) ;; *) return 0 ;; esac
    fi

    while :; do
        printf '      which? [1/2/3] '
        local pick; read -r pick </dev/tty || pick=""
        case "$pick" in
            1)  local key
                key="$(read_secret_value 'OpenCode Zen API key')"
                [ -n "$key" ] || { warn "nothing entered"; continue; }
                set_key_both OPENCODE_API_KEY "$key" replace
                ok "the assistant will answer on OpenCode Zen"; return 0 ;;
            2)  local host port
                printf '      which machine runs it? (hub/compute/storage) [compute]: '
                read -r host </dev/tty || host=""; host="${host:-compute}"
                printf '      port [11434]: '
                read -r port </dev/tty || port=""; port="${port:-11434}"
                "$VENV/bin/python" - "$CONFIG" "$host" "$port" <<'OLLAMA_PY'
import sys
from ruamel.yaml import YAML
y = YAML(); y.preserve_quotes = True
# 100, matching _round_trip_yaml() in admin/app.py, which owns this file.
# ruamel defaults to 80 and silently re-wraps every long scalar it did not
# write -- a three-line change arriving as a hundred lines of reflowed
# member notes, which is indistinguishable from something having gone wrong.
y.width = 100
cfg = y.load(open(sys.argv[1]))
o = cfg.setdefault("cloud", {}).setdefault("ollama", {}).setdefault("local", {})
o["enabled"] = True; o["host"] = sys.argv[2]; o["port"] = int(sys.argv[3])
y.dump(cfg, open(sys.argv[1], "w"))
OLLAMA_PY
                ok "using the Ollama on $host:$port"
                warn "this stack does not deploy Ollama -- it only says where it is"
                return 0 ;;
            3)  local key
                key="$(read_secret_value 'ollama.com API key')"
                [ -n "$key" ] || { warn "nothing entered"; continue; }
                set_key_both OLLAMA_API_KEY "$key" replace
                "$VENV/bin/python" - "$CONFIG" <<'CLOUD_PY'
import sys
from ruamel.yaml import YAML
y = YAML(); y.preserve_quotes = True
# 100, matching _round_trip_yaml() in admin/app.py, which owns this file.
# ruamel defaults to 80 and silently re-wraps every long scalar it did not
# write -- a three-line change arriving as a hundred lines of reflowed
# member notes, which is indistinguishable from something having gone wrong.
y.width = 100
cfg = y.load(open(sys.argv[1]))
c = cfg.setdefault("cloud", {}).setdefault("ollama", {}).setdefault("cloud", {})
c["enabled"] = True
y.dump(cfg, open(sys.argv[1], "w"))
CLOUD_PY
                ok "the assistant will answer on ollama.com"
                warn "camera frames and photos go to ollama.com in this mode"
                return 0 ;;
            *)  warn "1, 2 or 3 -- one of them has to be true or nothing answers" ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# opencode -- what the Programmer profession runs on
# ---------------------------------------------------------------------------
#
# Pointed at, never deployed: the household installs opencode and signs it in,
# and this only puts a systemd *user* unit in front of it so the server is up
# when somebody opens the Programmer space. A user unit rather than a system
# one because the credential, the config and the checkouts are all in one
# person's home -- running it as root would read none of them.
#
# Skipped rather than fatal when opencode is not installed. It backs one
# profession; a household that does not want it should not be blocked from
# installing everything else.

_opencode_bin() {
    if [ -x "$HOME/.opencode/bin/opencode" ]; then
        printf '%s' "$HOME/.opencode/bin/opencode"
    else
        command -v opencode 2>/dev/null || true
    fi
}

# The deployed config, if there is one. Same shape and the same reason as
# live_secrets_file above: `paths.config` in the checkout's copy names where the
# real file lives, and the deployer reads *that* one. Returns nothing on a
# machine that has never deployed, which is the ordinary case on a first
# install rather than a failure -- hence the bare `return 0` at the end, which
# is the bug live_secrets_file's own comment records.
live_config_file() {
    [ -f "$CONFIG" ] || return 0
    local dir
    dir="$("$VENV/bin/python" - "$CONFIG" <<'LIVECFG_PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print((cfg.get("paths") or {}).get("config", "/var/lib/home-stack/config"))
LIVECFG_PY
)"
    if [ -n "$dir" ] && [ -f "$dir/home-stack.yml" ]; then
        printf '%s' "$dir/home-stack.yml"
    fi
    return 0
}

# Both copies, and this is not belt-and-braces. The checkout's file is the seed
# a fresh install reads; the deployed one is what every deploy after that reads.
# Writing only the seed on a machine that has already deployed is the "setting
# that never arrives" this stack keeps a rule about -- the Programmer would stay
# on the assistant and nothing would say why.
#
# Only `enabled`. The ports come from `cloud.opencode.port_base` and
# `services.alfred-mcp.port_base`, which ship with working values, and *who*
# has a Programmer is `members[].programmer` on each person's admin page. An
# installer that also decided those would be deciding them for a household that
# has not been asked yet.
_opencode_set_config() {
    local live target
    live="$(live_config_file)"
    for target in "$CONFIG" ${live:+"$live"}; do
        "$VENV/bin/python" - "$target" <<'OC_PY'
import sys
from ruamel.yaml import YAML
y = YAML(); y.preserve_quotes = True
# 100, matching _round_trip_yaml() in admin/app.py, which owns this file.
y.width = 100
cfg = y.load(open(sys.argv[1]))
cfg.setdefault("cloud", {}).setdefault("opencode", {})["enabled"] = True
y.dump(cfg, open(sys.argv[1], "w"))
OC_PY
    done
    [ -n "$live" ] && ok "cloud.opencode switched on in the deployed copy ($live)"
    return 0
}

setup_opencode() {
    step "opencode (the Programmer profession)"
    [ -f "$CONFIG" ] || die "config/home-stack.yml does not exist; run without --opencode first"

    # The one step here that reaches outside the tree it was pointed at: a unit
    # in the user's systemd directory and `enable --now` against the real user
    # manager, neither of which `--paths-root` isolates. deploy/test_install.sh
    # runs the whole flow, so without this the suite would enable a service on
    # whatever machine ran it -- the same shape as the suites that edited the
    # live user store because their scratch paths were set too late.
    if [ -n "${HOME_STACK_NO_HOST_UNITS:-}" ]; then
        note "HOME_STACK_NO_HOST_UNITS is set; leaving systemd alone"
        return 0
    fi

    local bin; bin="$(_opencode_bin)"
    if [ -z "$bin" ]; then
        note "opencode is not installed, so the Programmer stays on the assistant."
        note "  to change that: curl -fsSL https://opencode.ai/install | bash"
        note "  then: opencode auth login, and re-run ./home-stack install --opencode"
        return 0
    fi
    ok "opencode found at $bin ($("$bin" --version 2>/dev/null | head -1))"

    # Signed in, and checked before the unit is installed rather than after. A
    # server with no credential starts, answers /global/health perfectly well
    # and fails every actual turn -- which reads as the Programmer being broken
    # rather than as nobody having logged in.
    if ! "$bin" auth list 2>/dev/null | grep -qi 'opencode'; then
        warn "opencode has no OpenCode credential"
        note "  run: opencode auth login   (choose OpenCode Zen or OpenCode Go)"
        note "  then re-run ./home-stack install --opencode"
        return 0
    fi

    local cfgroot="${XDG_CONFIG_HOME:-$HOME/.config}"
    local dest="$cfgroot/systemd/user"
    mkdir -p "$dest" "$cfgroot/home-stack"

    # The readiness check goes in first: the unit's ExecStartPost names it by
    # path, and enabling a unit whose ExecStartPost does not exist fails.
    cp "$ROOT/deploy/host/opencode-health.sh" "$cfgroot/home-stack/opencode-health.sh"
    chmod +x "$cfgroot/home-stack/opencode-health.sh"
    # A template, instantiated once per member: opencode-serve@user1, and so on.
    # One server each because opencode holds a single MCP block, and a shared
    # one would authenticate every member's household tools as whoever it was
    # configured for.
    cp "$ROOT/deploy/host/opencode-serve@.service" "$dest/opencode-serve@.service"

    # The reaper for preview servers a failed turn left running. Enabled here
    # rather than by the deployer: it is a property of this machine having
    # opencode on it at all, and it costs nothing on a household that never
    # starts a preview -- the script exits at once when there is no checkout
    # root to look in.
    cp "$ROOT/deploy/host/opencode-preview-reaper.sh" \
       "$cfgroot/home-stack/opencode-preview-reaper.sh"
    chmod +x "$cfgroot/home-stack/opencode-preview-reaper.sh"
    cp "$ROOT/deploy/host/opencode-preview-reaper.service" \
       "$dest/opencode-preview-reaper.service"
    cp "$ROOT/deploy/host/opencode-preview-reaper.timer" \
       "$dest/opencode-preview-reaper.timer"

    # The single-instance unit this replaces. Left in place it would hold a
    # port a member's instance now wants, and answer with a configuration
    # nothing writes any more.
    if [ -f "$dest/opencode-serve.service" ]; then
        systemctl --user disable --now opencode-serve >/dev/null 2>&1 || true
        rm -f "$dest/opencode-serve.service" \
              "$cfgroot/home-stack/opencode-serve.env"
        note "the old single-member opencode-serve unit was stopped and removed"
    fi
    systemctl --user daemon-reload
    ok "opencode-serve@.service installed"
    # The timer is enabled here, unlike the servers below: it has nothing
    # per-member about it and nothing to wait on.
    if systemctl --user enable --now opencode-preview-reaper.timer >/dev/null 2>&1; then
        ok "opencode-preview-reaper.timer installed"
    else
        warn "opencode-preview-reaper.timer could not be enabled"
    fi

    _opencode_set_config

    # Deliberately not started here. Which members have one, which port each
    # answers on and where its configuration lives are all things the
    # *deployer* writes -- from `members[].programmer` and the port bases -- and
    # an instance enabled before any of that exists would start on the
    # template's default port with no household configuration at all. Two
    # members would race for one port and neither would have an agent.
    #
    # `./home-stack deploy alfred-mcp` writes each member's configuration and
    # then enables and restarts their instance. Until it runs the Programmer
    # stays on the assistant, which is what the portal's readiness check
    # already decides.
    note "switch the Programmer on for somebody in the admin, then run:"
    note "  ./home-stack deploy alfred-mcp"

    # Lingering, or the unit dies with the last login shell. A household hub is
    # normally logged out, and a Programmer space that works over ssh and not
    # from a phone is the confusing half-failure this avoids.
    if command -v loginctl >/dev/null 2>&1; then
        if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
            note "the unit stops when you log out; to keep it running:"
            note "  sudo loginctl enable-linger $USER"
        fi
    fi
}

# A value that must not reach the scrollback or the process table. Same shape
# as read_new_password, without the repeat: an API key is pasted, not typed
# from memory, so asking twice only invites a paste into the wrong prompt.
read_secret_value() {
    local label="$1" value
    printf '      %s (hidden): ' "$label" >&2
    read -rs value </dev/tty; echo >&2
    printf '%s' "$value"
}

# ---------------------------------------------------------------------------

create_config() {
    step "site configuration"
    if [ -f "$CONFIG" ]; then
        ok "config/home-stack.yml already exists, leaving it alone"
        return
    fi
    cp "$CONFIG_EXAMPLE" "$CONFIG"
    ok "created config/home-stack.yml"

    # Every service is optional. Asked once, on first install, only at a
    # terminal -- a scripted run keeps the defaults and the admin page can
    # change any of it later.
    #
    # Each one is asked with what it actually is. The manifest already carries a
    # description per service, and this used to print the bare name, so the
    # question was `nanobot-house [Y/n]` -- answerable only by somebody who
    # already knew, which is nobody running this for the first time.
    if [ -t 0 ]; then
        echo
        echo "    Which services do you want?"
        echo "    They all run on this PC, and the admin page can change any of"
        echo "    this later. Press enter to keep one."
        echo
        local kept=0 dropped=0 svc description answer
        while IFS="$(printf '\t')" read -r svc description; do
            [ -z "$svc" ] && continue
            printf '      %s\n' "$description"
            printf '      %-24s [Y/n] ' "$svc"
            read -r answer </dev/tty || answer=""
            case "$answer" in
                [nN]*)
                    "$VENV/bin/python" - "$CONFIG" "$svc" <<'DISABLE_PY'
import sys
from ruamel.yaml import YAML
y = YAML(); y.preserve_quotes = True
# 100, matching _round_trip_yaml() in admin/app.py, which owns this file.
# ruamel defaults to 80 and silently re-wraps every long scalar it did not
# write -- a three-line change arriving as a hundred lines of reflowed
# member notes, which is indistinguishable from something having gone wrong.
y.width = 100
cfg = y.load(open(sys.argv[1]))
cfg["services"][sys.argv[2]]["enabled"] = False
y.dump(cfg, open(sys.argv[1], "w"))
DISABLE_PY
                    dropped=$((dropped + 1)) ;;
                *)  kept=$((kept + 1)) ;;
            esac
            echo
        done <<EOF_SVC
$("$VENV/bin/python" - "$CONFIG" "$ROOT/deploy/manifest.yml" <<'LIST_PY'
import sys, textwrap, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
manifest = yaml.safe_load(open(sys.argv[2])) or {}
described = manifest.get("services") or {}
for name in (cfg.get("services") or {}):
    if name == "admin":
        continue  # the page that manages the rest stays on, always
    text = " ".join(((described.get(name) or {}).get("description") or "").split())
    # The first sentence, not the first 66 characters. These are written for
    # whoever operates the stack and the rest of the paragraph is usually a
    # caveat about how it is configured; shortening by length cut mid-clause
    # and ended every other line in "[...]".
    first = text.split(". ")[0].rstrip(".")
    print(f"{name}\t{textwrap.shorten(first, 72) if first else name}.")
LIST_PY
)
EOF_SVC
        ok "$kept on, $dropped off, and admin always on"
    fi
    warn "hosts, timezone and members are still to set -- the admin page does all three"
}

# ---------------------------------------------------------------------------
# The first user
# ---------------------------------------------------------------------------
# Two passwords, because they guard two different things and sharing one value
# between them would quietly widen the second:
#
#   * the admin page, which edits the household's credentials and starts
#     deploys -- access to it is equivalent to root on every host in the config;
#   * the household portal, which is one person's own login.
#
# Neither is generated. A generated password nobody typed lives in a terminal
# scrollback and in the password manager of whoever ran the installer, and this
# is the account the household actually uses every day.
#
# Both are stored as bcrypt hashes; the plaintext is never written anywhere. In
# a scripted run this asks nothing and says what is left to do -- the admin page
# has its own first-run setup screen, and this step re-runs on its own with
# --create-user.

read_new_password() {
    # Prompts on the tty, twice, without echo. The password goes to stdout so
    # the caller can hash it; the prompts go to stderr so they cannot end up in
    # the captured value.
    local label="$1" minimum="$2" first second
    while :; do
        printf '      %s: ' "$label" >&2
        read -rs first </dev/tty; echo >&2
        if [ "${#first}" -lt "$minimum" ]; then
            printf '      (at least %s characters)\n' "$minimum" >&2
            continue
        fi
        printf '      repeat it: ' >&2
        read -rs second </dev/tty; echo >&2
        if [ "$first" != "$second" ]; then
            printf '      (those did not match)\n' >&2
            continue
        fi
        printf '%s' "$first"
        return 0
    done
}

hash_password() {
    # bcrypt, the same library the portal and the admin page verify with.
    #
    # On stdin, never as an argument. Process arguments are world-readable in
    # /proc/<pid>/cmdline for the life of the process, and bcrypt at the
    # default cost deliberately takes a few hundred milliseconds -- long
    # enough, and predictable enough, for any local account to read the
    # password out of `ps` while the installer is hashing it.
    printf '%s' "$1" | "$VENV/bin/python" -c \
        'import bcrypt,sys; print(bcrypt.hashpw(sys.stdin.buffer.read(), bcrypt.gensalt()).decode())'
}

create_first_user() {
    step "the first user"
    [ -f "$CONFIG" ] || die "config/home-stack.yml does not exist; run without --create-user first"
    [ -f "$SECRETS" ] || die "secrets/smart-home-bot.env does not exist; run --generate-secrets first"

    local have_admin users_file have_portal
    have_admin="$(get_key ADMIN_PASSWORD_HASH "$SECRETS")"
    users_file="$("$VENV/bin/python" -c 'import sys,yaml; c=yaml.safe_load(open(sys.argv[1])) or {}; print(((c.get("paths") or {}).get("state") or "/var/lib/home-stack/state") + "/home-core/users.json")' "$CONFIG")"
    have_portal="$("$VENV/bin/python" -c 'import json,sys
try:
    print("yes" if json.load(open(sys.argv[1])) else "")
except Exception:
    print("")' "$users_file")"

    if [ -n "$have_admin" ] && [ -n "$have_portal" ]; then
        ok "an admin password and a portal user already exist, leaving both alone"
        return
    fi

    if [ ! -t 0 ]; then
        # `if`, not `[ ... ] && warn`. Under `set -e` an `&&` chain whose test
        # is false returns 1, and the last one before `return` makes the whole
        # function return 1 -- which takes the installer down at the step that
        # was only trying to say it had nothing to do. This is the second time
        # that shape has done it here; see the header of deploy/test_install.sh
        # for the first.
        if [ -z "$have_admin" ]; then
            warn "no admin password: the admin page will ask for one on first open"
        fi
        if [ -z "$have_portal" ]; then
            warn "no portal user: run ./home-stack install --create-user at a terminal"
        fi
        return 0
    fi

    # --- the admin page -----------------------------------------------------
    if [ -z "$have_admin" ]; then
        echo
        echo "    A password for the admin page. It edits this household's"
        echo "    settings and credentials and can deploy, so treat it as the"
        echo "    key to the house."
        local admin_pw admin_hash
        admin_pw="$(read_new_password "admin page password" 6)"
        admin_hash="$(hash_password "$admin_pw")"
        unset admin_pw
        set_key_both ADMIN_PASSWORD_HASH "$admin_hash" replace
        ok "admin page password set"
    else
        ok "admin page password already set, leaving it alone"
    fi

    # --- the household portal ----------------------------------------------
    if [ -n "$have_portal" ]; then
        ok "the portal already has a user, leaving the user store alone"
        return
    fi

    local member_id member_name login portal_pw
    member_id="$("$VENV/bin/python" -c 'import sys,yaml
c = yaml.safe_load(open(sys.argv[1])) or {}
ms = c.get("members") or []
m = next((x for x in ms if x.get("admin")), ms[0] if ms else None)
print((m or {}).get("id", ""))' "$CONFIG")"
    if [ -z "$member_id" ]; then
        warn "no members in config/home-stack.yml; skipping the portal user"
        return
    fi
    member_name="$("$VENV/bin/python" -c 'import sys,yaml
c = yaml.safe_load(open(sys.argv[1])) or {}
for m in c.get("members") or []:
    if m.get("id") == sys.argv[2]:
        print(m.get("display_name") or sys.argv[2]); break' "$CONFIG" "$member_id")"

    echo
    echo "    The first login for the household portal. This is $member_name"
    echo "    ($member_id) -- the assistant, tasks and files it opens belong to"
    echo "    that member. Everyone else is added from the admin page."
    printf '      login name: '
    read -r login </dev/tty || login=""
    if [ -z "$login" ]; then
        warn "no login name given; skipping the portal user"
        return
    fi
    portal_pw="$(read_new_password "password for $login" 8)"

    # The password goes in on stdin for the same reason as in hash_password:
    # argv is readable by every account on the box.
    if printf '%s' "$portal_pw" | "$VENV/bin/python" -c 'import bcrypt,json,os,sys,yaml
path, login, member, config = sys.argv[1:5]
password = sys.stdin.read()
# `nanobot_id` is the member position in the deployed list, 1-based, because
# that is what the deployer adds to `api_port_base` when it publishes the
# instances. It was the digits in the id, which is the same number only while
# nobody has ever left: ids are monotonic and never reused, so a household
# that has seen departures runs user2 beside user15 -- and 15 is a port with
# nothing listening on it.
cfg = yaml.safe_load(open(config)) or {}
order = ((cfg.get("services") or {}).get("nanobot") or {}).get("members") \
        or [m["id"] for m in (cfg.get("members") or [])]
# `member` is the join back to the config: the portal reads it to decide which
# folder on the share is this person s and whether the adult-only pages open.
record = {"username": login,
          "hash": bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
          "member": member,
          "nanobot_id": (order.index(member) + 1) if member in order else 1}
os.makedirs(os.path.dirname(path), exist_ok=True)
try:
    existing = json.load(open(path))
except Exception:
    existing = []
existing.append(record)
tmp = path + ".tmp"
# 0600 from creation, for the same reason as set_key above: this holds every
# login and bcrypt hash, and `os.replace` puts the tmp mode on the user store.
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump(existing, fh, indent=2)
os.replace(tmp, path)
os.chmod(path, 0o600)' "$users_file" "$login" "$member_id" "$CONFIG"; then
        unset portal_pw
        ok "created $login in $users_file"
    else
        unset portal_pw
        warn "could not write $users_file (a directory under /var/lib may need sudo)"
        warn "re-run with: sudo -E ./home-stack install --create-user"
    fi
}

# The five directories `paths:` names. All five, not the three this used to
# create: `backups:` is where `./home-stack backup` writes and `plugins:` is
# where a household's own services live, and a path declared but never created
# is one docker makes as an empty root-owned mount on first use -- the
# silent-green failure the rest of this function exists to prevent.
read_paths() {
    "$VENV/bin/python" - "$CONFIG" <<'PATHS_PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
paths = cfg.get("paths") or {}
for kind in ("config", "state", "media", "backups", "plugins"):
    print(paths.get(kind, f"/var/lib/home-stack/{kind}"))
# The backup mirror, when a household has asked for one. Not one of the five
# `paths:` kinds -- it is optional and set outright rather than derived from a
# root -- but it has the same first-run problem and worse consequences: its
# parent is often a mount that only root can write (`/mnt/data/...` here), so
# the first `./home-stack backup` fails on mkdir several minutes in, or docker
# gets there first and leaves a root-owned directory the deploying user cannot
# refresh. Appended last so `dirs[0]` is still the config path.
export_path = ((cfg.get("backups") or {}).get("export") or {}).get("path")
if export_path:
    print(export_path)
PATHS_PY
}

# Where `paths:` goes when the configured place needs root and root is not on
# offer. Under the user's own state directory, and deliberately *not* under
# ~/.local/share/home-stack: that is the deploy root, and guard_state_paths()
# hard-fails a state path inside the tree the deployer rsyncs with --delete.
fallback_paths_root() {
    printf '%s/home-stack' "${XDG_STATE_HOME:-$HOME/.local/state}"
}

# Point `paths:` at DIR/<kind> and write it back. Used by --paths-root and by
# the automatic relocation below, so there is one place that decides what the
# five paths under a root are called.
set_paths_root() {
    "$VENV/bin/python" - "$CONFIG" "$1" <<'SETPATHS_PY'
import sys
from ruamel.yaml import YAML

cfg_path, root = sys.argv[1], sys.argv[2].rstrip("/")
yaml = YAML()                    # round-trip, so the file keeps its comments
yaml.preserve_quotes = True
yaml.width = 4096
with open(cfg_path) as fh:
    cfg = yaml.load(fh)
cfg.setdefault("paths", {})
for kind in ("config", "state", "media", "backups", "plugins"):
    cfg["paths"][kind] = f"{root}/{kind}"
with open(cfg_path, "w") as fh:
    yaml.dump(cfg, fh)
SETPATHS_PY
}

# Existing is not the same as usable. `mkdir -p` on a directory that is already
# there returns 0 whatever owns it, so the version of this check that only ran
# mkdir reported "local, ready" for a root-owned tree and left the first deploy
# to fail on `Permission denied` several minutes in. Writing a file is the only
# question worth asking.
dir_is_writable() {
    local d="$1" probe
    [ -d "$d" ] || return 1
    probe="$d/.home-stack-write-probe.$$"
    ( : > "$probe" ) 2>/dev/null || return 1
    rm -f "$probe" 2>/dev/null
    return 0
}

# Create them locally, owned by whoever is deploying. `install -d -o` under
# sudo rather than `sudo mkdir`: root is needed to get *into* /var/lib once,
# not to own the tree afterwards. A root-owned state directory makes every
# later deploy need root too, which is the thing this stack does not want.
make_paths_local() {
    local dir rc=0
    for dir in "$@"; do
        mkdir -p "$dir" 2>/dev/null && continue
        sudo -n install -d -o "$(id -un)" -g "$(id -gn)" "$dir" 2>/dev/null || rc=1
    done
    for dir in "$@"; do
        dir_is_writable "$dir" || rc=1
    done
    return "$rc"
}

# Is there anything in the configured place worth not walking away from? Only
# asked before relocating: moving `paths:` while a previous deploy has state
# there would orphan the camera registry, the user store and the databases in
# one silent step -- the same class of loss guard_state_paths() exists to stop.
paths_hold_state() {
    local dir
    for dir in "$@"; do
        [ -d "$dir" ] || continue
        [ -n "$(ls -A "$dir" 2>/dev/null)" ] && return 0
    done
    return 1
}

prepare_hosts() {
    step "preparing target hosts"
    [ -f "$CONFIG" ] || die "config/home-stack.yml does not exist; run without --prepare-hosts first"

    # The deploy root defaults to ~/.local/share/home-stack, which the deploying
    # user owns. Everything after this step runs as that user.
    local deployroot="${HOME_STACK_DEPLOY_ROOT:-$HOME/.local/share/home-stack}"

    local dirs=() line
    while IFS= read -r line; do dirs+=("$line"); done < <(read_paths)

    # Read into an array first rather than piping into `while`: a pipeline runs
    # its loop in a subshell, and the relocation below has to be visible to the
    # summary and to every later role.
    local hosts=()
    while IFS= read -r line; do hosts+=("$line"); done < <(
        "$VENV/bin/python" - "$CONFIG" <<'HOSTS_PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
for role, host in (cfg.get("hosts") or {}).items():
    print(role, host["address"], host.get("user", "root"))
HOSTS_PY
    )

    local role address user
    for line in "${hosts[@]}"; do
        read -r role address user <<<"$line"
        printf '    %s (%s): ' "$role" "$address"
        # A local role is local, and the deployer knows it: Target._detect_local
        # keys on the address alone and short-circuits ssh and rsync entirely.
        # So do the same here rather than trying to ssh to a user that a
        # single-box install has no reason to have -- the warning that produced
        # sent people off to run ssh-copy-id for a connection nothing ever makes.
        if [ "$address" = "127.0.0.1" ] || [ "$address" = "localhost" ] \
           || [ "$address" = "::1" ]; then
            if make_paths_local "${dirs[@]}" "$deployroot"; then
                printf '%slocal, ready%s\n' "$C_OK" "$C_OFF"
                continue
            fi
            # Configured somewhere this user cannot write, and sudo is not on
            # offer. Finishing with a warning and a command to run afterwards is
            # an install that did not install, so put the state somewhere this
            # user owns and say where it went.
            if paths_hold_state "${dirs[@]}"; then
                printf '%slocal, and its directories are not writable%s\n' \
                    "$C_WARN" "$C_OFF"
                warn "there is state under them already, so this will not move it."
                warn "hand them to yourself instead:"
                warn "  sudo chown -R \"\$(id -un):\$(id -gn)\" $(dirname "${dirs[0]}")"
                continue
            fi
            local root
            root="$(fallback_paths_root)"
            set_paths_root "$root"
            dirs=()
            while IFS= read -r line; do dirs+=("$line"); done < <(read_paths)
            if make_paths_local "${dirs[@]}" "$deployroot"; then
                printf '%slocal, ready%s\n' "$C_OK" "$C_OFF"
                note "the configured paths needed root, so the state lives under"
                note "your own home instead:  $root"
                note "change it on the admin page under Site, or re-run with"
                note "  ./home-stack install --paths-root /somewhere/else"
            else
                printf '%slocal, but its directories could not be created%s\n' \
                    "$C_WARN" "$C_OFF"
                warn "could not create $root either -- check the permissions on"
                warn "  $(dirname "$root")"
            fi
            continue
        fi
        # Created ahead of any deploy, deliberately. If a bind mount is what
        # creates these, docker makes an empty root-owned directory, the service
        # starts with no state, and the deploy still goes green.
        #
        # -n is load-bearing on every ssh here: without it ssh reads this
        # loop's stdin, swallows the remaining hosts, and only the first is
        # prepared -- silently, with exit 0. On a three-role install that left
        # two hosts without state directories, so the first deploy let docker
        # create them as empty root-owned mounts: the exact silent-green
        # failure this function exists to prevent.
        local remote_mk="" dir qdir
        for dir in "${dirs[@]}" "$deployroot"; do
            qdir=$(printf '%q' "$dir")
            remote_mk="$remote_mk mkdir -p $qdir 2>/dev/null || sudo -n install -d -o \"\$(id -un)\" -g \"\$(id -gn)\" $qdir 2>/dev/null;"
        done
        if ssh -n -o BatchMode=yes -o ConnectTimeout=8 \
               -o StrictHostKeyChecking=accept-new \
               "$user@$address" "$remote_mk true" 2>/dev/null; then
            printf '%sok%s' "$C_OK" "$C_OFF"
            # Existing is not usable, the same root-owned trap as locally --
            # and over ssh it is worse, because nothing on this side can see it.
            # Ask the far end to write a file in each one.
            local quoted="" unwritable
            for dir in "${dirs[@]}"; do quoted="$quoted $(printf '%q' "$dir")"; done
            unwritable=$(ssh -n -o BatchMode=yes -o ConnectTimeout=8 \
                "$user@$address" \
                "for d in$quoted; do
                     ( : > \"\$d/.probe.\$\$\" ) 2>/dev/null && rm -f \"\$d/.probe.\$\$\" \\
                        || printf '%s ' \"\$d\"
                 done" 2>/dev/null)
            if [ -n "$unwritable" ]; then
                printf ' %s(not writable there: %s)%s' \
                    "$C_WARN" "${unwritable% }" "$C_OFF"
            fi
            # Every verify check runs on the target, so the tools they need must
            # exist there. Checking only the deploying machine meant a poll loop
            # spent its whole timeout collecting exit 127.
            local missing_remote
            missing_remote=$(ssh -n -o BatchMode=yes -o ConnectTimeout=8 \
                "$user@$address" \
                'for t in docker curl mosquitto_pub python3; do
                     command -v "$t" >/dev/null 2>&1 || printf "%s " "$t"
                 done' 2>/dev/null)
            if [ -n "$missing_remote" ]; then
                printf ' %s(missing there: %s)%s' \
                    "$C_WARN" "${missing_remote% }" "$C_OFF"
            fi
            printf '\n'
        else
            printf '%sunreachable%s\n' "$C_WARN" "$C_OFF"
            warn "could not reach $user@$address over ssh, or could not create"
            warn "its directories. Set up key auth:  ssh-copy-id $user@$address"
            warn "Deploy somewhere you own if the path is the problem:"
            warn "  ./home-stack deploy --remote-root \$HOME/home-stack"
        fi
    done
}

summary() {
    # What is actually left, not a generic checklist. The installer knows
    # whether the API key is in the file and whether a password was set, so it
    # says so rather than listing steps somebody has already done.
    local have_key have_admin admin_port admin_bind
    have_key="$(get_model_key "$SECRETS")"
    have_admin="$(get_key ADMIN_PASSWORD_HASH "$SECRETS")"
    admin_port="$("$VENV/bin/python" - "$CONFIG" <<'PORT_PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print(((cfg.get("services") or {}).get("admin") or {}).get("port", 8099))
PORT_PY
)"
    admin_bind="$("$VENV/bin/python" - "$CONFIG" <<'BIND_PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print(((cfg.get("services") or {}).get("admin") or {}).get("bind", "127.0.0.1"))
BIND_PY
)"

    local assistant
    assistant="$("$VENV/bin/python" - "$CONFIG" <<'ASST_PY' 2>/dev/null || echo Alfred
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print((cfg.get("site") or {}).get("assistant_name", "Alfred"))
ASST_PY
)"

    printf '\n%sReady.%s\n\n' "$C_OK" "$C_OFF"
    printf '  The assistant is called %s.\n' "${assistant:-Alfred}"
    # Only what this run is certain to have done. The state directories need a
    # writable path and the passwords need a terminal, and each says so for
    # itself above -- claiming them here made the summary contradict the
    # warnings four lines over it.
    echo "  Done so far: the site config and every credential that can be"
    echo "  generated. Nothing is running yet."
    echo

    if [ -z "$have_key" ]; then
        printf '  %sOne thing is missing.%s The assistant needs a model API key:\n' "$C_WARN" "$C_OFF"
        echo "    secrets/smart-home-bot.env  ->  OPENCODE_API_KEY=..."
        echo "  It is the only credential that ever leaves your network. Everything"
        echo "  else was generated here and stays here."
        echo
    fi

    echo "  Start the house. Either order works:"
    echo "    ./home-stack deploy admin   just the page, ~15s, then drive the"
    echo "                                rest from it -- it deploys too"
    echo "    ./home-stack plan           the whole plan, changing nothing"
    echo "    ./home-stack deploy         all of it now"
    echo
    echo "  The admin page needs nothing else running: it seeds the live config"
    echo "  and credentials from the files this installer just wrote. Who lives"
    echo "  here, which services are on, the timezone, credentials, and"
    echo "  deploying again all happen there:"
    case "$admin_bind" in
        127.0.0.1|::1|localhost)
            echo "    http://127.0.0.1:$admin_port/    on this machine"
            echo "    ssh -L $admin_port:127.0.0.1:$admin_port <this-host>    from anywhere else"
            echo
            echo "  It is on loopback because it holds every credential in the stack"
            echo "  and can deploy. services.admin.bind opens it to the LAN." ;;
        *)
            echo "    http://<this-host>:$admin_port/"
            printf '  %swarn%s services.admin.bind is %s, so anyone who can reach\n' \
                "$C_WARN" "$C_OFF" "$admin_bind"
            echo "       this machine on the network can open it." ;;
    esac

    if [ -z "$have_admin" ]; then
        echo
        printf '  %sNo admin password yet%s -- whoever opens that page first sets it.\n' \
            "$C_WARN" "$C_OFF"
        echo "  Set one now instead:  ./home-stack install --create-user"
    fi

    echo
    echo "  The VPS, phone notifications and public certificates are all off. The"
    echo "  house works on your LAN without any of them; docs/optional-cloud.md"
    echo "  covers turning them on later."
    echo
}

# ---------------------------------------------------------------------------

usage() {
    # Written out rather than sliced out of the header with line numbers, which
    # drifted: `sed -n '2,24p'` had come to include the closing rule and
    # `set -euo pipefail`, so --help ended by printing a line of shell.
    cat <<EOF
  home-stack installer

  Takes this machine from nothing to a stack you can deploy. It does not deploy
  anything itself -- that is deploy/deploy.py -- so you can re-run it safely
  while the house is up.

  Everything runs on this one PC unless you say otherwise, and nothing here
  reaches the internet.

    ./home-stack install                     set everything up, asking as it goes
    ./home-stack install --check             just check prerequisites, change nothing
    ./home-stack install --generate-secrets  fill in every generated key
    ./home-stack install --prepare-hosts     create the state directories
    ./home-stack install --create-user       admin password and the first login
    ./home-stack install --opencode          set up opencode for the Programmer
    ./home-stack install --paths-root DIR    put config, state, media, backups
                                             and plugins under DIR

  State goes to /var/lib/home-stack by default, which needs root once. If this
  machine will not give it, the installer puts the five directories under your
  own home instead and tells you where -- it does not stop and ask you to run
  something. Either way they end up owned by you, so no deploy after this needs
  root at all.

  The one credential you have to bring is the assistant's model API key. The two
  passwords are asked for, not generated -- they are the ones a person types.
  Everything else is generated here and stays here.
EOF
}

main() {
    local do_check=0 do_secrets=0 do_hosts=0 do_user=0 do_assistant=0 do_runtime=0
    local do_opencode=0 do_all=1
    local paths_root=""
    # `--paths-root DIR` takes a value, so this is a while loop over $@ rather
    # than a for loop over the words -- the version that iterated `for arg`
    # could not see the argument after the flag.
    while [ $# -gt 0 ]; do
        arg="$1"
        case "$arg" in
            --paths-root)       shift; [ $# -gt 0 ] || die "--paths-root needs a directory"
                                paths_root="$1" ;;
            --paths-root=*)     paths_root="${arg#*=}" ;;
            --check)            do_check=1; do_all=0 ;;
            --generate-secrets) do_secrets=1; do_all=0 ;;
            --prepare-hosts)    do_hosts=1; do_all=0 ;;
            --create-user)      do_user=1;  do_all=0 ;;
            --assistant)        do_assistant=1; do_all=0 ;;
            --runtime)          do_runtime=1; do_all=0 ;;
            --opencode)         do_opencode=1; do_all=0 ;;
            -h|--help)          usage; exit 0 ;;
            *)                  die "unknown option: $arg" ;;
        esac
        shift
    done

    printf '\n  home-stack installer\n\n'

    check_prerequisites
    [ "$do_check" -eq 1 ] && exit 0

    setup_venv

    # Before anything reads `paths:`, and after create_config has put a file
    # there to edit. Applied on every run it is passed, so it is also how you
    # move an install that has not been deployed yet.
    if [ -n "$paths_root" ]; then
        [ -f "$CONFIG" ] || create_config
        case "$paths_root" in
            /*) ;;
            *)  die "--paths-root has to be an absolute path: $paths_root" ;;
        esac
        set_paths_root "$paths_root"
        ok "paths: now under $paths_root"
    fi

    if [ "$do_secrets" -eq 1 ]; then generate_secrets; exit 0; fi
    if [ "$do_hosts" -eq 1 ];   then prepare_hosts;    exit 0; fi
    if [ "$do_user" -eq 1 ];    then create_first_user; exit 0; fi
    if [ "$do_assistant" -eq 1 ]; then choose_assistant; exit 0; fi
    if [ "$do_runtime" -eq 1 ]; then choose_runtime; exit 0; fi
    if [ "$do_opencode" -eq 1 ]; then setup_opencode; exit 0; fi

    if [ "$do_all" -eq 1 ]; then
        create_config
        [ -n "$paths_root" ] && set_paths_root "$paths_root"
        generate_secrets
        # After the secrets exist, because it writes two of them; before the
        # summary, so what it settles is what the summary reports.
        choose_assistant
        choose_runtime
        # After choose_assistant, because it is the same decision seen from
        # the other end: which model answers, and now also which agent.
        setup_opencode
        prepare_hosts
        create_first_user
        summary
    fi
}

main "$@"
