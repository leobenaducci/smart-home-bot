package com.chat.app.family

import android.app.Activity
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.chat.app.R
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
 * One screen built in code -- no layouts, no libraries -- because this is the
 * screen for when things are going wrong, and it has to open.
 */
class FamilyActivity : Activity() {
    private lateinit var chips: LinearLayout
    private lateinit var log: LinearLayout
    private lateinit var scroll: ScrollView
    private lateinit var input: EditText
    private lateinit var urgentToggle: TextView
    private lateinit var status: TextView
    private var urgent = false
    private var threads: List<Pair<String, String>> = emptyList()
    private var thread: String? = null
    private val main = Handler(Looper.getMainLooper())

    private val wall by lazy { getColor(R.color.wall) }
    private val plaster by lazy { getColor(R.color.plaster) }
    private val accent by lazy { getColor(R.color.family_accent) }
    private val red by lazy { getColor(R.color.family_urgent) }
    private val ink by lazy { getColor(R.color.family_ink) }
    private val muted by lazy { getColor(R.color.family_muted) }
    private val line by lazy { getColor(R.color.family_line) }
    private val card by lazy { getColor(R.color.family_card) }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun round(fill: Int, radius: Int, stroke: Int? = null) = GradientDrawable().apply {
        setColor(fill)
        cornerRadius = dp(radius).toFloat()
        if (stroke != null) setStroke(dp(1), stroke)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = "Familia Chat"
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setBackgroundColor(plaster) }

