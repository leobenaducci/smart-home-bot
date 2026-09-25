package com.chat.app.assist

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.speech.RecognitionService
import android.speech.RecognitionSupport
import android.speech.RecognitionSupportCallback
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log
import androidx.annotation.RequiresApi
import com.chat.app.AppLog
import java.util.Locale
import java.util.concurrent.Executor
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Picks *how* Alfred should turn speech into text on this phone, without ever
 * touching the microphone. The answer has to be known before capture starts,
 * because a SpeechRecognizer and a MediaRecorder cannot both hold the mic —
 * once the user is talking it is too late to change our minds.
 *
 * Ladder: on-device recognition (instant, offline) → a third-party network
 * recognizer → nothing, meaning [AssistActivity] records audio and HomeCore
 * transcribes it with Whisper as it always has.
 *
 * Failures are remembered as *expiry timestamps*, never as plain booleans, so a
 * phone that later downloads the Spanish language pack heals itself instead of
 * being written off forever.
 */
object SttSupport {

    private const val TAG = "AlfredVoice"
    private const val PREFS = "alfred" // shared with MainActivity's beta_channel

    const val KEY_MODE = "stt_mode" // auto | ondevice | network | whisper
    private const val KEY_ONDEVICE_UNTIL = "stt_ondevice_until"
    private const val KEY_NET_UNTIL = "stt_net_until"
    private const val LANG_BAD = "stt_lang_bad_"

    // What the recognizer answered when asked what it can do (see [probeSupport]).
    private const val KEY_SUPPORT_AT = "stt_support_at"     // when we last asked
    private const val KEY_SUPPORT_LANG = "stt_support_lang" // installed tag to use, "" = none
    private const val KEY_SUPPORT_DESC = "stt_support_desc" // the answer, for humans
    private const val KEY_NOMATCH = "stt_nomatch_"          // + ondevice|network
    private const val KEY_OK = "stt_ok_"                    // + ondevice|network: ever transcribed

    const val DISABLE_LONG_MS = 7L * 24 * 60 * 60 * 1000  // hard failure: a week
    const val DISABLE_SHORT_MS = 60L * 60 * 1000          // network hiccup: an hour
    const val DISABLE_LANG_MS = 30L * 24 * 60 * 60 * 1000 // missing language pack
    const val DISABLE_NOMATCH_MS = 24L * 60 * 60 * 1000   // understands nothing: a day

    /** Re-ask what the recognizer supports about once a day; a language pack can
     *  finish downloading, and a system update can take one away. */
    private const val SUPPORT_TTL_MS = 24L * 60 * 60 * 1000

    /** Consecutive "understood nothing" results before a path that has worked
     *  here before is set aside. One is an utterance you mumbled; three in a row
     *  is the phone. A path that has never worked here is set aside after one —
     *  see [noteNoMatch]. */
    private const val NOMATCH_LIMIT = 3

    /** Spanish first — this house speaks Spanish and Alfred answers in it. */
    private val LANG_LADDER = listOf("es", "es-ES", "es")

    /** Google's recognizers, best first. Anything else found is used as a last resort. */
    private val PREFERRED_PACKAGES = listOf(
        "com.google.android.googlequicksearchbox",
        "com.google.android.as",
        "com.google.android.tts",
    )

    sealed class Choice {
        /** System on-device recognizer (API 31+). No network involved. */
        object OnDevice : Choice()
        /** A specific third-party RecognitionService — never our own stub. */
        data class Network(val component: ComponentName) : Choice()
        /** Record audio and let HomeCore/Whisper do it. */
        object None : Choice()
    }

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /**
     * Default is still `whisper`. On-device recognition is no longer *broken by
     * construction* — [probeSupport] asks what the recognizer has instead of
     * assuming, [noteNoMatch] stops it failing forever in silence — but nothing
     * here has been watched working on a real phone yet, and flipping the
     * default changes voice for five people at once.
     *
     * So: leave it, put one phone on 🎙️ Voz → "en el teléfono" or "automático",
     * and read what it says. The interesting lines now reach HomeCore by
     * themselves (`AppLog.report` → `/chat/applog` → `GET /debug/applog`) —
     * no adb, no standing in front of the phone. Flip this to `auto` once the
     * `support probe:` line shows an installed language and an utterance comes
     * back with text.
     */
    fun mode(ctx: Context): String = prefs(ctx).getString(KEY_MODE, "whisper") ?: "whisper"

