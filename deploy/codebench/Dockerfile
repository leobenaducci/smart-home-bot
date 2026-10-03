# The coding benchmark's sandbox: one container per attempt at one problem.
#
# The model drives opencode, and opencode runs whatever shell commands the
# model writes. Here, that is a throwaway container holding the toolchain, the
# problems and nothing of the house's: no host directory is mounted, it runs as
# an unprivileged user, and it is removed when the attempt ends. (bubblewrap
# would have been lighter; this host restricts unprivileged user namespaces.)
#
# opencode is pinned to the release the Programmer runs, so a model is measured
# in the harness it would really work in.
#
# The build context is staged by admin/codebench.py, not this directory: it
# holds each problem's starting files and nothing else. The hidden tests and
# the reference solutions never enter the image -- the model can run `find /`
# -- and the hidden tests arrive on stdin, which solve.py reads before opencode
# starts.
FROM python:3.12-slim

ARG OPENCODE_VERSION=1.18.30

RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ make git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pytest==8.4.2 \
 && curl -fsSL -o /tmp/opencode.tar.gz \
      "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" \
 && tar -xzf /tmp/opencode.tar.gz -C /usr/local/bin opencode \
 && rm /tmp/opencode.tar.gz \
 && useradd -m -u 1000 bench \
 && install -d -o bench -g bench /home/bench/.cache

COPY solve.py /bench/solve.py
COPY problems /bench/problems

USER bench
WORKDIR /home/bench
ENTRYPOINT ["python3", "/bench/solve.py"]
