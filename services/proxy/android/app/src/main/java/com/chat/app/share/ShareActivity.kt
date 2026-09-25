package com.chat.app.share

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.OpenableColumns
import android.webkit.CookieManager
import android.webkit.MimeTypeMap
import android.widget.Toast
import com.chat.app.AppLog
import com.chat.app.MainActivity
import com.chat.app.ntfy.NtfyClientService
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread
import com.chat.app.R

/**
 * "Compartir → Alfred" from anywhere on the phone.
 *
 * Two shapes arrive here and they are handled differently on purpose:
 *
 *  - **Text** (a link from Chrome, a selection, a note) goes straight into the
 *    chat input as a prefill. It is already text; there is nothing to extract,
 *    and putting it in the box rather than sending it lets the person say what
 *    they want done with it. Sharing a URL and having Alfred immediately answer
 *    something about it is worse than being asked.
 *  - **Files** are uploaded to HomeCore, which extracts their text
 *    (POST /chat/share) and answers with an id. The chat then opens at
 *    /chat?doc=<id> and draws the attachment chip. Extraction has to happen
 *    server-side — Alfred answers with a text model — and it happens *now*
 *    rather than at send so a scanned PDF is reported while the person is still
 *    holding the phone.
 *
 * This activity is translucent and short-lived: it does the upload, then hands
 * off to MainActivity. It deliberately does NOT send anything by itself. A
 * share is "here, look at this", not "go do something with this", and Alfred
 * holds tools — the person gets to type the actual instruction.
 */
class ShareActivity : Activity() {

    companion object {
        private const val TAG = "AlfredShare"
        private const val MAX_BYTES = 20L * 1024 * 1024   // matches DOC_MAX_UPLOAD
        private const val MAX_FILES = 5                   // matches DOC_MAX_PER_MESSAGE
        private val http = OkHttpClient.Builder().callTimeout(120, TimeUnit.SECONDS).build()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val incoming = intent
        // Explicit type argument, not a cast: getParcelableExtra is generic and
        // an `as?` on the result gives the compiler nothing to infer T from.
        val uris: List<Uri> = when (incoming?.action) {
            Intent.ACTION_SEND ->
                listOfNotNull(incoming.getParcelableExtra<Uri>(Intent.EXTRA_STREAM))
            Intent.ACTION_SEND_MULTIPLE ->
                incoming.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM).orEmpty()
            else -> emptyList()
        }
        val text = incoming?.getStringExtra(Intent.EXTRA_TEXT)

