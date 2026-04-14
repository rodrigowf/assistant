package com.assistant.peripheral.ui.theme

import android.app.Activity
import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat
import com.assistant.peripheral.data.ThemeMode

// Dark theme colors - matching web frontend CSS variables
private val DarkColorScheme = darkColorScheme(
    // Primary - cool slate-blue accent
    primary = Color(0xFF8BA3CC),           // --accent: hsl(220, 38%, 66%)
    onPrimary = Color(0xFF0d0d0f),         // --bg
    primaryContainer = Color(0xFF2A3A55),  // darker accent
    onPrimaryContainer = Color(0xFFEEEEF2), // --text-bright

    // Secondary
    secondary = Color(0xFF9090A0),
    onSecondary = Color(0xFF0d0d0f),
    secondaryContainer = Color(0xFF303038),
    onSecondaryContainer = Color(0xFFC0C0C8),

    // Tertiary - for orchestrator and special elements
    tertiary = Color(0xFF4AAA7A),          // success green
    onTertiary = Color(0xFF0d0d0f),
    tertiaryContainer = Color(0xFF1A3828),
    onTertiaryContainer = Color(0xFF8BE8B0),

    // Error
    error = Color(0xFFD45F5F),             // --error
    onError = Color(0xFF0d0d0f),
    errorContainer = Color(0xFF3D2020),    // --error-bg
    onErrorContainer = Color(0xFFF2B8B5),

    // Background & Surface - dark hierarchy
    background = Color(0xFF0d0d0f),        // --bg: darkest
    onBackground = Color(0xFFC0C0C8),      // --text
    surface = Color(0xFF171719),           // --bg-surface
    onSurface = Color(0xFFC0C0C8),         // --text
    surfaceVariant = Color(0xFF1e1e22),    // --bg-elevated
    onSurfaceVariant = Color(0xFFA0A0A8),  // slightly muted

    // Outline
    outline = Color(0xFF2a2a30),           // --border
    outlineVariant = Color(0xFF1c1c20),    // --border-subtle

    // Inverse
    inverseSurface = Color(0xFFE6E1E5),
    inverseOnSurface = Color(0xFF1C1B1F),
    inversePrimary = Color(0xFF4F68A0),

    // Surface tint
    surfaceTint = Color(0xFF8BA3CC),
)

// Light theme colors
private val LightColorScheme = lightColorScheme(
    primary = Color(0xFF4F68A0),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFD8E2FF),
    onPrimaryContainer = Color(0xFF001B3D),

    secondary = Color(0xFF5B5D72),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFE0E1F9),
    onSecondaryContainer = Color(0xFF181A2C),

    tertiary = Color(0xFF2D7A52),
    onTertiary = Color.White,
    tertiaryContainer = Color(0xFFB5F1CC),
    onTertiaryContainer = Color(0xFF002114),

    error = Color(0xFFBA1A1A),
    onError = Color.White,
    errorContainer = Color(0xFFFFDAD6),
    onErrorContainer = Color(0xFF410002),

    background = Color(0xFFFAFAFC),
    onBackground = Color(0xFF1B1B1F),
    surface = Color(0xFFFAFAFC),
    onSurface = Color(0xFF1B1B1F),
    surfaceVariant = Color(0xFFE2E2EC),
    onSurfaceVariant = Color(0xFF44464F),

    outline = Color(0xFF757680),
    outlineVariant = Color(0xFFC5C6D0),

    inverseSurface = Color(0xFF303034),
    inverseOnSurface = Color(0xFFF2F0F5),
    inversePrimary = Color(0xFFB0C6FF),

    surfaceTint = Color(0xFF4F68A0),
)

@Composable
fun AssistantTheme(
    themeMode: ThemeMode = ThemeMode.SYSTEM,
    dynamicColor: Boolean = false, // Disabled by default to use our custom theme
    content: @Composable () -> Unit
) {
    val darkTheme = when (themeMode) {
        ThemeMode.DARK -> true
        ThemeMode.LIGHT -> false
        ThemeMode.SYSTEM -> isSystemInDarkTheme()
    }

    val colorScheme = when {
        dynamicColor && Build.VERSION.SDK_INT >= Build.VERSION_CODES.S -> {
            val context = LocalContext.current
            if (darkTheme) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
        }
        darkTheme -> DarkColorScheme
        else -> LightColorScheme
    }

    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as Activity).window
            // Use surface color for status bar to match app
            window.statusBarColor = colorScheme.surface.toArgb()
            WindowCompat.getInsetsController(window, view).isAppearanceLightStatusBars = !darkTheme

            // Navigation bar
            window.navigationBarColor = colorScheme.surface.toArgb()
            WindowCompat.getInsetsController(window, view).isAppearanceLightNavigationBars = !darkTheme
        }
    }

    MaterialTheme(
        colorScheme = colorScheme,
        typography = Typography(),
        content = content
    )
}
