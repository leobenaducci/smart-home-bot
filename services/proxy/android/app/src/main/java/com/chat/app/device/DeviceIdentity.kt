package com.chat.app.device

import android.content.Context
import android.content.Intent
import android.os.Build
import android.provider.Settings
import com.chat.app.AppLog
import com.chat.app.family.FamilyApi
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/**
 * Which device this is, in the household's words: a name somebody gave it
 * ("the kids' tablet"), whether it may be controlled from Alfred, and the apps
 * it can open. Registered with the portal (/devices/api/register) whenever the
 * app connects, so "open Disney+ on the tablet" can be checked against what is
 * actually installed before anything is sent.
 *
 * Remote control is off until somebody switches it on here, on the device:
 * the portal cannot turn it on, and a device that never opted in ignores every
 * command.
 */
object DeviceIdentity {
    private const val PREFS = "alfred"
    private const val KEY_ID = "device_id"
    private const val KEY_NAME = "device_name"
    private const val KEY_REMOTE = "device_remote"
    private const val KEY_LAST = "device_registered_at"
    private const val KEY_SIG = "device_registered_sig"
    private const val TAG = "Device"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** A random id made once. Not the hardware's: nothing here needs to know which phone it is outside this house. */
    @Synchronized
    fun id(ctx: Context): String = prefs(ctx).getString(KEY_ID, null)
        ?: UUID.randomUUID().toString().also { prefs(ctx).edit().putString(KEY_ID, it).apply() }

    fun name(ctx: Context): String = prefs(ctx).getString(KEY_NAME, null)?.takeIf { it.isNotBlank() } ?: defaultName(ctx)

    fun hasName(ctx: Context): Boolean = !prefs(ctx).getString(KEY_NAME, null).isNullOrBlank()

    /** What Android itself calls the device (Settings → About), else its model. */
    fun defaultName(ctx: Context): String =
        runCatching { Settings.Global.getString(ctx.contentResolver, "device_name") }.getOrNull()
            ?.takeIf { it.isNotBlank() } ?: "${Build.MANUFACTURER} ${Build.MODEL}".trim()

    fun remote(ctx: Context): Boolean = prefs(ctx).getBoolean(KEY_REMOTE, false)

    fun isTablet(ctx: Context): Boolean = ctx.resources.configuration.smallestScreenWidthDp >= 600

    /** Android lets an app open another from the background only with "Display over other apps". */
    fun canOpenApps(ctx: Context): Boolean = Settings.canDrawOverlays(ctx)

    fun save(ctx: Context, name: String, remote: Boolean) {
        prefs(ctx).edit().putString(KEY_NAME, name.trim().take(60)).putBoolean(KEY_REMOTE, remote)
            .remove(KEY_LAST).apply()
    }

    /** (label, package) for every app with a launcher icon, this one left out. */
    fun launchableApps(ctx: Context): List<Pair<String, String>> {
        val pm = ctx.packageManager
        val main = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        return runCatching { pm.queryIntentActivities(main, 0) }.getOrDefault(emptyList())
            .map { it.loadLabel(pm).toString() to it.activityInfo.packageName }
            .filter { it.second != ctx.packageName }
            .distinctBy { it.second }
            .sortedBy { it.first.lowercase() }
    }

    /**
     * Tell the portal about this device. Blocking -- call it off the main
     * thread. At most every [minGapMs] unless something it reports changed --
     * a permission granted in Settings, an app installed -- so a resume storm
     * is one request and a change is never ten minutes late.
     */
    fun register(ctx: Context, minGapMs: Long = 10 * 60_000L): Boolean {
        val now = System.currentTimeMillis()
        val installed = launchableApps(ctx)
        val sig = listOf(name(ctx), remote(ctx), canOpenApps(ctx), installed.size).joinToString("|")
        if (now - prefs(ctx).getLong(KEY_LAST, 0L) < minGapMs && prefs(ctx).getString(KEY_SIG, "") == sig) return true
        val apps = JSONArray()
        installed.forEach { (label, pkg) -> apps.put(JSONObject().put("label", label).put("package", pkg)) }
        val body = JSONObject()
            .put("device_id", id(ctx))
            .put("name", name(ctx))
            .put("model", "${Build.MANUFACTURER} ${Build.MODEL}".trim())
            .put("kind", if (isTablet(ctx)) "tablet" else "phone")
            .put("remote", remote(ctx))
            .put("can_open_apps", canOpenApps(ctx))
            .put("version", runCatching {
                ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName
            }.getOrNull() ?: "")
            .put("apps", apps)
        val ok = FamilyApi.post("/devices/api/register", body) != null
        if (ok) prefs(ctx).edit().putLong(KEY_LAST, now).putString(KEY_SIG, sig).apply()
        AppLog.log(ctx, TAG, if (ok) "registered (${apps.length()} apps, remote=${remote(ctx)})" else "register failed")
        return ok
    }
}
