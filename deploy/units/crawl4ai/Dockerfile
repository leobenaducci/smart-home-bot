# The upstream image, plus a patched Brotli and nothing else.
#
# crawl4ai fetches any URL it is handed, and its HTTP clients (aiohttp, httpx)
# decode `Content-Encoding: br` with the Python `brotli` package. 0.9.3 ships
# brotli 1.2.0, which has CVE-2026-111497 -- an out-of-bounds write in the
# decoder that a hostile server reaches with one crafted response. 1.2.1 is the
# fix. Drop this layer once an upstream release ships 1.2.1 itself.
ARG CRAWL4AI_BASE=unclecode/crawl4ai:0.9.3
FROM ${CRAWL4AI_BASE}
USER root
RUN pip install --no-cache-dir "brotli>=1.2.1" && \
    python -c "import brotli, sys; v = tuple(map(int, brotli.__version__.split('.')[:3])); sys.exit(0 if v >= (1, 2, 1) else 1)"
USER appuser
