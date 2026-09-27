package com.chat.app.family

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Telephony
import android.telephony.SmsManager
import androidx.core.content.ContextCompat
import com.chat.app.AppLog

/**
 * The family chat by SMS: how a message looks as a text, sending one from this
 * phone's SIM, and recognising one that arrives.
 *
 * The format is the protocol between phones, so it is written down once, here:
 *
 *     [Alfred] <conversation> | <from>: <text> #fc:<key>
 *     [Alfred URGENTE] ...                  (the sender flagged it urgent)
 *
 * `<key>` is the portal's id as `s<id>` for a message the portal asked this
 * phone to text, or the sender's own client id for one sent with no data.
 * A text is only believed from a number in the family directory.
 */
object FamilySms {
    private const val TAG = "AlfredFamily"
    private val FORMAT = Regex("""^\[Alfred( URGENTE)?] (.+?) \| (.+?): ([\s\S]*) #fc:([A-Za-z0-9_-]+)\s*$""")

    data class Parsed(val urgent: Boolean, val threadName: String, val fromName: String, val text: String, val key: String)

    fun format(threadName: String, fromName: String, text: String, urgent: Boolean, key: String) =
        "[Alfred${if (urgent) " URGENTE" else ""}] $threadName | $fromName: $text #fc:$key"

    fun parse(body: String): Parsed? = FORMAT.matchEntire(body.trim())?.let { m ->
        Parsed(m.groupValues[1].isNotEmpty(), m.groupValues[2], m.groupValues[3], m.groupValues[4], m.groupValues[5])
    }

    fun canSend(ctx: Context) =
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.SEND_SMS) == PackageManager.PERMISSION_GRANTED

    /** Text one number from this phone's SIM. False when it could not even be queued. */
    fun send(ctx: Context, phone: String, body: String): Boolean {
        if (phone.isBlank()) return false
        if (!canSend(ctx)) {
            AppLog.report(ctx, TAG, "SMS to a family member NOT sent: no SMS permission")
            return false
        }
        return try {
            val sms = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                ctx.getSystemService(SmsManager::class.java)
            } else {
                @Suppress("DEPRECATION") SmsManager.getDefault()
            }
            sms.sendMultipartTextMessage(phone, null, sms.divideMessage(body), null, null)
            AppLog.log(ctx, TAG, "SMS sent (${body.length} chars)")
            true
        } catch (e: Exception) {
            AppLog.report(ctx, TAG, "SMS failed: ${e.message}")
            false
        }
    }
}

/** A family text arriving, which may be the only way it can on this phone right now. */
class FamilySmsReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        val ctx = context.applicationContext
        val parts = Telephony.Sms.Intents.getMessagesFromIntent(intent) ?: return
        parts.groupBy { it.originatingAddress.orEmpty() }.forEach { (from, pieces) ->
            val body = pieces.joinToString("") { it.messageBody.orEmpty() }
            val p = FamilySms.parse(body) ?: return@forEach
            val sender = FamilyDirectory.personByPhone(ctx, from) ?: run {
                AppLog.log(ctx, "AlfredFamily", "ignored a family-format SMS from an unknown number")
                return@forEach
            }
            val d = FamilyDirectory.get(ctx)
            val thread = d.groups.firstOrNull { FamilyDirectory.threadName(ctx, it.thread) == p.threadName }?.thread
                ?: d.me?.let { FamilyDirectory.dm(it, sender.login) } ?: ("p:" + sender.login)
            val fresh = FamilyStore.add(ctx, FamilyStore.Msg(p.key, thread, sender.name, p.text, p.urgent,
                System.currentTimeMillis() / 1000, mine = false, via = "sms"))
            if (!fresh) return@forEach        // the push got here first; it is already alerting or seen
            FamilyAlert.start(ctx, FamilyAlert.Alert(thread, p.threadName, sender.name, p.text, p.urgent))
            // If the portal knows it (the `s<id>` keys), tell it this phone has it.
            if (p.key.startsWith("s")) p.key.drop(1).toIntOrNull()?.let { id ->
                Thread { FamilyApi.post("/family-chat/api/delivered",
                    org.json.JSONObject().put("ids", org.json.JSONArray().put(id))) }.start()
            }
        }
    }
}