        when {
            uris.isNotEmpty() -> uploadThenOpen(uris.take(MAX_FILES))
            // Only when there is no file: a share can carry both (an image with
            // a caption), and the file is the part that needs handling.
            !text.isNullOrBlank() -> {
                openChat("/chat?prefill=" + Uri.encode(shareText(text)))
                finish()
            }
            else -> {
                toast("No había nada que compartir.")
                finish()
            }
        }
    }

    /** A subject is worth keeping when it isn't already the body — Gmail and a
     *  few others put the useful half there and repeat the URL below. */
    private fun shareText(text: String): String {
        val subject = intent?.getStringExtra(Intent.EXTRA_SUBJECT)?.trim()
        return if (!subject.isNullOrBlank() && !text.contains(subject)) "$subject\n$text" else text
    }

    private fun uploadThenOpen(uris: List<Uri>) {
        val cookie = CookieManager.getInstance().getCookie(NtfyClientService.API_BASE)
        if (cookie.isNullOrBlank()) {
            // No session means the WebView has never logged in on this device.
            // Opening the app is the only useful thing to do with the share.
            toast("Open " + getString(R.string.assistant_name) + " and sign in, then share again.")
            openChat("/chat")
            finish()
            return
        }
        val who = getString(R.string.assistant_name)
        toast(if (uris.size == 1) "Sending to $who…" else "Sending ${uris.size} files to $who…")
        thread(isDaemon = true) {
            val ids = mutableListOf<String>()
            // uploadOne explains its own failures — it has the server's Spanish
            // message and the filename, which is more than anything out here
            // knows. Only a thrown exception needs reporting from this level.
            var thrown: String? = null
            for (uri in uris) {
                val id = try {
                    uploadOne(uri, cookie)
                } catch (e: Exception) {
                    AppLog.report(this, TAG, "share upload threw", e)
                    thrown = "No pude enviar el archivo (${e.javaClass.simpleName})."
                    null
                }
                if (id != null) ids.add(id)
            }
            Handler(Looper.getMainLooper()).post {
                if (ids.isEmpty()) {
                    thrown?.let { toast(it) }
                } else {
                    openChat("/chat?" + ids.joinToString("&") { "doc=" + Uri.encode(it) })
                }
                finish()
            }
        }
    }

    /**
     * Uploads one shared file and returns its document id, or null with the
     * server's own Spanish explanation surfaced as a toast — those messages are
     * written to be read ("¿es un PDF escaneado?"), so passing them through
     * beats replacing them with "error".
     */
    private fun uploadOne(uri: Uri, cookie: String): String? {
        val name = displayName(uri)
        val size = fileSize(uri)
        if (size != null && size > MAX_BYTES) {
            postToast("\"$name\" pesa más de ${MAX_BYTES / (1024 * 1024)} MB.")
            return null
        }
        val bytes = contentResolver.openInputStream(uri)?.use { it.readBytes() }
        if (bytes == null || bytes.isEmpty()) {
            postToast("No pude leer \"$name\".")
            return null
        }
        if (bytes.size > MAX_BYTES) {   // size was unknown up front
            postToast("\"$name\" pesa más de ${MAX_BYTES / (1024 * 1024)} MB.")
            return null
        }
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart(
                "file", name,
                bytes.toRequestBody("application/octet-stream".toMediaType()),
            )
            .build()
        val req = Request.Builder()
            .url(NtfyClientService.API_BASE.trimEnd('/') + "/chat/share")
            .header("Cookie", cookie)
            .post(body)
            .build()
        http.newCall(req).execute().use { resp ->
            val payload = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) {
                val why = runCatching { JSONObject(payload).optString("error") }.getOrNull()
                AppLog.report(this, TAG, "share upload failed HTTP ${resp.code} for $name: $why")
                postToast(if (why.isNullOrBlank()) "No pude enviar \"$name\" (HTTP ${resp.code})." else why)
                return null
            }
            val id = runCatching { JSONObject(payload).optString("id") }.getOrNull()
            if (id.isNullOrBlank()) {
                AppLog.report(this, TAG, "share upload gave no id for $name")
                return null
            }
            AppLog.report(this, TAG, "shared $name → doc $id")
            return id
        }
    }

    /** The filename matters: HomeCore dispatches extraction on the extension, so
     *  a document arriving as "content://…/1234" cannot be read at all. Fall
     *  back to the MIME type when the provider offers no display name. */
    private fun displayName(uri: Uri): String {
        val fromProvider = runCatching {
            contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
                ?.use { c ->
                    val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (i >= 0 && c.moveToFirst()) c.getString(i) else null
                }
        }.getOrNull()
        if (!fromProvider.isNullOrBlank()) return fromProvider
        val ext = contentResolver.getType(uri)
            ?.let { MimeTypeMap.getSingleton().getExtensionFromMimeType(it) }
        return if (ext.isNullOrBlank()) "compartido" else "compartido.$ext"
    }

    private fun fileSize(uri: Uri): Long? = runCatching {
        contentResolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { c ->
            val i = c.getColumnIndex(OpenableColumns.SIZE)
            if (i >= 0 && c.moveToFirst() && !c.isNull(i)) c.getLong(i) else null
        }
    }.getOrNull()

    private fun openChat(path: String) {
        startActivity(Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra(MainActivity.EXTRA_DEEP_LINK, true)
            putExtra(MainActivity.EXTRA_OPEN_URL, path)
        })
    }

    private fun postToast(text: String) = Handler(Looper.getMainLooper()).post { toast(text) }

    private fun toast(text: String) {
        runCatching { Toast.makeText(applicationContext, text, Toast.LENGTH_LONG).show() }
    }
}
