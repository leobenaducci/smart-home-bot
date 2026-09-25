# The Alfred APK, built here rather than on a Jenkins agent.
#
# The build used to live in two Jenkins jobs against a separate repo, on the one
# machine with an Android SDK installed by hand. That made "release a new app"
# something only that machine could do, and the pipeline's own history is what it
# cost: the jobs are the only reason that agent needed an SDK at all.
#
# Everything the build needs is in this repository. The release is signed with
# the debug keystore committed beside the source -- deliberate for a household
# app that never sees an app store -- so this image needs no credential to build,
# only one to publish.
#
# It is large: the SDK platform and build-tools are ~2.5 GB before Gradle's own
# caches. That is the price the chosen answer carries, and it is paid once --
# `alfred-app-builder:latest` is reused, and the Gradle cache lives in a named
# volume so a second build does not re-download the world.
FROM eclipse-temurin:17-jdk

# smbclient is the publish half: the share is exported over SMB and this stack
# already owns a credential for it. The alternative was ssh to the machine
# holding it, which would have meant authorising a new key on a host this stack
# does not otherwise deploy to -- a bigger change than the feature.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl unzip git ca-certificates smbclient \
    && rm -rf /var/lib/apt/lists/*

ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV PATH="${PATH}:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools"

# Pinned. `latest` here means "whatever Google published this morning", and an
# SDK that moves under a build is the kind of failure that looks like the app.
ARG CMDLINE_TOOLS=11076708
RUN mkdir -p "${ANDROID_HOME}/cmdline-tools" \
    && curl -fsSL -o /tmp/tools.zip \
       "https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS}_latest.zip" \
    && unzip -q /tmp/tools.zip -d "${ANDROID_HOME}/cmdline-tools" \
    && mv "${ANDROID_HOME}/cmdline-tools/cmdline-tools" "${ANDROID_HOME}/cmdline-tools/latest" \
    && rm /tmp/tools.zip

# Matching app/build.gradle.kts: compileSdk 34, and the build-tools that go with
# it. Adding a platform here is cheaper than discovering it missing three
# minutes into a build.
RUN yes | sdkmanager --licenses > /dev/null \
    && sdkmanager --install "platform-tools" "platforms;android-34" "build-tools;34.0.0" > /dev/null

# Gradle's caches, so the second build is minutes rather than the first one
# again. Mounted as a volume by whoever runs this.
ENV GRADLE_USER_HOME=/gradle

COPY build-apk.sh /usr/local/bin/build-apk.sh
RUN chmod +x /usr/local/bin/build-apk.sh

WORKDIR /src
ENTRYPOINT ["/usr/local/bin/build-apk.sh"]
