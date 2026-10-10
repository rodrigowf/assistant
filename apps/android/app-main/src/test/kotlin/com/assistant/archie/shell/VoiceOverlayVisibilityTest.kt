package com.assistant.archie.shell

import com.assistant.archie.feature.chat.VoiceUi
import com.assistant.core.data.ItemKey
import com.assistant.core.voice.ports.SessionPhase
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The floating voice controls show over every view but the Archie conversation, only for this device's call. */
class VoiceOverlayVisibilityTest {
    private val live = VoiceUi.Active(SessionPhase.ACTIVE, false, false)

    @Test
    fun notOnTheArchieViewWhereTheDockIs() {
        assertFalse(ShellVoiceOverlay.visible(live, ItemKey.Archie, Workspace))
        assertFalse(ShellVoiceOverlay.visible(live, ItemKey.Archie, SessionSettings("O1")))
    }

    @Test
    fun overOtherTabsAndScreens() {
        assertTrue(ShellVoiceOverlay.visible(live, ItemKey.Memory("a.md"), Workspace))
        assertTrue(ShellVoiceOverlay.visible(live, ItemKey.Archie, SettingsHome))
        assertTrue(ShellVoiceOverlay.visible(live, ItemKey.Archie, MemoryDoc("a.md")))
        assertTrue(ShellVoiceOverlay.visible(live, null, Workspace))
    }

    @Test
    fun onlyThisDevicesCall() {
        assertFalse(ShellVoiceOverlay.visible(VoiceUi.Off, ItemKey.Memory("a.md"), Workspace))
        assertFalse(ShellVoiceOverlay.visible(VoiceUi.Elsewhere("Pixel 8", true), ItemKey.Memory("a.md"), Workspace))
    }
}
