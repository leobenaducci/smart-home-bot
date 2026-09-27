package com.chat.app.family

import android.app.Activity
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.ViewGroup
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

/**
 * The family chat, native and offline-first: the conversation list comes from
 * the directory built into the APK, and what was received and sent is kept on
 * the phone. With data it goes through the portal like the chat page's Family
 * panel; without it, a message goes out as an SMS from this phone's SIM to
 * every recipient with a number, and is synced to the portal later.
 *
 * Deliberately plain -- one screen built in code -- because this is the screen
 * for when things are going wrong.
 */
class FamilyActivity : Activity() {
    private lateinit var picker: Spinner
    private lateinit var log: LinearLayout
    private lateinit var scroll: ScrollView
    private lateinit var input: EditText
    private lateinit var urgent: CheckBox
    private lateinit var status: TextView
    private var threads: List<Pair<String, String>> = emptyList()
    private var thread: String? = null
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = "Chat familiar"
        val pad = (12 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad) }
        picker = Spinner(this)
        status = TextView(this).apply { setTextColor(Color.GRAY); textSize = 12f }
        log = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        scroll = ScrollView(this).apply { addView(log) }
        input = EditText(this).apply { hint = "Escribile a la familia…"; minLines = 2 }
        urgent = CheckBox(this).apply { text = "Urgente (hace sonar el teléfono)"; setTextColor(Color.rgb(192, 57, 43)) }
        val send = Button(this).apply { text = "Enviar"; setOnClickListener { send() } }
        root.addView(picker)
        root.addView(status)
        root.addView(scroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        root.addView(input)
        root.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            addView(urgent, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(send)
        })
        setContentView(root)
        loadThreads(intent.getStringExtra(FamilyAlert.EXTRA_THREAD))
        // The directory may be newer on the portal; the built-in one is enough to start.
        Thread {
            FamilyDirectory.refresh(applicationContext)
            FamilyStore.flush(applicationContext)
            main.post { loadThreads(thread) }
        }.start()
    }

    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        intent.getStringExtra(FamilyAlert.EXTRA_THREAD)?.let { loadThreads(it) }
    }

    private fun loadThreads(select: String?) {
        threads = FamilyDirectory.threads(this)
        picker.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, threads.map { it.second })
        val i = threads.indexOfFirst { it.first == select }.takeIf { it >= 0 } ?: 0
        picker.setSelection(i)
        picker.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(p: AdapterView<*>?, v: android.view.View?, pos: Int, id: Long) { open(threads[pos].first) }
            override fun onNothingSelected(p: AdapterView<*>?) {}
        }
        if (threads.isNotEmpty()) open(threads[i].first)
        else status.text = "No hay directorio familiar en esta app todavía."
    }

    private fun open(t: String) {
        thread = t
        // Looking at it is seeing it: stop the alert here, and everywhere else via the portal.
        FamilyAlert.stop(applicationContext, t)
        if (t.startsWith("g:") || t.startsWith("dm:")) Thread {
            FamilyApi.post("/family-chat/api/seen", JSONObject().put("thread", t))
        }.start()
        render()
    }

    private fun render() {
        val t = thread ?: return
        log.removeAllViews()
        val fmt = SimpleDateFormat("dd/MM HH:mm", Locale.getDefault())
        val msgs = FamilyStore.list(this, t)
        if (msgs.isEmpty()) log.addView(TextView(this).apply { text = "Sin mensajes en este teléfono." })
        for (m in msgs) log.addView(TextView(this).apply {
            text = (if (m.mine) "Yo" else m.fromName) + (if (m.urgent) " ‼" else "") + " · " +
                fmt.format(Date(m.ts * 1000)) + (if (m.via == "sms") " · SMS" else "") + "\n" + m.text
            setPadding(0, 10, 0, 10)
            if (m.urgent) setTypeface(typeface, Typeface.BOLD)
            gravity = if (m.mine) Gravity.END else Gravity.START
        })
        scroll.post { scroll.fullScroll(ScrollView.FOCUS_DOWN) }
    }

    private fun send() {
        val t = thread ?: return
        val text = input.text.toString().trim()
        if (text.isEmpty()) return
        val isUrgent = urgent.isChecked
        val cid = "a" + UUID.randomUUID().toString().replace("-", "").take(20)
        input.setText("")
        urgent.isChecked = false
        status.text = "Enviando…"
        val app = applicationContext
        Thread {
            val dir = FamilyDirectory.get(app)
            val me = dir.me?.let { FamilyDirectory.person(app, it) }
            val online = (t.startsWith("g:") || t.startsWith("dm:")) &&
                FamilyApi.post("/family-chat/api/send", JSONObject().put("thread", t).put("text", text)
                    .put("urgent", isUrgent).put("client_id", cid)) != null
            var note = "Enviado."
            if (!online) {
                // No portal: every recipient with a number gets it from this SIM now.
                val body = FamilySms.format(FamilyDirectory.threadName(app, t), me?.name ?: "Familia", text, isUrgent, cid)
                val rcpts = FamilyDirectory.recipients(app, if (t.startsWith("p:") && dir.me != null)
                    FamilyDirectory.dm(dir.me, t.removePrefix("p:")) else t)
                    .ifEmpty { listOfNotNull(FamilyDirectory.person(app, t.removePrefix("p:"))) }
                val sent = rcpts.count { FamilySms.send(app, it.phone, body) }
                val noNumber = rcpts.count { it.phone.isBlank() }
                FamilyStore.queue(app, cid, t, text, isUrgent)
                note = "Sin conexión: enviado por SMS a $sent" +
                    (if (noNumber > 0) " ($noNumber sin número)" else "") + "."
            }
            FamilyStore.add(app, FamilyStore.Msg(cid, t, me?.name ?: "Yo", text, isUrgent,
                System.currentTimeMillis() / 1000, mine = true, via = if (online) "app" else "sms"))
            main.post { status.text = note; render() }
        }.start()
    }
}
