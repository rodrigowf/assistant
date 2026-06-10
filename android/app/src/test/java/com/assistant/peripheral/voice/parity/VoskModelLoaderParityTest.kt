package com.assistant.peripheral.voice.parity

import com.assistant.peripheral.voice.VoskModelLoader
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.File
import java.io.InputStream

/**
 * Parity test for V2 (`VoskModelLoader`) of the Vosk migration plan
 * (assistant/plans/wakeword_vosk_migration_plan_2026_06_09.md, §4 V2).
 *
 * Refactor base: HEAD `482a311` (`Vosk V1` — dependency + bundled model in
 * place, no loader code yet).
 *
 * V2 is the first commit that adds Kotlin code referencing the Vosk JNI.
 * Two responsibilities live in `VoskModelLoader`:
 *
 *   1. **Extract** the bundled assets under `assets/vosk-model-small-en-us-0.15/`
 *      to a writable directory (`context.filesDir/vosk-model/`) on first call,
 *      skip on subsequent calls.
 *   2. **Load + cache** the `org.vosk.Model` instance, with a `Mutex` so
 *      concurrent callers don't race the load.
 *
 * The Vosk load itself depends on the native lib + Android filesystem and is
 * not unit-testable in plain JUnit. But the **file extraction** is pure I/O
 * and can be driven via the companion-object helpers below. This test pins
 * those helpers — the same pattern used by Inc 1–9's parity tests (companion
 * predicates testable in plain JUnit, instance behavior verified on-device).
 *
 * ---
 *
 * **Contract under test** (V2):
 *
 *   - `VoskModelLoader.shouldExtract(targetDir, manifestStamp, expectedStamp)`
 *       returns `true` IFF the target dir is missing OR the stamp file in it
 *       doesn't match `expectedStamp`. The stamp file is how we detect
 *       "model was extracted from a previous APK version, must re-extract".
 *
 *   - `VoskModelLoader.extractTree(sources, targetDir)` copies each source
 *       stream (keyed by relative path) into `targetDir/<relPath>`, creating
 *       parent directories as needed. Idempotent: re-extracting overwrites.
 *
 *   - `VoskModelLoader.assetFileList(walker)` flattens the asset tree rooted
 *       at `vosk-model-small-en-us-0.15/` into a list of relative paths
 *       (no directory entries, just files). `walker` is injected for
 *       testability — on Android it wraps `AssetManager.list()` recursively.
 *
 * **Tuned behaviors preserved**: N/A (new code; nothing to preserve).
 *
 * ---
 *
 * **Implications for downstream increments**:
 *
 *   - V3 will call `VoskModelLoader.getModel(context)` from
 *     `WakeWordDetector` after silence-monitor activity-detection — but
 *     since V2 wires eager loading from `AssistantService.onCreate`, the
 *     model will normally already be loaded by then.
 *   - V5's health check needs a non-null `Model` to be useful; V2 must
 *     surface failure cleanly (null return) so V3/V5 can degrade gracefully.
 */
class VoskModelLoaderParityTest {

    private lateinit var tempDir: File

    @Before
    fun setUp() {
        tempDir = File.createTempFile("vosk-model-test", "").apply {
            delete()
            mkdirs()
        }
    }

    @After
    fun tearDown() {
        tempDir.deleteRecursively()
    }

    /**
     * `shouldExtract` returns true when the target directory does not exist.
     */
    @Test
    fun `shouldExtractReturnsTrueWhenTargetMissing`() {
        val missing = File(tempDir, "no-such-dir")
        assertTrue(
            "Missing target dir → must extract",
            VoskModelLoader.shouldExtract(missing, expectedStamp = "v1"),
        )
    }

    /**
     * `shouldExtract` returns true when the target dir exists but the stamp
     * file is absent (corresponds to a half-extracted state from a previous
     * run that crashed mid-write).
     */
    @Test
    fun `shouldExtractReturnsTrueWhenStampMissing`() {
        val dir = File(tempDir, "vosk-model").apply { mkdirs() }
        // No `.stamp` file inside; previous extraction was incomplete.
        assertTrue(
            "Stamp missing → must re-extract",
            VoskModelLoader.shouldExtract(dir, expectedStamp = "v1"),
        )
    }

    /**
     * `shouldExtract` returns true when the stamp file's content doesn't
     * match the expected stamp (APK was upgraded; bundled model changed).
     */
    @Test
    fun `shouldExtractReturnsTrueWhenStampMismatch`() {
        val dir = File(tempDir, "vosk-model").apply { mkdirs() }
        File(dir, ".stamp").writeText("v0-from-old-apk")
        assertTrue(
            "Stamp mismatch → must re-extract",
            VoskModelLoader.shouldExtract(dir, expectedStamp = "v1"),
        )
    }

