## Build & Deploy

> **Normally you don't do any of this by hand — Jenkins does.** Two jobs, and
> the branch is the only difference that matters:
>
> | Job | Jenkinsfile | Branch | Who gets it |
> |---|---|---|---|
> | stable | `android/Jenkinsfile` | `main` | everyone, pushed over ntfy |
> | beta | `android/Jenkinsfile.beta` | **`beta`** | only phones with Debug → "Beta updates", in-app badge only |
>
> Both version themselves from the build clock, so **step 0 below does not
> apply to them** — no manual bump. Merge into `beta` to put something on one
> phone; merge into `main` once it's been watched working. A stable build always
> has a later timestamp, so beta testers converge back on it automatically.
>
> The steps below are the manual fallback, for when Jenkins is unavailable.

> **Before pushing Kotlin from a machine with no SDK**, run the lexer check:
>
> ```bash
> python android/tools/kt_lex.py android/app/src/main/java
> ```
>
> It catches the structural mistakes that otherwise cost a full Jenkins round
> trip to discover — chiefly **nested block comments**, which Kotlin supports and
> Java does not: a `/*` inside a KDoc opens a second comment, and the KDoc's
> closing delimiter then only closes the inner one, silently swallowing whatever
> follows. That has broken the beta build once. It is a lexer, not a compiler —
> it says nothing about types.

When asked to compile or build the Android app, always do all steps:

0. **Bump the version in `android/app/build.gradle.kts`** (required every release
   — the in-app update badge only appears when `versionCode` strictly increases):
   ```kotlin
   versionCode = 3        // MUST strictly increase
   versionName = "2.1"    // shown on the update badge
   ```

1. **Clean up the repo — stash any local changes so the working tree is clean:**
   ```
   git stash
   ```

2. **Pull latest:**
   ```
   git pull
   ```

3. **Restore local changes (signing config, deps):**
   ```
   git stash pop
   ```

4. **Resolve any conflicts that arise from the pop.**

5. **Full clean rebuild:**
   ```
   cd android && ./gradlew clean assembleRelease
   ```

6. **Publish for the in-app updater (hidden `alfred-app` folder).**
   The app shows an "⬆ actualizar" badge on the chat header when a newer APK is
   here (VPN/LAN, via HomeCore `/chat/app-version` + `/chat/app-download`). This
   folder is a **normal folder hidden from the Files UI** at the app layer
   (HomeCore `FILES_HIDDEN` = `alfred-app`) — do NOT use a leading-dot name, and
   keep the name in sync with `APK_DEPLOY_FOLDER` in `HomeCore/local/app.py`.

   Using the `versionName`/`versionCode` you set in step 0 (e.g. `2.1` / `3`):
   ```
   ssh homestack@compute.home "mkdir -p /mnt/data/share/alfred-app"
   # Copy the APK named alfred-<versionName>-<versionCode>.apk:
   scp /home/homestack/home-chat/android/app/build/outputs/apk/release/app-release.apk \
       homestack@compute.home:/mnt/data/share/alfred-app/alfred-2.1-3.apk
   # Write the manifest the badge reads (values MUST match build.gradle.kts and the filename above):
   ssh homestack@compute.home "cat > /mnt/data/share/alfred-app/latest.json" <<'JSON'
   {"versionCode": 3, "versionName": "2.1", "file": "alfred-2.1-3.apk"}
   JSON
   ```
   Only `latest.json`'s `file` is served (as a bare filename in this folder);
   older APKs can be left or cleaned up. Verify with
   `GET https://chat.home/chat/app-version` → it should report the new
   `versionCode`/`versionName`.

   (The ntfy `alfred_update` self-update in `docs/proxy-notifications.md` is an
   independent path and still works.)
