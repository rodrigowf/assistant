/*
 * vosk_stderr_shim.c — Lollipop (API 21–22) Bionic compatibility polyfill.
 *
 * Two-layer design:
 *   1. EXPORTS: `stderr`/`stdin`/`stdout` global pointers (below).
 *   2. RTLD_GLOBAL bootstrap: a JNI method that dlopens THIS lib with
 *      RTLD_GLOBAL so the symbols above land in the global namespace and
 *      Vosk's later dlopen of libvosk.so can resolve them.
 *
 * Why the JNI step is needed:
 *   System.loadLibrary on pre-N Android dlopens with RTLD_LOCAL. So when
 *   Vosk later calls System.loadLibrary("vosk"), libvosk.so cannot see the
 *   shim's symbols even though the shim is already in the process. We must
 *   re-dlopen ourselves with RTLD_GLOBAL to publish the symbols to the
 *   global scope. After that, libvosk.so resolves `stderr` cleanly.
 *
 * Plan: assistant/plans/wakeword_vosk_migration_plan_2026_06_09.md, V2.
 *
 * Why this exists
 * ---------------
 * Vosk's prebuilt libvosk.so (com.alphacephei:vosk-android:0.3.47) was built
 * with an NDK that resolves `stderr` (and friends) as exported function-style
 * symbols. On Android < M (API < 23) Bionic, `stderr` was a macro:
 *
 *     #define stderr (&__sF[2])
 *
 * — there is no exported `stderr` symbol. So loading libvosk.so on
 * Android 5.0.2 (Samsung A300M, our test device) fails with:
 *
 *     dlopen failed: cannot locate symbol "stderr" referenced by "libvosk.so"
 *
 * On M+ Bionic, `stderr` IS exported (matching the standard), so libvosk
 * loads cleanly there.
 *
 * This shim exports `stderr`, `stdin`, `stdout` as global pointers whose
 * values come from the legacy `__sF[]` array. The dynamic linker resolves
 * libvosk.so's references against this shim's exported symbols before
 * falling back to libc.so. End result: libvosk.so loads.
 *
 * Build target
 * ------------
 * The shim is compiled with NDK 26+ targeting minSdk 21. `__sF` is
 * `__REMOVED_IN(23)` in the API 21 sysroot — it's still in libc.so's
 * exported symbol table for API 21, so this links and runs on Lollipop.
 *
 * Modern-Android polyfill behavior
 * --------------------------------
 * This shim is loaded only when `Build.VERSION.SDK_INT < 23` (see
 * VoskModelLoader.maybeLoadStderrShim). On M+ devices it's never loaded
 * (and would fail to load anyway — `__sF` isn't exported), so the cost
 * is exactly zero there. True polyfill: pre-M only.
 */

#include <dlfcn.h>
#include <jni.h>
#include <stdio.h>

/*
 * Undef the NDK macros so we can define `stderr`/`stdin`/`stdout` as
 * actual exported symbols. NDK 26 stdio.h defines them either as
 *   - "#define stderr stderr"      (function-style, API 23+), or
 *   - "#define stderr (&__sF[2])"  (macro-style, pre-23).
 * Both conflict with declaring a variable named `stderr`.
 */
#undef stdin
#undef stdout
#undef stderr

/*
 * `__sF` is the legacy Bionic stdio file array. On API 21–22 it's exported
 * by libc.so; on M+ it's gone. We declare it here so the linker resolves
 * it from libc.so at runtime. (Layout: __sF[0]=stdin, [1]=stdout, [2]=stderr.)
 */
extern FILE __sF[];

/*
 * Visibility=default so the dynamic linker exports these symbols and
 * libvosk.so's lazy lookup of "stderr" / "stdin" / "stdout" finds them
 * here. Marked `__attribute__((used))` so LTO/dead-code-elim can't drop
 * them — they're never referenced inside this translation unit.
 */
__attribute__((visibility("default"), used)) FILE* stdin  = &__sF[0];
__attribute__((visibility("default"), used)) FILE* stdout = &__sF[1];
__attribute__((visibility("default"), used)) FILE* stderr = &__sF[2];

/*
 * Re-dlopen this lib with RTLD_GLOBAL so its symbols (above) join the
 * global namespace. Without this step, on pre-N Android the symbols are
 * only visible inside the shim's own scope, and libvosk.so's later dlopen
 * still fails to resolve `stderr`.
 *
 * Returns 0 on success, -1 on dlopen failure (caller logs).
 * Called by Kotlin via VoskModelLoader.publishStderrShimGlobally().
 */
JNIEXPORT jint JNICALL
Java_com_assistant_peripheral_voice_VoskModelLoader_publishStderrShimGlobally(
    JNIEnv* env, jobject thiz) {
    (void)env; (void)thiz;
    // Use just the soname — Bionic's linker resolves it via the same lib
    // search path System.loadLibrary used.
    void* h = dlopen("libvosk-stderr-shim.so", RTLD_NOW | RTLD_GLOBAL);
    return h ? 0 : -1;
}

/*
 * Pre-load libvosk.so ourselves with RTLD_GLOBAL after the shim is in
 * the global namespace. After this, JNA's later `System.loadLibrary("vosk")`
 * finds libvosk already in the process and reuses the handle — no new
 * dlopen, no new symbol resolution.
 *
 * On pre-N Bionic, even RTLD_GLOBAL doesn't always make symbols visible
 * across libs unless the consuming lib is opened AFTER the providing lib
 * is already in the global namespace. So the call order is:
 *   1. dlopen(libvosk-stderr-shim.so, RTLD_GLOBAL)   <- already done
 *   2. dlopen(libvosk.so,             RTLD_GLOBAL)   <- here, this function
 *   3. JNA's System.loadLibrary("vosk")               <- becomes a no-op
 *
 * Returns 0 on success, -1 on dlopen failure. Caller passes the absolute
 * path to libvosk.so (extracted from APK's lib dir).
 */
JNIEXPORT jint JNICALL
Java_com_assistant_peripheral_voice_VoskModelLoader_preloadVoskGlobally(
    JNIEnv* env, jobject thiz, jstring jpath) {
    (void)thiz;
    const char* path = (*env)->GetStringUTFChars(env, jpath, NULL);
    if (!path) return -1;
    void* h = dlopen(path, RTLD_NOW | RTLD_GLOBAL);
    (*env)->ReleaseStringUTFChars(env, jpath, path);
    return h ? 0 : -1;
}
