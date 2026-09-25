package com.chat.app.geo

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.chat.app.AppLog
import com.google.android.gms.location.Geofence
import com.google.android.gms.location.GeofencingEvent

/**
 * Fired by Play Services when the device enters/exits a registered geofence —
 * works with the app closed. Reports the transition to HomeCore, which decides
 * whether any reminder matches and delivers it.
 */
class GeofenceBroadcastReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val event = GeofencingEvent.fromIntent(intent) ?: return
        if (event.hasError()) {
            AppLog.log(context, "AlfredGeo", "geofence event error ${event.errorCode}")
            return
        }
        val transition = when (event.geofenceTransition) {
            Geofence.GEOFENCE_TRANSITION_ENTER -> "enter"
            Geofence.GEOFENCE_TRANSITION_EXIT -> "exit"
            else -> return
        }
        val fences = event.triggeringGeofences ?: return
        val appContext = context.applicationContext
        AppLog.log(
            appContext, "AlfredGeo",
            "OS geofence $transition: place(s)=${fences.joinToString(",") { it.requestId }}",
        )
        val pending = goAsync() // allow the network POST to finish off the main thread
        Thread {
            try {
                for (g in fences) GeofenceManager.reportEvent(appContext, g.requestId, transition)
            } finally {
                pending.finish()
            }
        }.start()
    }
}
