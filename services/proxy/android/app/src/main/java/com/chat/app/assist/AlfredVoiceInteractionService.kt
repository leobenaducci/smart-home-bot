package com.chat.app.assist

import android.service.voice.VoiceInteractionService

/**
 * Registers Alfred as a device assistant. Its presence (plus the manifest
 * declaration + res/xml/interaction_service.xml) makes Alfred selectable under
 * Settings → Apps → Default apps → Digital assistant app, and launchable by the
 * assist gesture (long-press home / power hold). No always-listening hotword —
 * the session simply opens the chat.
 */
class AlfredVoiceInteractionService : VoiceInteractionService()
