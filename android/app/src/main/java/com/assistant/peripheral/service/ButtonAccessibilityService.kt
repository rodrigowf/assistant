package com.assistant.peripheral.service

import android.accessibilityservice.AccessibilityService
import android.content.Context
import android.content.Intent
import android.util.Log
import android.view.KeyEvent
import androidx.localbroadcastmanager.content.LocalBroadcastManager
import com.assistant.peripheral.voice.WakeWordDetector

/**
 * Accessibility service that intercepts capacitive button long-presses.
 *
 * Detects a long-press on the recents button (KEYCODE_APP_SWITCH) and triggers
 * the realtime voice session — identical to the voice wake word path.
 *
 * Must be enabled once by the user in Settings → Accessibility → Assistant.
 */
class ButtonAccessibilityService : AccessibilityService() {

    companion object {
        private const val TAG = "ButtonAccessibility"
        private const val LONG_PRESS_MS = 600L
    }

    private var recentsDownAt = 0L

    override fun onServiceConnected() {
        Log.d(TAG, "Accessibility service connected")
    }

    override fun onKeyEvent(event: KeyEvent): Boolean {
        if (event.keyCode != KeyEvent.KEYCODE_APP_SWITCH) return false

        when (event.action) {
            KeyEvent.ACTION_DOWN -> {
                if (recentsDownAt == 0L) {
                    recentsDownAt = System.currentTimeMillis()
                }
            }
            KeyEvent.ACTION_UP -> {
                val held = System.currentTimeMillis() - recentsDownAt
                recentsDownAt = 0L
                if (held >= LONG_PRESS_MS) {
                    val enabled = getSharedPreferences("assistant_service_prefs", Context.MODE_PRIVATE)
                        .getBoolean("button_trigger_enabled", false)
                    if (!enabled) return false
                    Log.d(TAG, "Recents long-press (${held}ms) → starting voice session")
                    AssistantService.bringToForeground(this)
                    LocalBroadcastManager.getInstance(this)
                        .sendBroadcast(Intent(WakeWordDetector.ACTION_VOICE_WORD_DETECTED))
                    return true  // consume the event (suppress recents drawer)
                }
            }
        }
        return false  // let short press pass through normally
    }

    override fun onAccessibilityEvent(event: android.view.accessibility.AccessibilityEvent?) {}
    override fun onInterrupt() {}
}
