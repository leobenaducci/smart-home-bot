package com.chat.app.family

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * What this phone has sent and received in the family chat, kept locally so
 * the family screen works with no data, and an outbox of what was sent by SMS
 * while the portal was out of reach -- synced to it later under the same
 * client id, which the portal uses to keep one message one message.
 */
object FamilyStore {
    data class Msg(val key: String, val thread: String, val fromName: String, val text: String,
                   val urgent: Boolean, val ts: Long, val mine: Boolean, val via: String)

    private const val FILE = "family_messages.json"
    private const val OUTBOX = "family_outbox.json"
    private const val KEEP = 500

    @Synchronized
    private fun load(ctx: Context, name: String): JSONArray =
        runCatching { JSONArray(File(ctx.filesDir, name).readText()) }.getOrDefault(JSONArray())

    @Synchronized
    private fun save(ctx: Context, name: String, a: JSONArray) {
        runCatching { File(ctx.filesDir, name).writeText(a.toString()) }
    }

    /** False when a message with this key is already here (push and SMS both arrived). */
    @Synchronized
    fun add(ctx: Context, m: Msg): Boolean {
        val a = load(ctx, FILE)
        for (i in 0 until a.length()) if (a.optJSONObject(i)?.optString("key") == m.key) return false
        a.put(JSONObject().put("key", m.key).put("thread", m.thread).put("from", m.fromName)
            .put("text", m.text).put("urgent", m.urgent).put("ts", m.ts).put("mine", m.mine).put("via", m.via))
        val trimmed = JSONArray()
        for (i in maxOf(0, a.length() - KEEP) until a.length()) trimmed.put(a.get(i))
        save(ctx, FILE, trimmed)
        return true
    }

    @Synchronized
    fun has(ctx: Context, key: String): Boolean {
        val a = load(ctx, FILE)
        return (0 until a.length()).any { a.optJSONObject(it)?.optString("key") == key }
    }

    @Synchronized
    fun list(ctx: Context, thread: String): List<Msg> {
        val a = load(ctx, FILE)
        return (0 until a.length()).mapNotNull { a.optJSONObject(it) }
            .filter { it.optString("thread") == thread }
            .map { Msg(it.optString("key"), it.optString("thread"), it.optString("from"), it.optString("text"),
                       it.optBoolean("urgent"), it.optLong("ts"), it.optBoolean("mine"), it.optString("via")) }
    }

    @Synchronized
    fun queue(ctx: Context, clientId: String, thread: String, text: String, urgent: Boolean) {
        val a = load(ctx, OUTBOX)
        a.put(JSONObject().put("client_id", clientId).put("thread", thread).put("text", text).put("urgent", urgent))
        save(ctx, OUTBOX, a)
    }

    /** Send what was texted while offline to the portal, once it answers. Off the main thread. */
    @Synchronized
    fun flush(ctx: Context) {
        val a = load(ctx, OUTBOX)
        if (a.length() == 0) return
        val left = JSONArray()
        for (i in 0 until a.length()) {
            val o = a.optJSONObject(i) ?: continue
            if (!o.optString("thread").startsWith("g:") && !o.optString("thread").startsWith("dm:")) continue
            val ok = FamilyApi.post("/family-chat/api/send", JSONObject()
                .put("thread", o.optString("thread")).put("text", o.optString("text"))
                .put("urgent", o.optBoolean("urgent")).put("client_id", o.optString("client_id"))
                .put("sms_sent", true))
            if (ok == null) left.put(o)
        }
        save(ctx, OUTBOX, left)
    }
}
