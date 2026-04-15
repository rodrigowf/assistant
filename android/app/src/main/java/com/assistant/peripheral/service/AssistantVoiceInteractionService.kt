package com.assistant.peripheral.service

import android.content.Intent
import android.os.Bundle
import android.service.voice.VoiceInteractionService
import android.service.voice.VoiceInteractionSession
import android.util.Log
import com.assistant.peripheral.VoiceShortcutActivity

/**
 * Minimal VoiceInteractionService stub.
 *
 * On Lollipop, long-press home goes to the registered VoiceInteractionService, not
 * just an Activity with ASSIST intent filter. This service immediately launches
 * VoiceShortcutActivity (which does the actual work: wake screen, start voice session).
 */
class AssistantVoiceInteractionService : VoiceInteractionService() {

    companion object {
        private const val TAG = "AssistantVIS"
    }

    override fun onReady() {
        super.onReady()
        Log.d(TAG, "VoiceInteractionService ready")
    }

    override fun onShutdown() {
        super.onShutdown()
        Log.d(TAG, "VoiceInteractionService shutdown")
    }

    // Called when the user triggers the assist gesture (long-press home)
    override fun showSession(args: Bundle?, flags: Int) {
        Log.d(TAG, "showSession — launching VoiceShortcutActivity")
        val intent = Intent(this, VoiceShortcutActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }
        startActivity(intent)
    }
}
