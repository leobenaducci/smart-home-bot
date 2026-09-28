package com.chat.app.family

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The SMS format is the protocol between two phones that may have nothing
 * else in common at the moment it matters, so what one writes the other must
 * read back exactly. Invented household: Tomi, Mora.
 */
class FamilySmsFormatTest {

    @Test fun `a message reads back as it was written`() {
        val body = FamilySms.format("Familia", "Tomi", "¿Dónde están?", false, "s42")
        val p = FamilySms.parse(body)!!
        assertEquals("Familia", p.threadName)
        assertEquals("Tomi", p.fromName)
        assertEquals("¿Dónde están?", p.text)
        assertEquals("s42", p.key)
        assertFalse(p.urgent)
    }

    @Test fun `urgent survives the trip`() {
        val p = FamilySms.parse(FamilySms.format("Padres", "Mora", "Llamame ya", true, "a1b2c3"))!!
        assertTrue(p.urgent)
        assertEquals("a1b2c3", p.key)
    }

    @Test fun `text with the separators and new lines in it is kept whole`() {
        val text = "Estoy en: la esquina | cerca del colegio\nvengan"
        val p = FamilySms.parse(FamilySms.format("Familia", "Tomi", text, false, "s7"))!!
        assertEquals(text, p.text)
        assertEquals("Tomi", p.fromName)
    }

    @Test fun `an ordinary text is not a family message`() {
        assertNull(FamilySms.parse("Hola, ¿venís a cenar?"))
        assertNull(FamilySms.parse("[Alfred] Familia | Tomi: sin clave"))
    }
}
