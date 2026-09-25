package com.chat.app.assist

import android.content.Intent
import android.speech.RecognitionService
import android.speech.SpeechRecognizer
import android.util.Log

/**
 * Stub recognizer that always fails. It exists only because a
 * VoiceInteractionService must name a RecognitionService *in its own package* to
 * register at all (`res/xml/interaction_service.xml`) — Alfred's actual speech
 * recognition goes through the system's recognizer, picked explicitly by
 * [SttSupport.pickNetworkRecognizer], or through Whisper on the server.
 *
 * This class is a trap for exactly one reason: when Alfred is the chosen
 * assistant, the system's default recognizer can end up pointing here. Anything
 * that calls `SpeechRecognizer.createSpeechRecognizer(ctx)` without an explicit
 * component would then bind to this dead end and get ERROR_CLIENT every time.
 * Hence the warning below — if it ever shows up in logcat, something regressed
 * back to implicit binding.
 */
class AlfredRecognitionService : RecognitionService() {
    override fun onStartListening(recognizerIntent: Intent, listener: Callback) {
        Log.w(
            "AlfredVoice",
            "stub recognizer bound — someone resolved the default recognizer to us",
        )
        runCatching { listener.error(SpeechRecognizer.ERROR_CLIENT) }
    }
    override fun onCancel(listener: Callback) {}
    override fun onStopListening(listener: Callback) {}
}
