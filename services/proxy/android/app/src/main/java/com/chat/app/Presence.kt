package com.chat.app

import android.content.Context
import android.webkit.CookieManager
import com.chat.app.ntfy.NtfyClientService
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Tells HomeCore whether Alfred is actually in front of the user.
 *
 * The chat page reports this too, from `document.visibilityState` — but that
 * is a statement about a WebView's view hierarchy, and the "hidden" report has
 * to leave the device while the process is already on its way to the
 * background, which it does not reliably manage. A report that never arrives
 * leaves the user marked as watching for the server's whole threshold, and
 * every reply landing inside that window is deliberately not pushed. That is
 * the shape of "I asked Alfred something, locked the phone, and never heard
 * back".
 *
 * The activity knows its own lifecycle exactly. This is that, said out loud.
 * It does not replace the page's ping — the page is still what keeps a
 * long-open chat marked as watched — it just makes the transitions honest.
 */
object Presence {

    private const val TAG = "AlfredPresence"
    private val http = OkHttpClient.Builder().callTimeout(10, TimeUnit.SECONDS).build()
    private val io = Executors.newSingleThreadExecutor { r ->
        Thread(r, "presence").apply { isDaemon = true }
    }

    fun report(context: Context, visible: Boolean) {
        val app = context.applicationContext
        io.execute {
            val url = NtfyClientService.API_BASE.trimEnd('/') + "/chat/presence-native"
            // Scoped to the host we are logged into, same as every other
            // background call here — see NotifyActionReceiver.actionUrl.
            val cookie = CookieManager.getInstance().getCookie(url)
            if (cookie.isNullOrBlank()) return@execute  // never logged in: nothing to say
            val body = "{\"visible\":$visible}".toRequestBody("application/json".toMediaType())
            try {
                http.newCall(
                    Request.Builder().url(url).header("Cookie", cookie).post(body).build(),
                ).execute().use { resp ->
                    if (!resp.isSuccessful) {
                        AppLog.report(app, TAG, "presence visible=$visible → HTTP ${resp.code}")
                    }
                }
            } catch (e: Exception) {
                // Deliberately quiet. Presence is soft state that expires on
                // its own, the page pings it too, and logging every flaky
                // moment would push the interesting lines out of the applog.
            }
        }
    }
}
