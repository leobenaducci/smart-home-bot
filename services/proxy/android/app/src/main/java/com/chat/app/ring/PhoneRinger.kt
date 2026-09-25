package com.chat.app.ring

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
import androidx.core.content.ContextCompat
import com.chat.app.AppLog
import com.chat.app.MainActivity
import com.chat.app.R

/**
 * Makes the phone ring so someone can find it, *through* silent mode.
 *
 * The trick is the stream: ringer volume is what "silencio" turns off, but the
 * **alarm** stream keeps working, and Do Not Disturb lets alarms through by
 * default. So this plays the alarm tone with USAGE_ALARM, nudges the alarm
 * volume up for the duration (and puts it back), and vibrates alongside.
 *
 * It always stops by itself, it always posts a notification saying who asked
 * and how to stop it, and stopping is one tap. A phone you can't silence is a
 * worse problem than a phone you can't find.
 */
object PhoneRinger {

    private const val TAG = "AlfredRing"
    const val CHANNEL_RING = "alfred_ring"
    private const val NOTIF_ID_RING = 3
    private const val ACTION_STOP = "com.chat.app.ring.STOP"

    private val handler = Handler(Looper.getMainLooper())
    private var player: MediaPlayer? = null
    private var vibrator: Vibrator? = null
    private var previousAlarmVolume: Int? = null
    private var stopAt: Runnable? = null

    fun ensureChannel(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        val ch = NotificationChannel(
            CHANNEL_RING, "Encontrar mi teléfono", NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            description = "When " + ctx.getString(R.string.assistant_name) +
                    " rings this phone so you can find it"
            setSound(null, null)          // PhoneRinger owns the audio, not the channel
            enableVibration(false)        // ...and the vibration
            setBypassDnd(true)            // honoured only if the user allows it
        }
        nm.createNotificationChannel(ch)
    }

    /**
     * Start ringing for [seconds]. [by] is the person who asked, shown on the
     * phone; empty when you rang your own.
     */
    @Synchronized
    fun start(ctx: Context, seconds: Int, by: String) {
        stop(ctx)  // restart cleanly if one is already going
        ensureChannel(ctx)
        val secs = seconds.coerceIn(5, 120)
        AppLog.log(ctx, TAG, "ringing for ${secs}s" + (if (by.isNotBlank()) " (by $by)" else ""))

        val am = ctx.getSystemService(AudioManager::class.java)
        // Raise the alarm stream so a phone left at volume 0 still makes noise.
        // Blocked while DND is on unless the user granted notification-policy
        // access — the tone still plays either way, just possibly quietly.
        if (am != null) {
            try {
                previousAlarmVolume = am.getStreamVolume(AudioManager.STREAM_ALARM)
                val max = am.getStreamMaxVolume(AudioManager.STREAM_ALARM)
                am.setStreamVolume(AudioManager.STREAM_ALARM, max, 0)
            } catch (e: Exception) {
                previousAlarmVolume = null
                Log.w(TAG, "could not raise alarm volume", e)
            }
        }

        try {
            val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
            player = MediaPlayer().apply {
                setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build(),
                )
                setDataSource(ctx, uri)
                isLooping = true
                prepare()
                start()
            }
        } catch (e: Exception) {
            Log.e(TAG, "ringtone failed", e)
            AppLog.log(ctx, TAG, "ringtone failed: ${e.message}")
        }

        vibrate(ctx)
        postRingNotification(ctx, by)

        val stopper = Runnable { stop(ctx) }
        stopAt = stopper
        handler.postDelayed(stopper, secs * 1000L)
    }

    @Synchronized
    fun stop(ctx: Context) {
        stopAt?.let { handler.removeCallbacks(it) }
        stopAt = null
        player?.let { p ->
            try { if (p.isPlaying) p.stop() } catch (e: Exception) { /* ignore */ }
            try { p.release() } catch (e: Exception) { /* ignore */ }
        }
        player = null
        try { vibrator?.cancel() } catch (e: Exception) { /* ignore */ }
        vibrator = null
        previousAlarmVolume?.let { vol ->
            try {
                ctx.getSystemService(AudioManager::class.java)
                    ?.setStreamVolume(AudioManager.STREAM_ALARM, vol, 0)
            } catch (e: Exception) { /* ignore */ }
        }
        previousAlarmVolume = null
        runCatching { NotificationManagerCompat.from(ctx).cancel(NOTIF_ID_RING) }
    }

    private fun vibrate(ctx: Context) {
        val v = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            ctx.getSystemService(VibratorManager::class.java)?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            ctx.getSystemService(Vibrator::class.java)
        } ?: return
        vibrator = v
        val pattern = longArrayOf(0, 700, 400)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                v.vibrate(
                    VibrationEffect.createWaveform(pattern, 0),
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build(),
                )
            } else {
                @Suppress("DEPRECATION")
                v.vibrate(pattern, 0)
            }
        } catch (e: Exception) {
            Log.w(TAG, "vibrate failed", e)
        }
    }

    private fun postRingNotification(ctx: Context, by: String) {
        val stopIntent = PendingIntent.getBroadcast(
            ctx, 0, Intent(ctx, StopReceiver::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val open = PendingIntent.getActivity(
            ctx, 0,
            Intent(ctx, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val who = if (by.isNotBlank()) "$by está buscando este teléfono" else "Buscando este teléfono"
        val n = NotificationCompat.Builder(ctx, CHANNEL_RING)
            .setSmallIcon(R.drawable.ic_stat_ntfy)
            .setContentTitle("📣 " + ctx.getString(R.string.assistant_name))
            .setContentText(who)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setOngoing(true)
            .setAutoCancel(false)
            .setContentIntent(open)
            .setFullScreenIntent(open, true)   // wakes the screen so it's findable in the dark
            .addAction(0, "Detener", stopIntent)
            .build()
        runCatching { NotificationManagerCompat.from(ctx).notify(NOTIF_ID_RING, n) }
    }

    /** "Detener" on the ring notification. Fully qualified calls on purpose —
     *  a nested class shouldn't lean on the enclosing object's scope. */
    class StopReceiver : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val ctx = context.applicationContext
            AppLog.log(ctx, "AlfredRing", "stopped from the phone")
            PhoneRinger.stop(ctx)
        }
    }
}
