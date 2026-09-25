package com.chat.app.ntfy

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.RemoteInput
import com.chat.app.MainActivity
import com.chat.app.R
import org.json.JSONArray

/**
 * Builds — and rebuilds — an Alfred alert notification, buttons included.
 *
 * This lives outside [NtfyClientService] because [NotifyActionReceiver] has to
 * be able to re-post the *same* notification after a button is pressed: first
 * as "sending", then either gone (the server took it) or back with its buttons
 * and the reason it didn't.
 *
 * Why that matters: dismissing only on a 2xx is the right rule — a button that
 * lies about having been pressed is worse than one that doesn't respond — but
 * "leave the notification exactly as it was" is indistinguishable from "the tap
 * did nothing", which is precisely how a broken button gets reported. A press
 * now always changes something on screen, within milliseconds, whatever the
 * server ends up saying.
 */
object AlertNotification {

    internal const val CHANNEL_MESSAGES = "ntfy_messages"

    /**
     * Post (or replace) notification [notifId].
     *
     * [actionsJson] is the ntfy `actions` array, verbatim, carried along so a
     * rebuild can reproduce the buttons without the original message. Passing
     * [statusLine] shows it as the collapsed line and appends it under the body
     * when expanded; [withActions] false drops the buttons for the brief
     * in-flight state, so the same option can't be fired twice.
     */
    fun post(
        context: Context,
        notifId: Int,
        title: String,
        body: String,
        click: String,
        actionsJson: String?,
        statusLine: String? = null,
        withActions: Boolean = true,
    ) {
        if (notifId == 0) return
        val tapIntent = PendingIntent.getActivity(
            context, notifId,
            Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(MainActivity.EXTRA_DEEP_LINK, true)
                if (click.isNotBlank()) putExtra(MainActivity.EXTRA_OPEN_URL, click)
            },
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val builder = NotificationCompat.Builder(context, CHANNEL_MESSAGES)
            .setSmallIcon(R.drawable.ic_stat_ntfy)
            .setContentTitle(title.ifBlank { context.getString(R.string.assistant_name) })
            .setContentText(statusLine ?: body)
            .setAutoCancel(true)
            .setContentIntent(tapIntent)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            // Every state after the first is a replacement of a notification
            // the user is already looking at — buzzing again on each one would
            // punish them for pressing the button.
            .setOnlyAlertOnce(true)
        if (statusLine != null) {
            builder.setStyle(NotificationCompat.BigTextStyle().bigText("$body\n\n$statusLine"))
        }
        if (withActions) addActions(context, builder, notifId, title, body, click, actionsJson)
        runCatching { NotificationManagerCompat.from(context).notify(notifId, builder.build()) }
    }

    /**
     * Action buttons (e.g. a task reminder's "Lista ✓" / "Mas tarde") fire an
     * authenticated call from [NotifyActionReceiver] — no app open needed. Each
     * one also carries the notification it belongs to, so the receiver can put
     * it back the way it found it.
     */
    private fun addActions(
        context: Context,
        builder: NotificationCompat.Builder,
        notifId: Int,
        title: String,
        body: String,
        click: String,
        actionsJson: String?,
    ) {
        if (actionsJson.isNullOrBlank()) return
        val actions = (try {
            JSONArray(actionsJson)
        } catch (e: Exception) { null }) ?: return
        for (i in 0 until actions.length()) {
            val a = actions.optJSONObject(i) ?: continue
            if (!a.optString("action").equals("http", true)) continue
            val isReply = a.optBoolean("reply", false)
            val label = a.optString("label").ifBlank { "OK" }
            val ai = Intent(context, NotifyActionReceiver::class.java).apply {
                putExtra(NotifyActionReceiver.EXTRA_URL, a.optString("url"))
                putExtra(NotifyActionReceiver.EXTRA_METHOD, a.optString("method", "POST"))
                putExtra(NotifyActionReceiver.EXTRA_BODY, a.optString("body"))
                putExtra(NotifyActionReceiver.EXTRA_NOTIF_ID, notifId)
                putExtra(NotifyActionReceiver.EXTRA_CLEAR, a.optBoolean("clear", true))
                putExtra(NotifyActionReceiver.EXTRA_LABEL, label)
                putExtra(NotifyActionReceiver.EXTRA_TITLE, title)
                putExtra(NotifyActionReceiver.EXTRA_TEXT, body)
                putExtra(NotifyActionReceiver.EXTRA_CLICK, click)
                putExtra(NotifyActionReceiver.EXTRA_ACTIONS, actionsJson)
            }
            // A reply action needs a MUTABLE PendingIntent so the OS can add the typed text.
            val flags = (if (isReply) PendingIntent.FLAG_MUTABLE else PendingIntent.FLAG_IMMUTABLE) or
                PendingIntent.FLAG_UPDATE_CURRENT
            val pi = PendingIntent.getBroadcast(context, notifId + i + 1, ai, flags)
            val ab = NotificationCompat.Action.Builder(0, label, pi)
            if (isReply) {
                ab.addRemoteInput(
                    RemoteInput.Builder(NotifyActionReceiver.KEY_REPLY)
                        .setLabel(a.optString("reply_placeholder").ifBlank { "Escribe…" })
                        .build()
                ).setAllowGeneratedReplies(false)
            }
            builder.addAction(ab.build())
        }
    }
}
