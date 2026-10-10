package com.assistant.archie.feature.settings

import com.assistant.core.protocol.AccountServiceDto
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant

/**
 * Settings → Accounts model (spec 12 §8.1 Accounts / Env keys, ACC-1..3) against the MockWebServer
 * backend: loading, the link flow, credentials (server error verbatim), sign out, Test, env keys
 * (reveal never lands in state), and the pure helpers.
 */
class AccountsModelTest {
    private val h = Harness()
    private val model get() = h.feature.accounts

    @After fun tearDown() = h.close()

    private fun service(id: String): AccountServiceDto = model.state.value.services!!.first { it.id == id }

    @Test fun load_parsesEveryService_andTheSummary() = runBlocking {
        model.loadNow()
        val st = model.state.value
        assertEquals(listOf("claude", "codex", "qwen", "openai"), st.services!!.map { it.id })
        assertEquals("2 of 3 agent harnesses signed in", st.summary)
        val claude = service("claude")
        assertEquals("signed_in", claude.state)
        assertEquals(listOf("link", "credentials", "env", "signout"), claude.methods.map { it.kind })
        assertEquals("••••wxyz", claude.methods[2].fields[0].preview)
        assertFalse(service("qwen").methods[1].available)
        assertTrue(service("openai").canVerify)
    }

    @Test fun linkFlow_start_code_success_refetchesTheService_andReChecksTheGate() = runBlocking {
        model.loadNow()
        assertTrue(model.startLogin("claude", "token"))
        val flow = service("claude").flow!!
        assertEquals("waiting", flow.status)
        assertTrue(flow.active && flow.needsCode)
        assertTrue(model.state.value.anyFlowActive)
        assertTrue(model.submitCode("claude", " abc#def "))
        assertTrue(service("claude").flow?.status in setOf(null, "succeeded")) // succeeded, then the refetched service (no flow)
        assertTrue(h.backend.accountWrites.any { it.startsWith("POST /api/accounts/claude/login/code") && it.contains("\"code\":\"abc#def\"") })
        eventually { h.backend.requests.contains("GET /api/accounts/claude") }
        eventually { h.backend.requests.count { it == "GET /api/auth/status" } >= 1 }
    }

    @Test fun poll_updatesAnActiveFlow_andCancel() = runBlocking {
        model.loadNow()
        model.startLogin("claude", "token")
        h.backend.loginFlow = """{"id":"f1","service":"claude","method":"token","status":"failed","message":"Login failed: Request failed with status code 400"}"""
        model.pollFlows()
        assertEquals("failed", service("claude").flow?.status)
        assertFalse(model.state.value.anyFlowActive)
        model.dismissFlow("claude")
        assertNull(service("claude").flow)
        model.startLogin("claude", "token")
        assertTrue(model.cancelLogin("claude"))
        assertEquals("cancelled", service("claude").flow?.status)
    }

    @Test fun credentials_serverErrorVerbatim_thenSaved() = runBlocking {
        model.loadNow()
        assertFalse(model.saveCredentials("claude", "credentials", """{"nope":1}"""))
        assertEquals("Invalid credentials: the file has no claudeAiOauth.accessToken.", model.state.value.errors["claude"])
        assertTrue(model.saveCredentials("claude", "credentials", """{"claudeAiOauth":{"accessToken":"x"}}"""))
        assertNull(model.state.value.errors["claude"])
    }

    @Test fun signOut_andVerify_replaceTheService() = runBlocking {
        model.loadNow()
        assertTrue(model.signOut("claude"))
        assertEquals("signed_out", service("claude").state)
        assertTrue(model.verify("openai"))
        assertEquals("The provider rejected the key (401).", service("openai").verified?.message)
    }

    @Test fun envKeys_revealIsNotStored_andWritesRefetch() = runBlocking {
        model.loadNow()
        model.loadEnvNow()
        assertEquals(listOf("OPENAI_API_KEY", "VOICE_DEBUG_VAD"), model.state.value.envKeys!!.map { it.name })
        assertFalse(model.state.value.envKeys!![1].inProcess)
        assertEquals("sk-full-secret-value", model.reveal("OPENAI_API_KEY"))
        assertFalse(model.state.value.toString().contains("sk-full-secret-value"))
        val before = h.backend.requests.count { it == "GET /api/accounts" }
        assertTrue(model.setKey("openai", "OPENAI_API_KEY", "sk-new"))
        assertTrue(h.backend.accountWrites.any { it.startsWith("PUT /api/env/OPENAI_API_KEY") && it.contains("sk-new") })
        assertTrue("ACC-3: every service refetched", h.backend.requests.count { it == "GET /api/accounts" } > before)
        assertTrue(model.createKey("NEW_KEY", "v"))
        assertTrue(model.deleteKey("NEW_KEY"))
        assertTrue(h.backend.accountWrites.any { it.startsWith("DELETE /api/env/NEW_KEY") })
    }

    @Test fun authGate_linkSignIn_succeeds_andReChecks() = runBlocking {
        h.feature.auth.startLink()
        val flow = h.feature.auth.state.value.flow!!
        assertEquals("waiting", flow.status)
        val checks = h.backend.requests.count { it == "GET /api/auth/status" }
        h.feature.auth.submitLinkCode(" abc#def ")
        assertEquals("succeeded", h.feature.auth.state.value.flow?.status)
        assertTrue(h.backend.accountWrites.any { it.contains("/login/code") && it.contains("\"abc#def\"") })
        assertTrue(h.backend.requests.count { it == "GET /api/auth/status" } > checks)
    }

    @Test fun helpers() {
        assertNull(AccountsModel.envNameError("MY_KEY_2"))
        assertEquals("Enter a name", AccountsModel.envNameError(" "))
        assertTrue(AccountsModel.envNameError("my_key")!!.startsWith("Capital letters"))
        assertTrue(AccountsModel.envNameError("1KEY")!!.startsWith("Capital letters"))
        assertEquals("KEY already exists", AccountsModel.envNameError("KEY", listOf("KEY")))
        assertNull(AccountsModel.jsonError("""{"a":1}"""))
        assertEquals("The file is a JSON object ({ … })", AccountsModel.jsonError("[]"))
        assertTrue(AccountsModel.jsonError("{")!!.startsWith("That isn't valid JSON"))
        val now = Instant.parse("2026-10-09T00:00:00Z")
        assertEquals("until 9 Oct 2027", AccountsModel.formatExpiry("2027-10-09T00:00:00Z", now))
        assertEquals("expired 2 Jan 2020", AccountsModel.formatExpiry("2020-01-02T00:00:00Z", now))
        assertNull(AccountsModel.formatExpiry("nope", now))
        assertNull(AccountsModel.formatExpiry(null, now))
    }
}
