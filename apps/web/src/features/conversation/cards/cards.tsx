/**
 * The inline cards above the composer (IA §6, spec 13 §3.6) that are part of the initial bundle:
 * stall, connection / history errors, termination. Permission and agent-approval cards need the
 * tool registry and markdown and live in the lazy "rich" chunk (permission.tsx). Each card reads
 * only its own slice of the session store.
 */
import { memo, useState } from 'react';
import { busy, type ConnectionBanner, type SessionKind, type StallInfo } from '@/protocol';
import { getSessionRuntime } from '@/services';
import { getSessionEntry, useSession, useShallow } from '@/stores';
import { Button } from '@/ui/controls';
import { InlineCard } from './InlineCard';
import styles from './Cards.module.css';

// ───────────────────────── stall (§6.12, F-13) ─────────────────────────

/** `Ns` under 90 s, else `XmYs` (spec 12 §6.12; inv02 F-13, ChatPanel.tsx:9-14). */
export function formatStall(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s}s`;
  return `${Math.floor(s / 60)}m${s % 60}s`;
}

/** Provider-neutral copy (fixes "No response from Claude" for Qwen and Gemini, inv02 §6.2). */
export function stallText(stall: StallInfo): { title: string; body: string } {
  const t = formatStall(stall.elapsed_seconds);
  if (stall.last_tool_name) {
    return { title: `${stall.last_tool_name} silent for ${t}`, body: `${stall.last_tool_name} has been running for ${t} with no response.` };
  }
  return { title: `No response for ${t}`, body: `No response from the agent for ${t}.` };
}

/** Shown while `stall` is set and the view is busy. "Keep waiting" hides it until the next stall report. */
export const StallCard = memo(function StallCard({ localId }: { localId: string }) {
  const { stall, isBusy, readOnly } = useSession(
    localId,
    useShallow((s) => ({ stall: s.conv.stall, isBusy: busy(s.conv.status), readOnly: s.readOnly })),
  );
  const [dismissed, setDismissed] = useState<StallInfo | null>(null);
  if (!stall || !isBusy || dismissed === stall) return null;
  const { title, body } = stallText(stall);
  return (
    <InlineCard
      tone="stall"
      icon="hourglass_top"
      role="status"
      title={title}
      actions={
        <>
          <Button variant="text" className={styles.onTone} onClick={() => setDismissed(stall)}>
            Keep waiting
          </Button>
          <Button variant="filled" tone="warning" disabled={readOnly} onClick={() => getSessionRuntime(localId)?.interrupt()}>
            Interrupt
          </Button>
        </>
      }
    >
      {body} Interrupt stops it and lets the agent continue.
    </InlineCard>
  );
});

// ───────────────────────── connection / history errors (§4.4.4, §6.13) ─────────────────────────

/** Human copy for `connectionBanner` codes (never raw codes, W-6.2). */
export function bannerText(b: ConnectionBanner, kind: SessionKind): { title: string; body: string } {
  const who = kind === 'orchestrator' ? 'Archie' : 'the session';
  switch (b.code) {
    case 'disconnected':
      return { title: 'Connection lost', body: 'Reconnecting automatically. What you send is kept until the connection is back.' };
    case 'start_timeout':
      return { title: `Couldn't start ${who}`, body: b.detail || 'The server did not answer in time.' };
    case 'start_failed':
      return { title: `Couldn't start ${who}`, body: b.detail || 'The server could not start it.' };
    case 'orchestrator_active':
      return { title: 'Archie is already running', body: b.detail || 'Another Archie conversation is active. Open it, or stop it to start this one.' };
    case 'orchestrator_stopping':
      return { title: 'Archie is still stopping', body: b.detail || 'Try again in a moment.' };
    default:
      return { title: 'Connection problem', body: b.detail || 'The connection to the server failed.' };
  }
}

function ConnectionErrorCard({ localId, banner, kind, readOnly }: { localId: string; banner: ConnectionBanner; kind: SessionKind; readOnly: boolean }) {
  const [details, setDetails] = useState(false);
  const { title, body } = bannerText(banner, kind);
  return (
    <InlineCard
      tone="error"
      icon="error"
      role="alert"
      title={title}
      onDismiss={() => getSessionRuntime(localId)?.dismissBanner()}
      actions={
        <>
          <Button variant="text" className={styles.onTone} aria-expanded={details} onClick={() => setDetails(!details)}>
            {details ? 'Hide details' : 'Details'}
          </Button>
          {readOnly ? null : (
            <Button variant="filled" tone="error" onClick={() => getSessionRuntime(localId)?.retry()}>
              Retry
            </Button>
          )}
        </>
      }
    >
      {body}
      {details ? <span className={styles.details}>{`Code: ${banner.code}${banner.detail ? ` · ${banner.detail}` : ''}`}</span> : null}
    </InlineCard>
  );
}

/**
 * Transport and start errors (`connectionBanner`, never a timeline entry, I-15) with Retry
 * (reconnect now / restart the handshake, §6.13) and dismiss; history load failures quote the
 * server's detail verbatim with Retry. "disconnected" is shown as the "Connection lost at …" line
 * at the end of the list instead (mockups (k)).
 */
export const ErrorCards = memo(function ErrorCards({ localId }: { localId: string }) {
  const { banner, kind, historyError, readOnly } = useSession(
    localId,
    useShallow((s) => ({ banner: s.conv.connectionBanner, kind: s.conv.ref.kind, historyError: s.historyError, readOnly: s.readOnly })),
  );
  return (
    <>
      {banner && banner.code !== 'disconnected' ? <ConnectionErrorCard localId={localId} banner={banner} kind={kind} readOnly={readOnly} /> : null}
      {historyError ? (
        <InlineCard
          tone="error"
          icon="error"
          role="alert"
          title="Couldn't load the conversation"
          onDismiss={() => getSessionEntry(localId)?.handle.patch({ historyError: null })}
          actions={
            <Button variant="filled" tone="error" onClick={() => void getSessionRuntime(localId)?.reload()}>
              Retry
            </Button>
          }
        >
          {historyError}
        </InlineCard>
      ) : null}
    </>
  );
});

