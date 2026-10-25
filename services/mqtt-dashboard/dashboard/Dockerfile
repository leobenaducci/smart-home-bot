# Pinned to the patch, not `node:22-alpine`: a build reuses whatever image the
# floating tag last pulled, so only an exact tag guarantees the fix. 22.23.4
# carries the fix for CVE-2026-111497 (Brotli decoder); Node 20 is end of life
# and never got one.
FROM node:22.23.4-alpine

WORKDIR /app

COPY package.json ./
RUN npm install --production

COPY server.js ./
COPY public/ ./public/

# Broker host + port, matching the rest of the stack. This was
# mqtt://host.docker.internal:1883 — a fourth name for the same machine that
# only resolves under Docker Desktop, not on the Pi.
ENV MQTT_BROKER=mqtt.home
ENV MQTT_PORT=1883
ENV PORT=21050

# Bridge definitions live here; docker-compose.yml bind-mounts it.
ENV DATA_DIR=/app/data
RUN mkdir -p /app/data

EXPOSE 21050

CMD ["node", "server.js"]
