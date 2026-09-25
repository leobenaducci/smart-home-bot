package com.chat.app.assist

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.service.voice.VoiceInteractionSession
import android.util.Log

/**
 * Thin launcher. When Samsung routes the assist gesture through the voice
 * interaction session (rather than as an ACTION_ASSIST activity), the session's
 * own overlay window doesn't reliably surface on this device. So instead of
 * drawing/recording here, we hand off to [AssistActivity] via the sanctioned
 * startAssistantActivity (which is exempt from background-activity-launch
 * limits and comes to the foreground), then dismiss the session. All the
 * recording, UI and mic handling lives in AssistActivity — one single path.
 */
class AlfredVoiceInteractionSession(context: Context) : VoiceInteractionSession(context) {

    override fun onShow(args: Bundle?, showFlags: Int) {
        super.onShow(args, showFlags)
        Log.i("AlfredVoice", "session onShow -> launching AssistActivity")
        val i = Intent(context, AssistActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        try {
            startAssistantActivity(i)
        } catch (e: Throwable) {
            Log.w("AlfredVoice", "startAssistantActivity failed, falling back", e)
            try { context.startActivity(i) } catch (e2: Throwable) {
                Log.e("AlfredVoice", "activity launch failed", e2)
            }
        }
        hide()
    }
}
