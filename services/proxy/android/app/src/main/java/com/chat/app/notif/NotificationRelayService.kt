package com.chat.app.notif

import com.chat.app.BuildConfig

import android.app.Notification
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import android.webkit.CookieManager
import com.chat.app.AppLog
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/**
 * Relays other apps' notifications to HomeCore so Alfred can act on them, and
 * answers them through the notification's own reply action.
 *
 * Two things make this safe enough to run on a family phone:
 *
 *  - **The user grants it explicitly**, once, in Android's "Notification
 *    access" screen. Nothing here works until they do.
 *  - **Content never leaves the phone for an app the user hasn't allowed.** The
 *    allowlist is fetched from HomeCore and cached; for anything not on it we
 *    send the app's identity alone (package + label) so it can be offered in
 *    the settings list, and drop the title and text on the floor.
 *
 * Replying works the same way Android Auto and smartwatches do it: a
 * notification carries its own reply Action with a RemoteInput, and we fire it.
 * That means we can only answer while the notification is still live — Android
 * revokes the PendingIntent when the app dismisses it — which is why the server
 * ages replies out after an hour.
 */
class NotificationRelayService : NotificationListenerService() {

    private data class ReplyTarget(
        val pendingIntent: PendingIntent,
        val remoteInputs: Array<RemoteInput>,
    )

    companion object {
        private const val TAG = "AlfredNotif"
        private const val BASE = BuildConfig.CHAT_BASE_URL
        private const val PREFS = "alfred"
        private const val KEY_ALLOW = "notif_allow"      // CSV of allowed packages
        private const val KEY_ALLOW_AT = "notif_allow_at"
        private const val ALLOW_MAX_AGE_MS = 15L * 60 * 1000
        private const val DEDUPE_MAX = 60
        private const val DEDUPE_WINDOW_MS = 120_000L  // matches the server's window

        private val http = OkHttpClient.Builder()
            .callTimeout(20, TimeUnit.SECONDS)
            .build()
        private val JSON = "application/json; charset=utf-8".toMediaType()

        /** notification key → its live reply action. Cleared when it's dismissed. */
        private val replyTargets = ConcurrentHashMap<String, ReplyTarget>()

        /** Recently relayed "key|text" hashes — Android reposts a notification
         *  on every trivial update and we don't want Alfred woken each time. */
        private val recentlySent = object : LinkedHashMap<String, Long>(16, 0.75f, true) {
            override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Long>?) =
                size > DEDUPE_MAX
        }

        fun isEnabled(ctx: Context): Boolean = try {
            val flat = Settings.Secure.getString(
                ctx.contentResolver, "enabled_notification_listeners",
            ) ?: ""
            flat.split(':').any {
                ComponentName.unflattenFromString(it)?.packageName == ctx.packageName
            }
        } catch (e: Exception) {
            false
        }

        /** Android's "Notification access" screen — the only way to grant this. */
        fun settingsIntent(): Intent =
            Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

        /** Pull the allowed-package list from HomeCore. Cheap; called on connect,
         *  on a notif_sync push, and whenever the cache goes stale. */
        fun syncAllowlist(ctx: Context) {
            Thread {
                try {
                    val cookie = CookieManager.getInstance().getCookie(BASE)
                    val b = Request.Builder().url("$BASE/chat/notifications/allowlist")
                    if (!cookie.isNullOrBlank()) b.header("Cookie", cookie)
                    http.newCall(b.build()).execute().use { resp ->
                        if (!resp.isSuccessful) return@use
                        val arr = JSONObject(resp.body?.string().orEmpty())
                            .optJSONArray("packages") ?: return@use
                        val pkgs = (0 until arr.length()).map { arr.getString(it) }
                        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                            .putString(KEY_ALLOW, pkgs.joinToString(","))
                            .putLong(KEY_ALLOW_AT, System.currentTimeMillis())
                            .apply()
                        AppLog.log(ctx, TAG, "allowlist: ${pkgs.size} app(s)")
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "allowlist sync failed", e)
                }
            }.start()
        }

        private fun allowed(ctx: Context): Set<String> {
            val p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            if (System.currentTimeMillis() - p.getLong(KEY_ALLOW_AT, 0L) > ALLOW_MAX_AGE_MS) {
                syncAllowlist(ctx)  // refresh in the background; use what we have now
            }
            return (p.getString(KEY_ALLOW, "") ?: "")
                .split(',').map { it.trim() }.filter { it.isNotEmpty() }.toSet()
        }

