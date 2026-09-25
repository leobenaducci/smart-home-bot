# Alfred's household capabilities, as an MCP server for opencode.
#
# Small on purpose. It holds one person's credentials and proxies four things
# that already exist; everything it could plausibly grow into -- rendering a
# PDF, running git, resolving who somebody is -- belongs to a service that
# already does it, and pulling any of that in here would mean a second copy of
# a decision somebody else owns.
FROM python:3.12-slim

# No build toolchain: every dependency here is a pure-python wheel. Adding one
# that is not would be worth noticing rather than quietly compiling.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alfred_mcp/ ./alfred_mcp/

# Not root. It reads one mounted checkout tree and talks to two HTTP services;
# nothing here needs to own a file. The uid is fixed rather than assigned so
# the read-only mount's permissions are predictable across rebuilds.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin alfred
USER alfred

EXPOSE 21071

# `python -m` rather than a console script: the package is copied in rather
# than installed, so there is no entry point to generate.
ENTRYPOINT ["python", "-m", "alfred_mcp"]
