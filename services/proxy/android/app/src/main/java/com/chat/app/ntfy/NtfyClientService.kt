package com.chat.app.ntfy

import com.chat.app.BuildConfig

import android.app.DownloadManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.Network
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import android.webkit.CookieManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.chat.app.ApkInstaller
import com.chat.app.R
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.math.min
import okhttp3.Call
import okhttp3.Callback
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import com.chat.app.AppLog
import com.chat.app.geo.GeofenceManager
import com.chat.app.geo.LocationReporter
import com.chat.app.notif.NotificationRelayService
import com.chat.app.ring.PhoneRinger
import org.json.JSONArray
import org.json.JSONObject

/**
 * Foreground service that subscribes to this user's ntfy topics (fetched from
 * the home-chat proxy's /api/ntfy-config, authenticated via the same session
 * cookie the WebView uses) and turns incoming messages into real Android
 * notifications. Mirrors what the official ntfy app itself does for
 * self-hosted servers without Firebase relay configured: a persistent
 * WebSocket held open by a foreground service, not periodic polling.
 */
class NtfyClientService : Service() {

    companion object {
        private const val TAG = "NtfyClientService"
        internal const val API_BASE = BuildConfig.CHAT_BASE_URL
        // v2: recreated at IMPORTANCE_MIN so the mandatory foreground-service
        // notification shows no status-bar icon and sits collapsed at the bottom
        // of the shade. Android ignores importance downgrades to an existing
        // channel, so this needs a new id (the old "ntfy_service" is deleted).
        private const val CHANNEL_SERVICE = "ntfy_service2"
        private const val CHANNEL_MESSAGES = AlertNotification.CHANNEL_MESSAGES
        private const val CHANNEL_UPDATES = "ntfy_updates"
        private const val NOTIF_ID_SERVICE = 1
        private const val NOTIF_ID_UPDATE = 2
        private const val PREFS_NAME = "ntfy_cache"
        private const val KEY_SERVER = "server"
        private const val KEY_TOPICS = "topics"
        private const val KEY_TOKEN = "token"

        // App-release announcements are published to the same "Family" topic
        // everyone already subscribes to (no dedicated topic/ACL needed) and
        // distinguished by this tag - see docs/proxy-notifications.md for the
        // publish command. The APK itself rides as the ntfy message's own
        // attachment, so no separate download endpoint is needed either.
        private const val UPDATE_TAG = "alfred_update"
        private const val UPDATE_BETA_TAG = "alfred_update_beta"
        private const val SYNC_TAG = "geofence_sync"
        private const val LOCATE_TAG = "location_request"
        private const val TRACK_TAG = "location_track"
        private const val TRACK_STOP_TAG = "location_track_stop"
        private const val NOTIF_SYNC_TAG = "notif_sync"
        private const val NOTIF_REPLY_TAG = "notif_reply"
        private const val RING_TAG = "ring_phone"
        private const val RING_STOP_TAG = "ring_phone_stop"
        private const val UPDATE_APK_FILENAME = "alfred-update.apk"

        fun start(context: Context) {
            ContextCompat.startForegroundService(context, Intent(context, NtfyClientService::class.java))
        }

        /** Idempotent; safe to call from any Activity's onCreate before the service exists. */
        fun ensureChannels(context: Context) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
            val nm = context.getSystemService(NotificationManager::class.java) ?: return
            // Drop the old LOW-importance service channel (its persistent
            // status-bar notification is what we're getting rid of).
            runCatching { nm.deleteNotificationChannel("ntfy_service") }
            nm.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_SERVICE, context.getString(R.string.assistant_name) + " notifications service", NotificationManager.IMPORTANCE_MIN,
                )
            )
            nm.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_MESSAGES, context.getString(R.string.assistant_name) + " messages", NotificationManager.IMPORTANCE_HIGH,
                )
            )
            nm.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_UPDATES, context.getString(R.string.assistant_name) + " app updates", NotificationManager.IMPORTANCE_HIGH,
                )
            )
            PhoneRinger.ensureChannel(context)
        }
    }

    private var httpClient: OkHttpClient? = null
    private var webSocket: WebSocket? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private val handler = Handler(Looper.getMainLooper())
    private var retryAttempt = 0
    private var stopping = false

    // True from the moment we start a connection attempt until it actually
    // closes/fails. Guards against onStartCommand's callers (MainActivity's
    // onCreate/onResume/permission-grant, all of which call start() liberally
    // and fire far more often than the connection actually needs replacing)
    // tearing down and reopening an already-healthy socket on every resume.
    @Volatile private var hasSocket = false

    // Needed to authenticate the APK attachment download (same bearer token
    // used for the WebSocket subscription - ntfy attachments inherit their
    // topic's read ACL).
    private var currentToken = ""
    private var pendingUpdateDownloadId = -1L
    private var downloadReceiverRegistered = false

    private val downloadReceiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context, intent: Intent) {
            val id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1L)
            if (id == -1L || id != pendingUpdateDownloadId) return
            pendingUpdateDownloadId = -1L
            postInstallReadyNotification()
        }
    }

    override fun onCreate() {
        super.onCreate()
        ensureChannels(this)
        startForeground(NOTIF_ID_SERVICE, buildServiceNotification(null))
        httpClient = OkHttpClient.Builder().pingInterval(30, TimeUnit.SECONDS).build()
        registerNetworkCallback()
        // EXPORTED: ACTION_DOWNLOAD_COMPLETE comes from the system DownloadManager
        // (a different UID); a NOT_EXPORTED receiver never fires on Android 13+, so
        // the "update ready" notification would never appear. Safe — it's a
        // protected broadcast other apps can't spoof.
        ContextCompat.registerReceiver(
            this, downloadReceiver, IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE),
            ContextCompat.RECEIVER_EXPORTED,
        )
        downloadReceiverRegistered = true
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!hasSocket) fetchConfigAndConnect()
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        stopping = true
        hasSocket = false
        handler.removeCallbacksAndMessages(null)
        webSocket?.cancel()
        networkCallback?.let { cb ->
            runCatching { connectivityManager()?.unregisterNetworkCallback(cb) }
        }
        if (downloadReceiverRegistered) {
            runCatching { unregisterReceiver(downloadReceiver) }
            downloadReceiverRegistered = false
        }
        super.onDestroy()
    }

    private fun connectivityManager() = getSystemService(ConnectivityManager::class.java)

    private fun registerNetworkCallback() {
        val cm = connectivityManager() ?: return
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                // Unlike onStartCommand's guard, a network change (e.g. wifi <->
                // cellular) really does warrant tearing down a possibly-stale
                // socket immediately rather than waiting for a ping timeout.
                retryAttempt = 0
                webSocket?.cancel()
                hasSocket = false
                handler.post { if (!stopping) fetchConfigAndConnect() }
            }
        }
        networkCallback = callback
        runCatching { cm.registerDefaultNetworkCallback(callback) }
    }

    // ---------------------------------------------------------------------
    // Config fetch — GET /api/ntfy-config, authenticated via the WebView's
    // own session cookie. The app has no separate credential store; this is
    // the same login the family already uses for chat.
    // ---------------------------------------------------------------------
    private fun fetchConfigAndConnect() {
        if (hasSocket) return
        hasSocket = true
        val client = httpClient ?: return
        val cookie = CookieManager.getInstance().getCookie(API_BASE)
        val reqBuilder = Request.Builder().url("$API_BASE/api/ntfy-config")
        if (!cookie.isNullOrBlank()) reqBuilder.addHeader("Cookie", cookie)

        client.newCall(reqBuilder.build()).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.w(TAG, "ntfy-config fetch failed, falling back to cache", e)
                useCachedConfigOrStop()
            }

            override fun onResponse(call: Call, response: Response) {
                response.use { resp ->
                    if (!resp.isSuccessful) {
                        if (resp.code == 401 || resp.code == 404) {
                            // Not logged in yet, or no ntfy config for this user - don't spin.
                            clearCache()
                            hasSocket = false
                            stopSelf()
                        } else {
                            useCachedConfigOrStop()
                        }
                        return
                    }
                    val body = resp.body?.string().orEmpty()
                    val json = try { JSONObject(body) } catch (e: Exception) { null }
                    if (json == null) {
                        useCachedConfigOrStop()
                        return
                    }
                    val server = json.optString("server")
                    val topics = json.optJSONArray("topics")?.let { arr ->
                        (0 until arr.length()).map { arr.getString(it) }
                    }.orEmpty()
                    val token = json.optString("token")
                    if (server.isBlank() || topics.isEmpty()) {
                        useCachedConfigOrStop()
                        return
                    }
                    cacheConfig(server, topics, token)
                    connect(server, topics, token)
                }
            }
        })
    }

    private fun useCachedConfigOrStop() {
        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val server = prefs.getString(KEY_SERVER, null)
        val topicsCsv = prefs.getString(KEY_TOPICS, null)
        if (server.isNullOrBlank() || topicsCsv.isNullOrBlank()) {
            hasSocket = false
            stopSelf()
            return
        }
        connect(server, topicsCsv.split(","), prefs.getString(KEY_TOKEN, "").orEmpty())
    }

    private fun cacheConfig(server: String, topics: List<String>, token: String) {
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
            .putString(KEY_SERVER, server)
            .putString(KEY_TOPICS, topics.joinToString(","))
            .putString(KEY_TOKEN, token)
            .apply()
    }

    private fun clearCache() {
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit().clear().apply()
    }

    // ---------------------------------------------------------------------
    // ntfy WebSocket — native multi-topic endpoint, e.g.
    // wss://ntfy.home/Alex,Parents,Family/ws
    // ---------------------------------------------------------------------
    private fun connect(server: String, topics: List<String>, token: String) {
        currentToken = token
        webSocket?.cancel()
        val wsUrl = server.trimEnd('/').replaceFirst(Regex("^http"), "ws") + "/${topics.joinToString(",")}/ws"
        val reqBuilder = Request.Builder().url(wsUrl)
        if (token.isNotBlank()) reqBuilder.addHeader("Authorization", "Bearer $token")

        val client = httpClient ?: return
        webSocket = client.newWebSocket(reqBuilder.build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                retryAttempt = 0
                updateServiceNotification(topics)
                // Reconcile OS geofences on every (re)connect. ntfy has no
                // offline replay (we subscribe without `since=`), so any
                // geofence_sync published while this socket was down is lost.
                // Re-syncing here guarantees the phone registers its current
                // geofence set within seconds of regaining connectivity —
                // without waiting for the user to reopen the app.
                AppLog.log(applicationContext, TAG, "ntfy connected → geofence resync")
                Thread { GeofenceManager.sync(applicationContext) }.start()
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleMessage(text)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                hasSocket = false
                if (!stopping) scheduleReconnect()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "ntfy websocket failure", t)
                AppLog.log(applicationContext, TAG, "ntfy websocket failure: ${t.message ?: t.javaClass.simpleName}")
                hasSocket = false
                if (!stopping) scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        val delayMs = min(60_000L, 2_000L shl min(retryAttempt, 5))
        retryAttempt++
        handler.postDelayed({ if (!stopping) fetchConfigAndConnect() }, delayMs)
    }

    private fun handleMessage(text: String) {
        val json = try { JSONObject(text) } catch (e: Exception) { return }
        if (json.optString("event") != "message") return // ignore "open"/"keepalive" frames
        val tags = json.optJSONArray("tags")?.let { arr -> (0 until arr.length()).map { arr.getString(it) } }.orEmpty()
        if (UPDATE_TAG in tags) {
            handleUpdateMessage(json)
            return
        }
        if (UPDATE_BETA_TAG in tags) {
            // Beta builds never auto-download or notify — they are delivered
            // through the in-app update badge only (chat header, ?channel=beta).
            // This branch just swallows the message so it is never shown.
            AppLog.log(applicationContext, TAG, "beta update message ignored (badge-only channel)")
            return
        }
        if (SYNC_TAG in tags) {
            // Control message from HomeCore: the user's geofence set changed.
            // Re-register silently; never shown as a notification.
            AppLog.log(applicationContext, TAG, "geofence_sync received → resync")
            Thread { GeofenceManager.sync(applicationContext) }.start()
            return
        }
        if (LOCATE_TAG in tags) {
            // "Where is X now": take one fresh fix and report it. Silent.
            AppLog.log(applicationContext, TAG, "locate request received")
            Thread { LocationReporter.reportOnce(applicationContext) }.start()
            return
        }
        if (TRACK_TAG in tags) {
            // Start a bounded tracking window. Body carries {until, interval}.
            val o = try { JSONObject(json.optString("message")) } catch (e: Exception) { JSONObject() }
            AppLog.log(applicationContext, TAG, "track request received")
            LocationReporter.startTrack(applicationContext, o.optLong("until", 0L), o.optInt("interval", 60))
            return
        }
        if (TRACK_STOP_TAG in tags) {
            AppLog.log(applicationContext, TAG, "track stop received")
            LocationReporter.stopTrack(applicationContext)
            return
        }
        if (RING_TAG in tags) {
            // "Find my phone": ring through silent/DND on the alarm stream.
            val o = try { JSONObject(json.optString("message")) } catch (e: Exception) { JSONObject() }
            AppLog.log(applicationContext, TAG, "ring request received")
            PhoneRinger.start(applicationContext, o.optInt("seconds", 45), o.optString("by"))
            return
        }
        if (RING_STOP_TAG in tags) {
            AppLog.log(applicationContext, TAG, "ring stop received")
            PhoneRinger.stop(applicationContext)
            return
        }
        if (NOTIF_SYNC_TAG in tags) {
            // The user changed which apps Alfred may read; reload the allowlist.
            AppLog.log(applicationContext, TAG, "notif_sync received")
            NotificationRelayService.syncAllowlist(applicationContext)
            return
        }
        if (NOTIF_REPLY_TAG in tags) {
            // Alfred answering a message through its own notification. Body is
            // {"key": "<notification key>", "text": "..."} — never shown.
            val o = try { JSONObject(json.optString("message")) } catch (e: Exception) { JSONObject() }
            val key = o.optString("key")
            val text = o.optString("text")
            AppLog.log(applicationContext, TAG, "notif_reply received")
            if (key.isNotBlank() && text.isNotBlank()) {
                Thread { NotificationRelayService.sendReply(applicationContext, key, text) }.start()
            }
            return
        }
        val title = json.optString("title").ifBlank { json.optString("topic") }
        val body = json.optString("message")
        // ntfy's own "click" field: where tapping should land. HomeCore fills it
        // with a chat_link() (optionally carrying ?prefill=/?welcome=/?date=/
        // ?panel=), so the tap opens the actual conversation the notification is
        // about instead of a blank new chat.
        postAlertNotification(title, body, json.optJSONArray("actions"), json.optString("click"))
    }

    // ---------------------------------------------------------------------
    // App updates — published as an ntfy attachment tagged "alfred_update"
    // (see docs/proxy-notifications.md). Downloads the APK in the background and
    // prompts to install once it's ready; no separate update-check endpoint.
    // ---------------------------------------------------------------------
    private fun handleUpdateMessage(json: JSONObject) {
        val apkUrl = json.optJSONObject("attachment")?.optString("url").orEmpty()
        if (apkUrl.isBlank()) {
            Log.w(TAG, "alfred_update message with no attachment, ignoring")
            return
        }
        val label = json.optString("title").ifBlank { json.optString("message") }.ifBlank { getString(R.string.assistant_name) }
        val request = DownloadManager.Request(Uri.parse(apkUrl)).apply {
            if (currentToken.isNotBlank()) addRequestHeader("Authorization", "Bearer $currentToken")
            setTitle(label)
            setDestinationInExternalFilesDir(this@NtfyClientService, "updates", UPDATE_APK_FILENAME)
            setNotificationVisibility(DownloadManager.Request.VISIBILITY_HIDDEN)
            setMimeType("application/vnd.android.package-archive")
        }
        val dm = getSystemService(DownloadManager::class.java) ?: return
        runCatching { File(getExternalFilesDir("updates"), UPDATE_APK_FILENAME).delete() }
        pendingUpdateDownloadId = dm.enqueue(request)
    }

    private fun postInstallReadyNotification() {
        val file = File(getExternalFilesDir("updates"), UPDATE_APK_FILENAME)
        if (!file.exists()) return
        val installIntent = ApkInstaller.buildInstallIntent(this, file)
        val pending = PendingIntent.getActivity(
            this, 2, installIntent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notif = NotificationCompat.Builder(this, CHANNEL_UPDATES)
            .setSmallIcon(R.drawable.ic_stat_ntfy)
            .setContentTitle("Actualización lista")
            .setContentText("Toca para instalar")
            .setAutoCancel(true)
            .setContentIntent(pending)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .build()
        runCatching { NotificationManagerCompat.from(this).notify(NOTIF_ID_UPDATE, notif) }
    }

    // ---------------------------------------------------------------------
    // Notifications
    // ---------------------------------------------------------------------
    private fun postAlertNotification(
        title: String,
        body: String,
        actions: JSONArray? = null,
        click: String = "",
    ) {
        // Building lives in AlertNotification so NotifyActionReceiver can put
        // this exact notification back after a button press — see it for why.
        AlertNotification.post(
            this, System.currentTimeMillis().toInt(),
            title, body, click, actions?.toString(),
        )
    }

    private fun buildServiceNotification(topics: List<String>?): Notification =
        NotificationCompat.Builder(this, CHANNEL_SERVICE)
            .setContentTitle(getString(R.string.assistant_name) + " notifications active")
            .setContentText(topics?.joinToString(", ") ?: "Connecting…")
            .setSmallIcon(R.drawable.ic_stat_ntfy)
            .setOngoing(true)
            .setShowWhen(false)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .build()

    private fun updateServiceNotification(topics: List<String>) {
        runCatching { NotificationManagerCompat.from(this).notify(NOTIF_ID_SERVICE, buildServiceNotification(topics)) }
    }
}
