#!/usr/bin/env bash
# build-apk.sh — build the Alfred APK and put it where the family can install it.
#
#     build-apk.sh release
#     build-apk.sh beta
#
# Both run `assembleRelease`. The difference is only what the result is called
# and which manifest it updates -- the app has no beta flavour, no separate
# application id, and the two Jenkins jobs this replaces differed the same way.
# Calling that out because "beta" reads like a different build and is not one:
# installing a beta replaces the release on the phone.
#
# Signed with a debug-style keystore the household keeps in its config directory
# (see below): a household app that never sees an app store needs no more, and
# the build needs no credential beyond it -- only one to publish.
#
# Publishing is over SMB, to the share the family already has. It writes:
#
#   alfred-app/<name>.apk      the archive, every build ever
#   alfred-app/latest.json     what the in-app updater reads (latest-beta.json)
#   familia/<name>.apk         the copy people actually find, newest only
#
# The last one is the point of doing this here: an APK in `alfred-app/` is on the
# share but not in the Files view, which lists member folders. `familia/` is the
# folder everybody has.
set -euo pipefail

CHANNEL="${1:-release}"
case "$CHANNEL" in
    release|beta) ;;
    *) echo "usage: $0 [release|beta]" >&2; exit 2 ;;
esac

SRC="${SRC_DIR:-/src}"
cd "$SRC"

# A version a person can read back to a date, and a code that only ever climbs.
# The Jenkins jobs used the same shape, and the updater compares versionCode --
# so it has to be monotonic or a newer build looks older and is never offered.
VERSION_NAME="${VERSION_NAME:-$(date +%Y%m%d.%H%M)}"
VERSION_CODE="${VERSION_CODE:-$(( $(date +%s) / 100 - 14000000 ))}"

echo "==> building Alfred ${VERSION_NAME} (${VERSION_CODE}), channel ${CHANNEL}"

# `local.properties` names an SDK path from whatever machine wrote it last, and
# the one in the source tree points at the old agent. The environment is
# authoritative here, so this removes the file rather than letting a stale path
# win -- the tree is a copy, mounted read-write only for the build outputs.
rm -f local.properties

# Named rather than defaulted. This is set by the builder image; if it is not,
# the run is not in that image, and an empty `sdk.dir` fails several minutes
# later inside Gradle as something that reads like a broken project.
: "${ANDROID_HOME:?not set -- run this in the alfred-app builder image}"

# The signing key. app/build.gradle.kts reads ANDROID_KEYSTORE (store and key
# password "android", alias androiddebugkey). It is the household's own and is
# never in the repository: whoever holds it can sign an "update" every phone
# with the app installed will accept. It lives in the config directory, mounted
# here at /signing, and a house that has none gets one on its first build --
# after which it must be kept, or installed apps stop accepting updates.
if [ -z "${ANDROID_KEYSTORE:-}" ] && [ -d /signing ]; then
    if [ ! -f /signing/debug.keystore ]; then
        echo "==> no signing key yet: creating /signing/debug.keystore (keep it; back it up)"
        keytool -genkeypair -v -keystore /signing/debug.keystore -storepass android \
            -alias androiddebugkey -keypass android -keyalg RSA -keysize 2048 \
            -validity 10000 -dname "CN=Android Debug,O=Android,C=US" >/dev/null
    fi
    export ANDROID_KEYSTORE=/signing/debug.keystore
fi
: "${ANDROID_KEYSTORE:?no keystore: mount the config directory's alfred-app/ at /signing or set ANDROID_KEYSTORE}"
echo "==> signing with ${ANDROID_KEYSTORE}"

# Where the app will talk to the house. The source ships the sanitised name --
# `deploy/sanitize.py` rewrites the real one out when the package is extracted,
# correctly -- so an APK built without this points at `chat.home`, which
# resolves nowhere, and fails on its first request. The old CI built from an
# unsanitised checkout and never met this.
GRADLE_ARGS=""
if [ -n "${CHAT_BASE_URL:-}" ]; then
    echo "==> app will talk to ${CHAT_BASE_URL}"
    GRADLE_ARGS="-PchatBaseUrl=${CHAT_BASE_URL}"
else
    echo "CHAT_BASE_URL is not set. There is no packaged default any more:" >&2
    echo "an APK built without one pointed at a name no household has, and" >&2
    echo "that was found by somebody holding the phone. Set it to the chat" >&2
    echo "address from dns.chat and run this again." >&2
    exit 2
fi

# The second host the WebView is allowed to keep: opencode's interface for the
# Programmer space, from `dns.code`. Optional, and its absence is not an error
# -- a household that has not set `dns.code` has no such host, and the app then
# behaves exactly as it did before, keeping one host and handing every other to
# the browser.
#
# Worth knowing what it costs to leave out: the Programmer link opens, but in
# the phone's browser rather than in the app, because `shouldOverrideUrlLoading`
# only keeps a host it was told about.
if [ -n "${CODE_BASE_URL:-}" ]; then
    echo "==> the Programmer will stay in-app at ${CODE_BASE_URL}"
    GRADLE_ARGS="${GRADLE_ARGS} -PcodeBaseUrl=${CODE_BASE_URL}"