    /**
     * `shouldExtract` returns false when the target dir exists and the stamp
     * matches — the model is already correctly extracted, skip work.
     */
    @Test
    fun `shouldExtractReturnsFalseWhenStampMatches`() {
        val dir = File(tempDir, "vosk-model").apply { mkdirs() }
        File(dir, ".stamp").writeText("v1")
        assertFalse(
            "Stamp matches → no re-extract",
            VoskModelLoader.shouldExtract(dir, expectedStamp = "v1"),
        )
    }

    /**
     * `extractTree` writes each source stream to the correct relative path
     * under `targetDir`, creates parent dirs, and stamps the directory on
     * success.
     */
    @Test
    fun `extractTreeWritesFilesAndStamp`() {
        val target = File(tempDir, "vosk-model")
        val sources: Map<String, () -> InputStream> = mapOf(
            "README" to { "the readme".byteInputStream() },
            "am/final.mdl" to { byteArrayOf(1, 2, 3).inputStream() },
            "graph/phones/word_boundary.int" to { "1 2 3 4".byteInputStream() },
        )

        VoskModelLoader.extractTree(sources, target, stamp = "v1")

        assertEquals("the readme", File(target, "README").readText())
        assertArrayEquals(
            byteArrayOf(1, 2, 3),
            File(target, "am/final.mdl").readBytes(),
        )
        assertEquals(
            "1 2 3 4",
            File(target, "graph/phones/word_boundary.int").readText(),
        )
        assertEquals("v1", File(target, ".stamp").readText())
    }

    /**
     * Re-running `extractTree` over an existing directory overwrites files
     * (idempotent). This matters because `shouldExtract` returns true after
     * a half-extracted state, and we don't want a stale file from the prior
     * partial write to remain.
     */
    @Test
    fun `extractTreeIsIdempotentAcrossCalls`() {
        val target = File(tempDir, "vosk-model")
        val firstSources: Map<String, () -> InputStream> = mapOf(
            "README" to { "first".byteInputStream() },
        )
        val secondSources: Map<String, () -> InputStream> = mapOf(
            "README" to { "second".byteInputStream() },
        )

        VoskModelLoader.extractTree(firstSources, target, stamp = "v1")
        VoskModelLoader.extractTree(secondSources, target, stamp = "v1")

        assertEquals("second", File(target, "README").readText())
    }

    /**
     * `assetFileList` flattens the recursive asset walk into a flat list of
     * relative paths under the model root. Directories are excluded; only
     * file leaves are returned.
     */
    @Test
    fun `assetFileListFlattensTree`() {
        // Mimic AssetManager.list semantics: each call returns the immediate
        // children (files + subdirs) of the given asset path.
        val fakeAssetTree: Map<String, List<String>> = mapOf(
            "vosk-model-small-en-us-0.15" to listOf("README", "am", "conf", "graph"),
            "vosk-model-small-en-us-0.15/README" to emptyList(),
            "vosk-model-small-en-us-0.15/am" to listOf("final.mdl"),
            "vosk-model-small-en-us-0.15/am/final.mdl" to emptyList(),
            "vosk-model-small-en-us-0.15/conf" to listOf("mfcc.conf", "model.conf"),
            "vosk-model-small-en-us-0.15/conf/mfcc.conf" to emptyList(),
            "vosk-model-small-en-us-0.15/conf/model.conf" to emptyList(),
            "vosk-model-small-en-us-0.15/graph" to listOf("phones"),
            "vosk-model-small-en-us-0.15/graph/phones" to listOf("word_boundary.int"),
            "vosk-model-small-en-us-0.15/graph/phones/word_boundary.int" to emptyList(),
        )
        val walker: (String) -> List<String> = { path ->
            fakeAssetTree[path] ?: emptyList()
        }

        val files = VoskModelLoader.assetFileList(walker)

        // Relative-to-root paths, files only, sorted for stable assertion.
        assertEquals(
            listOf(
                "README",
                "am/final.mdl",
                "conf/mfcc.conf",
                "conf/model.conf",
                "graph/phones/word_boundary.int",
            ),
            files.sorted(),
        )
    }

    /**
     * Helper for byte-stream equivalence in `extractTree`. Empty inputs
     * produce empty files (Vosk has some 0-byte config files, e.g.
     * `online_cmvn.conf` may be very small).
     */
    @Test
    fun `extractTreeHandlesEmptyStream`() {
        val target = File(tempDir, "vosk-model")
        val sources: Map<String, () -> InputStream> = mapOf(
            "ivector/online_cmvn.conf" to { ByteArrayInputStream(ByteArray(0)) },
        )

        VoskModelLoader.extractTree(sources, target, stamp = "v1")

        assertTrue(File(target, "ivector/online_cmvn.conf").exists())
        assertEquals(0, File(target, "ivector/online_cmvn.conf").length())
    }
}
