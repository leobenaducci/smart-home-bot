package com.chat.app.ntfy

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.webkit.CookieManager
import android.widget.Toast
import androidx.core.app.NotificationManagerCompat
import com.chat.app.AppLog
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/**
 * Fires a notification action button (a task reminder's "Lista ✓" / "Más tarde",
 * or one of Alfred's own "Sí|No" answers) as an authenticated HTTP call to
 * HomeCore, using the WebView session cookie — so it works straight from the
 * notification shade without opening the app. See HomeCore
 * POST /tasks/api/notify-action and POST /chat/ask-answer.
 *
 * A press always changes the notification, immediately:
 *
 *  press → "⏳ Enviando «Lista ✓»…", buttons withdrawn
 *      → 2xx  : gone (or "✓ Lista ✓" for an action that asked to stay)
 *      → else : buttons back, with the reason on the notification itself
 *
 * The middle state is the point. Dismissing only on a 2xx is right — a button
 * that lies about having been pressed is worse than one that doesn't respond —
 * but the previous version expressed "we didn't manage it" by leaving the
 * notification untouched, which is exactly what a dead button looks like. The
 * reminder buttons were reported as "they don't do anything"; they were failing
 * and saying so only to a toast and a log file.
 */
class NotifyActionReceiver : BroadcastReceiver() {
    companion object {
        const val EXTRA_URL = "url"
        const val EXTRA_METHOD = "method"
        const val EXTRA_BODY = "body"
        const val EXTRA_NOTIF_ID = "notif_id"
        const val EXTRA_CLEAR = "clear"
        const val KEY_REPLY = "reply_text"
        // Enough of the original notification to put it back exactly as it was
        // if the server doesn't take the answer. Carried in the PendingIntent
        // rather than held in memory, because this receiver routinely runs in a
        // process the OS started for it, with no service and no state.
        const val EXTRA_LABEL = "label"
        const val EXTRA_TITLE = "title"
        const val EXTRA_TEXT = "text"
        const val EXTRA_CLICK = "click"
        const val EXTRA_ACTIONS = "actions"
        private const val TAG = "AlfredNotifyAction"
        private val http = OkHttpClient.Builder().callTimeout(20, TimeUnit.SECONDS).build()

        /**
         * The only paths a push is allowed to make us POST to. Exact matches,
         * no prefixes: [actionUrl] re-points every action at our own host, which
         * means it also attaches our session cookie, so this list — not the
         * host check that used to happen by accident — is what stops a crafted
         * ntfy message from firing an authenticated request at any endpoint it
         * likes. Adding a path here grants a push the right to call it.
         */
        private val ACTION_PATHS = setOf(
            "/tasks/api/notify-action",
            "/chat/ask-answer",
        )

        /**
         * Rewrites an action's URL onto the host the app is actually logged
         * into, exactly as MainActivity.deepLinkUrl does for the tap target.
         *
         * HomeCore builds these from HOMECORE_PUBLIC_URL, which is its Tailscale
         * address (https://hub.home:8443) unless someone set it otherwise —
         * so "Lista ✓" posted to a host that is unreachable on mobile data, and
         * on the tailnet got no Cookie header at all, because CookieManager
         * scopes the session cookie to chat.home. Both ways: 401 or a
         * timeout, and until recently the notification cleared itself anyway
         * and the task stayed pending. Only path + query survive the trip.
         *
         * Returns null when the URL is unparseable or off the allowlist; the
         * caller drops the action rather than guessing.
         */
        fun actionUrl(raw: String?): String? {
            if (raw.isNullOrBlank()) return null
            val uri = try { Uri.parse(raw) } catch (e: Exception) { return null }
            val path = uri.path ?: return null
            if (path !in ACTION_PATHS) return null
            val query = uri.encodedQuery
            return NtfyClientService.API_BASE.trimEnd('/') + path +
                (if (query.isNullOrBlank()) "" else "?$query")
        }
    }