else
    echo "==> CODE_BASE_URL not set: the Programmer will open in the browser"
fi

./gradlew --no-daemon \
    -Pandroid.sdk.dir="${ANDROID_HOME}" \
    ${GRADLE_ARGS} \
    -PversionName="${VERSION_NAME}" \
    -PversionCode="${VERSION_CODE}" \
    clean assembleRelease

APK_IN="app/build/outputs/apk/release/app-release.apk"
if [ ! -f "$APK_IN" ]; then
    echo "ERROR: gradle reported success but $APK_IN is not there" >&2
    exit 1
fi

if [ "$CHANNEL" = "beta" ]; then
    APK_NAME="alfred-${VERSION_NAME}-beta-${VERSION_CODE}.apk"
    MANIFEST="latest-beta.json"
else
    APK_NAME="alfred-${VERSION_NAME}-${VERSION_CODE}.apk"
    MANIFEST="latest.json"
fi

size=$(stat -c %s "$APK_IN")
echo "==> built ${APK_NAME} ($(( size / 1024 / 1024 )) MB)"

# An APK that is a few hundred bytes is a build that "succeeded" and produced
# nothing usable. Cheap to check, and the alternative is finding out on a phone.
if [ "$size" -lt 1000000 ]; then
    echo "ERROR: that APK is ${size} bytes, which is not an app" >&2
    exit 1
fi

if [ -z "${SMB_HOST:-}" ] || [ -z "${SMB_SHARE:-}" ]; then
    echo "==> no share configured, leaving the APK in ${SRC}/${APK_IN}"
    echo "    (set SMB_HOST, SMB_SHARE, SMB_USERNAME, SMB_PASSWORD to publish)"
    exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cp "$APK_IN" "$work/${APK_NAME}"
printf '{"versionCode": %s, "versionName": "%s", "file": "%s"}\n' \
    "${VERSION_CODE}" "${VERSION_NAME}" "${APK_NAME}" > "$work/${MANIFEST}"

# An auth file, not `-U user%pass`: a credential in argv is visible in `ps` to
# anything else in this container, and this one opens the family's whole share.
# 0600 before the password is written to it, and it dies with the temp dir.
AUTH="$work/smb-auth"
( umask 077; printf 'username=%s\npassword=%s\n' \
    "${SMB_USERNAME:-share}" "${SMB_PASSWORD:-}" > "$AUTH" )

smb() {
    smbclient "//${SMB_HOST}/${SMB_SHARE}" -A "$AUTH" -c "$1" 2>&1
}

# smbclient exits 0 even when a command inside `-c` failed, so the transcript is
# the only evidence there is. A collision from `mkdir` is the directory already
# existing, which is the normal case.
smb_failed() {
    printf '%s' "$1" | grep -E "NT_STATUS" | grep -qv "OBJECT_NAME_COLLISION"
}

echo "==> publishing to //${SMB_HOST}/${SMB_SHARE}"
out="$(smb "mkdir alfred-app; cd alfred-app; put \"$work/${APK_NAME}\" ${APK_NAME}; put \"$work/${MANIFEST}\" ${MANIFEST}")" || true
if smb_failed "$out"; then
    echo "$out" >&2
    echo "ERROR: publishing to alfred-app/ failed" >&2
    exit 1
fi
echo "    alfred-app/${APK_NAME}"
echo "    alfred-app/${MANIFEST}"

# The family copy. Newest only: this folder is where somebody looks for "the
# app", and a list of thirty builds is a worse answer than one file. The old
# ones stay in alfred-app/, which is the archive.
FAMILY_DIR="${FAMILY_DIR:-familia}"
# The first field only, and only when it looks like one of ours. smbclient
# echoes the pattern back inside its own error line when nothing matches
# ("NT_STATUS_NO_SUCH_FILE listing \alfred-*.apk"), and a looser match took that
# for a filename and tried to delete it. This loop deletes things.
old="$(smb "cd ${FAMILY_DIR}; ls alfred-*.apk" \
       | awk '$0 !~ /NT_STATUS/ && $1 ~ /^alfred-.*\.apk$/ {print $1}')" || true
for f in $old; do
    # An `if`, not `[ ... ] && continue`: under `set -e` the failing test in a
    # && list is the kind of thing that either exits the script or does not,
    # depending on where it sits, and this loop deletes files.
    if [ "$f" != "$APK_NAME" ]; then
        echo "    removing the previous ${FAMILY_DIR}/${f}"
        smb "cd ${FAMILY_DIR}; del ${f}" >/dev/null || true
    fi
done
out="$(smb "cd ${FAMILY_DIR}; put \"$work/${APK_NAME}\" ${APK_NAME}")" || true
if smb_failed "$out"; then
    echo "$out" >&2
    echo "ERROR: could not put the APK in ${FAMILY_DIR}/" >&2
    exit 1
fi
echo "    ${FAMILY_DIR}/${APK_NAME}"

echo "==> done. ${APK_NAME} is in ${FAMILY_DIR}/ on the share."