        /**
         * Fire a notification's reply action. Called from NtfyClientService when
         * the server pushes a `notif_reply` control message.
         */
        fun sendReply(ctx: Context, key: String, text: String): Boolean {
            val target = replyTargets[key]
            if (target == null) {
                AppLog.log(ctx, TAG, "reply: notification $key is gone")
                return false
            }
            return try {
                val intent = Intent()
                val results = Bundle()
                for (ri in target.remoteInputs) results.putCharSequence(ri.resultKey, text)
                RemoteInput.addResultsToIntent(target.remoteInputs, intent, results)
                // Some apps ignore a reply that doesn't say where it came from.
                // Both the method and the constant are API 28.
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    RemoteInput.setResultsSource(intent, RemoteInput.SOURCE_FREE_FORM_INPUT)
                }
                target.pendingIntent.send(ctx, 0, intent)
                AppLog.log(ctx, TAG, "replied to $key")
                true
            } catch (e: Exception) {
                Log.w(TAG, "reply failed", e)
                AppLog.log(ctx, TAG, "reply failed: ${e.message}")
                false
            }
        }
    }

    override fun onListenerConnected() {
        super.onListenerConnected()
        AppLog.log(applicationContext, TAG, "notification access connected")
        syncAllowlist(applicationContext)
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification?) {
        super.onNotificationRemoved(sbn)
        sbn?.key?.let { replyTargets.remove(it) }
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        val n = sbn?.notification ?: return
        if (sbn.packageName == packageName) return          // our own pushes
        if (!isRelayable(sbn, n)) return

        val ctx = applicationContext
        val pkg = sbn.packageName
        val label = appLabel(pkg)

        // Identity only for apps the user hasn't allowed: the server needs to
        // know the app exists to offer it in the settings list, and that is all
        // it gets. No title, no text, ever.
        if (pkg !in allowed(ctx)) {
            post(ctx, JSONObject().put("package", pkg).put("label", label))
            return
        }

        val title = text(n.extras, Notification.EXTRA_TITLE)
        val body = messageText(n)
        if (title.isBlank() && body.isBlank()) return

        // Same content again? Android reposts on every update to a notification.
        // Time-bounded on purpose: saying "ok" twice in a conversation is a real
        // second message, saying it twice in five seconds is Android repeating
        // itself.
        val fingerprint = "${sbn.key}|$title|$body"
        val now = System.currentTimeMillis()
        synchronized(recentlySent) {
            val last = recentlySent.put(fingerprint, now)
            if (last != null && now - last < DEDUPE_WINDOW_MS) return
        }

        val reply = findReplyAction(n)
        if (reply != null) replyTargets[sbn.key] = reply

        post(ctx, JSONObject()
            .put("package", pkg)
            .put("label", label)
            .put("title", title)
            .put("text", body)
            .put("key", sbn.key)
            .put("can_reply", reply != null))
    }

    /** Skip the things that are noise rather than messages. */
    private fun isRelayable(sbn: StatusBarNotification, n: Notification): Boolean {
        if (sbn.isOngoing) return false
        val flags = n.flags
        if (flags and Notification.FLAG_ONGOING_EVENT != 0) return false
        if (flags and Notification.FLAG_GROUP_SUMMARY != 0) return false  // the children carry the content
        if (flags and Notification.FLAG_LOCAL_ONLY != 0) return false     // not meant to leave the device
        return when (n.category) {
            Notification.CATEGORY_PROGRESS, Notification.CATEGORY_SERVICE,
            Notification.CATEGORY_TRANSPORT, Notification.CATEGORY_SYSTEM,
            Notification.CATEGORY_NAVIGATION -> false
            else -> true
        }
    }

    private fun text(extras: Bundle, key: String): String =
        (extras.getCharSequence(key) ?: "").toString().trim()

    /**
     * The most complete text available: MessagingStyle's newest message if the
     * app uses it, then EXTRA_BIG_TEXT (the expanded view), then EXTRA_TEXT.
     */
    private fun messageText(n: Notification): String {
        val extras = n.extras
        val messages = extras.getParcelableArray(Notification.EXTRA_MESSAGES)
        if (messages != null && messages.isNotEmpty()) {
            val last = messages.last() as? Bundle
            val t = last?.getCharSequence("text")?.toString()?.trim().orEmpty()
            val sender = last?.getCharSequence("sender")?.toString()?.trim().orEmpty()
            if (t.isNotEmpty()) return if (sender.isNotEmpty()) "$sender: $t" else t
        }
        val big = text(extras, Notification.EXTRA_BIG_TEXT)
        if (big.isNotEmpty()) return big
        return text(extras, Notification.EXTRA_TEXT)
    }

    /** The notification's own "Reply" action, if it has one. */
    private fun findReplyAction(n: Notification): ReplyTarget? {
        val actions = n.actions ?: return null
        var fallback: ReplyTarget? = null
        for (a in actions) {
            val inputs = a.remoteInputs?.filter { it.allowFreeFormInput }?.toTypedArray()
            if (inputs.isNullOrEmpty()) continue
            val target = ReplyTarget(a.actionIntent ?: continue, inputs)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P &&
                a.semanticAction == Notification.Action.SEMANTIC_ACTION_REPLY
            ) {
                return target
            }
            if (fallback == null) fallback = target
        }
        return fallback
    }

    private fun appLabel(pkg: String): String = try {
        packageManager.getApplicationLabel(
            packageManager.getApplicationInfo(pkg, 0),
        ).toString()
    } catch (e: PackageManager.NameNotFoundException) {
        pkg  // not visible to us — the package name is still recognizable enough
    } catch (e: Exception) {
        pkg
    }

    private fun post(ctx: Context, payload: JSONObject) {
        Thread {
            try {
                val cookie = CookieManager.getInstance().getCookie(BASE)
                val b = Request.Builder().url("$BASE/chat/notification")
                    .post(payload.toString().toRequestBody(JSON))
                if (!cookie.isNullOrBlank()) b.header("Cookie", cookie)
                http.newCall(b.build()).execute().use { resp ->
                    if (!resp.isSuccessful) Log.w(TAG, "relay HTTP ${resp.code}")
                }
            } catch (e: Exception) {
                Log.w(TAG, "relay failed", e)
            }
        }.start()
    }
}
