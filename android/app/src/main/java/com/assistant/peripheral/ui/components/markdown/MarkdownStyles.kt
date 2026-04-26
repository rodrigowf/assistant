package com.assistant.peripheral.ui.components.markdown

import android.app.ActivityManager
import android.content.Context
import android.os.Build
import androidx.compose.ui.graphics.Color

/**
 * Color constants matching the web frontend's CSS variables (index.css + App.css).
 * Used by MarkdownText composables and the restyled thinking/tool blocks.
 */
object MdColors {
    // Text hierarchy
    val text = Color(0xFFC0C0C8)            // --text
    val textBright = Color(0xFFEEEEF2)      // --text-bright
    val textMuted = Color(0xFF6A6A74)       // --text-muted

    // Accent (links)
    val accent = Color(0xFF8BA3CC)           // --accent: hsl(220, 38%, 66%)

    // Backgrounds
    val bgElevated = Color(0xFF1E1E22)      // --bg-elevated

    // Borders
    val border = Color(0xFF2A2A30)          // --border
    val borderSubtle = Color(0xFF1C1C20)    // --border-subtle
    val borderStrong = Color(0xFF3C3C44)    // --border-strong

    // Inline code
    val inlineCodeBg = Color(0xFF252529)    // ~--bg-hover

    // Code block (oneDark theme)
    val codeBlockBg = Color(0xFF282C34)     // oneDark background
    val codeHeaderBg = Color(0xFF1E1E22)    // = bgElevated
    val codeText = Color(0xFFABB2BF)        // oneDark default foreground

    // Thinking block
    val thinkingBorder = Color(0xFFC4923A)  // --thinking-border
    val thinkingBg = Color(0x0AC4923A)      // rgba(201,150,58, 0.04)

    // Syntax highlighting (oneDark palette)
    val synKeyword = Color(0xFFC678DD)      // purple
    val synString = Color(0xFF98C379)       // green
    val synComment = Color(0xFF5C6370)      // gray
    val synNumber = Color(0xFFD19A66)       // orange
    val synType = Color(0xFFE5C07B)         // yellow
    val synFunction = Color(0xFF61AFEF)     // blue
    val synOperator = Color(0xFF56B6C2)     // cyan
}

/**
 * Runtime capability check for syntax highlighting.
 * Only enable on devices with API 24+ and 2GB+ RAM.
 */
object MdCapabilities {
    private var _syntaxHighlightingEnabled: Boolean? = null

    fun canUseSyntaxHighlighting(context: Context): Boolean {
        _syntaxHighlightingEnabled?.let { return it }
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val memInfo = ActivityManager.MemoryInfo()
        am.getMemoryInfo(memInfo)
        val totalGb = memInfo.totalMem / (1024L * 1024 * 1024)
        val result = totalGb >= 2 && Build.VERSION.SDK_INT >= 24
        _syntaxHighlightingEnabled = result
        return result
    }
}
