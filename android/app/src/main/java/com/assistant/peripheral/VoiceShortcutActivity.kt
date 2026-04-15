package com.assistant.peripheral

import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.localbroadcastmanager.content.LocalBroadcastManager
import com.assistant.peripheral.service.AssistantService
import com.assistant.peripheral.voice.WakeWordDetector

/**
 * Transparent trampoline activity launched from the home screen shortcut.
 *
 * Does exactly what the voice wake word does:
 *   1. Acquires a wake lock to turn the screen on and dismiss the keyguard.
 *   2. Fires ACTION_VOICE_WORD_DETECTED so MainActivity navigates to Chat
 *      and starts the realtime voice session.
 *   3. Finishes immediately so it leaves no back-stack entry.
 */
class VoiceShortcutActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Turn screen on and dismiss keyguard (same flags as wake word path)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setTurnScreenOn(true)
            setShowWhenLocked(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(
                WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD
            )
        }

        // Bring MainActivity to front (same as AssistantService.bringToForeground but
        // we are already running in the context of a visible Activity so no wake lock needed).
        val mainIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or
                    Intent.FLAG_ACTIVITY_REORDER_TO_FRONT or
                    Intent.FLAG_ACTIVITY_SINGLE_TOP
            putExtra(AssistantService.EXTRA_WAKE_WORD_TRIGGERED, true)
        }
        startActivity(mainIntent)

        // Fire the voice word broadcast — MainActivity's wakeWordReceiver picks this up
        // and calls onVoiceWordDetected → navigate to Chat + startVoiceSession()
        LocalBroadcastManager.getInstance(this)
            .sendBroadcast(Intent(WakeWordDetector.ACTION_VOICE_WORD_DETECTED))

        finish()
    }
}
