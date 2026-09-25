package com.chat.app

import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.core.content.FileProvider
import java.io.File

/**
 * Shared APK download + install helpers.
 *
 * Used by both update paths:
 *  - the in-app update flow (MainActivity's AndroidApp bridge) downloads from
 *    HomeCore's /chat/app-download with the WebView session cookie;
 *  - the ntfy self-update path (NtfyClientService) downloads an ntfy attachment
 *    with a bearer token.
 *
 * The APK lands in the app-external "updates/" dir (mapped in file_paths.xml)
 * and is handed to the system package installer via a FileProvider content URI
 * (requires the REQUEST_INSTALL_PACKAGES permission, already declared).
 */
object ApkInstaller {
    const val UPDATES_DIR = "updates"
    const val APK_FILENAME = "alfred-update.apk"
    private const val APK_MIME = "application/vnd.android.package-archive"

    fun apkFile(context: Context): File = File(context.getExternalFilesDir(UPDATES_DIR), APK_FILENAME)

    /**
     * Enqueue a background download of the APK to updates/APK_FILENAME (deleting
     * any previous one). `headerName`/`headerValue` optionally add an auth header
     * — "Cookie" for HomeCore, "Authorization: Bearer …" for ntfy. Returns the
     * DownloadManager id, or -1 on failure.
     */
    fun enqueue(
        context: Context,
        url: String,
        title: String,
        headerName: String? = null,
        headerValue: String? = null,
    ): Long {
        val req = DownloadManager.Request(Uri.parse(url)).apply {
            if (!headerName.isNullOrBlank() && !headerValue.isNullOrBlank()) {
                addRequestHeader(headerName, headerValue)
            }
            setTitle(title)
            setDestinationInExternalFilesDir(context, UPDATES_DIR, APK_FILENAME)
            setNotificationVisibility(DownloadManager.Request.VISIBILITY_HIDDEN)
            setMimeType(APK_MIME)
        }
        val dm = context.getSystemService(DownloadManager::class.java) ?: return -1L
        runCatching { apkFile(context).delete() }
        return runCatching { dm.enqueue(req) }.getOrDefault(-1L)
    }

    /** Intent that launches the system package installer for `file`. */
    fun buildInstallIntent(context: Context, file: File): Intent {
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
        return Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, APK_MIME)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
    }

    /** Launch the installer for the downloaded APK (no-op if it isn't there). */
    fun launchInstall(context: Context, file: File = apkFile(context)) {
        if (!file.exists()) return
        runCatching { context.startActivity(buildInstallIntent(context, file)) }
    }
}
