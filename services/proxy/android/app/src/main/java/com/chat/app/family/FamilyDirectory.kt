package com.chat.app.family

import android.content.Context
import com.chat.app.AppLog
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Who the family is and how to reach them without data: names, phone numbers
 * and the family chat's groups.
 *
 * Two copies, newest wins: the one the APK was built with (assets, written by
 * the admin page's build from the users page), and the one fetched from the
 * portal whenever this phone is online. A phone that never gets data again
 * still has the first, which is the point -- SMS is the way through when
 * nothing else is.
 */
object FamilyDirectory {
    data class Person(val login: String, val name: String, val phone: String, val parents: Boolean)
    data class Group(val id: String, val thread: String, val name: String, val members: List<String>)
    data class Directory(val me: String?, val people: List<Person>, val groups: List<Group>)

    private const val FILE = "family_directory.json"
    private const val TAG = "AlfredFamily"
    @Volatile private var cached: Directory? = null

    fun get(ctx: Context): Directory {
        cached?.let { return it }
        val text = runCatching { File(ctx.filesDir, FILE).readText() }.getOrNull()
            ?: runCatching { ctx.assets.open(FILE).bufferedReader().use { it.readText() } }.getOrNull()
        return parse(text).also { cached = it }
    }

    fun parse(text: String?): Directory {
        val o = try { JSONObject(text ?: "{}") } catch (e: Exception) { JSONObject() }
        val people = (o.optJSONArray("people") ?: JSONArray()).let { a ->
            (0 until a.length()).mapNotNull { i ->
                a.optJSONObject(i)?.let {
                    Person(it.optString("login"), it.optString("name"), it.optString("phone"), it.optBoolean("parents"))
                }
            }.filter { it.login.isNotBlank() }
        }
        val groups = (o.optJSONArray("groups") ?: JSONArray()).let { a ->
            (0 until a.length()).mapNotNull { i ->
                a.optJSONObject(i)?.let { g ->
                    val m = g.optJSONArray("members") ?: JSONArray()
                    Group(g.optString("id"), g.optString("thread").ifBlank { "g:" + g.optString("id") },
                          g.optString("name"), (0 until m.length()).map { m.optString(it) })
                }
            }
        }
        val me = o.optString("me").takeIf { it.isNotBlank() && it != "null" }
        return Directory(me, people, groups)
    }

    /** Fetch the current directory from the portal and keep it. Off the main thread. */
    fun refresh(ctx: Context) {
        val fresh = FamilyApi.get("/family-chat/api/directory") ?: return
        if (!fresh.has("people")) return
        runCatching { File(ctx.filesDir, FILE).writeText(fresh.toString()) }
        cached = parse(fresh.toString())
        AppLog.log(ctx, TAG, "directory refreshed: ${cached?.people?.size} people, ${cached?.groups?.size} groups")
    }

    private fun digits(s: String) = s.filter { it.isDigit() }

    /** The family member a phone number belongs to, however the network wrote it. */
    fun personByPhone(ctx: Context, number: String): Person? {
        val n = digits(number)
        if (n.length < 6) return null
        return get(ctx).people.firstOrNull { p ->
            val d = digits(p.phone)
            d.length >= 6 && (d == n || d.endsWith(n.takeLast(9)) || n.endsWith(d.takeLast(9)))
        }
    }

    fun person(ctx: Context, login: String) = get(ctx).people.firstOrNull { it.login == login }

    fun dm(a: String, b: String) = "dm:" + listOf(a, b).sorted().joinToString(":")

    /** Everyone in a thread but this phone's own person. */
    fun recipients(ctx: Context, thread: String): List<Person> {
        val d = get(ctx)
        val logins = when {
            thread.startsWith("g:") -> d.groups.firstOrNull { it.thread == thread }?.members.orEmpty()
            thread.startsWith("dm:") -> thread.removePrefix("dm:").split(":")
            else -> emptyList()
        }
        return logins.filter { it != d.me }.mapNotNull { l -> d.people.firstOrNull { it.login == l } }
    }

    fun threadName(ctx: Context, thread: String): String {
        val d = get(ctx)
        if (thread.startsWith("g:")) {
            val g = d.groups.firstOrNull { it.thread == thread } ?: return thread
            return when (g.id) { "family" -> "Familia"; "parents" -> "Padres"; else -> g.name }
        }
        val other = thread.removePrefix("dm:").split(":").firstOrNull { it != d.me } ?: return thread
        return d.people.firstOrNull { it.login == other }?.name ?: other
    }

    /** Every conversation this phone can start: its groups, then each person. */
    fun threads(ctx: Context): List<Pair<String, String>> {
        val d = get(ctx)
        val groups = d.groups.filter { d.me == null || d.me in it.members }.map { it.thread to threadName(ctx, it.thread) }
        val people = d.people.filter { it.login != d.me }.map {
            (if (d.me != null) dm(d.me, it.login) else "p:" + it.login) to it.name
        }
        return groups + people
    }
}
