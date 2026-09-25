## Build & Deploy

When asked to compile or build the Android app, always do all steps:

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
   Needs `local.properties` with `sdk.dir=/opt/android-sdk` (gitignored, so a
   fresh clone has none) and network on the first run, to fetch the deps.

   Before that, and worth it because they answer in seconds rather than
   minutes:
   ```
   python tools/kt_lex.py app/src        # braces, comments, backticks
   python tools/kt_lex.py --self-test    # ...and that the checker is right
   ./gradlew :app:testReleaseUnitTest    # plain JVM tests, no device
   ```

6. **Copy the APK to the local shared folder (for Jenkins/ntfy publishing):**
   ```
   mkdir -p /opt/alfred && cp android/app/build/outputs/apk/release/app-release.apk /opt/alfred/
   ```

7. **Backup the previous APK on the family share (keep 1 backup):**
   ```
   ssh homestack@compute.home "cp /mnt/data/share/familia/app-release.apk /mnt/data/share/familia/app-release.apk.bak"
   ```
   (If the file doesn't exist yet, this will fail harmlessly — skip it.)

8. **Copy the new APK to the family share:**
   ```
   scp /home/homestack/home-chat/android/app/build/outputs/apk/release/app-release.apk homestack@compute.home:/mnt/data/share/familia/
   ```