    fun setMode(ctx: Context, mode: String) {
        prefs(ctx).edit().putString(KEY_MODE, mode).apply()
    }

    /** Wipes every remembered failure — the "it should work by now" button. */
    fun clearState(ctx: Context) {
        val e = prefs(ctx).edit()
        prefs(ctx).all.keys.filter { it.startsWith("stt_") && it != KEY_MODE }
            .forEach { e.remove(it) }
        e.apply()
    }

    private fun blocked(ctx: Context, key: String) =
        prefs(ctx).getLong(key, 0L) > System.currentTimeMillis()

    private fun block(ctx: Context, key: String, forMs: Long) {
        prefs(ctx).edit().putLong(key, System.currentTimeMillis() + forMs).apply()
    }

    fun disableOnDevice(ctx: Context, forMs: Long) = block(ctx, KEY_ONDEVICE_UNTIL, forMs)

    fun disableNetwork(ctx: Context, forMs: Long) = block(ctx, KEY_NET_UNTIL, forMs)

    // --- language ---------------------------------------------------------

    private fun langBad(ctx: Context, tag: String) = blocked(ctx, LANG_BAD + tag)

    fun markLanguageBad(ctx: Context, tag: String) = block(ctx, LANG_BAD + tag, DISABLE_LANG_MS)

    /** First tag on the ladder this phone hasn't already rejected. */
    fun languageTag(ctx: Context): String =
        LANG_LADDER.firstOrNull { !langBad(ctx, it) } ?: Locale.getDefault().toLanguageTag()

    /** The tag to retry with after [current] was rejected, or null when exhausted. */
    fun nextLanguageAfter(ctx: Context, current: String): String? {
        val i = LANG_LADDER.indexOf(current)
        if (i >= 0) {
            LANG_LADDER.drop(i + 1).firstOrNull { !langBad(ctx, it) }?.let { return it }
        }
        val fallback = Locale.getDefault().toLanguageTag()
        return if (fallback != current && !langBad(ctx, fallback)) fallback else null
    }

    // --- what the recognizer can actually do ------------------------------

