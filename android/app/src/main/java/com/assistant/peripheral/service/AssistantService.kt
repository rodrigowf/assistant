package com.assistant.peripheral.service

import android.app.*
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import com.assistant.peripheral.MainActivity
import com.assistant.peripheral.R
import com.assistant.peripheral.voice.WakeWordDetector

/**
 * Foreground service that keeps the assistant running in the background.
 * Maintains WebSocket connection and listens for wake word.
 */
class AssistantService : Service() {

    companion object {
        private const val TAG = "AssistantService"
        private const val NOTIFICATION_ID = 1001
        private const val CHANNEL_ID = "assistant_service_channel"
        private const val EXTRA_ENABLE_WAKE_WORD = "enable_wake_word"
        private const val EXTRA_WAKE_WORD = "wake_word"
        private const val EXTRA_VOICE_WORD = "voice_word"

        fun start(context: Context) {
            val intent = Intent(context, AssistantService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            val intent = Intent(context, AssistantService::class.java)
            context.stopService(intent)
        }

        fun updateWakeWord(context: Context, enabled: Boolean, wakeWord: String, voiceWord: String = "") {
            val intent = Intent(context, AssistantService::class.java).apply {
                putExtra(EXTRA_ENABLE_WAKE_WORD, enabled)
                putExtra(EXTRA_WAKE_WORD, wakeWord)
                putExtra(EXTRA_VOICE_WORD, voiceWord)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }
    }

    private var wakeWordDetector: WakeWordDetector? = null

    override fun onCreate() {
        super.onCreate()
        Log.d(TAG, "Service created")
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.d(TAG, "Service started")
        startForeground(NOTIFICATION_ID, createNotification())

        // Handle wake word enable/disable
        intent?.let {
            if (it.hasExtra(EXTRA_ENABLE_WAKE_WORD)) {
                val enableWakeWord = it.getBooleanExtra(EXTRA_ENABLE_WAKE_WORD, false)
                val wakeWord = it.getStringExtra(EXTRA_WAKE_WORD) ?: "hey assistant"
                val voiceWord = it.getStringExtra(EXTRA_VOICE_WORD) ?: ""
                if (enableWakeWord) {
                    startWakeWord(wakeWord, voiceWord)
                } else {
                    stopWakeWord()
                }
            }
        }

        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        super.onDestroy()
        wakeWordDetector?.release()
        Log.d(TAG, "Service destroyed")
    }

    private fun startWakeWord(wakeWord: String, voiceWord: String) {
        wakeWordDetector?.stop()
        wakeWordDetector = WakeWordDetector(this, wakeWord, voiceWord)
        wakeWordDetector?.start()
        Log.d(TAG, "Wake word detection started — wake: \"$wakeWord\", voice: \"$voiceWord\"")
    }

    private fun stopWakeWord() {
        wakeWordDetector?.stop()
        wakeWordDetector = null
        Log.d(TAG, "Wake word detection stopped")
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = getString(R.string.notification_channel_description)
                setShowBadge(false)
            }

            val notificationManager = getSystemService(NotificationManager::class.java)
            notificationManager.createNotificationChannel(channel)
        }
    }

    private fun createNotification(): Notification {
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(getString(R.string.notification_text))
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()
    }
}
