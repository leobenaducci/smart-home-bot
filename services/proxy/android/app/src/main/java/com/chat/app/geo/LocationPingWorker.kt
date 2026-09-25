package com.chat.app.geo

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import com.chat.app.AppLog
import java.util.concurrent.TimeUnit

/**
 * One location fix an hour, always.
 *
 * Until now a stored location existed only if somebody had explicitly asked —
 * `locate` or `track`. So "¿dónde está Kai?" answered from saved places and
 * geofence crossings, and if she had not crossed one that day there was simply
 * nothing to say. This gives every "where is" a recent answer without waking
 * anyone's GPS at the moment of asking.
 *
 * WorkManager rather than the existing foreground service: `NtfyClientService`
 * is declared `foregroundServiceType="dataSync"`, and on Android 14+ a
 * foreground service may only take location when it declares the `location`
 * type. The app already holds ACCESS_BACKGROUND_LOCATION, so a worker can do
 * this without touching that service's type or its notification.
 *
 * "At least hourly" is a target, not a guarantee: Doze defers periodic work,
 * and a phone left untouched overnight will report less often. That is the
 * platform's call, and fighting it (exact alarms, a second foreground service)
 * would cost far more battery than the question is worth.
 */
class LocationPingWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        AppLog.log(applicationContext, TAG, "hourly location heartbeat")
        // Fire-and-forget: getCurrentLocation is async and posts on its own
        // callback. Returning success immediately is right — a retry would only
        // ask for the same fix a moment later.
        LocationReporter.reportHeartbeat(applicationContext)
        return Result.success()
    }

    companion object {
        private const val TAG = "AlfredLoc"
        private const val WORK_NAME = "location-heartbeat"

        /**
         * Idempotent: KEEP means calling this on every app start and every boot
         * does not reset the schedule, so the heartbeat is not postponed by an
         * hour each time the app is opened.
         */
        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<LocationPingWorker>(1, TimeUnit.HOURS)
                // Without a network the fix cannot be POSTed, and taking one
                // just to throw it away is the one case that is pure battery
                // cost. The next run picks it up.
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                .build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                WORK_NAME, ExistingPeriodicWorkPolicy.KEEP, request
            )
        }

        fun cancel(context: Context) {
            WorkManager.getInstance(context).cancelUniqueWork(WORK_NAME)
        }
    }
}
