package com.chat.app.assist

import com.chat.app.BuildConfig

import android.Manifest
import android.app.Activity
import android.app.KeyguardManager
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log
import android.view.WindowManager
import android.webkit.CookieManager
import android.widget.Toast
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.chat.app.AppLog
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.util.concurrent.TimeUnit
import kotlin.math.max
import kotlin.math.pow

/**
 * The one and only voice-capture screen. Reached two ways — Samsung firing the
 * assist gesture as an ACTION_ASSIST activity, or the VoiceInteractionSession
 * handing off via startAssistantActivity — so there is a single overlay
 * (MicWaveView) and a single place the microphone is opened.
 *
 * Two capture paths, chosen by [SttSupport] BEFORE the mic is touched (a
 * recognizer and a MediaRecorder cannot both hold it, so there is no changing
 * our minds once the user is talking):
 *
 *  - **Speech recognition** (preferred): the phone transcribes as you speak,
 *    words appear live on the overlay, and only the text is sent. No audio
 *    upload, no Whisper round trip — the message is on its way within a few
 *    hundred ms of you stopping.
 *  - **Record and upload** (fallback): the original path — MediaRecorder with an
 *    adaptive noise-floor VAD, `.m4a` posted to HomeCore /chat/voice, transcribed
 *    there by Whisper. Used when no usable recognizer exists, when one fails
 *    before capturing anything, or when forced via the stt_mode debug setting.
 *
 * Either way the mic is always released: guarded start, watchdogs, and onStop.
 */
class AssistActivity : Activity() {

    companion object {
        private const val BASE = BuildConfig.CHAT_BASE_URL
        private const val TAG = "AlfredVoice"
        private const val SILENCE_MS = 900L      // stop this long after speech ends
        private const val NO_SPEECH_MS = 6000L   // give up if nothing is said
        private const val MAX_MS = 20000L        // hard cap on one utterance
        private const val POLL_MS = 80L
        private const val ONSET_MIN = 2600f      // floor for "started speaking"
        private const val END_MIN = 1500f        // floor for "still speaking"
        private const val REQ_MIC = 41
        private const val REARM_MS = 250L        // let the recognizer's mic go before ours opens
        private const val MIN_SENDING_MS = 700L  // hold the transcript on screen this long
        private const val FAIL_MS = 900L         // ...and a failure message this long
        // Whisper's transcript only exists once the upload answers, so this one
        // is counted from the moment it appears rather than from beginSending —
        // by then the upload has already eaten more than MIN_SENDING_MS and the
        // words would be drawn and cleared in the same breath.
        private const val TRANSCRIPT_HOLD_MS = 1400L
        private const val MIN_TEXT_CHARS = 3     // shorter than this is noise, not speech

        // Error codes added in API 33/34. Written as literals so the app still
        // builds and runs against older devices without @RequiresApi noise.
        private const val ERR_TOO_MANY_REQUESTS = 10
        private const val ERR_SERVER_DISCONNECTED = 11
        private const val ERR_LANGUAGE_NOT_SUPPORTED = 12
        private const val ERR_LANGUAGE_UNAVAILABLE = 13
        private const val ERR_CANNOT_CHECK_SUPPORT = 14
        private const val ERR_CANNOT_LISTEN_TO_DOWNLOAD_EVENTS = 15

        private val http = OkHttpClient.Builder().callTimeout(45, TimeUnit.SECONDS).build()
        // The text path is ~50 bytes and the server no longer waits for Alfred's
        // LLM turn before answering, so 45 s would only ever hide a real failure.
        private val httpText = http.newBuilder().callTimeout(15, TimeUnit.SECONDS).build()
    }

    private enum class St { PROBE, STT, REC, SENDING, DONE }

    private val handler = Handler(Looper.getMainLooper())
    private var waveView: MicWaveView? = null
    private var st = St.PROBE

    /** True from the moment something opens the mic until it's released. Also
     *  keeps onStop from tearing us down while the permission dialog is up. */
    private var micArmed = false
    private var spoke = false
    private var sending = false
    private var sendingSince = 0L

    // Recognizer path
    private var recognizer: SpeechRecognizer? = null
    private var sttComponent: ComponentName? = null
    private var onDevice = false
    private var lang = "es"
    private var rearmed = false   // one fallback to Whisper per invocation, never a loop

