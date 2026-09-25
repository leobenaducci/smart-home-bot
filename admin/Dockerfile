FROM python:3.12-slim

# The page runs the deployer, which drives compose and then proves each service
# answers -- locally through the mounted socket, or over ssh. It needs the same
# tools deploy.py does, on this side.
#
# `docker-cli`, not `docker.io`. On Debian trixie that package is the *daemon*:
# it installs a runtime we never use and leaves `/usr/bin/docker` absent, so
# every compose call failed with `docker: command not found` after the compose
# file had already been written. The client is the only half that belongs in
# here -- the daemon is the host's, reached through the socket.
#
# `docker-compose` for the `docker compose` subcommand. The name is the old
# one and the contents are not: on trixie that package is Compose v2.26 and it
# installs the CLI plugin at /usr/libexec/docker/cli-plugins/docker-compose,
# which is what makes `docker compose` a subcommand rather than a 404. The
# name `docker-compose-v2` does not exist there, and asking for it fails the
# whole build.
#
# `mosquitto-clients` because a verify check runs on the *target*, and on a
# one-machine install the target is this container. `mqtt_roundtrip` publishes
# with mosquitto_pub; without it that check spends its whole timeout collecting
# exit 127.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        openssh-client rsync curl ca-certificates \
        docker-cli docker-compose mosquitto-clients \
 && rm -rf /var/lib/apt/lists/*

# An account for the uid this container runs as.
#
# `user:` in the compose file is the deploying account's numeric id, so that
# what this writes is owned the way a deploy from a shell owns it. Without a
# matching /etc/passwd entry that id has no *name*, and every OpenSSH tool
# refuses outright: `ssh`, `ssh-keygen` and `ssh-keyscan` all call getpwuid()
# before anything else and exit 255 with "No user exists for uid 1000".
#
# So deploying to a second machine from the admin page could not work, and had
# never worked -- the failure is one line, in a deploy log, from a tool nobody
# expects to need an identity.
#
# Passed at build time because the deployer builds this image on the machine it
# will run on and already knows the number. `|| true` on both: a base image
# that happens to have that id is fine, it is the *name* that has to exist.
ARG DEPLOY_UID=1000
ARG DEPLOY_GID=1000
RUN groupadd -g "${DEPLOY_GID}" deployer 2>/dev/null || true;     useradd -u "${DEPLOY_UID}" -g "${DEPLOY_GID}" -M -d /deployer             -s /usr/sbin/nologin deployer 2>/dev/null || true;     getent passwd "${DEPLOY_UID}" >/dev/null

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The whole unit, including i18n/, deploy/ and config/ which the deployer stages
# into this build context (see `assets:` in deploy/manifest.yml).
COPY . /app/admin

# Two directories the deployer *writes* while deploying, and it runs here as
# `deployer`, not as root. `generated_compose:` renders
# `admin/docker-compose.plugins.yml` and
# `services/nanobot/docker-compose.members.yml` from the member list, and this
# tree is COPYed as root -- so a deploy started from the page died with
# "PermissionError: [Errno 13] ... docker-compose.members.yml" the first time a
# member changed. From a shell it worked, because that writes the real
# repository instead, which is why this survived so long.
#
# Only these two, not `chown -R /app/admin`: the rest is the code this container
# runs, and an app that can rewrite its own source is a different thing to
# reason about.
# Not `|| true`: a chown that silently does nothing brings the bug back with no
# sign of it. Absent is tolerated -- what is staged here depends on `assets:` --
# a failed chown on something present is not.
# The two files `generated_compose:` renders, made writable by the user this
# runs as. It runs as `deployer`, the tree is COPYed as root, and
# `compose_*.write()` calls `write_text` on the path -- so a deploy started from
# the page died with "PermissionError: [Errno 13] ...
# docker-compose.members.yml" the first time a member changed. From a shell it
# worked, because that writes the real repository instead, which is why this
# went unnoticed.
#
# Created here rather than only chowned: both are gitignored, so on a clean
# checkout neither is in the build context, and chowning what is absent would
# leave the bug exactly where it was. Empty is fine -- the deployer renders them
# before compose reads them, and writes whenever the content differs.
#
# The files, not their directories: making /app/admin writable would let this
# container replace its own source, which is a different thing to reason about
# and buys nothing here.
RUN for f in /app/admin/docker-compose.plugins.yml \
             /app/admin/services/nanobot/docker-compose.members.yml; do \
        mkdir -p "$(dirname "$f")" && touch "$f" && chown deployer:deployer "$f"; \
    done

ENV HOME_STACK_ROOT=/app/admin
ENV PYTHONUNBUFFERED=1

# A home directory for whoever this runs as, which is not root: the compose
# file sets `user:` to the host account that owns the state, so that a deploy
# from the page and a deploy from a shell leave identically owned files.
#
# /root cannot serve. It is mode 700 in the base image, so the moment the
# container stops being root, ssh cannot read the keys mounted under it and
# every remote deploy fails on a key it can see the name of.
#
# These were 0777, on the reasoning that the uid is the household's and is not
# known at build time. It is known -- `useradd -u ${DEPLOY_UID}` above is given
# it -- so this is simply that account's home directory, owned by it, 0700.
# There is no second group and no `group_add` for one: a fixed gid here
# collides with the name this image already has, and `groupadd` fails the build.
#
# **It has to be writable, not merely traversable.** An earlier version of this
# made it `root:deployer 0750`, which lets the account in and not write, and
# `$HOME` is where the docker CLI puts `.docker/config.json`. Every deploy
# launched from the page then died on `mkdir /deployer/.docker: permission
# denied` -- after the build, after the certificate check, at the compose call.
# Nothing else on the page fails that way, so it reads like docker being broken
# rather than a mode on a directory.
ENV HOME=/deployer
RUN mkdir -p /deployer/.ssh && \
    chown -R deployer:deployer /deployer && chmod 0700 /deployer && \
    chmod 0700 /deployer/.ssh

# /state has to be writable by whoever this runs as, and that is not root any
# more. The config and the credentials are single-file bind mounts *into* this
# directory, and save_config writes `home-stack.yml.tmp` beside them before
# renaming -- a serialise-then-swap, so a crash mid-write cannot leave the
# stack with a half-parsed config. The temporary needs the directory, not the
# file. Without this every Save on the page was a 500: the members screen, the
# site settings, the service toggles, the model choices, all of them.
RUN mkdir -p /state /cache && \
    chown root:deployer /state /cache && chmod 0770 /state /cache

EXPOSE 8099
CMD ["python", "/app/admin/app.py"]
