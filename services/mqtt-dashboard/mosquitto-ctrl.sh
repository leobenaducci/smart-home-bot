#!/bin/bash
# Mosquitto control script (local)
# Usage: ./mosquitto-ctrl.sh {install|build|start|stop|restart|status|enable|disable}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MQTT_DIR="$SCRIPT_DIR/mosquitto"
DASHBOARD_DIR="$SCRIPT_DIR/dashboard"
DASHBOARD_PIDFILE="/tmp/mqtt-dashboard.pid"

usage() {
    echo "Usage: $0 {install|build|start|stop|restart|status|enable|disable}"
    exit 1
}

case "${1:-usage}" in
    install)
        # Save script directory before any cd
        SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
        # Install all required dependencies
        sudo apt-get update && sudo apt-get install -y \
            build-essential \
            cmake \
            libssl-dev \
            libc-ares-dev \
            uuid-dev \
            libwebsockets-dev \
            libedit-dev \
            libmicrohttpd-dev \
            libsqlite3-dev \
            libcjson-dev
        # Clone repository if it doesn't exist
        if [ ! -d "$MQTT_DIR" ]; then
            git clone https://github.com/eclipse/mosquitto.git "$MQTT_DIR"
        fi
        # Build and install mosquitto (ignore dynamic-security plugin install errors)
        cd "$MQTT_DIR" && make -j$(nproc) && sudo make install || true
        # Install mosquitto binary if not in path
        if [ ! -f /usr/local/sbin/mosquitto ]; then
            cd "$MQTT_DIR/src" && sudo make install
        fi
        # Setup mosquitto user, config, and systemd service
        "$SCRIPT_DIR/mosquitto-ctrl.sh" setup
        echo "Mosquitto installation complete. Use './mosquitto-ctrl.sh start' to start the service."
        ;;
    setup)
        # Create mosquitto user if it doesn't exist
        if ! id -u mosquitto >/dev/null 2>&1; then
            sudo useradd -r -s /usr/sbin/nologin mosquitto
        fi
        # Create config directory and basic config
        sudo mkdir -p /etc/mosquitto
        if [ ! -f /etc/mosquitto/mosquitto.conf ]; then
            sudo bash -c 'cat > /etc/mosquitto/mosquitto.conf << EOF
# Mosquitto MQTT Broker Configuration
listener 1883
allow_anonymous true
persistence false
EOF'
        fi
        # Install systemd service with correct path
        sudo bash -c 'cat > /etc/systemd/system/mosquitto.service << EOF
[Unit]
Description=Mosquitto MQTT Broker
Documentation=man:mosquitto.conf(5) man:mosquitto(8)
After=network-online.target
Wants=network-online.target

[Service]
User=mosquitto
ExecStart=/usr/local/sbin/mosquitto -c /etc/mosquitto/mosquitto.conf
ExecReload=/bin/kill -HUP \$MAINPID
Restart=always
RestartSec=5
RuntimeDirectory=mosquitto
LogsDirectory=mosquitto

[Install]
WantedBy=multi-user.target
EOF'
        sudo systemctl daemon-reload
        ;;
    build)
        # Save script directory before any cd
        SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
        cd "$MQTT_DIR" && make -j$(nproc) && sudo make install || true
        # Install mosquitto binary if not in path
        if [ ! -f /usr/local/sbin/mosquitto ]; then
            cd "$MQTT_DIR/src" && sudo make install
        fi
        # Run setup steps
        "$SCRIPT_DIR/mosquitto-ctrl.sh" setup
        ;;
    start)
        sudo systemctl start mosquitto
        if [ -f "$DASHBOARD_DIR/package.json" ]; then
            if [ -f "$DASHBOARD_PIDFILE" ] && kill -0 $(cat "$DASHBOARD_PIDFILE") 2>/dev/null; then
                echo "Dashboard is already running (PID: $(cat $DASHBOARD_PIDFILE))"
            else
                cd "$DASHBOARD_DIR" && nohup npm start > /tmp/mqtt-dashboard.log 2>&1 &
                echo $! > "$DASHBOARD_PIDFILE"
                echo "Dashboard started (PID: $(cat $DASHBOARD_PIDFILE))"
            fi
        fi
        ;;
    stop)
        sudo systemctl stop mosquitto
        if [ -f "$DASHBOARD_PIDFILE" ]; then
            kill $(cat "$DASHBOARD_PIDFILE") 2>/dev/null && echo "Dashboard stopped"
            rm -f "$DASHBOARD_PIDFILE"
        fi
        ;;
    restart)
        sudo systemctl restart mosquitto
        if [ -f "$DASHBOARD_PIDFILE" ]; then
            kill $(cat "$DASHBOARD_PIDFILE") 2>/dev/null
            rm -f "$DASHBOARD_PIDFILE"
        fi
        if [ -f "$DASHBOARD_DIR/package.json" ]; then
            cd "$DASHBOARD_DIR" && nohup npm start > /tmp/mqtt-dashboard.log 2>&1 &
            echo $! > "$DASHBOARD_PIDFILE"
            echo "Dashboard restarted (PID: $(cat $DASHBOARD_PIDFILE))"
        fi
        ;;
    status)
        sudo systemctl status mosquitto
        if [ -f "$DASHBOARD_PIDFILE" ] && kill -0 $(cat "$DASHBOARD_PIDFILE") 2>/dev/null; then
            echo "Dashboard is running (PID: $(cat $DASHBOARD_PIDFILE))"
        else
            echo "Dashboard is not running"
        fi
        ;;
    enable)
        sudo systemctl enable mosquitto
        ;;
    disable)
        sudo systemctl disable mosquitto
        ;;
    *)
        usage
        ;;
esac