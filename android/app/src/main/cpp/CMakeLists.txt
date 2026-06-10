# CMakeLists.txt — Vosk stderr polyfill for Lollipop (API 21–22) Bionic.
# Plan: assistant/plans/wakeword_vosk_migration_plan_2026_06_09.md, V2.
# See vosk_stderr_shim.c for the rationale.

cmake_minimum_required(VERSION 3.18.1)

project(vosk-stderr-shim C)

# Build the shim as a small shared library. It ships in the APK on all
# devices but is only dlopen'd on API < 23 (see VoskModelLoader.kt).
add_library(vosk-stderr-shim SHARED vosk_stderr_shim.c)

# log lib for Log.d-equivalent during the JNI bootstrap path (currently
# unused but cheap to link); dl for dlopen.
find_library(log-lib log)
target_link_libraries(vosk-stderr-shim ${log-lib} dl)

set_target_properties(vosk-stderr-shim PROPERTIES
    LINK_FLAGS "-Wl,--no-undefined"
)
