# Add project specific ProGuard rules here.

# OkHttp
-dontwarn okhttp3.**
-dontwarn okio.**
-keepnames class okhttp3.internal.publicsuffix.PublicSuffixDatabase

# Keep data classes
-keep class com.assistant.peripheral.data.** { *; }

# Vosk + Kaldi JNI types
-keep class org.vosk.** { *; }
-keep class org.kaldi.** { *; }
