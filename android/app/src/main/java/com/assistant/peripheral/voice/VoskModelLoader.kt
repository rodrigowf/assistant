package com.assistant.peripheral.voice

import android.content.Context
import android.content.res.AssetManager
import android.os.Build
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.vosk.Model
import java.io.File
import java.io.InputStream

/**
 * Lazy loader for the bundled Vosk small-EN model
 * (`vosk-model-small-en-us-0.15`, ~68 MB extracted).
 *
 * Plan: `assistant/plans/wakeword_vosk_migration_plan_2026_06_09.md`, §4 V2.
 *
 * Responsibilities:
 *  1. **Extract** the bundled assets to `context.filesDir/vosk-model/` on
 *     first call. Skip on subsequent calls via a `.stamp` file whose content
 *     matches `MODEL_STAMP` (re-extracts on APK upgrade if we bump the stamp).
 *  2. **Load + cache** the `org.vosk.Model` instance under a `Mutex` so
 *     concurrent `getModel` calls don't race the load.
 *  3. Surface failure cleanly: return `null` if the native lib can't load,
 *     the assets are missing, or extraction fails. Callers (V3 onward) treat
 *     null as "fall back to SpeechRecognizer / degrade gracefully".
 *
 * The companion-object helpers (`shouldExtract`, `extractTree`,
 * `assetFileList`) are pure Kotlin so they can be unit-tested without
 * Android (`VoskModelLoaderParityTest`). Instance methods that touch
 * `Context`, `AssetManager`, or `org.vosk.Model` are not unit-testable —
 * verified on-device per plan §0.4.
 */
object VoskModelLoader {

    private const val TAG = "VoskModelLoader"

    /** Asset-root path: matches V1's bundled directory in `app/src/main/assets/`. */
    const val ASSET_ROOT = "vosk-model-small-en-us-0.15"

    /** Sub-directory under `context.filesDir/` where the model is extracted. */
    const val EXTRACT_DIRNAME = "vosk-model"

    /**
     * Stamp content. Change this when the bundled model changes — on next
     * launch, `shouldExtract` will return `true` (stamp mismatch) and the
     * model will be re-extracted, overwriting the stale tree.
     */
    const val MODEL_STAMP = "vosk-model-small-en-us-0.15"

    private val mutex = Mutex()

    @Volatile
    private var cachedModel: Model? = null

    @Volatile
    private var loadFailed = false

    @Volatile
    private var shimLoadAttempted = false

