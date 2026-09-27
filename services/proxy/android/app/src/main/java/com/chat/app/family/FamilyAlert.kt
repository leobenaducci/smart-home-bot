package com.chat.app.family

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.chat.app.AppLog
import com.chat.app.R
import org.json.JSONObject

/**
 * A family message, alerted the way an alarm is: the phone vibrates -- through
 * Do Not Disturb and silent mode, on the alarm stream -- until the message is
 * opened or snoozed, and one the sender flagged urgent rings as well, at alarm
 * volume. The same means PhoneRinger uses to be found, without its time limit:
 * this stops when a person has seen it, and not before.
 *
 * One vibration for however many threads are alerting, and one sound for the
 * urgent ones among them; a notification per thread, with Ver and Posponer.
 * "Seen" arrives from the portal (family_stop) when the thread is opened on
 * any of this person's devices, or is set here when it is opened on this one.
 */
object FamilyAlert {
    data class Alert(val thread: String, val threadName: String, val fromName: String,
                     val text: String, val urgent: Boolean)

    const val CHANNEL = "alfred_family"
    const val EXTRA_THREAD = "family_thread"
    private const val ACTION_SNOOZE = "com.chat.app.family.SNOOZE"
    private const val TAG = "AlfredFamily"
    private const val SNOOZE_MS = 5 * 60_000L

    private val handler = Handler(Looper.getMainLooper())
    private val active = linkedMapOf<String, Alert>()
    private val snoozed = mutableMapOf<String, Runnable>()
    private var vibrator: Vibrator? = null
    private var player: MediaPlayer? = null
    private var previousAlarmVolume: Int? = null

    private val ALARM = AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_ALARM)
        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
        .build()

    fun ensureChannel(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        nm.createNotificationChannel(NotificationChannel(
            CHANNEL, "Chat familiar", NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            description = "Mensajes de la familia: vibran hasta que los abrís o los posponés"
            setSound(null, null)      // this object owns the sound...
            enableVibration(false)    // ...and the vibration
            setBypassDnd(true)        // honoured once the user grants Do Not Disturb access
        })
    }

    private fun notifId(thread: String) = 4000 + (thread.hashCode() and 0x7ffff)

    @Synchronized
    fun start(ctx: Context, a: Alert) {
        ensureChannel(ctx)
        snoozed.remove(a.thread)?.let { handler.removeCallbacks(it) }
        active[a.thread] = a
        AppLog.log(ctx, TAG, "alert ${a.thread}" + if (a.urgent) " (urgent)" else "")
        vibrate(ctx)
        if (active.values.any { it.urgent }) ring(ctx) else silence(ctx)
        post(ctx, a)
    }

    /** Seen, here or on another of this person's devices. */
    @Synchronized
    fun stop(ctx: Context, thread: String) {
        snoozed.remove(thread)?.let { handler.removeCallbacks(it) }
        if (active.remove(thread) != null) AppLog.log(ctx, TAG, "stopped $thread")
        runCatching { NotificationManagerCompat.from(ctx).cancel(notifId(thread)) }
        settle(ctx)
    }

    /** Quiet for five minutes, then the same alert again, unless it is seen by then. */
    @Synchronized
    fun snooze(ctx: Context, thread: String) {
        val a = active.remove(thread) ?: return
        runCatching { NotificationManagerCompat.from(ctx).cancel(notifId(thread)) }
        settle(ctx)
        val again = Runnable { start(ctx.applicationContext, a) }
        snoozed[thread] = again
        handler.postDelayed(again, SNOOZE_MS)
        AppLog.log(ctx, TAG, "snoozed $thread for 5 min")
        Thread {
            FamilyApi.post("/family-chat/api/snooze", JSONObject().put("thread", thread))
        }.start()
    }

    private fun settle(ctx: Context) {
        if (active.isEmpty()) {
            runCatching { vibrator?.cancel() }
            vibrator = null
            silence(ctx)
        } else if (active.values.none { it.urgent }) {
            silence(ctx)
        }
    }

    private fun vibrate(ctx: Context) {
        if (vibrator != null) return
        val v = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            ctx.getSystemService(VibratorManager::class.java)?.defaultVibrator
        } else {
            @Suppress("DEPRECATION") ctx.getSystemService(Vibrator::class.java)
        } ?: return
        vibrator = v
        val pattern = longArrayOf(0, 900, 700)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                // USAGE_ALARM: what Do Not Disturb and silent mode let through.
                v.vibrate(VibrationEffect.createWaveform(pattern, 0), ALARM)
            } else {
                @Suppress("DEPRECATION") v.vibrate(pattern, 0)
            }
        } catch (e: Exception) {
            Log.w(TAG, "vibrate failed", e)
        }
    }

    private fun ring(ctx: Context) {
        if (player != null) return
        val am = ctx.getSystemService(AudioManager::class.java)
        if (am != null) runCatching {
            previousAlarmVolume = am.getStreamVolume(AudioManager.STREAM_ALARM)
            am.setStreamVolume(AudioManager.STREAM_ALARM, am.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0)
        }
        try {
            val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
            player = MediaPlayer().apply {
                setAudioAttributes(ALARM)
                setDataSource(ctx, uri)
                isLooping = true
                prepare()
                start()
            }
        } catch (e: Exception) {
            AppLog.log(ctx, TAG, "ringtone failed: ${e.message}")
        }
    }

    private fun silence(ctx: Context) {
        player?.let { p ->
            runCatching { if (p.isPlaying) p.stop() }
            runCatching { p.release() }
        }
        player = null
        previousAlarmVolume?.let { vol ->
            runCatching { ctx.getSystemService(AudioManager::class.java)?.setStreamVolume(AudioManager.STREAM_ALARM, vol, 0) }
        }
        previousAlarmVolume = null
    }

    private fun post(ctx: Context, a: Alert) {
        val id = notifId(a.thread)
        // Opening is seeing: the family screen stops the alert and tells the portal.
        val open = PendingIntent.getActivity(
            ctx, id, Intent(ctx, FamilyActivity::class.java)
                .putExtra(EXTRA_THREAD, a.thread)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val snooze = PendingIntent.getBroadcast(
            ctx, id, Intent(ctx, ActionReceiver::class.java).setAction(ACTION_SNOOZE)
                .putExtra(EXTRA_THREAD, a.thread),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val title = (if (a.urgent) "‼ URGENTE · " else "👪 ") + a.threadName
        val n = NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_ntfy)
            .setContentTitle(title)
            .setContentText("${a.fromName}: ${a.text}")
            .setStyle(NotificationCompat.BigTextStyle().bigText("${a.fromName}: ${a.text}"))
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setCategory(if (a.urgent) NotificationCompat.CATEGORY_ALARM else NotificationCompat.CATEGORY_MESSAGE)
            .setOngoing(true)
            .setAutoCancel(false)
            .setContentIntent(open)
            .apply { if (a.urgent) setFullScreenIntent(open, true) }
            .addAction(0, "Ver", open)
            .addAction(0, "Posponer 5 min", snooze)
            .build()
        runCatching { NotificationManagerCompat.from(ctx).notify(id, n) }
    }

    class ActionReceiver : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val thread = intent.getStringExtra(EXTRA_THREAD) ?: return
            if (intent.action == ACTION_SNOOZE) FamilyAlert.snooze(context.applicationContext, thread)
        }
    }
}