    override fun onReceive(context: Context, intent: Intent) {
        val notifId = intent.getIntExtra(EXTRA_NOTIF_ID, 0)
        val label = intent.getStringExtra(EXTRA_LABEL).orEmpty()
        val original = Original(
            intent.getStringExtra(EXTRA_TITLE).orEmpty(),
            intent.getStringExtra(EXTRA_TEXT).orEmpty(),
            intent.getStringExtra(EXTRA_CLICK).orEmpty(),
            intent.getStringExtra(EXTRA_ACTIONS),
        )
        val raw = intent.getStringExtra(EXTRA_URL)
        val url = actionUrl(raw)
        if (url == null) {
            AppLog.report(context, TAG, "action REFUSED, url not allowed: $raw")
            restore(context, notifId, original, "⚠️ Esta opción no se pudo enviar (destino no permitido)")
            toast(context, "No se pudo registrar. Toca de nuevo.")
            return
        }
        // Acknowledge the press NOW. Everything below can take up to the call
        // timeout, and until this existed a press showed nothing at all in the
        // meantime — and, if the call then failed, nothing at all afterwards
        // either, since the notification is deliberately left alone. A button
        // that is merely slow and a button that is broken looked identical.
        // The buttons come off with it, so the same option can't be double-fired.
        sending(context, notifId, original, label)
        val method = (intent.getStringExtra(EXTRA_METHOD) ?: "POST").uppercase()
        var body = intent.getStringExtra(EXTRA_BODY) ?: ""
        val reply = androidx.core.app.RemoteInput.getResultsFromIntent(intent)
            ?.getCharSequence(KEY_REPLY)?.toString()
        if (!reply.isNullOrBlank()) {
            body = try {
                org.json.JSONObject(if (body.isBlank()) "{}" else body).put("note", reply).toString()
            } catch (e: Exception) { body }
        }
        val clear = intent.getBooleanExtra(EXTRA_CLEAR, true)
        val pending = goAsync() // keep the receiver alive across the async call
        Thread {
            var ok = false
            var detail = ""
            try {
                val cookie = CookieManager.getInstance().getCookie(url)
                if (cookie.isNullOrBlank()) detail = "no session cookie"
                val reqBody = if (method == "GET") null
                              else body.toRequestBody("application/json".toMediaType())
                val builder = Request.Builder().url(url).method(method, reqBody)
                if (!cookie.isNullOrBlank()) builder.header("Cookie", cookie)
                http.newCall(builder.build()).execute().use { resp ->
                    ok = resp.isSuccessful
                    if (!ok) detail = "HTTP ${resp.code}"
                }
            } catch (e: Exception) {
                detail = e.javaClass.simpleName + ": " + (e.message ?: "")
                Log.e(TAG, "action failed", e)
            } finally {
                // Dismiss ONLY once the server actually took it. This used to
                // live in `finally` unconditionally, so a 401, a timeout or a
                // missing cookie made the notification disappear exactly as if
                // it had worked — the task stayed pending and nothing said so.
                // A button that lies about having been pressed is worse than
                // one that doesn't respond.
                if (ok && clear) {
                    if (notifId != 0) {
                        runCatching { NotificationManagerCompat.from(context).cancel(notifId) }
                    }
                } else if (ok) {
                    // Took it, but this action asked to stay put (clear:false).
                    // It must not be left sitting on "Enviando…" forever.
                    restore(context, notifId, original, if (label.isBlank()) "✓ Enviado" else "✓ $label")
                } else {
                    // Give the buttons back with the reason attached, so the
                    // press is visibly undone and can simply be repeated.
                    restore(context, notifId, original, "⚠️ No se pudo enviar ($detail). Toca de nuevo.")
                }
                // The URL goes in the line on purpose. A session cookie is
                // scoped to a host, so an action pointing at a different origin
                // than the one the WebView is logged into gets no cookie and a
                // 401 — and the two origins look identical in a screenshot.
                if (!ok) {
                    AppLog.report(context, TAG,
                        "action FAILED ($detail) url=$url body=$body — buttons put back")
                    toast(context, "No se pudo registrar. Toca de nuevo.")
                } else {
                    AppLog.report(context, TAG, "action ok, notification cleared: url=$url body=$body")
                }
                pending.finish()
            }
        }.start()
    }

    /** The notification this button belongs to, as posted. */
    private data class Original(
        val title: String,
        val text: String,
        val click: String,
        val actionsJson: String?,
    )

    /** In-flight state: the answer being sent, buttons withdrawn. */
    private fun sending(context: Context, notifId: Int, o: Original, label: String) {
        if (notifId == 0 || (o.text.isBlank() && o.title.isBlank())) return
        AlertNotification.post(
            context, notifId, o.title, o.text, o.click, o.actionsJson,
            statusLine = if (label.isBlank()) "⏳ Enviando…" else "⏳ Enviando «$label»…",
            withActions = false,
        )
    }

    /** Put the notification back with its buttons, plus a line saying what
     *  happened. Silent (setOnlyAlertOnce), so it changes under the user's eyes
     *  without buzzing at them again. */
    private fun restore(context: Context, notifId: Int, o: Original, status: String) {
        if (notifId == 0 || (o.text.isBlank() && o.title.isBlank())) return
        AlertNotification.post(
            context, notifId, o.title, o.text, o.click, o.actionsJson, statusLine = status,
        )
    }

    /** Receivers have no UI of their own; the notification is the real signal,
     *  and this just says why. Main thread, or it never shows. */
    private fun toast(context: Context, text: String) {
        runCatching {
            Handler(Looper.getMainLooper()).post {
                Toast.makeText(context.applicationContext, text, Toast.LENGTH_SHORT).show()
            }
        }
    }
}
