package com.chat.app.family

import android.webkit.CookieManager
import com.chat.app.ntfy.NtfyClientService
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * The portal's /family-chat/api, with the WebView's session cookie -- the same
 * way the notification buttons reach HomeCore (NotifyActionReceiver). Every
 * call answers null on any failure: this is the path that has to degrade to
 * SMS, so a timeout is an answer, not an exception.
 */
object FamilyApi {
    private val http = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .callTimeout(15, TimeUnit.SECONDS)
        .build()
    private val JSON = "application/json".toMediaType()

    private fun url(path: String) = NtfyClientService.API_BASE.trimEnd('/') + path

    private fun request(path: String, body: JSONObject?): JSONObject? {
        val u = url(path)
        val b = Request.Builder().url(u)
        CookieManager.getInstance().getCookie(u)?.takeIf { it.isNotBlank() }?.let { b.header("Cookie", it) }
        if (body != null) b.post(body.toString().toRequestBody(JSON))
        return try {
            http.newCall(b.build()).execute().use { r ->
                if (!r.isSuccessful) null else JSONObject(r.body?.string().orEmpty().ifBlank { "{}" })
            }
        } catch (e: Exception) {
            null
        }
    }

    fun get(path: String): JSONObject? = request(path, null)
    fun post(path: String, body: JSONObject): JSONObject? = request(path, body)
}
