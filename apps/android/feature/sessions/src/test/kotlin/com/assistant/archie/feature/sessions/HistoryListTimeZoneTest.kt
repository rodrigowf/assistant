package com.assistant.archie.feature.sessions

import com.assistant.core.data.HistoryBucket
import com.assistant.core.data.ItemKey
import com.assistant.core.data.ItemKind
import com.assistant.core.data.WorkspaceItem
import com.assistant.core.data.ConversationKey
import com.assistant.core.model.HarnessProvider
import com.assistant.core.model.SessionSummary
import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.Instant
import java.time.ZoneId
import java.util.Locale

/**
 * Relative times across time zones (spec 14 §7 B-06 DoD; fixes inv03 §1.5: the old app read the
 * first 19 characters of `last_activity` as device-local time, so every age was off by the UTC
 * offset — +3 h in Rio). The backend sends ISO-8601 with an offset (verified on the Jetson:
 * `2026-08-28T21:49:03.550000+00:00`); buckets and stamps must be the same instant shown in local time.
 */
class HistoryListTimeZoneTest {
    private fun s(id: String, last: String, title: String = id, orch: Boolean = false) =
        SessionSummary(id, last, last, title, 3, orch, if (orch) null else HarnessProvider.CLAUDE, null)

    /** 02:30 UTC on Sat 3 Oct 2026 — still Friday evening in Rio, already Saturday morning in Tokyo. */
    private val now = Instant.parse("2026-10-03T02:30:00Z")

    private val sessions = listOf(
        s("utc-late", "2026-10-02T23:30:00.123456+00:00"),
        s("rio-offset", "2026-10-02T21:00:00-03:00"),            // = 2026-10-03T00:00Z
        s("jetson-real", "2026-08-28T21:49:03.550000+00:00"),
        s("z-form", "2026-09-30T12:00:00Z"),
    )

    private fun table(zone: String): Map<String, Pair<HistoryBucket, String>> =
        HistoryList.groups(sessions, emptyList(), "", now, ZoneId.of(zone), Locale.US)
            .flatMap { g -> g.rows.map { it.summary.sdkId to (g.bucket to it.meta) } }.toMap()

    @Test fun utc() {
        val t = table("UTC")
        assertEquals(HistoryBucket.YESTERDAY to "23:30", t["utc-late"])
        assertEquals(HistoryBucket.TODAY to "00:00", t["rio-offset"])
        assertEquals(HistoryBucket.PREVIOUS_7_DAYS to "Wed", t["z-form"])
        assertEquals(HistoryBucket.EARLIER to "28 Aug", t["jetson-real"])
    }

    @Test fun rio_isNotSkewedByTheOffset() {
        val t = table("America/Sao_Paulo")      // UTC-3; "now" is Fri 2 Oct 23:30 local
        // The old bug would have shown 23:30 (the UTC wall time) and put it on the wrong day.
        assertEquals(HistoryBucket.TODAY to "20:30", t["utc-late"])
        assertEquals(HistoryBucket.TODAY to "21:00", t["rio-offset"])
        assertEquals(HistoryBucket.PREVIOUS_7_DAYS to "Wed", t["z-form"])
        assertEquals(HistoryBucket.EARLIER to "28 Aug", t["jetson-real"])   // 18:49 local, same day
    }

    @Test fun tokyo() {
        val t = table("Asia/Tokyo")             // UTC+9; "now" is Sat 3 Oct 11:30 local
        assertEquals(HistoryBucket.TODAY to "08:30", t["utc-late"])
        assertEquals(HistoryBucket.TODAY to "09:00", t["rio-offset"])
        assertEquals(HistoryBucket.PREVIOUS_7_DAYS to "Wed", t["z-form"])
        assertEquals(HistoryBucket.EARLIER to "29 Aug", t["jetson-real"])   // 06:49 next day in Tokyo
    }

    @Test fun kiritimati_dateLine() {
        val t = table("Pacific/Kiritimati")      // UTC+14; "now" is Sat 3 Oct 16:30 local
        assertEquals(HistoryBucket.TODAY to "13:30", t["utc-late"])
        assertEquals(HistoryBucket.TODAY to "14:00", t["rio-offset"])
    }

    @Test fun newestFirst_withinEachBucket() {
        val rows = HistoryList.groups(sessions, emptyList(), "", now, ZoneId.of("Asia/Tokyo"), Locale.US)
            .first { it.bucket == HistoryBucket.TODAY }.rows.map { it.summary.sdkId }
        assertEquals(listOf("rio-offset", "utc-late"), rows)
    }

    @Test fun openAndLiveSessionsLeaveTheHistory_searchMatchesEveryWord() {
        val list = listOf(
            s("A", "2026-10-02T10:00:00+00:00", "Weekly energy report", orch = true),
            s("B", "2026-10-02T09:00:00+00:00", "Fix context-sync delete race"),
            s("C", "2026-10-02T08:00:00+00:00", "Energy dashboard"),
            s("D", "2026-10-02T07:00:00+00:00", "Orchestrator", orch = true),
        )
        // "Open now" (OPEN-1) holds an open view and a pool session with no view here: both leave the history.
        val open = listOf(
            WorkspaceItem(ItemKey.Agent(ConversationKey.agent("L")), ItemKind.AGENT, "Fix", localId = "L", sdkId = "B"),
            WorkspaceItem(ItemKey.Agent(ConversationKey.agent("P")), ItemKind.AGENT, "Energy dashboard", localId = "P", sdkId = "C"),
        )
        val ids = { q: String -> HistoryList.groups(list, open, q, now, ZoneId.of("UTC"), Locale.US).flatMap { g -> g.rows.map { it.summary.sdkId } } }
        assertEquals(listOf("A", "D"), ids(""))
        assertEquals(listOf("A"), ids("energy weekly"))
        assertEquals(emptyList<String>(), ids("weekly dashboard"))
        // An untitled Archie conversation is "New conversation" (IA §1) and is searchable as such.
        assertEquals(listOf("D"), ids("new conv"))
    }
}
