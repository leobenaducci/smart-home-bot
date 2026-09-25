package com.chat.app.geo

import com.chat.app.BuildConfig

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.os.Looper
import android.webkit.CookieManager
import androidx.core.content.ContextCompat
import com.chat.app.AppLog
import com.google.android.gms.location.CurrentLocationRequest
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.google.android.gms.tasks.CancellationTokenSource
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * On-demand location reporting for "where is X now" and "track X for a while".
 * GPS runs ONLY while a request is active — a single fix for a locate, or
 * bounded periodic fixes for a track window that Play Services auto-stops at the
 * deadline, plus one cheap heartbeat an hour (see reportHeartbeat). Never a
 * continuous background stream. Driven by silent ntfy control messages (see
 * NtfyClientService) and by LocationPingWorker; auth is the WebView session
 * cookie, exactly like GeofenceManager, so it works with the app closed.
 */
object LocationReporter {
    private const val BASE = BuildConfig.CHAT_BASE_URL
    private const val TAG = "AlfredLoc"
    private val http = OkHttpClient.Builder().callTimeout(20, TimeUnit.SECONDS).build()

    @Volatile private var trackCallback: LocationCallback? = null

    private fun cookie(): String? = CookieManager.getInstance().getCookie(BASE)

    private fun hasLocationPermission(context: Context): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    private fun fused(context: Context): FusedLocationProviderClient =
        LocationServices.getFusedLocationProviderClient(context)

    /** One fresh fix, POSTed to HomeCore. */
    @SuppressLint("MissingPermission") // guarded by hasLocationPermission()
    fun reportOnce(context: Context) {
        if (!hasLocationPermission(context)) { AppLog.log(context, TAG, "locate skipped: no location permission"); return }
        val req = CurrentLocationRequest.Builder()
            .setPriority(Priority.PRIORITY_HIGH_ACCURACY)
            .setMaxUpdateAgeMillis(30_000L)
            .build()
        fused(context).getCurrentLocation(req, CancellationTokenSource().token)
            .addOnSuccessListener { loc ->
                if (loc != null) post(context, loc) else AppLog.log(context, TAG, "locate: getCurrentLocation returned null")
            }
            .addOnFailureListener { e -> AppLog.log(context, TAG, "locate failed: getCurrentLocation", e) }
    }

    /**
     * The hourly heartbeat, so "where is X" has an answer without waking
     * anyone's GPS.
     *
     * Deliberately not the same request as [reportOnce]: high accuracy every
     * hour, forever, on five phones is a real battery cost for a question that
     * only ever needs a neighbourhood. Balanced power lets Play Services answer
     * from wifi/cell, and a fix already taken in the last 15 minutes is good
     * enough — most hours this costs nothing at all because something else
     * already asked.
     */
    @SuppressLint("MissingPermission") // guarded by hasLocationPermission()
    fun reportHeartbeat(context: Context) {
        if (!hasLocationPermission(context)) {
            AppLog.log(context, TAG, "heartbeat skipped: no location permission"); return
        }
        val req = CurrentLocationRequest.Builder()
            .setPriority(Priority.PRIORITY_BALANCED_POWER_ACCURACY)
            .setMaxUpdateAgeMillis(15 * 60_000L)
            .build()
        fused(context).getCurrentLocation(req, CancellationTokenSource().token)
            .addOnSuccessListener { loc ->
                if (loc != null) post(context, loc)
                else AppLog.log(context, TAG, "heartbeat: no fix available")
            }
            .addOnFailureListener { e -> AppLog.log(context, TAG, "heartbeat failed", e) }
    }

    /** Periodic fixes until [untilTs] (epoch seconds), roughly every
     *  [intervalS] seconds. Play Services stops the updates at the deadline. */
    @SuppressLint("MissingPermission") // guarded by hasLocationPermission()
    fun startTrack(context: Context, untilTs: Long, intervalS: Int) {
        if (!hasLocationPermission(context)) { AppLog.log(context, TAG, "track skipped: no location permission"); return }
        val nowS = System.currentTimeMillis() / 1000L
        val durationMs = (untilTs - nowS) * 1000L
        if (durationMs <= 0L) { AppLog.log(context, TAG, "track skipped: deadline already passed"); return }
        stopTrack(context) // replace any running track
        val intervalMs = intervalS.coerceIn(30, 900) * 1000L
        val request = LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, intervalMs)
            .setMinUpdateIntervalMillis(intervalMs)
            .setDurationMillis(durationMs) // Play Services auto-stops at the deadline
            .setWaitForAccurateLocation(false)
            .build()
        val cb = object : LocationCallback() {
            override fun onLocationResult(result: LocationResult) {
                result.lastLocation?.let { post(context, it) }
                if (System.currentTimeMillis() / 1000L >= untilTs) stopTrack(context)
            }
        }
        trackCallback = cb
        fused(context).requestLocationUpdates(request, cb, Looper.getMainLooper())
        AppLog.log(context, TAG, "tracking until $untilTs, every ${intervalMs}ms")
    }

    fun stopTrack(context: Context) {
        val cb = trackCallback ?: return
        trackCallback = null
        fused(context).removeLocationUpdates(cb)
        AppLog.log(context, TAG, "tracking stopped")
    }

    /** POST one fix. Runs the blocking HTTP off the caller's thread — the
     *  location callbacks fire on the main looper. */
    private fun post(context: Context, loc: Location) {
        val c = cookie()
        if (c.isNullOrBlank()) { AppLog.log(context, TAG, "location fix DROPPED: no session cookie"); return }
        Thread {
            try {
                val json = JSONObject()
                    .put("lat", loc.latitude)
                    .put("lon", loc.longitude)
                    .put("ts", loc.time)
                if (loc.hasAccuracy()) json.put("acc", loc.accuracy.toDouble())
                val req = Request.Builder()
                    .url("$BASE/geo/api/location")
                    .header("Cookie", c)
                    .post(json.toString().toRequestBody("application/json".toMediaType()))
                    .build()
                http.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) AppLog.log(context, TAG, "location POST failed: HTTP ${resp.code}")
                }
            } catch (e: Exception) {
                AppLog.log(context, TAG, "location POST failed", e)
            }
        }.start()
    }
}
