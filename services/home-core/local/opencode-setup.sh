#!/usr/bin/env bash
# OpenCode web setup — per-user instances on dedicated ports.
#
# Usage:
#   ./opencode-setup.sh                  # setup only (configs + services)
#   ./opencode-setup.sh --passwords      # print per-user passwords
#   ./opencode-setup.sh --status         # check running instances
#
# Run as root (sudo) to install system services.
# Run as user to create configs / start via nohup.

set -euo pipefail

# ---- config (keep in sync with local/app.py OPENCODE_WEB_PORTS) ----
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME="$(getent passwd "$REAL_USER" 2>/dev/null | cut -d: -f6 || echo "$HOME")"
if command -v opencode &>/dev/null; then
    OPENCODE_BIN="${OPENCODE_BIN:-"$(command -v opencode)"}"
elif sudo -u "$REAL_USER" command -v opencode &>/dev/null 2>&1; then
    OPENCODE_BIN="${OPENCODE_BIN:-"$(sudo -u "$REAL_USER" command -v opencode)"}"
else
    OPENCODE_BIN="${OPENCODE_BIN:-/home/homestack/.opencode/bin/opencode}"
fi
BASE_DIR="${BASE_DIR:-${XDG_DATA_HOME:-$REAL_HOME/opencode}}"
BASE_PORT=4097

declare -A USERS
USERS[user1]=4097
USERS[user2]=4098
# ----------------------------------------------------------------

info()  { printf "\033[36m==>\033[0m %s\n" "$*"; }
warn()  { printf "\033[33m==>\033[0m %s\n" "$*" >&2; }
err()   { printf "\033[31m==>\033[0m %s\n" "$*" >&2; exit 1; }

ENV_FILE="$BASE_DIR/.env"

load_env() {
    if [[ -f "$ENV_FILE" ]]; then
        set -a; source "$ENV_FILE"; set +a
    fi
}

save_env() {
    mkdir -p "$BASE_DIR"
    local user pw
    for user in "${!USERS[@]}"; do
        if grep -q "^OPENCODE_PW_${user}=" "$ENV_FILE" 2>/dev/null; then
            continue
        fi
        pw=$(openssl rand -base64 24 2>/dev/null || uuidgen | base64)
        echo "OPENCODE_PW_${user}=${pw}" >> "$ENV_FILE"
    done
}

setup_configs() {
    info "Creating config directories under $BASE_DIR"
    local user port
    for user in "${!USERS[@]}"; do
        port="${USERS[$user]}"
        local dir="$BASE_DIR/$user"
        mkdir -p "$dir"
        if [[ ! -f "$dir/opencode.json" ]]; then
            cat > "$dir/opencode.json" <<CONF
{
  "server": {
    "hostname": "0.0.0.0"
  }
}
CONF
            info "  Wrote $dir/opencode.json"
        else
            info "  Config exists at $dir/opencode.json (skipped)"
        fi
    done
}

setup_password_file() {
    load_env
    local missing=0
    for user in "${!USERS[@]}"; do
        local var="OPENCODE_PW_${user}"
        if [[ -z "${!var:-}" ]]; then
            missing=1
            break
        fi
    done
    if [[ $missing -eq 1 ]]; then
        info "Generating passwords → $ENV_FILE"
        save_env
    else
        info "Passwords already set in $ENV_FILE"
    fi
    load_env
}

setup_systemd() {
    if [[ $EUID -ne 0 ]]; then
        warn "Not running as root — skipping systemd installation."
        warn "Run with sudo to install system services: sudo $0"
        return
    fi

    local user port pw
    for user in "${!USERS[@]}"; do
        port="${USERS[$user]}"
        local var="OPENCODE_PW_${user}"
        pw="${!var:-}"

        if [[ -z "$pw" ]]; then
            err "Password not set for user $user — run without sudo first"
        fi

        local svc="opencode-${user}.service"
        local path="/etc/systemd/system/$svc"

        cat > "$path" <<UNIT
[Unit]
Description=opencode web for ${user}
After=network.target

[Service]
Type=simple
WorkingDirectory=${BASE_DIR}/${user}
Environment=OPENCODE_SERVER_PASSWORD=${pw}
ExecStart=${OPENCODE_BIN} web --port ${port} --hostname 0.0.0.0
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

        info "  Wrote $path"

        systemctl enable "$svc" 2>/dev/null || true
        systemctl restart "$svc" 2>/dev/null || true
        info "  Enabled and restarted $svc"
    done
}

start_nohup() {
    info "Starting instances via nohup (use systemd for production)"

    local user port pw
    for user in "${!USERS[@]}"; do
        port="${USERS[$user]}"
        local var="OPENCODE_PW_${user}"
        pw="${!var:-}"

        if [[ -z "$pw" ]]; then
            err "Password not set for user $user — run with --passwords first"
        fi

        local log="$BASE_DIR/$user/opencode.log"
        local pid="$BASE_DIR/$user/opencode.pid"

        if [[ -f "$pid" ]] && kill -0 "$(cat "$pid")" 2>/dev/null; then
            info "  ${user} (port ${port}) already running PID $(cat "$pid")"
            continue
        fi

        OPENCODE_SERVER_PASSWORD="$pw" nohup "$OPENCODE_BIN" web \
            --port "$port" --hostname 0.0.0.0 \
            > "$log" 2>&1 &
        echo $! > "$pid"
        info "  Started ${user} → port ${port}, PID $!"
    done
}

print_passwords() {
    load_env
    local user pw
    echo "Per-user opencode passwords (also in $ENV_FILE):"
    echo
    for user in "${!USERS[@]}"; do
        local var="OPENCODE_PW_${user}"
        pw="${!var:-<not set>}"
        printf "  %-12s  port %s  password: %s\n" "${user}" "${USERS[$user]}" "$pw"
    done
}

print_status() {
    load_env
    local user port pw ok=0 total=0
    for user in "${!USERS[@]}"; do
        total=$((total + 1))
        port="${USERS[$user]}"
        local var="OPENCODE_PW_${user}"
        pw="${!var:-}"
        if [[ -n "$pw" ]] && curl -sf -u "opencode:${pw}" -o /dev/null "http://127.0.0.1:${port}/global/health" 2>/dev/null; then
            printf "  \033[32m✔\033[0m  %-12s  port %s  healthy\n" "${user}" "${port}"
            ok=$((ok + 1))
        else
            printf "  \033[31m✘\033[0m  %-12s  port %s  unreachable\n" "${user}" "${port}"
        fi
    done
    echo
    if [[ "$ok" -eq "$total" ]]; then
        info "All $total instances healthy"
    else
        warn "$ok/$total instances healthy"
    fi
}

# ---- main ----
case "${1:-}" in
    --passwords)
        setup_configs
        setup_password_file
        print_passwords
        ;;
    --status)
        print_status
        ;;
    *)
        setup_configs
        setup_password_file
        if [[ $EUID -eq 0 ]]; then
            setup_systemd
        else
            start_nohup
        fi
        echo
        print_passwords
        echo
        print_status
        ;;
esac
