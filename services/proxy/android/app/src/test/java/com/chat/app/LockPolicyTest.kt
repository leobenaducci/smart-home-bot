package com.chat.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * When Alfred asks for a fingerprint again.
 *
 * Run: `./gradlew :app:testReleaseUnitTest` (plain JVM — no device, no
 * emulator, which is why the policy holds no Android types).
 *
 * These are the cases nobody was going to act out on a phone, and they are the
 * ones that matter: the gate opening is a stranger reading the family's chat,
 * the gate sticking is somebody being asked for a fingerprint for the fourth
 * time in a minute. Both were live — the second is what prompted the change.
 */
class LockPolicyTest {

    private val fiveMinutes = 5 * 60_000L

    private fun unlocked(awayMs: Long = 5 * 60_000L) =
        LockPolicy(awayMs).apply { onAuthenticated() }

    // --- the complaint this change came from ---------------------------------

    @Test fun `a quick trip to another app does not ask again`() {
        val lock = unlocked()
        lock.onBackground(1_000)
        assertFalse(lock.needsAuthOnForeground(4_000))   // three seconds later
    }

    @Test fun `nor does a long look at another app, while the phone stays awake`() {
        val lock = unlocked()
        lock.onBackground(1_000)
        assertFalse(lock.needsAuthOnForeground(1_000 + fiveMinutes - 1))
    }

    @Test fun `nor do the app's own pickers and dialogs`() {
        // This is what the nine skipRelock flags existed for. Picking a photo
        // stops the activity exactly like any other app switch — so if the
        // ordinary switch no longer re-locks, neither does this, and the flags
        // have nothing left to do.
        val lock = unlocked()
        repeat(4) { i ->
            lock.onBackground(1_000L + i * 2_000)
            assertFalse("picker #$i", lock.needsAuthOnForeground(2_000L + i * 2_000))
        }
    }

    // --- what does re-lock ----------------------------------------------------

    @Test fun `the screen turning off locks it`() {
        val lock = unlocked()
        lock.onBackground(1_000)
        lock.onScreenOff()
        assertTrue(lock.needsAuthOnForeground(2_000))
    }

    @Test fun `the screen turning off in the foreground locks it too`() {
        // The phone idling out while Alfred is open. No onBackground has run
        // yet when the screen goes off — Android stops us after — so a policy
        // that only remembered "screen went off while away" would miss the
        // commonest lock of all.
        val lock = unlocked()
        lock.onScreenOff()
        lock.onBackground(1_000)
        assertTrue(lock.needsAuthOnForeground(1_500))
    }

    @Test fun `a long absence locks it even if the screen never went off`() {
        val lock = unlocked()
        lock.onBackground(1_000)
        assertTrue(lock.needsAuthOnForeground(1_000 + fiveMinutes + 1))
    }

    // --- where the lock lives, which is half of whether it works -----------

    @Test fun `the lock survives the Activity being recreated`() {
        // The bug that made the whole fix look like it had not shipped. The
        // policy was a field of MainActivity, so every Activity rebuild — a
        // reclaimed background task, a locale or dark-mode change, "Don't keep
        // activities" — constructed a new one, and a new one is locked. The
        // fingerprint came back on every app switch exactly as before.
        LockPolicy.resetForTest()
        LockPolicy.shared(fiveMinutes).onAuthenticated()
        // ...the Activity dies and is built again; it asks for the lock afresh.
        val afterRebuild = LockPolicy.shared(fiveMinutes)
        assertTrue("same lock, not a new one", afterRebuild.isUnlocked)
        assertFalse(afterRebuild.needsAuthOnForeground(1_000))
        LockPolicy.resetForTest()
    }

    @Test fun `but the process dying still locks it`() {
        LockPolicy.resetForTest()
        LockPolicy.shared(fiveMinutes).onAuthenticated()
        LockPolicy.resetForTest()            // what process death amounts to
        assertTrue(LockPolicy.shared(fiveMinutes).needsAuthOnForeground(0))
        LockPolicy.resetForTest()
    }

    @Test fun `a fresh policy is locked, which is what a cold start gets`() {
        assertTrue(LockPolicy(fiveMinutes).needsAuthOnForeground(0))
    }

    // --- staying locked -------------------------------------------------------

    @Test fun `it stays locked until somebody actually authenticates`() {
        val lock = LockPolicy(fiveMinutes)
        assertTrue(lock.needsAuthOnForeground(1_000))
        // Coming back again is not authenticating. Were `needsAuth` to clear
        // the state it reports on, a second onStart — which Android will
        // happily deliver — would open the gate on its own.
        lock.onBackground(2_000)
        assertTrue(lock.needsAuthOnForeground(3_000))
        assertFalse(lock.isUnlocked)
        lock.onAuthenticated()
        assertFalse(lock.needsAuthOnForeground(4_000))
    }

    @Test fun `a timeout lock is a lock, not a one-off answer`() {
        // The subtle one, and the reason needsAuth *settles* the state instead
        // of computing an answer from it: if the timeout only coloured the
        // return value, the very next foreground would find a zero-length
        // absence, see `isUnlocked` still true, and let the person straight in
        // without a fingerprint. Two onStarts in a row is not exotic — a
        // rotation or a notification tap does it.
        val lock = unlocked()
        lock.onBackground(1_000)
        assertTrue(lock.needsAuthOnForeground(1_000 + fiveMinutes + 1))
        assertFalse("the timeout must actually lock it", lock.isUnlocked)
        lock.onBackground(2_000_000)
        assertTrue(lock.needsAuthOnForeground(2_000_001))
    }

    @Test fun `an absence already paid for is not charged twice`() {
        // The activity can be foregrounded twice without a background between
        // (configuration change, notification tap on an open app). The second
        // pass must not re-measure the same absence, or it would compare
        // against an `awaySince` from two switches ago and lock at random.
        val lock = unlocked()
        lock.onBackground(1_000)
        assertFalse(lock.needsAuthOnForeground(2_000))
        assertFalse(lock.needsAuthOnForeground(2_000 + fiveMinutes + 1))
    }

    @Test fun `a screen-off during the prompt survives the prompt`() {
        // The device-credential fallback runs in its own activity, so the
        // sequence is: prompt up → we stop → the person gives up and the
        // screen locks → they come back. The gate must still be shut.
        val lock = LockPolicy(fiveMinutes)
        assertTrue(lock.needsAuthOnForeground(0))
        lock.onBackground(1_000)
        lock.onScreenOff()
        lock.onAuthenticated()          // whatever the prompt does next...
        lock.onScreenOff()              // ...another lock still shuts it
        assertTrue(lock.needsAuthOnForeground(2_000))
    }

    // --- the clock ------------------------------------------------------------

    @Test fun `the timeout cannot be walked past by the clock going backwards`() {
        // elapsedRealtime is monotonic, so this cannot happen in the app —
        // but it is exactly what a wall clock would do if somebody changed the
        // time zone, and it is the reason the caller must pass the other one.
        // Backwards means a negative gap, which must never read as "no time
        // passed, stay open" for a policy that was already locked.
        val lock = LockPolicy(fiveMinutes)
        lock.onBackground(10_000)
        assertTrue(lock.needsAuthOnForeground(1_000))
    }

    @Test fun `a zero timeout re-locks on any real absence`() {
        val lock = unlocked(awayMs = 0)
        lock.onBackground(1_000)
        assertTrue(lock.needsAuthOnForeground(1_001))
    }

    @Test fun `but not on a foreground that never left`() {
        val lock = unlocked(awayMs = 0)
        assertFalse(lock.needsAuthOnForeground(1_000))
    }
}
