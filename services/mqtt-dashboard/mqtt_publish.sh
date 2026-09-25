#!/bin/bash

# Mosquitto topic publisher
# Usage: ./mqtt_publish.sh <topic> <message> [host]

HOST="${3:-localhost}"
TOPIC="${1:-test/topic}"
MESSAGE="${2:-hello}"

export LD_LIBRARY_PATH=/usr/local/lib

mosquitto_pub -h "$HOST" -t "$TOPIC" -m "$MESSAGE"
