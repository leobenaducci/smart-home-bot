package com.chat.app.device

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.chat.app.AppLog
import com.chat.app.R
import com.chat.app.family.FamilyApi
import com.chat.app.ring.PhoneRinger
import org.json.JSONObject
import kotlin.math.roundToInt

/**
 * A `device_cmd` control message: one command for one device, from Alfred.
 *
 * It arrives on the owner's channel, which every device signed in as that
 * person hears; only the device whose id it names acts, and only if remote
 * control was switched on here. Every command is answered
 * (/devices/api/ack), so Alfred says what happened rather than "sent".
 */
object DeviceCommands {
    private const val TAG = "Device"
    private const val CHANNEL = "alfred_remote"
    private const val NOTIF_OPEN = 7301

    fun handle(ctx: Context, o: JSONObject) {
        if (o.optString("device_id") != DeviceIdentity.id(ctx)) return
        val cmd = o.optString("cmd")
        val action = o.optString("action")
        if (!DeviceIdentity.remote(ctx)) {
            AppLog.log(ctx, TAG, "$action refused: remote control is off")
            ack(cmd, false, "remote_off")
            return
        }
        AppLog.log(ctx, TAG, "$action" + (o.optString("by").takeIf { it.isNotBlank() }?.let { " (by $it)" } ?: ""))
        when (action) {
            "volume" -> volume(ctx, cmd, o)
            "open_app" -> openApp(ctx, cmd, o.optString("package"), o.optString("label"))
            "ring" -> {
                PhoneRinger.start(ctx, o.optInt("seconds", 45), o.optString("by"))
                ack(cmd, true, "ringing")
            }
            "ring_stop" -> {
                PhoneRinger.stop(ctx)
                ack(cmd, true, "stopped")
            }
            else -> ack(cmd, false, "unknown_action")
        }
    }

    /** Media volume: what a video or a game plays at. Shown on screen as it changes. */
    private fun volume(ctx: Context, cmd: String, o: JSONObject) {
        val am = ctx.getSystemService(AudioManager::class.java)
        val stream = AudioManager.STREAM_MUSIC
        val max = am.getStreamMaxVolume(stream).coerceAtLeast(1)
        try {
            when (o.optString("step")) {
                "up" -> am.adjustStreamVolume(stream, AudioManager.ADJUST_RAISE, AudioManager.FLAG_SHOW_UI)
                "down" -> am.adjustStreamVolume(stream, AudioManager.ADJUST_LOWER, AudioManager.FLAG_SHOW_UI)
                "mute" -> am.adjustStreamVolume(stream, AudioManager.ADJUST_MUTE, AudioManager.FLAG_SHOW_UI)
                "unmute" -> am.adjustStreamVolume(stream, AudioManager.ADJUST_UNMUTE, AudioManager.FLAG_SHOW_UI)
                else -> am.setStreamVolume(stream, (o.optInt("level", 50) / 100.0 * max).roundToInt(),
                    AudioManager.FLAG_SHOW_UI)
            }
        } catch (e: SecurityException) {
            // Do Not Disturb can forbid volume changes to an app without policy access.
            ack(cmd, false, "not_allowed")
            return
        }
        val muted = am.isStreamMute(stream)
        val level = if (muted) 0 else (am.getStreamVolume(stream) * 100.0 / max).roundToInt()
        ack(cmd, true, if (muted) "muted" else "volume", level)
    }

    /**
     * Opened directly when Android allows it ("Display over other apps"),
     * otherwise a notification on the device that opens it with one tap --
     * and the answer says which, so Alfred does not claim it is open.
     */
    private fun openApp(ctx: Context, cmd: String, pkg: String, label: String) {
        val launch = ctx.packageManager.getLaunchIntentForPackage(pkg)
        if (launch == null) {
            ack(cmd, false, "not_installed")
            return
        }
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
        if (DeviceIdentity.canOpenApps(ctx)) {
            val opened = runCatching { ctx.startActivity(launch) }.isSuccess
            if (opened) {
                ack(cmd, true, "opened")
                return
            }
        }
        ensureChannel(ctx)
        val tap = PendingIntent.getActivity(ctx, pkg.hashCode(), launch,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_ntfy)
            .setContentTitle(label.ifBlank { pkg })
            .setContentText("Tocá para abrir")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_RECOMMENDATION)
            .setContentIntent(tap)
            .setAutoCancel(true)
            .build()
        val shown = runCatching { NotificationManagerCompat.from(ctx).notify(NOTIF_OPEN, n) }.isSuccess
        ack(cmd, shown, if (shown) "notified" else "not_allowed")
    }

    private fun ensureChannel(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) != null) return
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Control remoto",
            NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Apps que un padre pidió abrir en este dispositivo desde " + ctx.getString(R.string.assistant_name)
        })
    }

    private fun ack(cmd: String, ok: Boolean, detail: String, level: Int? = null) {
        if (cmd.isBlank()) return
        val body = JSONObject().put("cmd", cmd).put("ok", ok).put("detail", detail)
        if (level != null) body.put("level", level)
        Thread { FamilyApi.post("/devices/api/ack", body) }.start()
    }
}
