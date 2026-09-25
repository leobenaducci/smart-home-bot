#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# new-plugin.sh — scaffold a household service the package does not ship
# ---------------------------------------------------------------------------
# Asks what the service is and writes a plugin that deploys. Run it through
# `./home-stack plugin`.
#
# The one rule it enforces without asking: a plugin lives OUTSIDE this tree.
# In here it would be swept up by sanitize.py, shipped by any clone, and
# destroyed by a checkout. See docs/plugins.md.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/config/home-stack.yml"

if [ -t 1 ]; then
    C_STEP=$'\033[36m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
    C_STEP=""; C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi
step() { printf '%s==> %s%s\n' "$C_STEP" "$1" "$C_OFF"; }
ok()   { printf '%s    ok  %s%s\n' "$C_OK" "$1" "$C_OFF"; }
warn() { printf '%s    warn %s%s\n' "$C_WARN" "$1" "$C_OFF"; }
die()  { printf '%s    FAIL %s%s\n' "$C_ERR" "$1" "$C_OFF" >&2; exit 1; }

ask() {  # ask <prompt> <default>  -> answer on stdout, prompts on stderr
    local prompt="$1" default="${2:-}" reply
    if [ -n "$default" ]; then
        printf '  %s [%s] ' "$prompt" "$default" >&2
    else
        printf '  %s ' "$prompt" >&2
    fi
    read -r reply </dev/tty || reply=""
    printf '%s' "${reply:-$default}"
}

yesno() {  # yesno <prompt> <Y|N>
    local prompt="$1" default="${2:-Y}" reply
    printf '  %s [%s/%s] ' "$prompt" \
        "$([ "$default" = Y ] && echo Y || echo y)" \
        "$([ "$default" = Y ] && echo n || echo N)" >&2
    read -r reply </dev/tty || reply=""
    reply="${reply:-$default}"
    case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

[ -t 0 ] || die "this asks questions; run it from a terminal"

printf '\n  %sa new plugin%s\n\n' "$C_STEP" "$C_OFF"
echo "  A plugin is one of your own services, deployed by this package but"
echo "  living outside it -- so the package stays clean and redistributable"
echo "  while your house runs on it. docs/plugins.md has the whole story."
echo

# --- name ------------------------------------------------------------------
name=""
while [ -z "$name" ]; do
    name="$(ask 'What is it called? (lowercase, e.g. home-switches)' '')"
    case "$name" in
        "") ;;
        [a-z0-9]*[a-z0-9]|[a-z0-9]) ;;
        *) warn "lowercase letters, digits and dashes; it becomes a service name"; name="" ;;
    esac
    case "$name" in *[!a-z0-9-]*) warn "lowercase letters, digits and dashes only"; name="" ;; esac
done
# A plugin may not take a name this package already uses -- the deployer
# refuses it later, but saying so now is cheaper than finding out at deploy.
if grep -qE "^  $name:" "$ROOT/deploy/manifest.yml" 2>/dev/null; then
    die "$name is a service this package already ships. Pick another name."
fi

desc="$(ask 'One line: what does it do?' "The $name service.")"

# The env-var spelling of the name, computed once. Uppercasing a whole line
# with `tr` also uppercased `{services.x.port}`, which has to stay lowercase or
# the deployer cannot resolve it -- the generated plugin.yml said
# {SERVICES.HOME-SOLAR.PORT} and interpolation failed.
UPPER="$(printf '%s' "$name" | tr 'a-z-' 'A-Z_')"

# --- where -----------------------------------------------------------------
default_dir="$("$ROOT/.venv/bin/python" - "$CONFIG" <<'PY' 2>/dev/null || echo /var/lib/home-stack/plugins
import sys, yaml
try:
    cfg = yaml.safe_load(open(sys.argv[1])) or {}
except Exception:
    cfg = {}
print((cfg.get("paths") or {}).get("plugins", "/var/lib/home-stack/plugins"))
PY
)"
parent="$(ask 'Where should it live?' "$default_dir")"
dir="$parent/$name"