    /**
     * Lollipop polyfill: Vosk's prebuilt libvosk.so references `stderr` as an
     * exported function-style symbol, but Bionic on Android < M (API < 23)
     * has `stderr` as a macro `(&__sF[2])` — no exported symbol — so the
     * dlopen fails. Our `libvosk-stderr-shim.so` (built from cpp/) exports
     * `stderr/stdin/stdout` pointing at the legacy `__sF[]` array, so when
     * loaded BEFORE the Vosk JNI triggers libvosk.so's dlopen, the linker
     * resolves Vosk's references against the shim.
     *
     * On M+ (SDK_INT >= 23) the shim is unnecessary (Bionic exports `stderr`
     * itself) AND can't load (`__sF` was removed from libc). True polyfill:
     * pre-M only, zero cost on modern devices.
     */
    private fun maybeLoadStderrShim(context: Context) {
        if (shimLoadAttempted) return
        shimLoadAttempted = true
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            Log.d(TAG, "Stderr shim skipped — SDK_INT=${Build.VERSION.SDK_INT} >= 23")
            return
        }
        try {
            // Step 1: pull the shim into the process. System.loadLibrary uses
            // RTLD_LOCAL on pre-N — symbols aren't yet globally visible.
            System.loadLibrary("vosk-stderr-shim")

            // Step 2: re-dlopen the shim with RTLD_GLOBAL via its own JNI
            // method so its stderr/stdin/stdout exports join the global
            // namespace.
            val pubRc = publishStderrShimGlobally()

            // Step 3: pre-load libvosk.so with RTLD_GLOBAL ourselves AFTER
            // the shim is in the global scope. On pre-N Bionic, this is the
            // only reliable way to make the linker see the shim's exports
            // when resolving libvosk's `stderr` reference. After this, JNA's
            // System.loadLibrary("vosk") finds the lib already in the process
            // and reuses it without re-doing symbol resolution.
            //
            // Path: APK's lib dir under /data/app/<pkg>-<n>/lib/<abi>/. The
            // ApplicationInfo.nativeLibraryDir is the canonical pointer.
            val voskSo = "${context.applicationInfo.nativeLibraryDir}/libvosk.so"
            val preRc = preloadVoskGlobally(voskSo)
            Log.d(TAG, "Stderr shim loaded (SDK_INT=${Build.VERSION.SDK_INT}, " +
                    "publishGlobal rc=$pubRc, preloadVosk[$voskSo] rc=$preRc)")
        } catch (t: Throwable) {
            Log.e(TAG, "Stderr shim failed to load — Vosk will likely fail next", t)
        }
    }

    /**
     * Implemented in `libvosk-stderr-shim.so`. Re-dlopens the shim with
     * `RTLD_NOW | RTLD_GLOBAL` so its exported `stderr` / `stdin` / `stdout`
     * land in the global namespace. Returns 0 on success, -1 on failure.
     */
    private external fun publishStderrShimGlobally(): Int

    /**
     * Implemented in `libvosk-stderr-shim.so`. Dlopens libvosk.so with
     * `RTLD_NOW | RTLD_GLOBAL` AFTER the shim is global, so the linker
     * resolves libvosk's `stderr` reference against the shim. Returns 0
     * on success, -1 on failure.
     */
    private external fun preloadVoskGlobally(path: String): Int

    /**
     * Suspend-load the Vosk model, extracting from assets on first call.
     *
     * Returns `null` if:
     *   - assets are missing or extraction fails;
     *   - `org.vosk.Model` constructor throws (native lib load failed, model
     *     files corrupted, etc.).
     *
     * Once a load fails, subsequent calls return `null` immediately without
     * retrying — the failure is sticky for the process lifetime. Restart the
     * service / process to retry. (Rationale: every retry takes ~500 ms-2 s
     * and would just block the caller; if the first load fails the
     * environment is wrong and won't fix itself.)
     */
    suspend fun getModel(context: Context): Model? {
        cachedModel?.let { return it }
        if (loadFailed) return null

        return withContext(Dispatchers.IO) {
            mutex.withLock {
                cachedModel?.let { return@withLock it }
                if (loadFailed) return@withLock null

                val started = System.currentTimeMillis()
                val targetDir = File(context.filesDir, EXTRACT_DIRNAME)
                try {
                    if (shouldExtract(targetDir, expectedStamp = MODEL_STAMP)) {
                        Log.d(TAG, "Extracting Vosk model to ${targetDir.absolutePath}")
                        val assets = context.assets
                        val sources = assetSources(assets)
                        extractTree(sources, targetDir, stamp = MODEL_STAMP)
                        Log.d(TAG, "Vosk model extracted (${sources.size} files) " +
                                "in ${System.currentTimeMillis() - started} ms")
                    } else {
                        Log.d(TAG, "Vosk model already extracted at ${targetDir.absolutePath}")
                    }

                    // Polyfill for Lollipop Bionic. Must run BEFORE the Vosk
                    // JNI is touched — Model() triggers dlopen of libvosk.so.
                    maybeLoadStderrShim(context)

                    val loadStart = System.currentTimeMillis()
                    val model = Model(targetDir.absolutePath)
                    Log.d(TAG, "Vosk model loaded in ${System.currentTimeMillis() - loadStart} ms " +
                            "(total ${System.currentTimeMillis() - started} ms since start)")
                    cachedModel = model
                    model
                } catch (t: Throwable) {
                    Log.e(TAG, "Vosk model load failed", t)
                    loadFailed = true
                    null
                }
            }
        }
    }

    /**
     * Whether the cached model is currently available without I/O. V3/V5 can
     * use this to decide between waiting vs falling back. Doesn't trigger
     * a load.
     */
    val isLoaded: Boolean
        get() = cachedModel != null

    /**
     * Build the `relPath → InputStream provider` map from the Android
     * `AssetManager`. Walks the asset tree under `ASSET_ROOT` recursively.
     * Each provider opens a fresh `InputStream` so callers can stream-copy
     * without exhausting a shared one.
     */
    private fun assetSources(assets: AssetManager): Map<String, () -> InputStream> {
        val files = assetFileList { path -> assets.list(path)?.toList() ?: emptyList() }
        return files.associateWith { rel ->
            { assets.open("$ASSET_ROOT/$rel") }
        }
    }

    // -------------------------------------------------------------------------
    // Pure helpers — unit-tested by VoskModelLoaderParityTest.
    // -------------------------------------------------------------------------

    /**
     * Returns true when the model must be (re-)extracted. False when the
     * target dir exists AND its `.stamp` file's content equals
     * `expectedStamp`.
     */
    fun shouldExtract(targetDir: File, expectedStamp: String): Boolean {
        if (!targetDir.isDirectory) return true
        val stampFile = File(targetDir, ".stamp")
        if (!stampFile.isFile) return true
        return stampFile.readText() != expectedStamp
    }

    /**
     * Copy each source stream to `targetDir/<relPath>`, creating parent dirs
     * as needed, then write the stamp file. Idempotent: re-extracting
     * overwrites. `sources` keys are relative paths under the model root.
     */
    fun extractTree(
        sources: Map<String, () -> InputStream>,
        targetDir: File,
        stamp: String,
    ) {
        targetDir.mkdirs()
        sources.forEach { (rel, openStream) ->
            val outFile = File(targetDir, rel)
            outFile.parentFile?.mkdirs()
            openStream().use { input ->
                outFile.outputStream().use { out ->
                    input.copyTo(out)
                }
            }
        }
        File(targetDir, ".stamp").writeText(stamp)
    }

    /**
     * Flatten an asset subtree rooted at `ASSET_ROOT` into a list of relative
     * paths (file leaves only, no directory entries). `walker` is injected:
     * on Android it wraps `AssetManager.list(path)`; in tests it's a fake.
     *
     * AssetManager.list semantics:
     *   - returns null/empty for files (leaves);
     *   - returns immediate children (mix of files + subdirs) for directories.
     *
     * We use this contract to distinguish: if `walker(child).isEmpty()`, the
     * child is a leaf (file); otherwise it's a directory and we recurse.
     */
    fun assetFileList(walker: (String) -> List<String>): List<String> {
        val out = mutableListOf<String>()
        walkInto(ASSET_ROOT, "", walker, out)
        return out
    }

    private fun walkInto(
        assetPath: String,
        relPath: String,
        walker: (String) -> List<String>,
        out: MutableList<String>,
    ) {
        val children = walker(assetPath)
        if (children.isEmpty()) {
            // Leaf — a file (or an empty directory, which we conservatively
            // treat as nothing-to-extract).
            if (relPath.isNotEmpty()) out.add(relPath)
            return
        }
        children.forEach { child ->
            val childAsset = "$assetPath/$child"
            val childRel = if (relPath.isEmpty()) child else "$relPath/$child"
            walkInto(childAsset, childRel, walker, out)
        }
    }
}
