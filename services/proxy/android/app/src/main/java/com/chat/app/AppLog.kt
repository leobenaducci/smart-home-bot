package com.chat.app

import android.content.Context
import android.util.Log
import android.webkit.CookieManager
import okhttp3.FormBody
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Tiny persistent on-device log so geofence/location/ntfy activity can be
 * inspected from the chat UI (Apps → 📜 Log) without adb logcat. Appends
 * timestamped lines to a ring-buffered file in filesDir, so entries written by
 * background receivers (app closed) survive until the user opens the panel.
 * Every entry is mirrored to logcat too. Writing must never crash the caller.
 *
 * Two levels, and the difference matters:
 *
 *  - [log] stays on the phone. Cheap, chatty, read by whoever is holding it.
 *  - [report] *also* ships the line to HomeCore (`POST /chat/applog`), where it
 *    can be read over HTTP at `/debug/applog` by someone who is not in the
 *    house. Use it for the handful of lines that decide a diagnosis.
 *
 * The split exists because the phone-only log could not answer the question it
 * was built for. On-device speech recognition failed every utterance for a
 * week; the assist overlay lives about two seconds, usually on a lock screen,
 * and the error code that explained it never left the device.
 */
object AppLog {
    private const val FILE_NAME = "applog.txt"
    private const val MAX_BYTES = 96 * 1024   // trim when the file grows past this
    private const val KEEP_BYTES = 64 * 1024  // size kept after a trim (oldest lines dropped)
    private val lock = Any()

    // --- upstream half ----------------------------------------------------
    private const val BASE = BuildConfig.CHAT_BASE_URL
    private const val PENDING_NAME = "applog-pending.txt"
    private const val PENDING_MAX_BYTES = 32 * 1024
    private const val FLUSH_DELAY_MS = 2500L  // coalesce a burst into one POST
    private val pendingLock = Any()
    private val flushQueued = AtomicBoolean(false)
    private val http by lazy { OkHttpClient.Builder().callTimeout(20, TimeUnit.SECONDS).build() }
    private val uploader by lazy {
        Executors.newSingleThreadScheduledExecutor { r ->
            Thread(r, "applog-upload").apply { isDaemon = true }
        }
    }

    private fun file(context: Context) = File(context.applicationContext.filesDir, FILE_NAME)

    private fun pendingFile(context: Context) =
        File(context.applicationContext.filesDir, PENDING_NAME)

    private fun formatLine(tag: String, msg: String, err: Throwable?): String {
        val suffix = if (err != null) " — ${err.javaClass.simpleName}: ${err.message}" else ""
        val ts = SimpleDateFormat("dd/MM HH:mm:ss", Locale.US).format(Date())
        return "$ts [$tag] $msg$suffix\n"
    }

    fun log(context: Context, tag: String, msg: String, err: Throwable? = null) {
        Log.d(tag, msg, err)
        val line = formatLine(tag, msg, err)
        synchronized(lock) {
            try {
                val f = file(context)
                f.appendText(line)
                if (f.length() > MAX_BYTES) {
                    val txt = f.readText()
                    var keep = txt.substring(txt.length - KEEP_BYTES)
                    val nl = keep.indexOf('\n')
                    if (nl >= 0) keep = keep.substring(nl + 1)
                    f.writeText(keep)
                }
            } catch (_: Exception) { }
        }
    }

    fun read(context: Context): String = synchronized(lock) {
        try {
            file(context).takeIf { it.exists() }?.readText() ?: ""
        } catch (e: Exception) { "" }
    }

    fun clear(context: Context) {
        synchronized(lock) { try { file(context).delete() } catch (_: Exception) { } }
    }

    // ---------------------------------------------------------------------
    // Reporting upstream
    // ---------------------------------------------------------------------

    /**
     * Like [log], but the line is also queued for HomeCore so it can be read
     * from outside the house. Reserve it for lines that decide something —
     * error codes, what a capability probe answered, which path was taken —
     * because the server end is size-capped and a chatty caller would push the
     * interesting lines out of the buffer.
     *
     * Never blocks and never throws: the upload is debounced onto a daemon
     * thread, and a phone with no signal simply keeps the lines until later.
     */
    fun report(context: Context, tag: String, msg: String, err: Throwable? = null) {
        val ctx = context.applicationContext
        log(ctx, tag, msg, err)
        synchronized(pendingLock) {
            try {
                val f = pendingFile(ctx)
                if (f.length() < PENDING_MAX_BYTES) f.appendText(formatLine(tag, msg, err))
            } catch (_: Exception) { }
        }
        scheduleFlush(ctx)
    }

    /** Push anything still pending now — worth calling when the app comes to the
     *  foreground, which is the moment a phone that was offline gets a network
     *  back and a session cookie that is certainly fresh. */
    fun flush(context: Context) {
        val ctx = context.applicationContext
        try { uploader.execute { flushNow(ctx) } } catch (_: Exception) { }
    }

    private fun scheduleFlush(ctx: Context) {
        if (!flushQueued.compareAndSet(false, true)) return  // one already pending
        try {
            // Explicit Runnable: schedule() is also overloaded for Callable, and
            // a bare lambda makes the two candidates ambiguous.
            uploader.schedule(Runnable {
                flushQueued.set(false)
                flushNow(ctx)
            }, FLUSH_DELAY_MS, TimeUnit.MILLISECONDS)
        } catch (e: Exception) {
            flushQueued.set(false)
        }
    }

    /**
     * Take everything pending and POST it. On any failure the lines go back at
     * the front, so a dropped connection costs nothing but a delay — bounded,
     * because a phone that is offline for a week must not fill its own storage
     * with its own complaints.
     */
    private fun flushNow(context: Context) {
        val ctx = context.applicationContext
        val f = pendingFile(ctx)
        val body = synchronized(pendingLock) {
            try {
                if (!f.exists()) return
                val text = f.readText()
                f.delete()
                text
            } catch (e: Exception) {
                return
            }
        }
        if (body.isBlank()) return
        var ok = false
        try {
            val cookie = CookieManager.getInstance().getCookie(BASE)
            if (cookie.isNullOrBlank()) {
                Log.d("AppLog", "applog upload skipped: no session cookie")
            } else {
                val req = Request.Builder()
                    .url("$BASE/chat/applog")
                    .header("Cookie", cookie)
                    .post(FormBody.Builder().add("lines", body).build())
                    .build()
                http.newCall(req).execute().use { ok = it.isSuccessful }
            }
        } catch (e: Exception) {
            Log.w("AppLog", "applog upload failed", e)
        }
        if (!ok) {
            synchronized(pendingLock) {
                try {
                    val rest = if (f.exists()) f.readText() else ""
                    var merged = body + rest
                    if (merged.length > PENDING_MAX_BYTES) {
                        merged = merged.takeLast(PENDING_MAX_BYTES)
                        // takeLast can cut mid-line; drop the fragment.
                        merged = merged.substringAfter('\n', merged)
                    }
                    f.writeText(merged)
                } catch (_: Exception) { }
            }
        }
    }
}
