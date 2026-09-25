package com.chat.app

import android.Manifest
import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.graphics.Color
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.media.MediaRecorder
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.PowerManager
import android.os.SystemClock
import android.provider.MediaStore
import android.provider.Settings
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import android.util.Log
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import android.net.http.SslError
import android.webkit.ConsoleMessage
import android.webkit.JavascriptInterface
import android.view.Gravity
import android.view.View
import android.webkit.CookieManager
import android.webkit.MimeTypeMap
import android.webkit.URLUtil
import android.webkit.PermissionRequest
import android.webkit.SslErrorHandler
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_WEAK
import androidx.biometric.BiometricManager.Authenticators.DEVICE_CREDENTIAL
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.core.graphics.ColorUtils
import androidx.core.view.WindowInsetsControllerCompat
import androidx.core.content.FileProvider
import androidx.fragment.app.FragmentActivity
import com.chat.app.geo.GeofenceManager
import com.chat.app.geo.LocationPingWorker
import com.chat.app.notif.NotificationRelayService
import com.chat.app.ntfy.NtfyClientService
import java.io.File

/**
 * Alfred — a thin WebView wrapper around the home-chat cloud proxy
 * (https://chat.home), which fronts HomeCore's Alfred chat. Handles a
 * biometric/device-credential lock, cookie persistence (stay logged in),
 * microphone (voice/Whisper), image/camera uploads, in-app back navigation, and
 * keeps navigation locked to the domain.
 */
class MainActivity : FragmentActivity() {

    private lateinit var webView: WebView
    private lateinit var lockView: TextView
    private var loaded = false

    // The wall/plaster pair currently painted on the window, as the page sent
    // it — so the bridge can tell a real theme change from the same two colours
    // arriving again on every page load.
    @Volatile private var appliedWall: String? = null
    @Volatile private var appliedPlaster: String? = null

    // Biometric gate state. When the lock comes back on lives in LockPolicy,
    // which holds no Android types and is tested in LockPolicyTest; this flag
    // is only about the prompt UI, so that two of them never stack.
    //
    // `shared`, not a field of this Activity: Android recreates the Activity
    // on a whim and a fresh policy is a locked one, which put the fingerprint
    // back on every app switch even after the policy itself was fixed.
    private val lock get() = LockPolicy.shared(AWAY_RELOCK_MS)
    private var promptShowing = false

    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private var cameraImageUri: Uri? = null
    private var pendingPermissionRequest: PermissionRequest? = null

    // Native audio capture (fallback for WebView getUserMedia, which fails with
    // "Could not start audio source" on some devices even though Chrome works).
    private var recorder: MediaRecorder? = null
    private var audioFile: File? = null

    // Real-time location for Alfred (foreground only). The chat page reads this
    // via window.AndroidLocation.getLocation() and sends it with messages; see
    // chat.html. Kept fresh by LocationManager updates (100m displacement).
    private var lastLocation: Location? = null
    private var locationListener: LocationListener? = null

