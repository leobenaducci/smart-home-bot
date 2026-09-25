# browser-use, with the browser it drives.
#
# Big on purpose and nowhere else: this ships Chromium, which is most of the
# ~1.5 GB. That is exactly why it is a service of its own and off by default,
# rather than a dependency of the assistants -- the same argument the qwen-tts
# sidecar makes about torch, one floor along.
#
# The Playwright base image rather than python:slim + a browser install: it
# pins the browser to the driver that was tested against it. Installing
# Chromium separately is how you get a driver that launches a browser it cannot
# talk to, and the error for that is a timeout rather than a version mismatch.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Chromium in a container has no shared memory to speak of and crashes on
    # pages that want it. The alternative is --disable-dev-shm-usage, which
    # trades the crash for swapping to disk; this says the real size instead.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Not root. The Playwright image ships `pwuser` for this, and a browser that
# renders whatever a page serves it is the last process here that should be
# running as root.
USER pwuser

EXPOSE 21034
CMD ["python", "server.py"]
