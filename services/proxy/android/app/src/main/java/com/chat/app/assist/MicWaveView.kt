package com.chat.app.assist

import android.content.Context
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.RadialGradient
import android.graphics.RectF
import android.graphics.Shader
import android.view.View
import kotlin.math.max
import kotlin.math.sin
import kotlin.random.Random

/**
 * Modern voice-capture overlay: a glowing microphone above a live, gradient
 * waveform that scrolls and reacts to the mic level. Drives its own smooth
 * animation loop; the recorder just feeds it amplitude via [setLevel] and
 * flips [setState] on send / permission states.
 */
class MicWaveView(context: Context) : View(context) {

    companion object {
        private const val LEVEL_HOLD_MS = 160L
        private const val PARTIAL_LINES = 3
    }

    enum class State { LISTENING, THINKING, SENDING, DENIED }

    private val accent = 0xFFF4C15A.toInt()        // warm gold
    private val accent2 = 0xFFE8863A.toInt()       // amber
    private val bgTop = 0xF0141821.toInt()
    private val bgBottom = 0xF00B0D12.toInt()

    private val bgPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val wavePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val micPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val glowPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0xAAF2ECDD.toInt()
        textAlign = Paint.Align.CENTER
        textSize = 40f
        letterSpacing = 0.04f
    }
    // What the recognizer thinks it heard so far — brighter and larger than the
    // state label, since it's the thing you actually want to read.
    private val partialPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0xF2F2ECDD.toInt()
        textAlign = Paint.Align.CENTER
        textSize = 46f
    }

    private val barCount = 60
    private val samples = FloatArray(barCount)
    private var head = 0
    private var target = 0f
    private var smooth = 0f
    private var phase = 0f
    private var state = State.LISTENING
    private var running = true
    private var partial: String? = null
    private var secure = false
    // Level sustain: the frame loop runs at ~60 fps and decays `target` every
    // frame, but a recognizer only reports level ~10x/s — without holding the
    // last value the bars visibly gasp between callbacks.
    private var sustain = 0f
    private var lastLevelAt = 0L

    private val frame = object : Runnable {
        override fun run() {
            if (!running) return
            smooth += (target - smooth) * 0.28f
            target *= 0.85f
            if (System.currentTimeMillis() - lastLevelAt < LEVEL_HOLD_MS) {
                target = max(target, sustain * 0.8f)
            }
            phase += 0.16f
            val idle = (sin(phase.toDouble()) * 0.035 + 0.05).toFloat()
            val jitter = Random.nextFloat() * 0.12f
            val v = if (state == State.LISTENING) max(idle, smooth * (0.72f + jitter)) else idle * 0.6f
            samples[head] = v
            head = (head + 1) % barCount
            invalidate()
            postOnAnimation(this)
        }
    }

    init { postOnAnimation(frame) }

    /** amp is MediaRecorder.getMaxAmplitude(): 0..32767 */
    fun setLevel(amp: Int) = setLevelNormalized((amp / 8500f).coerceIn(0f, 1f))

    /** Already-normalized 0..1 loudness — used by the speech recognizer, whose
     *  onRmsChanged reports dB and has nothing to do with amplitude. */
    fun setLevelNormalized(v: Float) {
        val n = v.coerceIn(0f, 1f)
        sustain = n
        lastLevelAt = System.currentTimeMillis()
        if (n > target) target = n
    }

    fun setState(s: State) {
        state = s
        if (s == State.SENDING) target = 0f
        invalidate()
    }

    /** Live transcript. No-op on the lock screen: the waveform gives nothing
     *  away, readable words would. */
    fun setPartial(text: String?) {
        if (secure) return
        partial = text?.trim()?.takeIf { it.isNotEmpty() }
        invalidate()
    }

    fun setSecure(v: Boolean) {
        secure = v
        if (v) partial = null
    }

    fun stop() {
        running = false
        removeCallbacks(frame)
    }

    override fun onDetachedFromWindow() {
        super.onDetachedFromWindow()
        stop()
    }

    override fun onDraw(c: Canvas) {
        val w = width.toFloat()
        val h = height.toFloat()
        bgPaint.shader = LinearGradient(0f, 0f, 0f, h, bgTop, bgBottom, Shader.TileMode.CLAMP)
        c.drawRect(0f, 0f, w, h, bgPaint)

        val cx = w / 2f
        val micY = h * 0.34f

        // soft glow that breathes with the level
        val glowR = dp(52f) + smooth * dp(30f)
        glowPaint.shader = RadialGradient(
            cx, micY, glowR,
            (0x40 shl 24) or (accent and 0x00FFFFFF), 0x00000000, Shader.TileMode.CLAMP,
        )
        c.drawCircle(cx, micY, glowR, glowPaint)

        drawMic(c, cx, micY, dp(20f))

        if (state != State.DENIED) drawWave(c, w, h * 0.64f)

        val p = partial
        val hasPartial = p != null && state != State.DENIED
        if (hasPartial) drawPartial(c, cx, w, h * 0.80f, p!!)

        val label = when (state) {
            State.LISTENING -> "Escuchando"
            State.THINKING -> "Un momento…"
            State.SENDING -> "Enviando…"
            State.DENIED -> "Toca para permitir el micrófono"
        }
        c.drawText(label, cx, if (hasPartial) h * 0.94f else h * 0.86f, textPaint)
    }

    /** Word-wrapped, newest words kept: at most [PARTIAL_LINES] lines, with a
     *  leading ellipsis when earlier words scrolled off. */
    private fun drawPartial(c: Canvas, cx: Float, w: Float, topY: Float, text: String) {
        val maxW = w * 0.86f
        val lines = ArrayList<String>()
        var line = StringBuilder()
        for (word in text.split(' ')) {
            val candidate = if (line.isEmpty()) word else "$line $word"
            if (partialPaint.measureText(candidate) <= maxW || line.isEmpty()) {
                line = StringBuilder(candidate)
            } else {
                lines.add(line.toString())
                line = StringBuilder(word)
            }
        }
        if (line.isNotEmpty()) lines.add(line.toString())
        val truncated = lines.size > PARTIAL_LINES
        val shown = if (truncated) lines.takeLast(PARTIAL_LINES) else lines
        val lineH = partialPaint.textSize * 1.25f
        var y = topY - (shown.size - 1) * lineH
        shown.forEachIndexed { i, l ->
            c.drawText(if (truncated && i == 0) "…$l" else l, cx, y, partialPaint)
            y += lineH
        }
    }

    private fun drawWave(c: Canvas, w: Float, midY: Float) {
        val barW = dp(3.5f)
        val gap = dp(3.5f)
        val total = barCount * (barW + gap)
        var x = (w - total) / 2f + barW / 2f
        wavePaint.shader = LinearGradient(
            0f, midY - dp(58f), 0f, midY + dp(58f),
            intArrayOf(accent, accent2), null, Shader.TileMode.CLAMP,
        )
        val r = barW / 2f
        for (i in 0 until barCount) {
            val v = samples[(head + i) % barCount]
            val bh = max(dp(3f), v * dp(56f))
            c.drawRoundRect(x - r, midY - bh, x + r, midY + bh, r, r, wavePaint)
            x += barW + gap
        }
    }

    private fun drawMic(c: Canvas, cx: Float, cy: Float, r: Float) {
        micPaint.color = accent
        micPaint.style = Paint.Style.FILL
        val body = RectF(cx - r * 0.72f, cy - r * 1.35f, cx + r * 0.72f, cy + r * 0.35f)
        c.drawRoundRect(body, r * 0.72f, r * 0.72f, micPaint)

        micPaint.style = Paint.Style.STROKE
        micPaint.strokeWidth = dp(3.4f)
        micPaint.strokeCap = Paint.Cap.ROUND
        val arc = RectF(cx - r * 1.15f, cy - r * 0.95f, cx + r * 1.15f, cy + r * 1.1f)
        c.drawArc(arc, 25f, 130f, false, micPaint)
        c.drawLine(cx, cy + r * 1.1f, cx, cy + r * 1.7f, micPaint)
        c.drawLine(cx - r * 0.62f, cy + r * 1.7f, cx + r * 0.62f, cy + r * 1.7f, micPaint)

        if (state == State.DENIED) {
            micPaint.color = 0xFFE06B6B.toInt()
            micPaint.strokeWidth = dp(3.6f)
            c.drawLine(cx - r * 1.25f, cy - r * 1.5f, cx + r * 1.25f, cy + r * 1.7f, micPaint)
        }
    }

    private fun dp(v: Float) = v * resources.displayMetrics.density
}
