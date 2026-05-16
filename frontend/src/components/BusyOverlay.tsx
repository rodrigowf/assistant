interface Props {
  /** When true, the overlay is rendered. Anything below it is dimmed and
   *  becomes pointer-inert so a second click can't queue a duplicate
   *  mutation while the first is in flight. */
  show: boolean;
  /** Short label shown next to the spinner, e.g. "Rewinding…". */
  label?: string;
}

/**
 * App-wide busy overlay used by slower mutations (duplicate / rewind /
 * fork). Mirrors the dim-and-spinner pattern from the session-list delete
 * overlay but covers the whole viewport because these actions affect more
 * than just the sidebar (they close/open tabs, swap message lists, etc.).
 */
export function BusyOverlay({ show, label = "Working…" }: Props) {
  if (!show) return null;
  return (
    <div className="busy-overlay" aria-busy="true" aria-live="polite" role="status">
      <div className="busy-overlay-spinner" aria-hidden="true" />
      <span className="busy-overlay-label">{label}</span>
    </div>
  );
}
