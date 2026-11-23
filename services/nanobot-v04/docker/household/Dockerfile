# household trial image: smart-home-bot's services/nanobot/Dockerfile, rebuilt on
# upstream v0.4.1 plus nanobot/household. Build context is the trial tree root:
#
#   docker build -f docker/household/Dockerfile -t nanobot-trial:local .
#
# Differences from the fork's image, each deliberate:
# - deps from pyproject (`uv pip install -r pyproject.toml`, upstream's pattern),
#   then the package with --no-deps; the WebUI is not built
#   (NANOBOT_SKIP_WEBUI_BUILD=1; gateway.serveWebui defaults off);
# - NANOBOT_HOUSEHOLD=1, because channel classes load before the gateway binds
#   the household loop (the WhatsApp bridge override and the mqtt plugin need it);
# - pi's lockfile and extension come from nanobot/household/harness/pi;
# - no bench/ and no baked /etc/nanobot/config.json: both belong to the deploy
#   layer (smart-home-bot), not to this tree.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git tmux bubblewrap openssh-client chromium && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*


# The Baileys bridge, keyed on its package.json alone.
FROM base AS bridge-build
WORKDIR /app
COPY bridge/package.json bridge/
RUN git config --global --add url."https://github.com/".insteadOf ssh://git@github.com/ && \
    git config --global --add url."https://github.com/".insteadOf git@github.com: && \
    cd bridge && npm install
COPY bridge/ bridge/
RUN cd bridge && npm run build


FROM base AS runtime

RUN useradd -m -u 1000 -s /bin/bash nanobot && \
    mkdir -p /home/nanobot/.nanobot && \
    chown nanobot:nanobot /home/nanobot/.nanobot

WORKDIR /app

# Python dependencies from pyproject alone (cached until it changes), plus the
# household's extras: paho-mqtt (mqtt channel), smbprotocol (file share),
# pandas (documents), brotli >= 1.2.1 (CVE floor the fork states),
# websocket-client (the home-assistant skill's device registry) and reportlab
# (the document skill's fallback): fork dependencies v0.4.1 dropped.
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml && \
    uv pip install --system --no-cache paho-mqtt smbprotocol pandas "brotli>=1.2.1" \
        "websocket-client>=1.9.0,<2.0.0" "reportlab>=4.0.0,<5.0.0"

# pi (pi.dev) for the harness, pinned by its lockfile; never phones home.
ENV PI_HOME=/opt/pi PI_TELEMETRY=0 PI_OFFLINE=1
COPY nanobot/household/harness/pi/package.json nanobot/household/harness/pi/package-lock.json /opt/pi/
RUN mkdir -p /opt/pi/ext && cd /opt/pi && \
    npm ci --omit=dev --no-audit --no-fund --no-update-notifier && \
    npm cache clean --force && \
    /opt/pi/node_modules/.bin/pi --version

# Python source: what changes on every commit.
COPY README.md LICENSE THIRD_PARTY_NOTICES.md hatch_build.py ./
COPY docs/ docs/
COPY nanobot/ nanobot/
RUN NANOBOT_SKIP_WEBUI_BUILD=1 uv pip install --system --no-cache --no-deps .
COPY nanobot/household/harness/pi/alfred.ts /opt/pi/ext/alfred.ts
# The token encoding upstream otherwise downloads from the internet at every
# start (and falls back to byte estimates without it): baked in, read-only.
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
RUN python3 -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" && chmod -R a+rX /opt/tiktoken

# The built bridge, where the whatsapp-bridge-<member> service runs it
# (working_dir /app/bridge, entrypoint node, command dist/index.js).
COPY bridge/package.json bridge/
COPY --from=bridge-build /app/bridge/node_modules bridge/node_modules
COPY --from=bridge-build /app/bridge/dist bridge/dist

COPY docker/household/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && \
    chmod +x /usr/local/bin/entrypoint.sh && \
    printf '#!/bin/sh\nexec env PYTHONPATH=/app python3 -m nanobot.household.md2pdf "$@"\n' > /usr/local/bin/md2pdf && \
    chmod +x /usr/local/bin/md2pdf

USER nanobot
ENV HOME=/home/nanobot PYTHONPATH=/app NANOBOT_HOUSEHOLD=1 PYTHONUNBUFFERED=1

EXPOSE 18790

ENTRYPOINT ["entrypoint.sh"]
CMD []
