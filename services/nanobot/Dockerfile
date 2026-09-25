FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base

# Node.js 20 for the WhatsApp bridge.
# tmux backs the skill of the same name, which drives interactive CLIs by
# sending keystrokes to a detached session and scraping the pane. Without the
# binary the skill still appeared in the model's list and was offered to the
# family, then failed at the first command.
# chromium renders a designed HTML page to PDF (`--print-to-pdf`, see
# skills/document/create_doc.py). A real browser rather than weasyprint because
# the Diseñador writes grid, flex and print-color-adjust, and the PDF has to be
# the page it designed — not a narrower engine's approximation of it. Debian's
# package pulls its own font set, which is also what stops the PDF rendering in
# fallback glyphs. Without it document generation still works; only the PDF
# companion is skipped, with a line saying so.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git tmux bubblewrap openssh-client chromium && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*


# ---------------------------------------------------------------------------
# Bridge builder — separate stage so `npm install` is keyed on bridge/package.json
# alone. In one stage it sat below `COPY nanobot/`, so every Python commit threw
# away the node_modules cache and reinstalled 74 packages for nothing.
# ---------------------------------------------------------------------------
FROM base AS bridge-build
WORKDIR /app
COPY bridge/package.json bridge/
RUN git config --global --add url."https://github.com/".insteadOf ssh://git@github.com/ && \
    git config --global --add url."https://github.com/".insteadOf git@github.com: && \
    cd bridge && npm install
COPY bridge/ bridge/
RUN cd bridge && npm run build


# ---------------------------------------------------------------------------
# Runtime. Layers are ordered rarest-change first so a normal deploy (Python
# source only) reuses everything above COPY nanobot/.
# ---------------------------------------------------------------------------
FROM base AS runtime

# Created before anything is copied, so it never needs a recursive chown after.
# /app stays root-owned and world-readable: nothing writes there at runtime (the
# workspace is /home/nanobot/.nanobot, a bind mount). The old
# `chown -R … /app` walked node_modules and cost ~150s a build.
RUN useradd -m -u 1000 -s /bin/bash nanobot && \
    mkdir -p /home/nanobot/.nanobot && \
    chown nanobot:nanobot /home/nanobot/.nanobot

WORKDIR /app

# Python dependencies, resolved from pyproject alone (cached until it changes).
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p nanobot bridge && touch nanobot/__init__.py && \
    uv pip install --system --no-cache . && \
    uv pip install --system --no-cache paho-mqtt smbprotocol pandas && \
    rm -rf nanobot bridge

# pi (pi.dev): the harness long background tasks run on (nanobot/harness/
# pi_runner.py). Pinned, and installed where nothing writes at runtime. Never
# phones home: PI_TELEMETRY=0 stops the install ping, PI_OFFLINE=1 the update
# checks -- nothing about this household leaves it through a harness.
# `npm ci` from the lockfile in nanobot/harness/pi, so pi's own dependencies
# (pi-ai, pi-agent-core, which it asks for as ^ranges) are pinned too.
ENV PI_HOME=/opt/pi PI_TELEMETRY=0 PI_OFFLINE=1
COPY nanobot/harness/pi/package.json nanobot/harness/pi/package-lock.json /opt/pi/
RUN mkdir -p /opt/pi/ext && cd /opt/pi && \
    npm ci --omit=dev --no-audit --no-fund --no-update-notifier && \
    npm cache clean --force && \
    /opt/pi/node_modules/.bin/pi --version

# Bridge SOURCE only, before the wheel build: pyproject force-includes `bridge`
# into the wheel as nanobot/bridge, and node_modules must not end up in there
# (.gitignore doesn't cover bridge/node_modules, so nothing else would stop it).
# The built artefacts land further down, after the install.
COPY bridge/ bridge/

# Python source: what changes on every commit, so it sits as low as possible.
COPY nanobot/ nanobot/
RUN uv pip install --system --no-cache .
# The harness's tools, beside pi's own node_modules so its imports resolve.
COPY nanobot/harness/pi/alfred.ts /opt/pi/ext/alfred.ts

# The model benchmark (bench/model_bench.py). In the image rather than copied
# in per run, so the admin page's Models > Benchmark can start it with one
# `docker exec` and it always matches the nanobot it measures. Below the
# install, so editing a case does not rebuild the wheel.
COPY bench/ bench/

# Built bridge from the builder stage. After the wheel build on purpose.
COPY --from=bridge-build /app/bridge/node_modules bridge/node_modules
COPY --from=bridge-build /app/bridge/dist bridge/dist

# No webui build: nanobot's own web UI is unused here — the family reaches
# Alfred through HomeCore's chat page and HomeCore never links :876N. Building it
# cost ~47s a deploy (409 npm packages plus a vite build). ChannelManager's
# _default_webui_dist() returns None when nanobot/web/dist is absent, so the
# gateway simply doesn't serve it. To bring it back, add a second builder stage
# mirroring bridge-build and copy nanobot/web/dist out of it.

# The same file the containers get on their /nanobot-config mount.
# It used to be a second config at the repo root, and the two drifted: the
# mounted one moved to deepseek while the baked one still said qwen on Together,
# had no websocket token (the handshake gate reads as off when it is empty) and
# a stale SSRF whitelist. Nothing announced the difference — it only showed if
# the mount was missing, which is exactly when nobody is reading logs.
COPY --chown=nanobot:nanobot config/config.json /etc/nanobot/config.json

COPY entrypoint.sh /usr/local/bin/
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && \
    chmod +x /usr/local/bin/entrypoint.sh

USER nanobot
ENV HOME=/home/nanobot
ENV PYTHONPATH=/app

# Gateway default port
EXPOSE 18790

ENTRYPOINT ["entrypoint.sh"]
CMD ["status"]
