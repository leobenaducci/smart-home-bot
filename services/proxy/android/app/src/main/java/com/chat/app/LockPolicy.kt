package com.chat.app

/**
 * When Alfred asks for a fingerprint again.
 *
 * Its own class, holding no Android types, so the decision can be run in a
 * plain JVM test. That is not tidiness: this policy used to live as three
 * fields tangled through `onStart`/`onStop`, where the only way to try a case
 * was to install the app and act it out on a phone — so the cases nobody
 * fancied acting out (screen off while the prompt is up, the clock moving
 * under us, coming back after an hour) were never tried at all.
 *
 * The rule is that the lock follows the **phone's** lock, not the app's own
 * comings and goings. It used to drop on every trip to the background, so
 * glancing at another app for three seconds cost another fingerprint, and each
 * place where that was obviously wrong — the file picker, the camera, a
 * permission dialog, the settings screen the app opens itself — carried an
 * exemption flag. There were nine, one per complaint, and every one of them
 * was a patch on the policy rather than a fix: none of those moments is the
 * phone leaving your hand, which is the thing the lock is about.
 *
 * Two things re-lock, and both mean the phone genuinely left you:
 *
 *  - **the screen turned off** — the phone locking itself, which is the whole
 *    of the ordinary case: a phone put down does this within a minute or two;
 *  - **a long absence with the screen still on** — an unlocked phone in
 *    somebody else's hands.
 *
 * A third comes free: this object is built unlocked-for-nobody, so a cold
 * start after the process dies asks again.
 *
 * The trade, made deliberately: while the phone is unlocked and in use, Alfred
 * opens without asking. The phone's own lock screen is the boundary; this one
 * stands behind it and catches the phone that is unlocked but no longer yours.
 *
 * Times are on a **monotonic** clock (`SystemClock.elapsedRealtime`), never
 * wall time — a policy that measures absence with a clock the user can change
 * is a policy that can be opened by changing the clock.
 */
class LockPolicy(private val awayRelockMs: Long) {

    companion object {
        @Volatile private var instance: LockPolicy? = null

        /**
         * The one lock for this process.
         *
         * It lived on the Activity as `private val lock = LockPolicy(...)`,
         * which gives it the wrong lifetime by a long way: Android destroys
         * and recreates an Activity whenever it feels like reclaiming a
         * backgrounded one, whenever a configuration it was not told to
         * absorb changes — the locale, dark mode, display size, font size —
         * and always with "Don't keep activities" switched on. Each of those
         * built a *fresh* policy, and a fresh policy is locked, so the
         * fingerprint came back on the next app switch exactly as before.
         *
         * The bug survived the whole test suite because those tests hold a
         * policy and drive it; nothing they could do would replace the object
         * underneath them, which is precisely what was happening.
         *
         * A process-scoped singleton is the right lifetime, and the fail-safe
         * is unchanged: when the process dies this goes with it, so a cold
         * start is locked.
         */
        fun shared(awayRelockMs: Long): LockPolicy =
            instance ?: synchronized(this) {
                instance ?: LockPolicy(awayRelockMs).also { instance = it }
            }

        /** Testing only: forget the process-wide instance. */
        fun resetForTest() = synchronized(this) { instance = null }
    }

    /** True while the person has authenticated and nothing has revoked it. */
    var isUnlocked: Boolean = false
        private set

    /** When we went to the background; 0 while in the foreground. */
    private var awaySince = 0L

    /** They passed the prompt. */
    fun onAuthenticated() {
        isUnlocked = true
    }

    /**
     * The screen turned off — the phone is locking itself.
     *
     * Takes effect immediately rather than being remembered as "it happened
     * while we were away", because the screen also turns off while Alfred is
     * in the *foreground* (the phone idling out in your hand), and that is
     * every bit as much the phone locking.
     */
    fun onScreenOff() {
        isUnlocked = false
    }

    /** Alfred went to the background at `now`. */
    fun onBackground(now: Long) {
        awaySince = now
    }

    /**
     * Alfred came back to the foreground at `now`. True if the person has to
     * authenticate.
     *
     * Not a query — it settles the absence and clears it, so asking twice
     * cannot re-charge a timeout that has already been paid.
     */
    fun needsAuthOnForeground(now: Long): Boolean {
        val away = if (awaySince == 0L) 0L else now - awaySince
        awaySince = 0L
        // `>` and not `>=`: a zero-length absence is being restarted in place,
        // which is not an absence at all. Only matters when awayRelockMs is 0,
        // and there it is the difference between "never re-lock on time" and
        // "re-lock on every flicker".
        if (away > awayRelockMs) isUnlocked = false
        return !isUnlocked
    }
}
