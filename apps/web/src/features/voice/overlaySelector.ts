/**
 * The overlay's root. A compact screen (a modal full-screen layer) leaves it exposed and focusable
 * (`useOverlayLayer({keepExposed})`); dialogs, sheets and menus hide it like everything else.
 * Its own module: the shell needs it at startup, the rest of `overlay.ts` lives in the lazy
 * overlay chunk (spec 13 §5.4).
 */
export const VOICE_OVERLAY_SELECTOR = '[data-voice-overlay]';