    /**
     * Asks the on-device recognizer which languages it *has*, and remembers it.
     *
     * This is the question that was never asked, and not asking it is what made
     * every utterance come back "no te entendí" for a week.
     * `isOnDeviceRecognitionAvailable()` — the check that used to stand alone —
     * answers "does this phone have an on-device recognition service", not "can
     * it understand Spanish". A phone with the service and no Spanish model
     * passes it, then fails every single utterance, and the failure it reports
     * is ERROR_NO_MATCH: indistinguishable, to the old code, from a person
     * mumbling. So it was retried on the next press, and the next, forever.
     *
     * Two things come out of the answer:
     *  - the tag to actually pass to the recognizer, taken from what it says is
     *    installed. `es` is on our ladder and is not a locale any on-device
     *    model ships as; `es-US` or `es-ES` is what a phone really has, and a
     *    Chilean talking to either is fine. (HomeCore learned the same thing from
     *    the other side — faster-whisper rejects `es` outright.)
     *  - a download, when Spanish is supported but not yet installed. Nothing
     *    ever triggered one, so a phone missing the model stayed missing it.
     *
     * API 33+ only; 31/32 have no way to ask. Never touches the microphone, so
     * it is safe whenever — but it must not run *during* capture, which is why
     * the result is cached and the capture path only ever reads the cache.
     */
    fun probeSupport(ctx: Context, force: Boolean = false) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val app = ctx.applicationContext
        val age = System.currentTimeMillis() - prefs(app).getLong(KEY_SUPPORT_AT, 0L)
        if (!force && age < SUPPORT_TTL_MS) return
        // SpeechRecognizer is main-thread-only, including creation and teardown.
        val main = Handler(Looper.getMainLooper())
        main.post { probeOnMain(app, main) }
    }

    /** Annotated, unlike the error-code constants elsewhere in this package: the
     *  callback below *implements* an API-33 interface, so the guard in
     *  [probeSupport] is the only thing keeping the class off older phones. */
    @RequiresApi(Build.VERSION_CODES.TIRAMISU)
    private fun probeOnMain(app: Context, main: Handler) {
        val rec = try {
            SpeechRecognizer.createOnDeviceSpeechRecognizer(app)
        } catch (e: Throwable) {
            AppLog.report(app, TAG, "support probe: no on-device recognizer", e)
            rememberSupport(app, null, "no on-device recognizer")
            return
        }
        // Bounce the callback back onto the main looper: it arrives on whatever
        // thread we hand over, and triggerModelDownload/destroy are main-only.
        val onMain = Executor { main.post(it) }
        // checkRecognitionSupport is allowed to call back more than once on some
        // implementations; the first answer is the one we act on.
        val done = AtomicBoolean(false)
        try {
            rec.checkRecognitionSupport(probeIntent(), onMain, object : RecognitionSupportCallback {
                override fun onSupportResult(support: RecognitionSupport) {
                    if (!done.compareAndSet(false, true)) return
                    val installed = support.installedOnDeviceLanguages.map { it.replace('_', '-') }
                    val pending = support.pendingOnDeviceLanguages.map { it.replace('_', '-') }
                    val supported = support.supportedOnDeviceLanguages.map { it.replace('_', '-') }
                    val pick = bestTag(installed)
                    val desc = "installed=$installed pending=$pending supported=$supported → $pick"
                    AppLog.report(app, TAG, "support probe: $desc")
                    rememberSupport(app, pick, desc)
                    // Supported but not here yet: ask for it, then get out of the
                    // way. There is no callback before API 34 and no hurry —
                    // the next probe will see it in installed or pending.
                    if (pick == null) {
                        bestTag(supported)?.let { want ->
                            AppLog.report(app, TAG, "support probe: requesting model download for $want")
                            try { rec.triggerModelDownload(probeIntent(want)) } catch (e: Throwable) {
                                AppLog.report(app, TAG, "support probe: download request failed", e)
                            }
                        }
                    }
                    destroy(rec)
                }

                override fun onError(error: Int) {
                    if (!done.compareAndSet(false, true)) return
                    AppLog.report(app, TAG, "support probe: error $error")
                    rememberSupport(app, null, "check failed, error $error")
                    destroy(rec)
                }
            })
        } catch (e: Throwable) {
            AppLog.report(app, TAG, "support probe: checkRecognitionSupport threw", e)
            rememberSupport(app, null, "check threw")
            destroy(rec)
        }
    }

    private fun destroy(rec: SpeechRecognizer) {
        try { rec.destroy() } catch (e: Throwable) { /* ignore */ }
    }

    /**
     * A language goes in only when we are naming one to download. The *check*
     * deliberately asks unconstrained: the lists it returns describe the
     * recognizer, not the request, and pinning it to a locale the phone does
     * not have invites an error where we wanted an inventory — which is the
     * same mistake, one level up, as the one this whole probe exists to undo.
     */
    private fun probeIntent(tag: String? = null) =
        Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            if (tag != null) putExtra(RecognizerIntent.EXTRA_LANGUAGE, tag)
        }

    private fun rememberSupport(ctx: Context, tag: String?, desc: String) {
        prefs(ctx).edit()
            .putLong(KEY_SUPPORT_AT, System.currentTimeMillis())
            .putString(KEY_SUPPORT_LANG, tag ?: "")
            .putString(KEY_SUPPORT_DESC, desc)
            .apply()
    }

    /** Forget what the recognizer told us, so the next probe actually asks. */
    private fun invalidateSupport(ctx: Context) {
        prefs(ctx).edit().putLong(KEY_SUPPORT_AT, 0L).putString(KEY_SUPPORT_LANG, "").apply()
    }

    /**
     * The best tag in *available* for this house: an exact ladder match, else
     * anything sharing the ladder's primary subtag. Region is negotiable —
     * Spanish is not.
     */
    private fun bestTag(available: List<String>): String? {
        if (available.isEmpty()) return null
        LANG_LADDER.forEach { want ->
            available.firstOrNull { it.equals(want, ignoreCase = true) }?.let { return it }
        }
        val primary = LANG_LADDER.first().substringBefore('-')
        return available.firstOrNull { it.substringBefore('-').equals(primary, ignoreCase = true) }
    }

    /** Confirmed-installed tag, `""` for "asked, it has nothing", null for
     *  "never asked / could not ask". The three are not interchangeable. */
    private fun supportLang(ctx: Context): String? =
        if (prefs(ctx).getLong(KEY_SUPPORT_AT, 0L) == 0L) null
        else prefs(ctx).getString(KEY_SUPPORT_LANG, "") ?: ""

    /** What the recognizer last said about itself, for the Debug menu. */
    fun supportSummary(ctx: Context): String =
        prefs(ctx).getString(KEY_SUPPORT_DESC, "") ?: ""

    /** The tag to speak to the on-device recognizer in — what it told us it has,
     *  falling back to the ladder when it never answered. */
    fun onDeviceLanguageTag(ctx: Context): String =
        supportLang(ctx).takeUnless { it.isNullOrEmpty() } ?: languageTag(ctx)

    // --- "understood nothing", counted -------------------------------------

    /**
     * ERROR_NO_MATCH is the one failure the old code wrote nothing down for.
     * Every other branch of the error handler blocks a path or marks a language;
     * this one just told the user "no te entendí" and left the state untouched,
     * so a phone that could never match anything was asked again on every press
     * and answered the same way every time. Counting it is what turns that from
     * a permanent condition into a self-healing one.
     *
     * Returns the new streak length.
     */
    fun noteNoMatch(ctx: Context, onDevice: Boolean): Int {
        val key = KEY_NOMATCH + tag(onDevice)
        val n = prefs(ctx).getInt(key, 0) + 1
        prefs(ctx).edit().putInt(key, n).apply()
        // Three strikes for a path that has transcribed something here before —
        // one failure is an utterance you mumbled. A path that has NEVER once
        // produced a transcript on this phone gets exactly one, because there is
        // nothing to give the benefit of the doubt to: on this phone that is not
        // a capability yet, only a candidate. `auto` spending three utterances
        // apiece proving it is how "automático" still answered "no te entendí"
        // — Whisper works here, and it is one fallback away.
        val limit = if (everWorked(ctx, onDevice)) NOMATCH_LIMIT else 1
        if (n >= limit) {
            AppLog.report(ctx, TAG, "$n no-match on ${path(onDevice)} " +
                "(everWorked=${everWorked(ctx, onDevice)}) — setting it aside for a day")
            if (onDevice) disableOnDevice(ctx, DISABLE_NOMATCH_MS) else disableNetwork(ctx, DISABLE_NOMATCH_MS)
            prefs(ctx).edit().putInt(key, 0).apply()
            // Whatever we believed about the languages, it was wrong — so drop
            // it and let the next onResume ask again. Deliberately NOT a probe
            // from here: this runs inside the recognizer's own error callback,
            // with a MediaRecorder about to open the mic, and that is the last
            // place to go standing up another SpeechRecognizer.
            invalidateSupport(ctx)
        }
        return n
    }

    /** Any real transcript clears the streak — the counter is for *consecutive*
     *  failures, and a phone that works occasionally is a phone that works —
     *  and records that this path has, at least once, actually worked here. */
    fun noteRecognized(ctx: Context, onDevice: Boolean) {
        prefs(ctx).edit()
            .putInt(KEY_NOMATCH + "ondevice", 0)
            .putInt(KEY_NOMATCH + "network", 0)
            .putBoolean(KEY_OK + tag(onDevice), true)
            .apply()
    }

    /**
     * Has this path ever returned a transcript on this phone? Nothing else here
     * distinguishes "might work" from "has worked", and the two deserve very
     * different patience. Cleared by [clearState] along with every other
     * remembered failure — the reset button forgets the proof too, on purpose.
     */
    private fun everWorked(ctx: Context, onDevice: Boolean) =
        prefs(ctx).getBoolean(KEY_OK + tag(onDevice), false)

    private fun tag(onDevice: Boolean) = if (onDevice) "ondevice" else "network"

    private fun path(onDevice: Boolean) = if (onDevice) "on-device" else "the system recognizer"

    // --- selection --------------------------------------------------------

    /**
     * Resolves the choice and hands it to [cb]. Still synchronous, and now
     * deliberately so: the check that *does* go away and ask something
     * ([probeSupport]) is kept off this path entirely and answers from cache,
     * because every millisecond here is a millisecond before the microphone
     * opens on someone already talking. The callback shape stays because the
     * caller shouldn't have to know either way.
     */
    fun choose(ctx: Context, cb: (Choice) -> Unit) {
        when (mode(ctx)) {
            "whisper" -> { cb(Choice.None); return }
            "network" -> { cb(networkChoice(ctx)); return }
            // Forced on-device: honour it even when the probe says there is
            // nothing installed. This is the escape hatch for reproducing a
            // failure on purpose, and refusing to fail would defeat it.
            "ondevice" -> { cb(Choice.OnDevice); return }
        }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S || blocked(ctx, KEY_ONDEVICE_UNTIL)) {
            cb(networkChoice(ctx))
            return
        }
        cb(if (onDeviceAvailable(ctx)) Choice.OnDevice else networkChoice(ctx))
    }

    /**
     * Whether `auto` should spend this utterance on the on-device recognizer.
     *
     * `isOnDeviceRecognitionAvailable()` used to be the whole answer, and it is
     * the wrong question — it reports that a recognition *service* exists, not
     * that it holds a language anyone here speaks. See [probeSupport]. So on
     * API 33+ this now believes the probe and nothing else, and treats "never
     * asked" as a no: the utterance is in flight, Whisper works, and there is
     * always the next press.
     *
     * On 31/32 there is no way to ask, so stay optimistic and let the error
     * path write off what doesn't work — now including a no-match streak.
     *
     * Reads the cache and nothing else. It must not kick off a probe: this runs
     * microseconds before the microphone opens, and standing up a second
     * SpeechRecognizer next to the one about to listen is how you earn an
     * ERROR_RECOGNIZER_BUSY. Refreshing is [MainActivity]'s job (onResume) and
     * [noteNoMatch]'s. A stale answer is still used — going quiet because
     * nobody asked recently would be worse than the staleness.
     */
    private fun onDeviceAvailable(ctx: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return true
        val lang = supportLang(ctx)
        if (lang.isNullOrEmpty()) {
            Log.i(TAG, "on-device not chosen: " +
                if (lang == null) "support never established" else "no installed language")
            return false
        }
        return true
    }

    private fun networkChoice(ctx: Context): Choice {
        if (blocked(ctx, KEY_NET_UNTIL)) return Choice.None
        val cn = pickNetworkRecognizer(ctx) ?: return Choice.None
        return Choice.Network(cn)
    }

    /**
     * Resolves a RecognitionService that is NOT ours.
     *
     * This app declares its own [AlfredRecognitionService] — a stub that always
     * returns ERROR_CLIENT — because a VoiceInteractionService is required to
     * name one (res/xml/interaction_service.xml). When Alfred is the chosen
     * assistant, the system's default recognizer can end up pointing at that
     * stub, so `createSpeechRecognizer(ctx)` with no component would bind us to
     * our own dead end on every single press. Always pass an explicit component,
     * and never our own package.
     *
     * Needs the <queries> block in AndroidManifest.xml: without it this returns
     * only our own service on API 30+, the filter empties the list, and voice
     * silently falls back to Whisper forever with nothing in the log.
     */
    fun pickNetworkRecognizer(ctx: Context): ComponentName? {
        val services = try {
            ctx.packageManager.queryIntentServices(Intent(RecognitionService.SERVICE_INTERFACE), 0)
        } catch (e: Exception) {
            Log.w(TAG, "queryIntentServices failed", e)
            return null
        }
        val usable = services.filter {
            it.serviceInfo != null &&
                it.serviceInfo.enabled &&
                it.serviceInfo.packageName != ctx.packageName
        }
        if (usable.isEmpty()) {
            AppLog.report(ctx, TAG, "no external recognizer (${services.size} total) → whisper")
            return null
        }
        val best = usable.minByOrNull {
            PREFERRED_PACKAGES.indexOf(it.serviceInfo.packageName)
                .let { i -> if (i < 0) PREFERRED_PACKAGES.size else i }
        }!!
        return ComponentName(best.serviceInfo.packageName, best.serviceInfo.name)
    }

    /** Diagnostic only: logs a WARN when the system default recognizer is us. */
    fun logDefaultRecognizer(ctx: Context) {
        val cur = try {
            Settings.Secure.getString(ctx.contentResolver, "voice_recognition_service")
        } catch (e: Exception) {
            null
        }
        if (cur != null && cur.startsWith(ctx.packageName)) {
            AppLog.report(ctx, TAG, "system default recognizer is OUR stub ($cur) — never bind it implicitly")
        } else {
            Log.i(TAG, "system default recognizer: $cur")
        }
    }
}