    // Recorder path (unchanged)
    private var recorder: MediaRecorder? = null
    private var audioFile: File? = null
    private var startAt = 0L
    private var lastVoiceAt = 0L
    private var floor = 0f

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Log.i(TAG, "AssistActivity.onCreate")
        // Show over the lock screen without unlocking (no biometric), wake screen.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                    WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON,
            )
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        val v = MicWaveView(this)
        v.setOnClickListener { cancelAll() }
        v.setSecure(isLocked())   // no readable transcript on the lock screen
        waveView = v
        setContentView(v)

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            v.setState(MicWaveView.State.DENIED)
            ActivityCompat.requestPermissions(
                this, arrayOf(Manifest.permission.RECORD_AUDIO), REQ_MIC,
            )
            return
        }
        beginCapture()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_MIC && grantResults.isNotEmpty() &&
            grantResults[0] == PackageManager.PERMISSION_GRANTED
        ) {
            beginCapture()
        } else {
            cancelAll()
        }
    }

    private fun isLocked(): Boolean =
        getSystemService(KeyguardManager::class.java)?.isKeyguardLocked == true

    // ---------------------------------------------------------------------
    // Capture selection
    // ---------------------------------------------------------------------
    private fun beginCapture() {
        SttSupport.logDefaultRecognizer(this)
        SttSupport.choose(this) { choice ->
            if (st != St.PROBE || isFinishing) return@choose
            AppLog.report(this, TAG, "stt choice = $choice (mode=${SttSupport.mode(this)})")
            when (choice) {
                is SttSupport.Choice.OnDevice -> startStt(null)
                is SttSupport.Choice.Network ->
                    // A network recognizer on the lock screen is unreliable across
                    // OEMs, and a 1-2 s failure there costs more than it saves.
                    if (isLocked()) startRecording() else startStt(choice.component)
                is SttSupport.Choice.None -> startRecording()
            }
        }
    }

    // ---------------------------------------------------------------------
    // Path A — on-device / system speech recognition
    // ---------------------------------------------------------------------
    private fun startStt(component: ComponentName?) {
        sttComponent = component
        onDevice = component == null
        // On-device gets the tag the recognizer said it actually has installed;
        // the ladder's own first choice (es) is not a locale any on-device
        // model ships as. See SttSupport.probeSupport.
        lang = if (onDevice) SttSupport.onDeviceLanguageTag(this) else SttSupport.languageTag(this)
        val r = try {
            when {
                component != null -> SpeechRecognizer.createSpeechRecognizer(this, component)
                Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ->
                    SpeechRecognizer.createOnDeviceSpeechRecognizer(this)
                else -> null
            }
        } catch (e: Throwable) {
            Log.w(TAG, "recognizer create failed", e)
            null
        }
        if (r == null) {
            startRecording()
            return
        }
        recognizer = r
        r.setRecognitionListener(listener)
        try {
            r.startListening(sttIntent(lang))
        } catch (e: Throwable) {
            Log.w(TAG, "startListening failed", e)
            fallbackToWhisper("startListening threw")
            return
        }
        st = St.STT
        micArmed = true
        spoke = false
        waveView?.setState(MicWaveView.State.LISTENING)
        handler.postDelayed(noSpeechWatchdog, NO_SPEECH_MS)
        AppLog.report(this, TAG, "listening (onDevice=$onDevice lang=$lang)")
    }

    private fun sttIntent(tag: String) = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
        putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        putExtra(RecognizerIntent.EXTRA_LANGUAGE, tag)
        putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, tag)
        putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
        // Google's recognizer refuses to start without a calling package.
        putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, packageName)
        // Only on the on-device branch: forcing offline on a network recognizer
        // is how you get ERROR_LANGUAGE_UNAVAILABLE on a phone with no pack.
        if (onDevice) putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
        // Hints, not guarantees — the recognizer's own endpointer usually wins,
        // which is why the watchdogs below still exist. These three are *int*
        // extras: passing a Long puts a long, which getIntExtra never finds.
        putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, SILENCE_MS.toInt())
        putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 700)
        putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS, 800)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            putExtra(RecognizerIntent.EXTRA_ENABLE_FORMATTING, RecognizerIntent.FORMATTING_OPTIMIZE_LATENCY)
            putExtra(RecognizerIntent.EXTRA_MASK_OFFENSIVE_WORDS, false)
        }
    }

    /** Nothing said at all — same outcome as the recorder path's `gaveUp`. */
    private val noSpeechWatchdog = Runnable {
        if (st == St.STT && !spoke) {
            Log.i(TAG, "no speech watchdog")
            cancelAll()
        }
    }

    /** Utterance ran long: stop (never cancel) so the result still arrives. */
    private val maxLenWatchdog = Runnable {
        if (st == St.STT) {
            Log.i(TAG, "max length watchdog")
            try { recognizer?.stopListening() } catch (e: Exception) { /* ignore */ }
        }
    }

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {
            micArmed = true
            waveView?.setState(MicWaveView.State.LISTENING)
        }

        override fun onBeginningOfSpeech() {
            spoke = true
            handler.removeCallbacks(noSpeechWatchdog)
            handler.postDelayed(maxLenWatchdog, MAX_MS)
        }

        /** rmsdB is roughly -2 (silence) to 10 (loud) and explicitly unitless. */
        override fun onRmsChanged(rmsdB: Float) {
            val n = ((rmsdB + 2f) / 12f).coerceIn(0f, 1f).pow(0.7f)
            waveView?.setLevelNormalized(n)
        }

        override fun onBufferReceived(buffer: ByteArray?) {
            // Not reliably delivered by Google's recognizer — do NOT try to
            // capture the PCM here to hedge with Whisper. It won't be there.
        }

        override fun onEndOfSpeech() {
            handler.removeCallbacks(maxLenWatchdog)
            if (st == St.STT) waveView?.setState(MicWaveView.State.THINKING)
        }

        override fun onPartialResults(partialResults: Bundle?) {
            val t = partialResults
                ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                ?.firstOrNull()
            // Never send on a partial — they get revised and truncated.
            if (!t.isNullOrBlank()) waveView?.setPartial(t)
        }

        override fun onResults(results: Bundle?) {
            val text = results
                ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                ?.firstOrNull()?.trim().orEmpty()
            if (text.length < MIN_TEXT_CHARS) {
                // Empty, or a stray syllable: the recognizer's quieter way of
                // saying no match, and it needs to count as one.
                AppLog.report(this@AssistActivity, TAG,
                    "onResults EMPTY (len=${text.length}) onDevice=$onDevice lang=$lang")
                SttSupport.noteNoMatch(this@AssistActivity, onDevice)
                failSoft("No te entendí")
                return
            }
            AppLog.report(this@AssistActivity, TAG,
                "onResults ok len=${text.length} onDevice=$onDevice lang=$lang")
            SttSupport.noteRecognized(this@AssistActivity, onDevice)
            destroyRecognizer()
            waveView?.setPartial(text)
            beginSending()
            sendText(text)
        }

        override fun onError(error: Int) = handleSttError(error)

        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    private fun handleSttError(code: Int) {
        handler.removeCallbacks(noSpeechWatchdog)
        handler.removeCallbacks(maxLenWatchdog)
        // A failure seen only behind the lock screen says nothing about how this
        // phone behaves unlocked — don't write off a path for a week over it.
        val locked = isLocked()
        AppLog.report(this, TAG,
            "stt error=$code spoke=$spoke onDevice=$onDevice lang=$lang locked=$locked")
        when (code) {
            SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> {
                waveView?.setState(MicWaveView.State.DENIED)
                cancelAll()
            }
            SpeechRecognizer.ERROR_NO_MATCH -> {
                // Counted now. This branch used to record nothing at all, which
                // is why a phone that could never match anything was asked again
                // on every press and answered the same way every time.
                SttSupport.noteNoMatch(this, onDevice)
                if (!spoke) fallbackToWhisper("no match before any speech")
                else failSoft("No te entendí")
            }
            SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> failSoft("No escuché nada")
            ERR_LANGUAGE_UNAVAILABLE, ERR_LANGUAGE_NOT_SUPPORTED -> retryNextLanguage()
            SpeechRecognizer.ERROR_CLIENT, ERR_CANNOT_CHECK_SUPPORT,
            ERR_CANNOT_LISTEN_TO_DOWNLOAD_EVENTS -> {
                if (!locked) {
                    if (onDevice) SttSupport.disableOnDevice(this, SttSupport.DISABLE_LONG_MS)
                    else SttSupport.disableNetwork(this, SttSupport.DISABLE_LONG_MS)
                }
                fallbackToWhisper("client error $code")
            }
            SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT,
            SpeechRecognizer.ERROR_SERVER, ERR_SERVER_DISCONNECTED, ERR_TOO_MANY_REQUESTS -> {
                if (!locked && !onDevice) SttSupport.disableNetwork(this, SttSupport.DISABLE_SHORT_MS)
                fallbackToWhisper("network error $code")
            }
            SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> fallbackToWhisper("recognizer busy")
            SpeechRecognizer.ERROR_AUDIO -> fallbackToWhisper("audio error")
            else -> fallbackToWhisper("error $code")
        }
    }

    /** Language packs are per-tag: step down the ladder before giving up. */
    private fun retryNextLanguage() {
        val next = SttSupport.nextLanguageAfter(this, lang)
        SttSupport.markLanguageBad(this, lang)
        if (next == null) {
            fallbackToWhisper("no usable language")
            return
        }
        Log.i(TAG, "language $lang unavailable → $next")
        destroyRecognizer()
        st = St.PROBE
        handler.postDelayed({ if (st == St.PROBE) startStt(sttComponent) }, 120L)
    }

    /**
     * Hand this utterance to the recorder+Whisper path. Only possible before the
     * user has actually spoken — audio already consumed by a failing recognizer
     * is gone — and only once, because a re-arm loop is worse than no voice.
     */
    private fun fallbackToWhisper(reason: String) {
        destroyRecognizer()
        if (rearmed || spoke) {
            AppLog.report(this, TAG, "cannot re-arm ($reason, rearmed=$rearmed spoke=$spoke)")
            failSoft(if (spoke) "No te entendí" else null)
            return
        }
        rearmed = true
        st = St.PROBE
        AppLog.report(this, TAG, "re-arming to whisper: $reason")
        waveView?.setState(MicWaveView.State.THINKING)
        // The recognition service releases the mic asynchronously when it unbinds.
        handler.postDelayed({ if (st == St.PROBE) startRecording() }, REARM_MS)
    }

    private fun failSoft(message: String?) {
        destroyRecognizer()
        st = St.DONE
        micArmed = false
        if (message != null) {
            waveView?.setPartial(message)
            Toast.makeText(applicationContext, message, Toast.LENGTH_SHORT).show()
        }
        handler.postDelayed({ finishAndRemoveTask() }, if (message != null) FAIL_MS else 0L)
    }

    private fun destroyRecognizer() {
        val r = recognizer ?: return
        recognizer = null
        micArmed = false
        try { r.cancel() } catch (e: Exception) { /* ignore */ }
        try { r.destroy() } catch (e: Exception) { /* ignore */ }
    }

    // ---------------------------------------------------------------------
    // Path B — record and let HomeCore/Whisper transcribe (fallback)
    // ---------------------------------------------------------------------
    private fun startRecording() {
        val rec = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) MediaRecorder(this)
                  else @Suppress("DEPRECATION") MediaRecorder()
        recorder = rec // assign first so teardown can always release the mic
        try {
            val dir = File(cacheDir, "assist-audio").apply { mkdirs() }
            val f = File(dir, "voice_${System.currentTimeMillis()}.m4a")
            audioFile = f
            rec.setAudioSource(MediaRecorder.AudioSource.MIC)
            rec.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            rec.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            rec.setAudioSamplingRate(16000)
            rec.setAudioEncodingBitRate(64000)
            rec.setOutputFile(f.absolutePath)
            rec.prepare()
            rec.start()
            st = St.REC
            micArmed = true
            startAt = SystemClock.elapsedRealtime()
            lastVoiceAt = startAt
            floor = 0f
            spoke = false
            waveView?.setPartial(null)
            waveView?.setState(MicWaveView.State.LISTENING)
            try { rec.maxAmplitude } catch (e: Exception) { /* prime */ }
            handler.postDelayed(poll, POLL_MS)
            handler.postDelayed({ if (st == St.REC) stopRecording(send = spoke) }, MAX_MS + 1000L)
            Log.i(TAG, "recording started")
        } catch (e: Exception) {
            Log.e(TAG, "start failed", e)
            cancelAll()
        }
    }

    // Adaptive VAD: learn the room's noise floor while quiet, then treat a drop
    // back toward that floor (held for SILENCE_MS) as "done talking".
    private val poll = object : Runnable {
        override fun run() {
            val r = recorder ?: return
            val amp = (try { r.maxAmplitude } catch (e: Exception) { 0 }).toFloat()
            val now = SystemClock.elapsedRealtime()
            if (!spoke) floor = if (floor == 0f) amp else floor * 0.9f + amp * 0.1f
            val onset = max(ONSET_MIN, floor * 2.2f)
            val endLevel = max(END_MIN, floor * 1.5f)
            if (amp > onset) {
                spoke = true; lastVoiceAt = now
            } else if (spoke && amp > endLevel) {
                lastVoiceAt = now
            }
            waveView?.setLevel(amp.toInt())
            val tooLong = now - startAt > MAX_MS
            val silenceDone = spoke && (now - lastVoiceAt > SILENCE_MS)
            val gaveUp = !spoke && (now - startAt > NO_SPEECH_MS)
            when {
                silenceDone || tooLong -> stopRecording(send = true)
                gaveUp -> stopRecording(send = false)
                else -> handler.postDelayed(this, POLL_MS)
            }
        }
    }

    private fun stopRecording(send: Boolean) {
        if (st != St.REC) return
        handler.removeCallbacks(poll)
        Log.i(TAG, "stopRecording send=$send spoke=$spoke")
        val rec = recorder
        val f = audioFile
        recorder = null
        micArmed = false
        var ok = true
        try { rec?.stop() } catch (e: Exception) { ok = false }
        try { rec?.release() } catch (e: Exception) { /* ignore */ }
        if (send && ok && f != null && f.exists() && f.length() > 0L) {
            beginSending()
            upload(f)
        } else {
            f?.delete()
            audioFile = null
            st = St.DONE
            finishAndRemoveTask()
        }
    }

    // ---------------------------------------------------------------------
    // Delivery
    // ---------------------------------------------------------------------
    private fun beginSending() {
        st = St.SENDING
        sending = true
        sendingSince = SystemClock.elapsedRealtime()
        handler.removeCallbacks(noSpeechWatchdog)
        handler.removeCallbacks(maxLenWatchdog)
        waveView?.setState(MicWaveView.State.SENDING)
    }

    /** Transcript from the phone — no audio, no Whisper. */
    private fun sendText(text: String) {
        Thread {
            var code = -1
            try {
                val cookie = CookieManager.getInstance().getCookie(BASE)
                val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                    .addFormDataPart("text", text)
                    .addFormDataPart("src", if (onDevice) "ondevice" else "network")
                    .addFormDataPart("lang", lang)
                    .build()
                val b = okhttp3.Request.Builder().url("$BASE/chat/voice").post(body)
                if (!cookie.isNullOrBlank()) b.header("Cookie", cookie)
                httpText.newCall(b.build()).execute().use { resp -> code = resp.code }
                AppLog.report(applicationContext, TAG, "voice(text) HTTP $code src=${if (onDevice) "ondevice" else "network"}")
            } catch (e: Exception) {
                AppLog.report(applicationContext, TAG, "voice(text) send failed", e)
            } finally {
                finishAfterSend(code)
            }
        }.start()
    }

    private fun upload(f: File) {
        Thread {
            var code = -1
            var transcript: String? = null
            try {
                val cookie = CookieManager.getInstance().getCookie(BASE)
                val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                    .addFormDataPart("file", "audio.m4a", f.asRequestBody("audio/mp4".toMediaType()))
                    .addFormDataPart("lang", lang)
                    .build()
                val b = okhttp3.Request.Builder().url("$BASE/chat/voice").post(body)
                if (!cookie.isNullOrBlank()) b.header("Cookie", cookie)
                http.newCall(b.build()).execute().use { resp ->
                    code = resp.code
                    // /chat/voice has always answered {ok, text} and this path
                    // has always read only the status code — so on the Whisper
                    // route the words came back over the wire and were thrown
                    // away, and you watched "Enviando…" without ever seeing
                    // what was heard. The recognizer routes have shown their
                    // text since the first partial; this is the same courtesy.
                    if (resp.isSuccessful) {
                        transcript = runCatching {
                            org.json.JSONObject(resp.body?.string().orEmpty()).optString("text")
                        }.getOrNull()?.trim()?.takeIf { it.isNotEmpty() }
                    }
                }
                // Length, never the words: /debug/applog is readable over HTTP,
                // and the family's speech does not belong in it.
                AppLog.report(applicationContext, TAG,
                    "voice(audio) HTTP $code transcript=${transcript?.length ?: 0} chars lang=$lang")
            } catch (e: Exception) {
                AppLog.report(applicationContext, TAG, "voice(audio) upload failed", e)
            } finally {
                f.delete()
                finishAfterSend(code, transcript,
                                heardNothing = code in 200..299 && transcript == null)
            }
        }.start()
    }

    /**
     * Closes the overlay once the send is done, but never so fast that the
     * transcript flashes past unread. Only failures get a Toast — with the text
     * shown on screen, "Enviado a Alfred" is just noise.
     */
    /**
     * @param revealed text the user has NOT already seen — the Whisper path's
     *   transcript, which only exists once the upload answers. The recognizer
     *   paths pass nothing: their words have been on screen since the first
     *   partial, and re-showing them would only delay the dismissal.
     * @param heardNothing a 2xx that carried no transcript at all.
     */
    private fun finishAfterSend(code: Int, revealed: String? = null, heardNothing: Boolean = false) {
        val ok = code in 200..299
        // setPartial is a no-op behind the lock screen on purpose — the
        // waveform gives nothing away, readable words would — so there is
        // nothing to hold the overlay open for there either.
        val shown = revealed != null && !isLocked()
        if (shown) handler.post { waveView?.setPartial(revealed) }
        val wait = if (shown) {
            // From now, not from beginSending: the upload already spent more
            // than MIN_SENDING_MS, so measuring from there would draw the
            // transcript and clear it in the same frame.
            TRANSCRIPT_HOLD_MS
        } else {
            max(0L, MIN_SENDING_MS - (SystemClock.elapsedRealtime() - sendingSince))
        }
        handler.postDelayed({
            sending = false
            st = St.DONE
            if (!ok) {
                Toast.makeText(applicationContext, "No se pudo enviar", Toast.LENGTH_SHORT).show()
            } else if (heardNothing) {
                // 2xx carrying no text: Whisper heard nothing. This used to
                // close in silence, which looks exactly like a message that was
                // sent — the one case where saying nothing is a lie.
                Toast.makeText(applicationContext, "No te entendí", Toast.LENGTH_SHORT).show()
            }
            finishAndRemoveTask()
        }, wait)
    }

    // ---------------------------------------------------------------------
    // Teardown
    // ---------------------------------------------------------------------
    /** Give up on this utterance: release everything, send nothing. */
    private fun cancelAll() {
        if (st == St.SENDING) return
        st = St.DONE
        handler.removeCallbacksAndMessages(null)
        releaseMic()
        finishAndRemoveTask()
    }

    private fun releaseMic() {
        destroyRecognizer()
        val rec = recorder
        recorder = null
        try { rec?.stop() } catch (e: Exception) { /* ignore */ }
        try { rec?.release() } catch (e: Exception) { /* ignore */ }
        audioFile?.delete()
        audioFile = null
        micArmed = false
    }

    override fun onStop() {
        super.onStop()
        // Left the foreground mid-capture: release the mic, don't send. Gated on
        // micArmed rather than "is there a recorder" so the recognizer path is
        // covered too — and so the runtime permission dialog, which stops us
        // before anything is armed, doesn't kill the activity.
        if (st != St.DONE && st != St.SENDING && micArmed) {
            st = St.DONE
            handler.removeCallbacksAndMessages(null)
            releaseMic()
            if (!isFinishing) finishAndRemoveTask()
        }
        waveView?.stop()
    }

    override fun onDestroy() {
        super.onDestroy()
        destroyRecognizer()
    }
}
