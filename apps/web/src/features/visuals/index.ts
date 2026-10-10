/**
 * Entry point of `@/features/visuals` (W-14, spec 13 §3.6, §3.7, §3.9).
 *
 *   <VisualsPane onOpen? selectedPath? />          list pane / Visuals screen
 *   <VisualViewer path url hidden />               sandboxed iframe, remount reload, Show on TV, ⋮ (lazy chunk)
 *   <VisualCard path title modified? onOpen />     inline card (Open + Show on TV) for the conversation
 *   showOnTv(path, title?)                         BX-2 cast with snackbars
 */
export { VisualsPane, type VisualsPaneProps } from './VisualsPane';
export { VisualViewer, preloadVisualViewer } from './lazy';
export type { VisualViewerProps } from './VisualViewer';
export { VisualCard, type VisualCardProps } from './VisualCard';
export { findVisual, showOnTv, visualUrl, vizFolder, vizHref, vizOrigin } from './viz';