    // In-app APK update (window.AndroidApp.downloadAndInstall). When the chat
    // header badge is tapped, we download the APK from HomeCore (cookie-auth) and
    // launch the installer once it lands.
    private var updateDownloadId = -1L
    private val updateDownloadReceiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context, intent: Intent) {
            val id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1L)
            if (id == -1L || id != updateDownloadId) return
            updateDownloadId = -1L
            // Check the actual outcome — DownloadManager broadcasts completion on
            // failure too. Launching the installer blindly on a failed/partial
            // download is a silent no-op, which reads as "nothing happened".
            val (status, reason) = downloadStatus(id)
            if (status == DownloadManager.STATUS_SUCCESSFUL) {
                ApkInstaller.launchInstall(this@MainActivity)
            } else {
                Log.e("AlfredWeb", "update download failed: status=$status reason=$reason")
                Toast.makeText(
                    this@MainActivity,
                    "No se pudo descargar la actualización (error $reason)",
                    Toast.LENGTH_LONG,
                ).show()
            }
        }
    }

    /** Query DownloadManager for a finished download's (status, reason) pair. */
    private fun downloadStatus(id: Long): Pair<Int, Int> {
        val dm = getSystemService(DownloadManager::class.java) ?: return -1 to -1
        return runCatching {
            dm.query(DownloadManager.Query().setFilterById(id)).use { c ->
                if (!c.moveToFirst()) return -1 to -1
                val status = c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS))
                val reason = c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_REASON))
                status to reason
            }
        }.getOrDefault(-1 to -1)
    }

    /**
     * Replace the failed page with one that says what went wrong.
     *
     * The WebView's own answer to a cancelled load is a blank screen, so a
     * person taps something and watches nothing happen -- there is no error to
     * read, no retry, and no clue that the phone is simply on the wrong
     * network. This is deliberately a page and not a Toast: the navigation has
     * already left the chat behind, so something has to occupy the screen, and
     * a Toast that has faded is indistinguishable from the blank screen it was
     * meant to explain.
     *
     * Back returns to the chat, because this is an ordinary history entry.
     */
    private fun showBlockedPage(view: WebView, title: String, detail: String) {
        fun esc(s: String) = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        val html = """
            <!doctype html><meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <style>
              body { margin:0; min-height:100vh; display:flex; align-items:center;
                     justify-content:center; background:#F3EFE7; color:#2E2A25;
                     font:16px/1.5 -apple-system,Roboto,sans-serif; padding:24px; }
              main { max-width:22rem; }
              h1 { font-size:1.15rem; margin:0 0 .6rem; }
              p { margin:0; color:#5B534A; }
            </style>
            <main><h1>${esc(title)}</h1><p>${esc(detail)}</p></main>
        """.trimIndent()
        view.loadDataWithBaseURL(null, html, "text/html", "utf-8", null)
    }

    companion object {
        private const val BASE_URL = BuildConfig.CHAT_BASE_URL + "/"
        private const val HOST = BuildConfig.CHAT_HOST
        private const val CODE_HOST = BuildConfig.CODE_HOST

        /**
         * Where a plain launch lands: the chat, asking for a new conversation.
         *
         * It used to be [BASE_URL], which is HomeCore's dashboard — so opening
         * Alfred meant looking at a list of services and then tapping into the
         * chat, which then resumed whatever the last conversation had got to.
         * Two taps and a scroll to say something new. Opening this app is a new
         * thought far more often than it is a continuation.
         *
         * `?new=1` is honoured by chat.html and is the *only* thing that starts
         * a fresh conversation, which is what keeps the notification path
         * intact: a tap carries the ntfy click URL through [deepLinkUrl], that
         * URL never has `new`, and it therefore opens the conversation the
         * notification was about. The distinction lives in which URL is chosen
         * here, not in a flag the page has to interpret.
         *
         * It is a request, not an order: the page keeps a conversation still
         * being talked in (`launchStaysWarm`, three hours of silence — the same
         * window that splits conversations everywhere else; it was 30 minutes
         * until 2026-08-18) rather than opening a clean one on top of it. So a
         * launch inside that window lands where a notification tap would.
         * That decision belongs there and not here —
         * from this side a launch looks the same whether the person is starting
         * something or coming back to finish it, and the page has just read the
         * history that tells them apart. See HomeCore's AGENTS.md.
         *
         * No `embed=1`, unlike [deepLinkUrl]'s fallback. Embedding hides the
         * "← Inicio" link, and now that the dashboard is no longer where the
         * app opens, that link is the only way back to it.
         */
        private const val HOME_URL = BASE_URL + "chat?new=1"
        private val AUTHENTICATORS = BIOMETRIC_WEAK or DEVICE_CREDENTIAL
        private const val DEVICE_KEY_ALIAS = "alfred_device_identity"

        /**
         * How long Alfred may sit in the background, screen never off, before
         * the lock comes back on its own.
         *
         * The screen turning off is the signal that matters and it covers the
         * ordinary case: a phone put down locks itself within a minute or two.
         * This is for the phone that stays awake in somebody else's hands —
         * handed to a kid with a video playing. Five minutes is longer than
         * picking a photo or granting a permission takes, which is why those
         * no longer need exemptions, and shorter than lending your phone.
         */
        private const val AWAY_RELOCK_MS = 5 * 60_000L

        /**
         * "This launch is a deep link, not somebody opening the app" — set by a
         * notification tap and by ShareActivity. See maybeReloadChat().
         *
         * It is emphatically *not* a request for a new chat, which is what its
         * old name said and what it now does the exact opposite of: a plain
         * launch starts a new conversation, and this flag is how a tap avoids
         * that and lands on the one it was sent to. The wire value is still
         * `"open_new_chat"` so a notification posted by an older build, still
         * sitting in somebody's shade, keeps working.
         */
        const val EXTRA_DEEP_LINK = "open_new_chat"

        /**
         * Deep link carried by a notification tap: the ntfy message's own
         * `click` URL (HomeCore's chat_link(), e.g. /chat?prefill=…/?date=…).
         * Without it every tap dumped the user in a blank new chat instead of
         * the conversation the notification was about.
         */
        const val EXTRA_OPEN_URL = "open_url"

        /**
         * Paths the app is willing to open from a notification. Everything else
         * (and any unparseable URL) falls back to the chat root — a push is
         * untrusted input, so it gets to pick *which* of our pages opens, not
         * to navigate the WebView anywhere it likes.
         */
        private val DEEP_LINK_PREFIXES = listOf("/chat", "/tasks", "/grocery", "/geo", "/menu")

        /**
         * Extensions MimeTypeMap does not reliably know, spelled out so the
         * document picker can filter on them. Everything here is a format
         * HomeCore's /chat/upload-doc can actually read — see DOC_EXTS there;
         * the two lists are meant to agree.
         */
        private val EXTRA_MIME_TYPES = mapOf(
            "pdf" to "application/pdf",
            "docx" to "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx" to "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "csv" to "text/csv",
            "txt" to "text/plain",
            "md" to "text/plain",
        )

        /**
         * Rewrites a notification's click URL onto this app's own host: HomeCore
         * builds those links with HOMECORE_PUBLIC_URL, which is usually its
         * Tailscale address — reachable from the home network but not from a
         * phone on mobile data, and off-domain for our WebView either way. Only
         * the path + query survives the trip.
         */
        fun deepLinkUrl(raw: String?): String {
            val fallback = BASE_URL + "chat?embed=1"
            if (raw.isNullOrBlank()) return fallback
            val uri = try { Uri.parse(raw) } catch (e: Exception) { return fallback }
            val path = uri.path ?: return fallback
            if (DEEP_LINK_PREFIXES.none { path == it || path.startsWith("$it/") || path.startsWith("$it?") }) {
                return fallback
            }
            val query = uri.encodedQuery
            val sep = if (query.isNullOrBlank()) "?" else "&"
            val embed = if (path.startsWith("/chat")) "${sep}embed=1" else ""
            return BASE_URL.trimEnd('/') + path + (if (query.isNullOrBlank()) "" else "?$query") + embed
        }
    }

    // Set when launched/resumed via a notification tap, consumed once the
    // WebView is actually loaded and unlocked - see maybeReloadChat().
    private var pendingChatReload = false
    private var pendingChatUrl: String? = null

    // --- Runtime permissions (mic + camera) ---
    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        pendingPermissionRequest?.let { req ->
            val ok = grants[Manifest.permission.RECORD_AUDIO] == true
            runOnUiThread { if (ok) req.grant(req.resources) else req.deny() }
            if (!ok) toastMicDenied()
            pendingPermissionRequest = null
        }
        ensureNtfyServiceState()
        maybeStartLocationUpdates()
        maybeRequestBackgroundLocation() // fine just granted → ask for "all the time"
    }

    // Background location ("Permitir todo el tiempo") — required for geofence
    // reminders to fire while the app is closed. Must be requested separately
    // from fine location (Android 10+). Asked at most once; if denied, the user
    // can still enable it later in system settings.
    private val bgLocationLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted -> if (granted) syncGeofences() }

    // --- File chooser result (gallery or camera) ---
    private val fileChooserLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val cb = filePathCallback
        filePathCallback = null
        if (cb == null) return@registerForActivityResult
        var uris: Array<Uri>? = null
        if (result.resultCode == RESULT_OK) {
            val data = result.data
            when {
                data?.clipData != null -> {
                    val clip = data.clipData!!
                    uris = Array(clip.itemCount) { clip.getItemAt(it).uri }
                }
                data?.dataString != null -> uris = arrayOf(Uri.parse(data.dataString))
                cameraImageUri != null -> uris = arrayOf(cameraImageUri!!)
            }
        }
        cb.onReceiveValue(uris)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        pendingChatReload = intent?.getBooleanExtra(EXTRA_DEEP_LINK, false) == true
        if (pendingChatReload) pendingChatUrl = deepLinkUrl(intent?.getStringExtra(EXTRA_OPEN_URL))

        // Enable chrome://inspect for debug builds only (helps diagnose mic/JS).
        if ((applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0) {
            WebView.setWebContentsDebuggingEnabled(true)
        }

        webView = WebView(this)
        lockView = TextView(this).apply {
            text = "🔒 " + getString(R.string.assistant_name)
            textSize = 22f
            gravity = Gravity.CENTER
            setTextColor(ContextCompat.getColor(this@MainActivity, R.color.wall))
            setBackgroundColor(ContextCompat.getColor(this@MainActivity, R.color.plaster))
            // The panel has to *stop* things, not just cover them. A TextView
            // is not clickable, and a view that does not consume a touch lets
            // the FrameLayout hand it to the child underneath — so every tap
            // went straight through the lock into the chat behind it. Covering
            // and blocking are separate properties and it only had the first.
            isClickable = true
            isFocusable = true
        }
        val root = FrameLayout(this).apply {
            addView(webView, FrameLayout.LayoutParams(-1, -1))
            addView(lockView, FrameLayout.LayoutParams(-1, -1))
        }
        setContentView(root)
        // Before the page has had a chance to say anything: the window is
        // already painted by now, and a launch that flashes the old house and
        // then corrects itself reads as a glitch.
        restoreTheme()
        // A new Activity starts behind the panel and `onStart` settles it a
        // moment later — either passing it or opening it. Starting the other
        // way round would flash the chat before the lock on every cold start.
        showLockPanel()

        // Persist cookies across launches so the login session survives.
        CookieManager.getInstance().apply {
            setAcceptCookie(true)
            setAcceptThirdPartyCookies(webView, true)
        }

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            allowFileAccess = false
            allowContentAccess = false
        }

        // Native audio bridge — the page uses this instead of getUserMedia when
        // present. Safe because navigation is locked to our own domain.
        webView.addJavascriptInterface(AudioBridge(), "AndroidAudio")

        // Device-identity bridge — lets the login page detect whether this
        // device is enrolled and sign login challenges. Also safe: navigation
        // is locked to our own domain, so no other site can reach this bridge.
        webView.addJavascriptInterface(DeviceKeyBridge(), "AndroidDeviceKey")

        // App-update bridge — lets the chat header report the installed version
        // and download+install a newer APK from HomeCore's hidden deploy folder.
        webView.addJavascriptInterface(AppBridge(), "AndroidApp")

        // Location bridge — lets the chat page fetch the current device location
        // for Alfred (weather/nearby/etc.). Safe: navigation is locked to our
        // own domain, so no other site can reach it.
        webView.addJavascriptInterface(LocationBridge(), "AndroidLocation")

        // App-log bridge — lets the chat page's Log panel (Apps → 📜 Log) read
        // the persistent on-device log (geofences, location, ntfy) without adb.
        // Safe: navigation is locked to our own domain.
        webView.addJavascriptInterface(LogBridge(), "AndroidLogs")

        // Notification-access bridge — the Notificaciones panel uses this to
        // show whether the listener is granted and to open Android's settings.
        webView.addJavascriptInterface(NotificationBridge(), "AndroidNotifications")

        // Theme bridge — the page paints itself from /theme.css, but the status
        // bar and the window behind the WebView are ours, and a plum house with
        // an olive status bar looks like a bug in the app. The page hands us the
        // two colours it is actually using.
        webView.addJavascriptInterface(ThemeBridge(), "AndroidTheme")
        // EXPORTED: ACTION_DOWNLOAD_COMPLETE is sent by the system DownloadManager
        // (a different UID), so on Android 13+ a NOT_EXPORTED receiver never fires
        // and the install is never triggered. Safe — it's a protected broadcast
        // other apps can't spoof.
        ContextCompat.registerReceiver(
            this, updateDownloadReceiver,
            IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE),
            ContextCompat.RECEIVER_EXPORTED,
        )
        // EXPORTED for the same reason: ACTION_SCREEN_OFF comes from the
        // system, and it is a protected broadcast no other app can send.
        // Registered here and dropped in onDestroy, so it keeps arriving while
        // the activity is merely stopped — which is the whole point, since
        // that is when the screen goes off. If the process dies instead, the
        // lock is on anyway: a fresh LockPolicy starts locked.
        ContextCompat.registerReceiver(
            this, screenOffReceiver,
            IntentFilter(Intent.ACTION_SCREEN_OFF),
            ContextCompat.RECEIVER_EXPORTED,
        )

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val host = request.url.host ?: return false
                // Two hosts, not one. opencode's interface cannot live under a
                // path on the chat host -- its assets and its API are absolute
                // from the origin root, and `/api` collides with the portal's --
                // so the Programmer is a second origin. Without it here, opening
                // the Programmer threw the family out of the app and into the
                // browser, which is not what "open the Programmer" should mean.
                //
                // CODE_HOST is empty unless the build was told one, and an empty
                // host matches nothing: a build that does not know about
                // opencode behaves exactly as this did before.
                if (host.equals(HOST, ignoreCase = true)) return false // load in-app
                if (CODE_HOST.isNotEmpty() && host.equals(CODE_HOST, ignoreCase = true)) return false
                startActivity(Intent(Intent.ACTION_VIEW, request.url))  // off-domain → browser
                return true
            }

            // Without these two, a WebView that cannot load a page renders
            // nothing and explains nothing -- the tap simply does not appear to
            // do anything. That is not a hypothetical: opening the Programmer
            // looked like a dead button, and the cause (an untrusted certificate
            // on a name only the house network serves) was invisible from the
            // phone. A blank screen is the one failure a person cannot act on.
            //
            // Both only speak up for the *main frame*. A sub-resource that fails
            // -- an icon, a font, a beacon -- is not worth throwing a page away
            // over, and reporting those made the chat unusable on a slow link.
            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                // cancel(), never proceed(): this reports the failure, it does
                // not wave it through. An app that quietly accepts any
                // certificate on the house network accepts one on any other.
                handler.cancel()
                val host = Uri.parse(error.url ?: "").host ?: ""
                Log.w("AlfredWeb", "ssl error on $host: ${error.primaryError}")
                showBlockedPage(
                    view,
                    "No se pudo verificar $host",
                    if (host.equals(CODE_HOST, ignoreCase = true) && CODE_HOST.isNotEmpty())
                        "Este nombre lo sirve la casa con su propio certificado. " +
                            "Instalá el CA de la casa en este teléfono (Ajustes → " +
                            "Seguridad → Cifrado y credenciales → Instalar un certificado " +
                            "→ Certificado de CA) y volvé a entrar."
                    else
                        "El certificado de este sitio no es de fiar. No se cargó la página.",
                )
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (!request.isForMainFrame) return
                val host = request.url.host ?: ""
                Log.w("AlfredWeb", "load error on $host: ${error.errorCode} ${error.description}")
                showBlockedPage(
                    view,
                    "No se pudo abrir $host",
                    if (host.equals(CODE_HOST, ignoreCase = true) && CODE_HOST.isNotEmpty())
                        "Este nombre sólo responde en la red de casa. " +
                            "Conectate al wifi de casa o a la VPN y probá de nuevo."
                    else
                        "${error.description}",
                )
            }
        }

        // File downloads (e.g. Alfred's [📥 Descargar …] links, /files downloads).
        // The server sends these as Content-Disposition: attachment, which a
        // WebView never renders itself — without this listener the tap is a no-op.
        // Hand off to DownloadManager, forwarding the session cookie so the
        // @login_required /chat/download route authorizes the request.
        webView.setDownloadListener { url, userAgent, contentDisposition, mimeType, _ ->
            try {
                val fileName = URLUtil.guessFileName(url, contentDisposition, mimeType)
                val cookie = CookieManager.getInstance().getCookie(url)
                val req = DownloadManager.Request(Uri.parse(url)).apply {
                    if (!cookie.isNullOrBlank()) addRequestHeader("Cookie", cookie)
                    if (!userAgent.isNullOrBlank()) addRequestHeader("User-Agent", userAgent)
                    setMimeType(mimeType)
                    setTitle(fileName)
                    setNotificationVisibility(
                        DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED,
                    )
                    setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName)
                }
                getSystemService(DownloadManager::class.java)?.enqueue(req)
                Toast.makeText(this, "Descargando $fileName…", Toast.LENGTH_SHORT).show()
            } catch (e: Exception) {
                Log.e("AlfredWeb", "file download failed", e)
                Toast.makeText(this, "No se pudo descargar el archivo", Toast.LENGTH_SHORT).show()
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onConsoleMessage(m: ConsoleMessage): Boolean {
                Log.d("AlfredWeb", "${m.message()} @${m.sourceId()}:${m.lineNumber()}")
                return true
            }

            override fun onPermissionRequest(request: PermissionRequest) {
                val wantsAudio = request.resources.any {
                    it == PermissionRequest.RESOURCE_AUDIO_CAPTURE
                }
                runOnUiThread {
                    if (!wantsAudio) { request.deny(); return@runOnUiThread }
                    if (hasPermission(Manifest.permission.RECORD_AUDIO)) {
                        request.grant(request.resources)
                    } else {
                        // Ask the OS for mic; the launcher grants the web request on success.
                        pendingPermissionRequest = request
                        permissionLauncher.launch(arrayOf(Manifest.permission.RECORD_AUDIO))
                    }
                }
            }

            override fun onShowFileChooser(
                view: WebView,
                callback: ValueCallback<Array<Uri>>,
                params: FileChooserParams,
            ): Boolean {
                filePathCallback?.onReceiveValue(null)
                filePathCallback = callback

                val allowMultiple = params.mode == FileChooserParams.MODE_OPEN_MULTIPLE
                // Honour the page's accept="" instead of hardcoding images. It
                // was "image/*" here regardless of what the page asked for, so
                // when chat.html started accepting PDFs and spreadsheets the
                // picker on the phone would still have offered only photos.
                val mimeTypes = fileChooserMimeTypes(params.acceptTypes)
                val contentIntent = Intent(Intent.ACTION_GET_CONTENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    type = mimeTypes.singleOrNull() ?: "*/*"
                    if (mimeTypes.size > 1) {
                        putExtra(Intent.EXTRA_MIME_TYPES, mimeTypes.toTypedArray())
                    }
                    putExtra(Intent.EXTRA_ALLOW_MULTIPLE, allowMultiple)
                }
                val cameraIntent = createCameraIntent()
                val chooser = Intent(Intent.ACTION_CHOOSER).apply {
                    putExtra(Intent.EXTRA_INTENT, contentIntent)
                    putExtra(Intent.EXTRA_TITLE, "Selecciona un archivo")
                    if (cameraIntent != null) {
                        putExtra(Intent.EXTRA_INITIAL_INTENTS, arrayOf(cameraIntent))
                    }
                }
                return try {
                    fileChooserLauncher.launch(chooser)
                    true
                } catch (e: Exception) {
                    filePathCallback = null
                    false
                }
            }
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                // Ask the page before touching WebView history. Everything a
                // back press should act on in the chat — an open panel, an old
                // conversation pinned from the sidebar, the sidebar itself —
                // is DOM state that never made a history entry, while the
                // history under the page is stale /chat loads accumulated
                // across days (this Activity lives that long and warm launches
                // never loadUrl), so goBack() from the chat used to walk into
                // an old conversation instead of closing what was open.
                //
                // The page answers 'handled' (it closed one thing), 'exit'
                // (chat at its root — back now means leave the app), or 'pass'
                // (not the chat page / handler absent), and only 'pass' falls
                // through to history — which is the right answer on /stats and
                // friends, where the entry behind you is the chat you came
                // from. A page that predates __handleBack, an evaluation
                // error, or a null result all land in the else branch, which
                // is byte-for-byte the old behaviour.
                webView.evaluateJavascript(
                    "(function(){try{return window.__handleBack?window.__handleBack():'pass'}catch(e){return 'pass'}})()"
                ) { result ->
                    when (result?.trim('"')) {
                        "handled" -> {}
                        "exit" -> finish()
                        else -> if (webView.canGoBack()) webView.goBack() else finish()
                    }
                }
            }
        })

        // Not gated behind the biometric lock: notifications should keep
        // arriving even while the app is locked. The service itself figures
        // out (via /api/ntfy-config) whether this user has notifications
        // configured at all.
        ensureNtfyServiceState()
    }

    private fun ensureNtfyServiceState() {
        NtfyClientService.start(this)
    }

    /**
     * The page's `accept=""` turned into MIME types the document picker
     * understands. A MIME wildcard such as `image` + slash + star passes
     * through; `.pdf` has to be resolved, because ACTION_GET_CONTENT filters on
     * MIME and knows nothing about extensions.
     *
     * (Spelled out like that on purpose: Kotlin **nests** block comments, so a
     * slash-star sequence inside this KDoc opens a second one and the closing
     * delimiter below only closes the inner comment. That silently swallowed
     * this whole function once already.)
     *
     * MimeTypeMap is missing entries on some builds — `.md` in particular — and
     * a type we fail to resolve is a file the user simply cannot select, with no
     * error to explain why. So an unresolved entry widens the picker to
     * everything rather than silently narrowing it: the server validates the
     * extension anyway and says so in Spanish when it refuses.
     */
    private fun fileChooserMimeTypes(accept: Array<String>?): List<String> {
        if (accept.isNullOrEmpty()) return emptyList()
        val out = LinkedHashSet<String>()
        accept.forEach { entry ->
            entry.split(',').map { it.trim() }.filter { it.isNotEmpty() }.forEach { one ->
                when {
                    one.startsWith(".") -> {
                        val ext = one.removePrefix(".").lowercase()
                        val mime = EXTRA_MIME_TYPES[ext]
                            ?: MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext)
                        if (mime == null) return emptyList()  // unknown: widen to all types
                        out.add(mime)
                    }
                    one.contains('/') -> out.add(one)
                    else -> return emptyList()
                }
            }
        }
        return out.toList()
    }

    // Fires when the app is already running and a notification is tapped
    // (CLEAR_TOP reuses this instance rather than recreating it).
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (intent.getBooleanExtra(EXTRA_DEEP_LINK, false)) {
            pendingChatReload = true
            pendingChatUrl = deepLinkUrl(intent.getStringExtra(EXTRA_OPEN_URL))
            if (lock.isUnlocked) maybeReloadChat()
        }
    }

    /** Opens the conversation the tapped notification points at (its ntfy
     *  `click` URL), or the chat root when it carried none. */
    private fun maybeReloadChat() {
        if (pendingChatReload && loaded) {
            pendingChatReload = false
            // Never HOME_URL in practice — deepLinkUrl always returns a URL, so
            // pendingChatUrl is set whenever this runs. It is the fallback only
            // so that a future caller which forgets cannot silently navigate
            // somebody out of the conversation they were sent to.
            webView.loadUrl(pendingChatUrl ?: HOME_URL)
            pendingChatUrl = null
        }
    }

    // ---------------------------------------------------------------------
    // Biometric / device-credential lock. What re-locks and why is written
    // down in LockPolicy; these three are the wiring to the lifecycle.
    // ---------------------------------------------------------------------

    /** The phone locking itself. The only signal that reliably means it: by
     *  the time onStart runs the user has already unlocked, so asking
     *  KeyguardManager then always answers "no", and asking in onStop races
     *  the keyguard engaging. */
    private val screenOffReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            lock.onScreenOff()
        }
    }

    override fun onStart() {
        super.onStart()
        // elapsedRealtime, never wall time: a lock whose timeout is measured
        // on a clock the user can set is a lock the user can set open.
        val needsAuth = lock.needsAuthOnForeground(SystemClock.elapsedRealtime())
        if (!needsAuth) {
            // Nothing to ask for — and the gate still has to be opened, which
            // is what was missing. The only way out of the locked state used
            // to run through the prompt, so an Activity that skipped it was
            // left exactly as `onCreate` built it: the panel up, the WebView
            // hidden behind it, and nothing ever asked to load. A lock on
            // screen with no way past it.
            //
            // Skipping the prompt was rare until the lock became
            // process-scoped; now it is the normal case for every Activity
            // rebuild (dark mode, locale, a reclaimed task, "Don't keep
            // activities"), and each of those pinned the lock panel over a
            // blank page with no way past it.
            onUnlocked()
            return
        }
        showLockPanel()
        if (!promptShowing) promptAuth()
    }

    /**
     * The two views, decided in one place from one fact.
     *
     * They used to be set across three functions — `onCreate`, `promptAuth`,
     * `onAuthSuccess` — and the case none of them covered, unlocked with a
     * freshly built panel on top, is what left the lock stuck on screen.
     * Nothing else in this file may touch
     * `lockView.visibility` or `webView.visibility`; `LockPanelTest` fails the
     * build if it does.
     */
    private fun showLockPanel() {
        lockView.visibility = View.VISIBLE
        webView.visibility = View.INVISIBLE
    }

    private fun hideLockPanel() {
        lockView.visibility = View.GONE
        webView.visibility = View.VISIBLE
    }

    override fun onStop() {
        super.onStop()
        // Belt as well as braces. The receiver above is the prompt signal, but
        // it is a broadcast, and a broadcast to a process the OS has frozen is
        // delivered when it thaws — which is a race with our own onStart. This
        // is the same question asked synchronously, and it settles the
        // commonest lock of all: the power button pressed with Alfred open,
        // where the screen is already off by the time we are stopped.
        //
        // What it does not cover is the phone locking while Alfred was already
        // in the background; there the receiver is all there is, and if that
        // broadcast lost the race Alfred opens once without asking.
        val power = getSystemService(PowerManager::class.java)
        if (power?.isInteractive == false) lock.onScreenOff()
        lock.onBackground(SystemClock.elapsedRealtime())
        // Say we are gone before the process can be frozen. The page's own
        // "hidden" report races that freeze and does not always win, and a
        // report that never lands keeps every reply from being pushed until
        // the server's threshold expires.
        Presence.report(this, false)
    }

    override fun onResume() {
        super.onResume()
        ensureNtfyServiceState()
        syncGeofences() // keep OS geofences current with the server on each resume
        // The foreground is where the session cookie is certainly fresh and the
        // network is certainly up, so it's the moment to ship anything the
        // background wrote (AppLog.report) and to re-ask the recognizer what
        // languages it holds. Both self-throttle; neither blocks.
        AppLog.flush(this)
        com.chat.app.assist.SttSupport.probeSupport(this)
        Presence.report(this, true)
    }

    private fun promptAuth() {
        val bm = BiometricManager.from(this)
        if (bm.canAuthenticate(AUTHENTICATORS) != BiometricManager.BIOMETRIC_SUCCESS) {
            // No fingerprint and no screen lock configured — can't enforce a lock.
            Toast.makeText(
                this,
                "Configura una huella o bloqueo de pantalla para proteger el chat.",
                Toast.LENGTH_LONG,
            ).show()
            onAuthSuccess()
            return
        }

        val info = BiometricPrompt.PromptInfo.Builder()
            .setTitle(getString(R.string.assistant_name))
            .setSubtitle("Desbloquea para acceder al chat")
            .setAllowedAuthenticators(AUTHENTICATORS)
            .build()

        val prompt = BiometricPrompt(
            this,
            ContextCompat.getMainExecutor(this),
            object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                    promptShowing = false
                    onAuthSuccess()
                }

                override fun onAuthenticationError(code: Int, msg: CharSequence) {
                    promptShowing = false
                    // User cancelled / lockout → don't leave the chat exposed.
                    finish()
                }
                // onAuthenticationFailed: a single bad attempt; the prompt stays up.
            },
        )
        promptShowing = true
        showLockPanel()
        prompt.authenticate(info)
    }

    private fun onAuthSuccess() {
        lock.onAuthenticated()
        onUnlocked()
    }

    /**
     * The gate is open: take the panel down and make sure there is a page
     * behind it.
     *
     * Reached two ways — the prompt was passed, or `onStart` found the lock
     * already satisfied — and the second one is why the load lives here rather
     * than in `onAuthSuccess`. `loaded` is a field of the Activity, so a rebuilt
     * one has never loaded anything; when it also skips the prompt, the first
     * `loadUrl` has to happen somewhere, and this is the only place both paths
     * pass through.
     */
    private fun onUnlocked() {
        hideLockPanel()
        if (!loaded) {
            loaded = true
            // Request mic/camera once, after unlocking, so it doesn't collide
            // with the biometric prompt at launch.
            requestStartupPermissions()
            maybeStartLocationUpdates() // if already granted from a prior run
            maybeRequestBackgroundLocation()
            syncGeofences()
            // A notification tap that cold-starts the app should land on the
            // conversation it points at; every other launch gets a clean sheet.
            webView.loadUrl(if (pendingChatReload) (pendingChatUrl ?: HOME_URL) else HOME_URL)
            pendingChatReload = false // this first load already satisfies it
            pendingChatUrl = null
        } else {
            maybeReloadChat()
        }
    }

    // ---------------------------------------------------------------------
    private fun createCameraIntent(): Intent? {
        if (!hasPermission(Manifest.permission.CAMERA)) return null
        return try {
            val dir = File(externalCacheDir, "camera").apply { mkdirs() }
            val photo = File(dir, "capture_${System.currentTimeMillis()}.jpg")
            cameraImageUri = FileProvider.getUriForFile(this, "$packageName.fileprovider", photo)
            Intent(MediaStore.ACTION_IMAGE_CAPTURE).apply {
                putExtra(MediaStore.EXTRA_OUTPUT, cameraImageUri)
            }
        } catch (e: Exception) {
            cameraImageUri = null
            null
        }
    }

    private fun requestStartupPermissions() {
        val needed = listOfNotNull(
            Manifest.permission.RECORD_AUDIO,
            Manifest.permission.CAMERA,
            Manifest.permission.ACCESS_FINE_LOCATION,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) Manifest.permission.POST_NOTIFICATIONS else null,
        ).filter { !hasPermission(it) }
        if (needed.isNotEmpty()) {
            permissionLauncher.launch(needed.toTypedArray())
        }
    }

    private fun toastMicDenied() {
        Toast.makeText(
            this,
            "Micrófono bloqueado. Actívalo en Ajustes para usar la voz.",
            Toast.LENGTH_LONG,
        ).show()
        // Offer a shortcut to the app's settings.
        try {
            startActivity(
                Intent(
                    Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.fromParts("package", packageName, null),
                )
            )
        } catch (_: Exception) {}
    }

    private fun hasPermission(perm: String) =
        checkSelfPermission(perm) == PackageManager.PERMISSION_GRANTED

    // Start caching device location once permission is granted. Idempotent.
    // Foreground-only (no background location here — that's the geofencing
    // feature); the OS pauses these updates when the app isn't in use.
    private fun maybeStartLocationUpdates() {
        if (locationListener != null) return
        if (!hasPermission(Manifest.permission.ACCESS_FINE_LOCATION) &&
            !hasPermission(Manifest.permission.ACCESS_COARSE_LOCATION)) return
        val lm = getSystemService(LocationManager::class.java) ?: return
        val listener = object : LocationListener {
            override fun onLocationChanged(loc: Location) { lastLocation = loc }
            override fun onProviderEnabled(provider: String) {}
            override fun onProviderDisabled(provider: String) {}
            @Deprecated("required on API < 30")
            override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
        }
        locationListener = listener
        try {
            for (p in listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)) {
                if (!lm.isProviderEnabled(p)) continue
                lm.requestLocationUpdates(p, 60_000L, 100f, listener) // 60s / 100m
                lm.getLastKnownLocation(p)?.let { seed ->
                    val cur = lastLocation
                    if (cur == null || seed.time > cur.time) lastLocation = seed
                }
            }
        } catch (e: SecurityException) {
            Log.e("AlfredWeb", "location updates failed", e)
        }
    }

    // Ask once for background location so geofence reminders can fire with the
    // app closed. No-op if fine isn't granted yet, already granted, or asked
    // before (respects a prior denial rather than nagging).
    private fun maybeRequestBackgroundLocation() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return
        if (!hasPermission(Manifest.permission.ACCESS_FINE_LOCATION)) return
        if (hasPermission(Manifest.permission.ACCESS_BACKGROUND_LOCATION)) return
        val prefs = getSharedPreferences("alfred", MODE_PRIVATE)
        if (prefs.getBoolean("bg_loc_asked", false)) return
        prefs.edit().putBoolean("bg_loc_asked", true).apply()
        bgLocationLauncher.launch(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
    }

    // Reconcile OS geofences with the server set (network → off the main thread).
    private fun syncGeofences() {
        if (!hasPermission(Manifest.permission.ACCESS_FINE_LOCATION)) return
        Thread { GeofenceManager.sync(applicationContext) }.start()
        // Arm the hourly location heartbeat from the same place, and behind the
        // same permission check: both are pointless without location, and this
        // runs once permission actually exists rather than at cold start when
        // it may still be unanswered. enqueueUniquePeriodicWork(KEEP) makes it
        // idempotent, so opening the app does not push the next run out.
        LocationPingWorker.schedule(applicationContext)
    }

    // ---------------------------------------------------------------------
    // Location bridge (window.AndroidLocation.getLocation()) — returns the
    // latest fix as JSON {"lat","lon","acc","ts"}, or "" if none/no permission.
    // ---------------------------------------------------------------------
    inner class LocationBridge {
        @JavascriptInterface
        fun getLocation(): String {
            val loc = lastLocation ?: return ""
            // Double.toString / Long.toString are locale-independent (always '.').
            val sb = StringBuilder("{\"lat\":${loc.latitude},\"lon\":${loc.longitude}")
            if (loc.hasAccuracy()) sb.append(",\"acc\":${loc.accuracy}")
            sb.append(",\"ts\":${loc.time}}")
            return sb.toString()
        }
    }

    // ---------------------------------------------------------------------
    // App-log bridge (window.AndroidLogs) — the chat page's Log panel reads
    // and clears the persistent on-device log (see AppLog).
    // ---------------------------------------------------------------------
    inner class LogBridge {
        @JavascriptInterface
        fun get(): String = AppLog.read(applicationContext)

        @JavascriptInterface
        fun clear() = AppLog.clear(applicationContext)
    }

    // ---------------------------------------------------------------------
    // Notification-access bridge (window.AndroidNotifications) — whether the
    // listener is granted, and a way to open the system screen that grants it.
    // The permission can only be given by the user in Android's own settings;
    // there is no runtime-permission dialog for it.
    // ---------------------------------------------------------------------
    inner class NotificationBridge {
        @JavascriptInterface
        fun isEnabled(): Boolean = NotificationRelayService.isEnabled(this@MainActivity)

        @JavascriptInterface
        fun openSettings() = runOnUiThread {
            try {
                startActivity(NotificationRelayService.settingsIntent())
            } catch (e: Exception) {
                Toast.makeText(
                    this@MainActivity,
                    "No se pudo abrir los ajustes de notificaciones",
                    Toast.LENGTH_LONG,
                ).show()
            }
        }

        /** Re-read the allowlist now (after the panel changes a toggle). */
        @JavascriptInterface
        fun sync() = NotificationRelayService.syncAllowlist(applicationContext)
    }

    // ---------------------------------------------------------------------
    // Theme bridge (window.AndroidTheme.apply(wall, plaster)) — the chat page
    // sends the colours it is painting itself with, so the status bar and the
    // window behind the WebView match the house the person chose.
    //
    // Remembered in prefs, and re-applied in onCreate before the WebView has
    // loaded anything: the window background is painted long before any
    // JavaScript runs, so without the memory every launch would flash The House
    // olive and then correct itself.
    // ---------------------------------------------------------------------
    inner class ThemeBridge {
        @JavascriptInterface
        fun apply(wall: String, plaster: String) {
            // Every page hands these over on every load, and the theme changes
            // maybe once a month — so say nothing when nothing changed rather
            // than repainting the window and touching prefs on each navigation.
            if (wall == appliedWall && plaster == appliedPlaster) return
            if (!useTheme(wall, plaster)) return
            getSharedPreferences("alfred", MODE_PRIVATE).edit()
                .putString("theme_wall", wall)
                .putString("theme_plaster", plaster)
                .apply()
        }
    }

    /** `#RRGGBB` (what HomeCore's theme validator emits, and what every page's
     *  own `:root` fallback is written as) or an explicit `rgb()`/`rgba()`.
     *  Anything else is refused rather than guessed at: scraping the digits out
     *  of an `hsl()` or a `color-mix()` would produce a confident wrong colour. */
    private fun parseColor(value: String): Int? {
        val v = value.trim()
        try {
            if (v.startsWith("#")) return Color.parseColor(v)
            if (v.startsWith("rgb(") || v.startsWith("rgba(")) {
                val nums = Regex("\\d+").findAll(v).map { it.value.toInt() }.toList()
                if (nums.size >= 3) return Color.rgb(
                    nums[0].coerceIn(0, 255), nums[1].coerceIn(0, 255), nums[2].coerceIn(0, 255)
                )
            }
        } catch (e: Exception) {
            AppLog.log(applicationContext, "theme", "could not read colour '$v'", e)
        }
        return null
    }

    /** Parse a wall/plaster pair and paint the app's chrome with it. The one
     *  path both the bridge and the launch-time restore go through, so they
     *  cannot drift. Returns false when either colour is unreadable. */
    private fun useTheme(wall: String, plaster: String): Boolean {
        val bar = parseColor(wall) ?: return false
        val bg = parseColor(plaster) ?: return false
        appliedWall = wall
        appliedPlaster = plaster
        runOnUiThread { applyThemeColors(bar, bg) }
        return true
    }

    private fun applyThemeColors(statusBar: Int, background: Int) {
        window.statusBarColor = statusBar
        window.decorView.setBackgroundColor(background)
        // The lock panel covers the whole frame on every launch and every
        // return from background, so leaving it on the static house colours
        // would show a The House screen under a themed status bar — the exact
        // mismatch this bridge exists to remove.
        if (::lockView.isInitialized) {
            lockView.setBackgroundColor(background)
            lockView.setTextColor(statusBar)
        }
        // Dark text on a light bar, light on a dark one — the house palette is
        // dark-chrome today, but a theme is free not to be.
        val light = ColorUtils.calculateLuminance(statusBar) > 0.5
        WindowInsetsControllerCompat(window, window.decorView)
            .isAppearanceLightStatusBars = light
    }

    /** The last theme this device saw, applied before anything is loaded. */
    private fun restoreTheme() {
        val prefs = getSharedPreferences("alfred", MODE_PRIVATE)
        val wall = prefs.getString("theme_wall", null) ?: return
        val plaster = prefs.getString("theme_plaster", null) ?: return
        useTheme(wall, plaster)
    }

    // ---------------------------------------------------------------------
    // Native audio capture bridge (window.AndroidAudio.start()/stop())
    // ---------------------------------------------------------------------
    inner class AudioBridge {
        @JavascriptInterface
        fun start() = runOnUiThread { startNativeRecording() }

        @JavascriptInterface
        fun stop() = runOnUiThread { stopNativeRecording() }
    }

    // ---------------------------------------------------------------------
    // App-update bridge (window.AndroidApp) — the chat header reads the
    // installed versionCode and, when HomeCore reports a newer APK, downloads it
    // (with the session cookie) and launches the installer.
    // ---------------------------------------------------------------------
    inner class AppBridge {
        /** Installed versionCode, as a string (JS parses it). */
        @JavascriptInterface
        fun getVersionCode(): String = installedVersionCode().toString()

        /** `path` is a HomeCore path like "/chat/app-download" (or an absolute URL). */
        @JavascriptInterface
        fun downloadAndInstall(path: String) = runOnUiThread { startUpdateDownload(path) }

        /** Beta update channel (Debug → "Beta updates"). Stored in the shared
         *  "alfred" prefs so NtfyClientService's background update handler sees
         *  the same choice as the chat page's update badge. */
        @JavascriptInterface
        fun getBetaChannel(): Boolean =
            getSharedPreferences("alfred", MODE_PRIVATE).getBoolean("beta_channel", false)

        @JavascriptInterface
        fun setBetaChannel(on: Boolean) {
            getSharedPreferences("alfred", MODE_PRIVATE).edit().putBoolean("beta_channel", on).apply()
        }

        /** Which speech-to-text path the assist overlay uses:
         *  `auto` (on-device → system recognizer → Whisper), or one forced.
         *  The only practical way to exercise each branch on a real phone — and
         *  the escape hatch if on-device transcription quality disappoints. */
        @JavascriptInterface
        fun getSttMode(): String = com.chat.app.assist.SttSupport.mode(this@MainActivity)

        @JavascriptInterface
        fun setSttMode(mode: String) =
            com.chat.app.assist.SttSupport.setMode(this@MainActivity, mode)

        /** What the on-device recognizer last said it can do — the installed,
         *  pending and supported languages, and which tag we settled on. Shown
         *  under 🎙️ Voz so "why is it still using Whisper" has an answer that
         *  doesn't need a cable. Empty until the first probe answers. */
        @JavascriptInterface
        fun getSttStatus(): String =
            com.chat.app.assist.SttSupport.supportSummary(this@MainActivity)

        /** Forgets every remembered speech-recognition failure (language packs
         *  installed since, a recognizer that's been fixed, …) and re-asks the
         *  recognizer what it supports. */
        @JavascriptInterface
        fun clearSttState() {
            com.chat.app.assist.SttSupport.clearState(this@MainActivity)
            com.chat.app.assist.SttSupport.probeSupport(this@MainActivity, force = true)
        }
    }

    @Suppress("DEPRECATION")
    private fun installedVersionCode(): Long = try {
        val pi = packageManager.getPackageInfo(packageName, 0)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) pi.longVersionCode else pi.versionCode.toLong()
    } catch (e: Exception) { 0L }

    private fun startUpdateDownload(path: String) {
        val url = if (path.startsWith("http")) path
                  else BASE_URL.trimEnd('/') + "/" + path.trimStart('/')
        val cookie = CookieManager.getInstance().getCookie(BASE_URL)
        updateDownloadId = ApkInstaller.enqueue(
            this, url, getString(R.string.assistant_name),
            if (!cookie.isNullOrBlank()) "Cookie" else null, cookie,
        )
        if (updateDownloadId == -1L) {
            Log.e("AlfredWeb", "update enqueue failed for $url")
            Toast.makeText(this, "No se pudo iniciar la descarga", Toast.LENGTH_LONG).show()
            return
        }
        Toast.makeText(this, "Descargando actualización…", Toast.LENGTH_SHORT).show()
    }

    // ---------------------------------------------------------------------
    // Device-identity bridge (window.AndroidDeviceKey) — restricts remote
    // login to this specific enrolled device. A per-device EC keypair lives in
    // the Android Keystore; the private key is non-exportable and never leaves
    // the device — the server only ever sees the public key (at enrollment)
    // and per-login signatures (which prove possession without revealing it).
    // ---------------------------------------------------------------------
    inner class DeviceKeyBridge {
        @JavascriptInterface
        fun isEnrolled(): Boolean = deviceKeyExists()

        @JavascriptInterface
        fun getPublicKey(): String = getOrCreateDeviceKeyPublicKeyB64()

        @JavascriptInterface
        fun sign(challenge: String): String = signWithDeviceKey(challenge)
    }

    private fun androidKeyStore(): KeyStore =
        KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun deviceKeyExists(): Boolean =
        try { androidKeyStore().containsAlias(DEVICE_KEY_ALIAS) } catch (e: Exception) { false }

    private fun getOrCreateDeviceKeyPublicKeyB64(): String {
        return try {
            val ks = androidKeyStore()
            if (!ks.containsAlias(DEVICE_KEY_ALIAS)) {
                val kpg = KeyPairGenerator.getInstance(
                    KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore",
                )
                val spec = KeyGenParameterSpec.Builder(
                    DEVICE_KEY_ALIAS, KeyProperties.PURPOSE_SIGN,
                )
                    .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                    .setDigests(KeyProperties.DIGEST_SHA256)
                    .build()
                kpg.initialize(spec)
                kpg.generateKeyPair()
            }
            val cert = ks.getCertificate(DEVICE_KEY_ALIAS)
            Base64.encodeToString(cert.publicKey.encoded, Base64.NO_WRAP)
        } catch (e: Exception) {
            Log.e("AlfredWeb", "device key generation failed", e)
            ""
        }
    }

    private fun signWithDeviceKey(challenge: String): String {
        return try {
            val ks = androidKeyStore()
            val privateKey = ks.getKey(DEVICE_KEY_ALIAS, null) as? PrivateKey ?: return ""
            val sig = Signature.getInstance("SHA256withECDSA")
            sig.initSign(privateKey)
            sig.update(challenge.toByteArray(Charsets.UTF_8))
            Base64.encodeToString(sig.sign(), Base64.NO_WRAP)
        } catch (e: Exception) {
            Log.e("AlfredWeb", "device key signing failed", e)
            ""
        }
    }

    private fun startNativeRecording() {
        if (recorder != null) return // already recording
        if (!hasPermission(Manifest.permission.RECORD_AUDIO)) {
            permissionLauncher.launch(arrayOf(Manifest.permission.RECORD_AUDIO))
            nativeAudioError("Permiso de microfono requerido, intenta de nuevo.")
            return
        }
        try {
            val dir = File(cacheDir, "audio").apply { mkdirs() }
            val f = File(dir, "rec_${System.currentTimeMillis()}.m4a")
            audioFile = f
            val rec = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                MediaRecorder(this)
            } else {
                @Suppress("DEPRECATION") MediaRecorder()
            }
            rec.setAudioSource(MediaRecorder.AudioSource.MIC)
            rec.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            rec.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            rec.setAudioSamplingRate(16000) // Whisper works at 16 kHz
            rec.setAudioEncodingBitRate(64000)
            rec.setOutputFile(f.absolutePath)
            rec.prepare()
            rec.start()
            recorder = rec
        } catch (e: Exception) {
            releaseRecorder()
            nativeAudioError(e.message ?: "no se pudo iniciar la grabacion")
        }
    }

    private fun stopNativeRecording() {
        val rec = recorder ?: return
        recorder = null
        var ok = true
        try {
            rec.stop()
        } catch (e: Exception) {
            ok = false // stopped too quickly / no audio captured
        } finally {
            try { rec.release() } catch (_: Exception) {}
        }
        val f = audioFile
        audioFile = null
        if (!ok || f == null || !f.exists() || f.length() == 0L) {
            f?.delete()
            nativeAudioError("Grabacion demasiado corta.")
            return
        }
        try {
            val b64 = Base64.encodeToString(f.readBytes(), Base64.NO_WRAP)
            webView.evaluateJavascript("window.onNativeAudio && window.onNativeAudio('$b64','audio/mp4')", null)
        } catch (e: Exception) {
            nativeAudioError(e.message ?: "error leyendo el audio")
        } finally {
            f.delete()
        }
    }

    private fun releaseRecorder() {
        try { recorder?.release() } catch (_: Exception) {}
        recorder = null
        audioFile?.delete()
        audioFile = null
    }

    private fun nativeAudioError(msg: String) {
        val safe = msg.replace("\\", "\\\\").replace("'", "\\'")
        webView.evaluateJavascript("window.onNativeAudioError && window.onNativeAudioError('$safe')", null)
    }

    override fun onPause() {
        super.onPause()
        CookieManager.getInstance().flush() // persist session cookie to disk
    }

    override fun onDestroy() {
        runCatching { unregisterReceiver(updateDownloadReceiver) }
        runCatching { unregisterReceiver(screenOffReceiver) }
        super.onDestroy()
    }
}
