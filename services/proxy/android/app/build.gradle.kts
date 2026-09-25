plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.chat.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.chat.app"
        minSdk = 26
        targetSdk = 34
        // CI (android/Jenkinsfile) injects an automatic timestamp version via
        // -PversionName=YYYYMMDD.HHMM -PversionCode=<minutes-since-2020>.
        // The literals below are the fallback for local/manual builds.
        versionCode = (project.findProperty("versionCode") as String?)?.toInt() ?: 2
        versionName = (project.findProperty("versionName") as String?) ?: "2.0"

        // What the household calls its assistant, from `site.assistant_name`.
        // Build with -PassistantName=Jarvis and the launcher icon, the
        // notification channels and every sentence the app says use it.
        //
        // A build property rather than a source rewrite, because that is how
        // Android does it: the manifest cannot read a runtime value for the
        // launcher label, and the deployer does not build this app, so it has
        // no business rewriting Kotlin it never compiles.
        //
        // Log tags, class names and the theme keep `Alfred` deliberately --
        // they are identifiers, like `alfred-nanobot` and `alfred/documents`
        // in the package. Renaming those orphans logs and breaks intents for
        // nothing anybody sees.
        val assistantName = (project.findProperty("assistantName") as String?) ?: "Alfred"
        manifestPlaceholders["assistantName"] = assistantName
        resValue("string", "assistant_name", assistantName)

        // Where this app talks to the house, from `cloud.vps.domain`. Build with
        // -PchatBaseUrl=https://chat.example.com.
        //
        // A build property for the same reason the name above is one, plus a
        // sharper one: this address was a literal in eight files, and
        // `deploy/sanitize.py` rewrites it to `chat.home` when the package
        // is extracted -- correctly, it is household data. So an APK built from
        // this repository pointed at a name that resolves nowhere, and the app
        // failed on its first request with "https://chat.home/chat?new=1".
        // The old CI built from an unsanitised checkout and never met this.
        //
        // The default stays the sanitised name: a package that ships somebody
        // else's domain is the thing the sanitiser exists to prevent.
        // No fallback name. The comment above is the record of what one costs:
        // the packaged default was a household's own name, the sanitizer
        // rewrote it to a reserved suffix, and the APK shipped pointing at an
        // address that exists nowhere -- discovered by a person holding the
        // phone. A build that cannot be told where the chat lives should stop,
        // not produce an installable that fails on its first request.
        val chatBaseUrl = (project.findProperty("chatBaseUrl") as String?)
            ?: throw GradleException(
                "chatBaseUrl is not set. Pass -PchatBaseUrl=https://<your chat host>, " +
                "or build through deploy/alfred-app/build-apk.sh with CHAT_BASE_URL set."
            )
        buildConfigField("String", "CHAT_BASE_URL", "\"${chatBaseUrl.trimEnd('/')}\"")
        buildConfigField("String", "CHAT_HOST",
            "\"${chatBaseUrl.substringAfter("://").substringBefore("/")}\"")

        // The second host the WebView is allowed to keep: opencode's interface
        // for the Programmer space. It has to be a host of its own -- its
        // assets and its API are absolute from the origin root and `/api`
        // collides with the portal's -- and MainActivity hands every other host
        // to the browser, so without this the Programmer leaves the app.
        //
        // Optional, and empty by default. No fallback name for the same reason
        // chatBaseUrl has none: a packaged default is a household's own name,
        // and one that has been sanitised is an address that exists nowhere.
        // Empty simply means the app keeps one host, as it always did.
        val codeBaseUrl = (project.findProperty("codeBaseUrl") as String?) ?: ""
        val codeHost = if (codeBaseUrl.isBlank()) ""
                       else codeBaseUrl.substringAfter("://").substringBefore("/")
        buildConfigField("String", "CODE_HOST", "\"$codeHost\"")

        // And the same host into the network security config, which needs it as
        // a literal: a resource file takes no manifest placeholders. Written
        // here rather than checked in, because a checked-in copy would carry a
        // household's own name -- the thing `deploy/sanitize.py` exists to
        // prevent -- and a sanitised one would name a host that resolves
        // nowhere.
        //
        // The chat's certificate is public and is left to the system store.
        // This adds the family's own CA for the code host alone, because that
        // name is served with it: Android 7 and later ignore a user-installed
        // CA unless an app says otherwise, and the symptom when it does not is
        // a WebView that shows nothing and explains nothing.
        // Into `src/main/res/xml/`, NOT under `build/`. This is written during
        // configuration, and the build runs `clean assembleRelease` -- so a file
        // under build/ is written, deleted by clean, and then missing when
        // resources merge. The failure is a puzzle: the manifest references
        // @xml/network_security_config, the file was demonstrably created, and
        // AAPT says the resource does not exist. `src/main/res` survives clean
        // and needs no source-set registration, because it already is one.
        //
        // Generated rather than committed for the reason `chatBaseUrl` has no
        // default: it names the household's own host. It is gitignored.
        val nsc = project.file("src/main/res/xml/network_security_config.xml")
        nsc.parentFile.mkdirs()
        nsc.writeText(
            """<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <base-config cleartextTrafficPermitted="false">
        <trust-anchors><certificates src="system" /></trust-anchors>
    </base-config>""" +
            (if (codeHost.isBlank()) "" else """
    <domain-config cleartextTrafficPermitted="false">
        <domain includeSubdomains="true">$codeHost</domain>
        <trust-anchors>
            <certificates src="system" />
            <certificates src="user" />
        </trust-anchors>
    </domain-config>""") +
            "\n</network-security-config>\n"
        )
    }

    buildFeatures {
        // For CHAT_BASE_URL above. AGP 8 does not generate BuildConfig unless
        // asked.
        buildConfig = true
    }

    signingConfigs {
        create("debugKeystore") {
            storeFile = file(System.getenv("ANDROID_KEYSTORE") ?: (System.getProperty("user.home") + "/.android/debug.keystore"))
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs["debugKeystore"]
        }
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-ktx:1.9.0")
    implementation("androidx.fragment:fragment-ktx:1.7.1")
    implementation("androidx.biometric:biometric:1.1.0")
    // WebSocket client for the ntfy notification foreground service.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // On-device geofencing (arrival reminders). Requires Google Play Services
    // on the device at runtime.
    implementation("com.google.android.gms:play-services-location:21.3.0")
    // Hourly location heartbeat (geo/LocationPingWorker).
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    // Plain JVM tests — no device, no emulator. For decisions that can be
    // pulled out of the Android types and run on their own; see LockPolicy,
    // which is when Alfred asks for a fingerprint again.
    //   ./gradlew :app:testReleaseUnitTest
    testImplementation("junit:junit:4.13.2")
}
