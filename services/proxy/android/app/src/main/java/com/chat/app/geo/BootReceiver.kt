package com.chat.app.geo

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.chat.app.AppLog

/**
 * Android clears all registered geofences on reboot, so re-register them from
 * HomeCore once the device finishes booting — and re-arm the hourly location
 * heartbeat, which a reboot also drops.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val appContext = context.applicationContext
        AppLog.log(appContext, "AlfredGeo", "device booted → geofence resync + heartbeat")
        LocationPingWorker.schedule(appContext)
        val pending = goAsync()
        Thread {
            try {
                GeofenceManager.sync(appContext)
            } finally {
                pending.finish()
            }
        }.start()
    }
}
