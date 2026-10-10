/**
 * `SessionActionsHost` — mounted once per app. Renders the session-action dialogs and the busy
 * overlay, and binds F2 (rename the active conversation, P-7 / mockups "Rename F2").
 *
 * - Delete confirm (mockups "Delete this session?"; spec 12 §6.8 copy: moved to the server's
 *   trash, recoverable). Deleting closes the session for every device first (P-1).
 * - Fork confirm (the ⋮ menu's whole-conversation copy).
 * - The three-action Archie dialog (spec 12 §6.11, inv02 F-25): Open the running one / Stop it
 *   and start new (or resume this one) / Cancel.
 * - `BusyOverlay` (inv02 F-12): rewind, fork, delete, replacing Archie.
 */
import { useEffect } from 'react';
import { findTab, tabsStore, useCatalog } from '@/stores';
import { Button } from '@/ui/controls';
import { overlayStack } from '@/ui/a11y';
import { BusyOverlay, ConfirmDialog, Dialog, Portal } from '@/ui/overlays';
import { deleteNow, forkWhole, resolveArchieConflict, sessionTitle, sdkIdOf } from './actions';
import { patchSessionActions, useSessionActionsState } from './actionsStore';

export interface SessionActionsHostProps {
  /** F2 → the shell's rename flow (W-07 `requestRename`). */
  readonly onRename?: (localId: string) => void;
}

function DeleteDialog() {
  const confirm = useSessionActionsState((s) => (s.confirm?.kind === 'delete' ? s.confirm : null));
  const localId = confirm?.localId ?? null;
  const sdk = localId ? sdkIdOf(localId) : null;
  const count = useCatalog((c) => (sdk ? (c.sessions.items.find((s) => s.session_id === sdk)?.message_count ?? null) : null));
  const tab = localId ? findTab(localId) : undefined;
  const archie = tab?.kind === 'archie';
  const title = localId ? sessionTitle(localId) : '';
  const close = (): void => {
    patchSessionActions({ confirm: null });
  };
  return (
    <ConfirmDialog
      open={!!confirm}
      title={archie ? 'Delete this conversation?' : 'Delete this session?'}
      confirmLabel="Delete"
      destructive
      icon="delete"
      onCancel={close}
      onConfirm={() => {
        close();
        if (localId) void deleteNow(localId);
      }}
    >
      {`“${title}”${count ? ` and its ${count} messages` : ''} move to the server’s trash (recoverable from context/trash/). Memory files are kept.`}
      {tab && !tab.readOnly ? (archie ? ' Archie stops on every device.' : ' The session stops on every device.') : ''}
    </ConfirmDialog>
  );
}

function ForkDialog() {
  const confirm = useSessionActionsState((s) => (s.confirm?.kind === 'fork' ? s.confirm : null));
  const localId = confirm?.localId ?? null;
  const archie = localId ? findTab(localId)?.kind === 'archie' : false;
  const close = (): void => {
    patchSessionActions({ confirm: null });
  };
  return (
    <ConfirmDialog
      open={!!confirm}
      title="Fork this conversation?"
      confirmLabel="Fork"
      icon="call_split"
      onCancel={close}
      onConfirm={() => {
        close();
        if (localId) void forkWhole(localId);
      }}
    >
      {archie
        ? 'A copy of the whole conversation opens read-only in a new tab. Resume it there to continue the copy. The original is unchanged.'
        : 'A copy of the whole conversation opens in a new tab. The original is unchanged.'}
    </ConfirmDialog>
  );
}

function ArchieConflictDialog() {
  const c = useSessionActionsState((s) => s.archieConflict);
  const resume = c?.mode === 'resume';
  const stopLabel = resume ? 'Stop it and resume this one' : 'Stop it and start new';
  return (
    <Dialog
      open={!!c}
      onClose={() => void resolveArchieConflict('cancel')}
      title={resume ? 'Another Archie conversation is running' : 'Archie is already active'}
      icon="forum"
      role="alertdialog"
      maxWidth={440}
      actions={
        <>
          <Button variant="text" onClick={() => void resolveArchieConflict('cancel')}>
            Cancel
          </Button>
          <Button variant="text" tone="error" onClick={() => void resolveArchieConflict('replace')}>
            {stopLabel}
          </Button>
          <Button variant="text" data-autofocus="" onClick={() => void resolveArchieConflict('open')}>
            Open the running one
          </Button>
        </>
      }
    >
      {`Only one Archie conversation runs at a time. ${
        resume ? 'Resuming this conversation' : 'Starting a new one'
      } stops the running one on every device. You can resume it later from the history.`}
    </Dialog>
  );
}

function Busy() {
  const label = useSessionActionsState((s) => s.busy);
  return label ? (
    <Portal>
      <BusyOverlay fixed label={label} />
    </Portal>
  ) : null;
}

/** F2 renames the active conversation (when no overlay is open and no field has focus). */
function useRenameKey(onRename: ((localId: string) => void) | undefined): void {
  useEffect(() => {
    if (!onRename) return undefined;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key !== 'F2' || e.ctrlKey || e.altKey || e.metaKey) return;
      if (overlayStack.size > 0) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable || t.getAttribute('contenteditable') === 'true')) return;
      const id = tabsStore.getState().activeId;
      const tab = id ? findTab(id) : undefined;
      if (!tab || (tab.kind !== 'archie' && tab.kind !== 'agent')) return;
      e.preventDefault();
      onRename(tab.id);
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
    };
  }, [onRename]);
}

export function SessionActionsHost({ onRename }: SessionActionsHostProps) {
  useRenameKey(onRename);
  return (
    <>
      <DeleteDialog />
      <ForkDialog />
      <ArchieConflictDialog />
      <Busy />
    </>
  );
}
