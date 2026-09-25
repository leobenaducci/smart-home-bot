package com.chat.app.geo

import com.chat.app.BuildConfig

import android.Manifest
import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.webkit.CookieManager
import androidx.core.content.ContextCompat
import com.chat.app.AppLog
import com.google.android.gms.location.Geofence
import com.google.android.gms.location.GeofencingRequest
import com.google.android.gms.location.LocationServices
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * On-device geofencing for arrival reminders. Fetches the set of places this
 * device's user must watch from HomeCore (GET /geo/api/geofences), registers OS
 * geofences via Play Services, and reports transitions back
 * (POST /geo/api/event). Authenticates with the WebView session cookie, exactly
 * like NtfyClientService — so it works from receivers with the app closed.
 *
 * Only discrete enter/exit events leave the device; there is no continuous
 * location upload.
 *
 * All significant outcomes are written to AppLog so they can be inspected from
 * the chat UI's Log panel (Apps → 📜 Log) without adb logcat.
 */
object GeofenceManager {
    private const val BASE = BuildConfig.CHAT_BASE_URL
    private const val TAG = "AlfredGeo"
    private val http = OkHttpClient.Builder().callTimeout(20, TimeUnit.SECONDS).build()

    private fun cookie(): String? = CookieManager.getInstance().getCookie(BASE)

    private fun pendingIntent(context: Context): PendingIntent {
        val intent = Intent(context, GeofenceBroadcastReceiver::class.java)
        var flags = PendingIntent.FLAG_UPDATE_CURRENT
        // Geofencing requires a MUTABLE PendingIntent on API 31+.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) flags = flags or PendingIntent.FLAG_MUTABLE
        return PendingIntent.getBroadcast(context, 0, intent, flags)
    }

    private fun hasBackgroundLocation(context: Context): Boolean {
        val fine = ContextCompat.checkSelfPermission(
            context, Manifest.permission.ACCESS_FINE_LOCATION,
        ) == PackageManager.PERMISSION_GRANTED
        val bg = Build.VERSION.SDK_INT < Build.VERSION_CODES.Q ||
            ContextCompat.checkSelfPermission(
                context, Manifest.permission.ACCESS_BACKGROUND_LOCATION,
            ) == PackageManager.PERMISSION_GRANTED
        return fine && bg
    }

    /** Fetch the geofence set and (re)register with the OS. Blocking network —
     *  call from a background thread / a receiver's goAsync() block. */
    @SuppressLint("MissingPermission") // guarded by hasBackgroundLocation()
    fun sync(context: Context) {
        if (!hasBackgroundLocation(context)) {
            AppLog.log(context, TAG, "sync skipped: no background-location permission"); return
        }
        val c = cookie()
        if (c.isNullOrBlank()) { AppLog.log(context, TAG, "sync skipped: no session cookie"); return }
        val fences = try {
            val req = Request.Builder().url("$BASE/geo/api/geofences").header("Cookie", c).build()
            http.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) {
                    AppLog.log(context, TAG, "sync failed: geofences HTTP ${resp.code}"); return
                }
                JSONObject(resp.body?.string() ?: "{}").optJSONArray("geofences")
            }
        } catch (e: Exception) { AppLog.log(context, TAG, "sync failed: fetch geofences", e); return }

        val client = LocationServices.getGeofencingClient(context)
        val pi = pendingIntent(context)
        client.removeGeofences(pi) // clear then re-add: simplest correct reconciliation
        if (fences == null || fences.length() == 0) {
            AppLog.log(context, TAG, "sync: server set empty — geofences cleared"); return
        }

        val list = ArrayList<Geofence>()
        for (i in 0 until fences.length()) {
            val f = fences.optJSONObject(i) ?: continue
            list.add(
                Geofence.Builder()
                    .setRequestId(f.optInt("place_id").toString())
                    .setCircularRegion(
                        f.optDouble("lat"), f.optDouble("lng"),
                        f.optInt("radius", 150).toFloat(),
                    )
                    .setTransitionTypes(
                        Geofence.GEOFENCE_TRANSITION_ENTER or Geofence.GEOFENCE_TRANSITION_EXIT,
                    )
                    .setExpirationDuration(Geofence.NEVER_EXPIRE)
                    .build(),
            )
        }
        if (list.isEmpty()) {
            AppLog.log(context, TAG, "sync: server set empty — geofences cleared"); return
        }
        val request = GeofencingRequest.Builder()
            .setInitialTrigger(GeofencingRequest.INITIAL_TRIGGER_ENTER)
            .addGeofences(list).build()
        client.addGeofences(request, pi)
            .addOnSuccessListener { AppLog.log(context, TAG, "sync ok: ${list.size} geofences registered") }
            .addOnFailureListener { e -> AppLog.log(context, TAG, "sync failed: addGeofences", e) }
    }

    /** Report an enter/exit for one place to HomeCore. Blocking network. */
    fun reportEvent(context: Context, placeId: String, transition: String) {
        val c = cookie()
        if (c.isNullOrBlank()) {
            AppLog.log(context, TAG, "event $transition place=$placeId LOST: no session cookie"); return
        }
        try {
            val json = JSONObject()
                .put("place_id", placeId.toIntOrNull() ?: placeId)
                .put("transition", transition)
                .toString()
            val req = Request.Builder()
                .url("$BASE/geo/api/event")
                .header("Cookie", c)
                .post(json.toRequestBody("application/json".toMediaType()))
                .build()
            http.newCall(req).execute().use { resp ->
                val body = try { resp.body?.string()?.take(120) } catch (e: Exception) { null }
                if (resp.isSuccessful) {
                    AppLog.log(context, TAG, "event $transition place=$placeId → HTTP ${resp.code} ${body ?: ""}")
                } else {
                    AppLog.log(context, TAG, "event $transition place=$placeId FAILED: HTTP ${resp.code}")
                }
            }
        } catch (e: Exception) {
            AppLog.log(context, TAG, "event $transition place=$placeId LOST (network error, no retry)", e)
        }
    }
}
