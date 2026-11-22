# Dispositivos / Devices: one list of every device in the house, where it is,
# and how to reach it. See README.md.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# The suite runs against this image before it replaces the running container
# (`test:` in the manifest), so its runner is in it. ~3 MB.
RUN pip install --no-cache-dir 'pytest>=8,<9'

COPY home_devices/ ./home_devices/
# The shared translation catalogues, staged by the deployer (`assets:`).
COPY i18n/ ./home_devices/static/i18n/

# CMD, not ENTRYPOINT: the pre-deploy test runs this image with its own command.
CMD ["python", "-m", "home_devices"]