        status = TextView(this).apply {
            textSize = 13f; setTextColor(plaster); alpha = 0.75f
            text = "Con datos por Alfred · sin datos por SMS"
        }
        root.addView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(wall)
            setPadding(dp(18), dp(14), dp(18), dp(14))
            addView(TextView(context).apply {
                text = "Familia Chat"; textSize = 21f; setTextColor(plaster)
                setTypeface(typeface, Typeface.BOLD)
            })
            addView(status)
        })

        chips = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(dp(12), dp(10), dp(12), dp(10))
        }
        root.addView(HorizontalScrollView(this).apply {
            isHorizontalScrollBarEnabled = false
            addView(chips)
        })
        root.addView(View(this).apply { setBackgroundColor(line) },
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1)))

        log = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(6), dp(12), dp(12))
        }
        scroll = ScrollView(this).apply { isFillViewport = true; addView(log) }
        root.addView(scroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))

        urgentToggle = TextView(this).apply {
            textSize = 13f
            setTypeface(typeface, Typeface.BOLD)
            setPadding(dp(12), dp(6), dp(12), dp(6))
            setOnClickListener { urgent = !urgent; paintUrgent() }
        }
        input = EditText(this).apply {
            hint = "Escribile a la familia…"
            textSize = 16f; setTextColor(ink); setHintTextColor(muted)
            minLines = 1; maxLines = 5
            background = round(Color.WHITE, 22, line)
            setPadding(dp(16), dp(10), dp(16), dp(10))
        }
        val send = TextView(this).apply {
            text = "➤"; textSize = 20f; gravity = Gravity.CENTER; setTextColor(plaster)
            contentDescription = "Enviar"
            background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(wall) }
            setOnClickListener { send() }
        }
        root.addView(View(this).apply { setBackgroundColor(line) },
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1)))
        root.addView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(card)
            setPadding(dp(12), dp(8), dp(12), dp(12))
            addView(urgentToggle, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = dp(8) })
            addView(LinearLayout(context).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.BOTTOM
                addView(input, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
                addView(send, LinearLayout.LayoutParams(dp(46), dp(46)).apply { marginStart = dp(8) })
            })
        })
        paintUrgent()
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

    private fun paintUrgent() {
        // Off, it is an outline; on, it is the red the alert rings with.
        urgentToggle.text = if (urgent) "‼ Urgente · va a sonar" else "‼ Marcar urgente"
        urgentToggle.setTextColor(if (urgent) Color.WHITE else red)
        urgentToggle.background = if (urgent) round(red, 16) else round(Color.TRANSPARENT, 16, red)
    }

    private fun loadThreads(select: String?) {
        threads = FamilyDirectory.threads(this)
        val pick = threads.firstOrNull { it.first == select }?.first ?: threads.firstOrNull()?.first
        if (pick == null) {
            chips.removeAllViews()
            log.removeAllViews()
            status.text = "No hay directorio familiar en esta app todavía."
            return
        }
        open(pick)
    }

    private fun paintChips() {
        chips.removeAllViews()
        for ((id, name) in threads) {
            val on = id == thread
            chips.addView(TextView(this).apply {
                text = name; textSize = 14f
                setTextColor(if (on) plaster else ink)
                if (on || id.startsWith("g:")) setTypeface(typeface, Typeface.BOLD)
                background = if (on) round(wall, 18) else round(Color.WHITE, 18, line)
                setPadding(dp(14), dp(8), dp(14), dp(8))
                setOnClickListener { if (thread != id) open(id) }
            }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT).apply { marginEnd = dp(8) })
        }
    }

    private fun open(t: String) {
        thread = t
        paintChips()
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
        val msgs = FamilyStore.list(this, t)
        if (msgs.isEmpty()) {
            log.addView(TextView(this).apply {
                text = "Sin mensajes en este teléfono todavía."
                textSize = 14f; setTextColor(muted); gravity = Gravity.CENTER
                setPadding(0, dp(56), 0, 0)
            }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
            return
        }
        val clock = SimpleDateFormat("HH:mm", Locale.getDefault())
        val dayKey = SimpleDateFormat("yyyyMMdd", Locale.getDefault())
        val dayName = SimpleDateFormat("EEEE d MMM", Locale.getDefault())
        val today = dayKey.format(Date())
        var lastDay = ""
        for (m in msgs) {
            val d = Date(m.ts * 1000)
            val k = dayKey.format(d)
            if (k != lastDay) {
                lastDay = k
                log.addView(TextView(this).apply {
                    text = if (k == today) "Hoy" else dayName.format(d)
                    textSize = 12f; setTextColor(muted)
                    background = round(line, 10)
                    setPadding(dp(10), dp(3), dp(10), dp(3))
                }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT).apply {
                    gravity = Gravity.CENTER_HORIZONTAL; topMargin = dp(10); bottomMargin = dp(6)
                })
            }
            log.addView(bubble(m, clock.format(d)))
        }
        scroll.post { scroll.fullScroll(ScrollView.FOCUS_DOWN) }
    }

    /** One message: mine on the right in the house colour, theirs on the left; urgent in red. */
    private fun bubble(m: FamilyStore.Msg, time: String): View {
        val fg = if (m.mine) plaster else ink
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(8), dp(12), dp(6))
            background = GradientDrawable().apply {
                setColor(when {
                    m.mine && m.urgent -> red
                    m.mine -> wall
                    m.urgent -> Color.rgb(0xFB, 0xE4, 0xDF)
                    else -> Color.WHITE
                })
                // The corner nearest the sender is the tail.
                val r = dp(18).toFloat(); val s = dp(4).toFloat()
                cornerRadii = if (m.mine) floatArrayOf(r, r, r, r, s, s, r, r)
                              else floatArrayOf(r, r, r, r, r, r, s, s)
                if (m.urgent && !m.mine) setStroke(dp(2), red)
            }
            elevation = if (m.mine) 0f else dp(1).toFloat()
        }
        if (!m.mine) box.addView(TextView(this).apply {
            text = m.fromName; textSize = 12f; setTextColor(if (m.urgent) red else accent)
            setTypeface(typeface, Typeface.BOLD)
        })
        if (m.urgent) box.addView(TextView(this).apply {
            text = "‼ URGENTE"; textSize = 11f; setTextColor(if (m.mine) plaster else red)
            setTypeface(typeface, Typeface.BOLD)
        })
        box.addView(TextView(this).apply {
            text = m.text; textSize = 16f; setTextColor(fg)
            maxWidth = (resources.displayMetrics.widthPixels * 0.72).toInt()
            setTextIsSelectable(true)
        })
        box.addView(TextView(this).apply {
            text = time + (if (m.via == "sms") " · SMS" else "")
            textSize = 11f; setTextColor(fg); alpha = 0.6f
        }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = dp(2) })
        return LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = if (m.mine) Gravity.END else Gravity.START
            setPadding(if (m.mine) dp(40) else 0, dp(3), if (m.mine) 0 else dp(40), dp(3))
            addView(box)
        }
    }

    private fun send() {
        val t = thread ?: return
        val text = input.text.toString().trim()
        if (text.isEmpty()) return
        val isUrgent = urgent
        val cid = "a" + UUID.randomUUID().toString().replace("-", "").take(20)
        input.setText("")
        urgent = false
        paintUrgent()
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