# Outside the package, always. This is the whole arrangement, not a preference.
case "$(cd "$parent" 2>/dev/null && pwd || echo "$parent")" in
    "$ROOT"|"$ROOT"/*)
        die "that is inside the package. A plugin there would be swept up by
    sanitize.py, shipped by every clone, and destroyed by a checkout.
    Put it somewhere like $default_dir." ;;
esac
[ -e "$dir" ] && die "$dir already exists"

# --- shape -----------------------------------------------------------------
role="$(ask 'Which machine runs it? (hub | compute | storage)' hub)"
case "$role" in hub|compute|storage) ;; *) die "role must be hub, compute or storage" ;; esac
port="$(ask 'Which port does it listen on?' 8095)"

want_tile=0;   yesno 'Put a tile for it on the dashboard?' Y && want_tile=1
want_state=0;  yesno 'Does it keep data between restarts?' Y && want_state=1
want_secret=0; secret_key=""
if yesno 'Does it need a credential you will paste in?' N; then
    want_secret=1
    secret_key="$(ask '  name of the credential (UPPER_SNAKE)' "${UPPER}_API_TOKEN")"
fi
want_skill=0;  yesno 'Should the assistant be able to use it?' N && want_skill=1
want_i18n=0;   yesno 'Will its page be translated?' N && want_i18n=1


# --- write it --------------------------------------------------------------
step "writing $dir"
mkdir -p "$dir/server"
[ "$want_i18n" -eq 1 ] && mkdir -p "$dir/i18n"
[ "$want_skill" -eq 1 ] && mkdir -p "$dir/floor"

url_var="${UPPER}_API_URL"

{
  echo "contract: 1                 # the plugin contract this was written against"
  echo "name: $name"
  echo
  echo "services:"
  echo "  $name:"
  echo "    description: $desc"
  echo "    role: $role"
  echo "    units:"
  echo "      - name: server"
  echo "        dir: server         # relative to THIS plugin, not to the package"
  echo "        build: true"
  echo "        compose: docker-compose.yml"
  echo "        env:"
  echo "          ${UPPER}_PORT: \"{services.$name.port}\""
  if [ "$want_state" -eq 1 ]; then
      echo "        state:"
      echo "          # Never inside the deploy directory: a deploy replaces that tree,"
      echo "          # so live state in there is destroyed and the deploy still says ok."
      echo "          - { path: \"{paths.state}/$name\", mount: /data,"
      echo "              env: ${UPPER}_STATE_DIR }"
  fi
  echo "        verify:"
  echo "          # A check must be able to fail. A bare status code passes against a"
  echo "          # redirect to a login page and against a 404 handler returning 200."
  echo "          - http: \"http://127.0.0.1:{services.$name.port}/api/ping\""
  echo "            expect_json: {status: healthy, service: $name}"
  echo "            timeout: 60"
  if [ "$want_secret" -eq 1 ]; then
      echo "    secrets:"
      echo "      required: [$secret_key]"
  fi
  if [ "$want_tile" -eq 1 ]; then
      echo
      echo "tiles:"
      echo "  - name: ${name^}"
      echo "    # href is opened by a person's browser; siteMonitor is fetched by"
      echo "    # Homepage's own container, so it takes the container-facing address."
      echo "    href: \"http://{hosts.$role.address}:{services.$name.port}\""
      echo "    description: $desc"
      echo "    siteMonitor: \"http://{hosts.$role.from_container}:{services.$name.port}/api/ping\""
  fi
  echo
  echo "impact:"
  echo "  services.$name: [$name]"
  if [ "$want_skill" -eq 1 ]; then
      echo
      echo "# Telling the assistant where to ask. The one place a plugin reaches into"
      echo "# a service it does not own, so it is narrow and named."
      echo "contributes:"
      echo "  nanobot:"
      echo "    env:"
      echo "      $url_var: \"http://{hosts.$role.from_container}:{services.$name.port}\""
      echo "    # Without this the skill runs, finds nothing, and reports your"
      echo "    # service as down. Additive only -- that list is a security boundary."
      echo "    allowed_env_keys: [$url_var]"
      echo "    skills:"
      echo "      - name: $name"
      echo "        env: $url_var"
      echo "        floor: floor          # used when your service is unreachable"
  fi
} > "$dir/plugin.yml"

cat > "$dir/server/app.py" <<PY
"""$desc

Answers the two things the deployer and the dashboard ask for:
  GET /api/ping   -- the health check the deploy verifies against
  GET /           -- whatever this service is actually for
"""
import os

from flask import Flask, jsonify

app = Flask(__name__)
STATE_DIR = os.environ.get("${UPPER}_STATE_DIR", "/data")


@app.get("/api/ping")
def ping():
    # The deploy asserts this payload, not the status code. Keep both fields.
    return jsonify(status="healthy", service="$name")


@app.get("/")
def index():
    return "<h1>$name</h1><p>$desc</p>"


if __name__ == "__main__":
    # 0.0.0.0 is the container's own interfaces, which is the only address the
    # published port can forward to. Who may reach it is the port publish in
    # docker-compose.yml, not this.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "$port")))
PY

cat > "$dir/server/Dockerfile" <<DOCKER
FROM python:3.12-slim

WORKDIR /app
# Requirements first, so editing app.py does not reinstall flask on every build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app

CMD ["python", "/app/app.py"]
DOCKER

printf 'flask>=3.0\n' > "$dir/server/requirements.txt"

{
  echo "services:"
  echo "  $name:"
  echo "    build: ."
  echo "    container_name: $name"
  echo "    restart: unless-stopped"
  echo "    environment:"
  echo "      # Real defaults, not \${VAR:-}. Compose substitutes the empty string"
  echo "      # for an unset name, and an empty value beats a code-side default."
  echo "      - PORT=\${${UPPER}_PORT:-$port}"
  [ "$want_state" -eq 1 ] && \
  echo "      - ${UPPER}_STATE_DIR=/data"
  [ "$want_secret" -eq 1 ] && echo "      - $secret_key=\${$secret_key:?$secret_key is required}"
  echo "    ports:"
  echo "      - \"\${${UPPER}_PORT:-$port}:$port\""
  if [ "$want_state" -eq 1 ]; then
      echo "    volumes:"
      echo "      # Absolute, and supplied by the deployer. A relative bind mount here"
      echo "      # points inside the tree a deploy replaces -- which is how four"
      echo "      # separate wipes happened, each reporting success."
      echo "      - \${${UPPER}_STATE_DIR}:/data"
  fi
} > "$dir/server/docker-compose.yml"

if [ "$want_skill" -eq 1 ]; then
cat > "$dir/floor/SKILL.md" <<SKILL
# $name

$desc

This is the floor copy -- what the assistant uses when the service is not
answering. The live version is whatever \`GET \$$url_var/skill\` returns, and
that one wins.

## What it can do

Describe the calls in plain terms. The assistant reads this, so write it for a
reader who has never seen your API.

    GET  /api/ping        is it up
    GET  /                the page a person opens
SKILL
fi

if [ "$want_i18n" -eq 1 ]; then
cat > "$dir/i18n/en.json" <<JSON
{
  "_meta.name": "English",
  "$name.title": "${name^}",
  "$name.description": "$desc"
}
JSON
fi

cat > "$dir/README.md" <<MD
# $name

$desc

A plugin for smart-home-bot: deployed by that package, living outside it, so
the package stays clean under \`sanitize.py --check\` while this house runs on
it.

## Turning it on

1. Add it to \`config/home-stack.yml\` in the package:

   \`\`\`yaml
   plugins:
     - $name          # relative to paths.plugins, or an absolute path

   services:
     $name:
       enabled: true
       host: $role
       port: $port
   \`\`\`

2. Check and deploy:

   \`\`\`bash
   ./home-stack list                 # $name should appear, marked a plugin
   ./home-stack check                # every compose variable is supplied
   ./home-stack plan $name           # the plan, changing nothing
   ./home-stack deploy $name
   \`\`\`
$( [ "$want_secret" -eq 1 ] && printf '\n3. Set `%s` on the admin page, under Credentials.\n' "$secret_key" )

## What the deployer will not let you do

- keep live state inside the deploy directory -- it is replaced on every deploy
- write a verify check that cannot fail
- hand a bridge-networked container a loopback address
- take a name this package already ships

All four are refused with a reason. \`docs/plugins.md\` in the package explains
each one.
MD

ok "$dir"
find "$dir" -type f | sed "s|$dir|  .|" | sort

# --- what is left ----------------------------------------------------------
echo
step "what is left"
echo "  1. Add it to $CONFIG:"
echo
echo "       plugins:"
echo "         - $name"
echo
echo "       services:"
echo "         $name:"
echo "           enabled: true"
echo "           host: $role"
echo "           port: $port"
echo
[ "$want_secret" -eq 1 ] && echo "  2. Set $secret_key on the admin page, under Credentials." && echo
echo "  Then:"
echo "     ./home-stack list          $name should appear, marked a plugin"
echo "     ./home-stack check         every compose variable is supplied"
echo "     ./home-stack deploy $name"
echo
warn "the generated service answers /api/ping and serves a placeholder page."
warn "replace server/app.py with the real thing -- everything around it deploys."
