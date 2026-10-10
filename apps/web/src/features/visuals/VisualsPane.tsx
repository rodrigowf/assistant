/**
 * Visuals list (spec 13 §3.6 `features/visuals`, IA §3: thumbnail-less cards with title,
 * project folder and age; inv02 F-36 VizItem). Refreshed when the pane opens and on Refresh
 * (spec 12 §9.1; turn ends and pool pushes refresh it in `@/services`). Rename only, no delete
 * (by design, F-36). Each row's actions: Rename, Show on TV (when the probe says available),
 * Open in browser, Copy link.
 */
import { useEffect, useMemo, useState } from 'react';
import { ActionRow, matchesQuery, parseServerTime, RenameDialog, shortAge, useMinuteClock, type RowAction } from '@/features/history';
import { copyText } from '@/platform';
import { errorMessage, refreshVisuals, renameVisualization, type VisualizationInfo } from '@/services';
import { showSnackbar, useCapabilities, useCatalog, useTabs } from '@/stores';
import { Button, EmptyState, IconButton, SearchField } from '@/ui/controls';
import { Icon } from '@/ui/primitives';
import { preloadVisualViewer } from './lazy';
import { showOnTv, vizFolder, vizHref } from './viz';
import styles from './visuals.module.css';

export interface VisualsPaneProps {
  /** Open a visual (W-07 `openDocument('visual', …)`: a tab on medium/expanded, a screen on compact). */
  readonly onOpen?: (visual: VisualizationInfo) => void;
  /** Highlighted visual; defaults to the active visual tab's path. */
  readonly selectedPath?: string | null;
  /** Fixed clock (tests, gallery). */
  readonly now?: number;
}

export function VisualsPane({ onOpen, selectedPath, now }: VisualsPaneProps) {
  const visuals = useCatalog((s) => s.visuals);
  const castAvailable = useCapabilities((s) => s.castAvailable);
  const activeVisual = useTabs((s) => {
    const t = s.tabs.find((x) => x.id === s.activeId);
    return t && t.kind === 'visual' ? (t.path ?? null) : null;
  });
  const [query, setQuery] = useState('');
  const [renaming, setRenaming] = useState<VisualizationInfo | null>(null);

  useEffect(() => {
    void refreshVisuals();
    preloadVisualViewer();
  }, []);

  const clock = useMinuteClock(now);
  const shown = useMemo(() => visuals.items.filter((v) => matchesQuery(`${v.title} ${v.path}`, query)), [visuals.items, query]);
  const selected = selectedPath !== undefined ? selectedPath : activeVisual;
  const loadedOnce = visuals.loadedAt > 0;

  const actions = (v: VisualizationInfo): RowAction[] => {
    const out: RowAction[] = [
      {
        id: 'rename',
        icon: 'edit',
        label: 'Rename',
        onSelect: () => {
          setRenaming(v);
        },
      },
    ];
    if (castAvailable)
      out.push({
        id: 'cast',
        icon: 'cast',
        label: 'Show on TV',
        onSelect: () => {
          void showOnTv(v.path, v.title);
        },
      });
    out.push(
      {
        id: 'browser',
        icon: 'open_in_new',
        label: 'Open in browser',
        menuOnly: true,
        onSelect: () => {
          window.open(vizHref(v.path, v.url), '_blank', 'noopener,noreferrer');
        },
      },
      {
        id: 'copy',
        icon: 'link',
        label: 'Copy link',
        menuOnly: true,
        onSelect: () => {
          void copyText(vizHref(v.path, v.url)).then((ok) => {
            showSnackbar(ok ? 'Link copied' : 'Could not copy the link', ok ? {} : { tone: 'error' });
          });
        },
      },
    );
    return out;
  };

  return (
    <div className={styles.pane}>
      <SearchField
        label="Search visuals"
        value={query}
        onValueChange={setQuery}
        className={styles.search}
        trailing={
          <IconButton
            icon="refresh"
            size="small"
            iconSize={20}
            aria-label="Refresh visuals"
            loading={visuals.loading}
            onClick={() => {
              void refreshVisuals();
            }}
          />
        }
      />
      {visuals.error ? (
        <div className={styles.error} role="alert">
          <span>Could not load the visuals: {visuals.error}</span>
          <Button
            variant="text"
            size="small"
            onClick={() => {
              void refreshVisuals();
            }}
          >
            Retry
          </Button>
        </div>
      ) : null}
      {!loadedOnce && visuals.loading ? <p className={styles.note}>Loading visuals…</p> : null}
      {loadedOnce && visuals.items.length === 0 ? (
        <EmptyState icon="bar_chart" title="No visuals yet" description="HTML files under context/public/ appear here." headingLevel={3} />
      ) : null}
      {query.trim() && shown.length === 0 && visuals.items.length > 0 ? <p className={styles.note}>No visuals match “{query.trim()}”</p> : null}
      <div role="list" aria-label="Visuals">
        {shown.map((v) => {
          const age = shortAge(parseServerTime(v.modified), clock);
          return (
            <div role="listitem" key={v.path}>
              <ActionRow
                data-row={v.path}
                leading={
                  <span className={styles.tile} aria-hidden="true">
                    <Icon name="bar_chart" size={20} />
                  </span>
                }
                label={v.title || v.path}
                supporting={age ? `${vizFolder(v.path)} · ${age}` : vizFolder(v.path)}
                title={v.path}
                active={v.path === selected}
                onClick={() => onOpen?.(v)}
                actions={actions(v)}
              />
            </div>
          );
        })}
      </div>
      {renaming ? (
        <RenameDialog
          key={renaming.path}
          title="Rename visual"
          initial={renaming.title}
          onCommit={async (t) => {
            try {
              await renameVisualization(renaming.path, t);
              return true;
            } catch (err) {
              showSnackbar(errorMessage(err), { tone: 'error' });
              return false;
            }
          }}
          onClose={() => {
            setRenaming(null);
          }}
        />
      ) : null}
    </div>
  );
}
