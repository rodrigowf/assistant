/**
 * The voice dock (IA §6; mockups phone (d), (k) and the components board "Voice dock").
 *
 * - `VoiceDockView` is presentational (snapshot + callbacks), shared by the live dock, tests and
 *   the gallery.
 * - Own device: level orb, state word (Listening · Speaking · Thinking · Using tools), the VAD
 *   "Hearing you · Ns" counter in the dock (fixes W-4), mic mute, speaker mute, End.
 * - P-2: Reconnecting… with the elapsed timer and a dimmed warning orb; then a short
 *   "Reconnected" confirmation, or "Couldn't reconnect" with End and Reconnect.
 * - Errors: the message and its recovery hint (typed `voice_error`, fixes inv02 §6.4), Retry.
 * - `ActiveElsewhereView`: read-only "Voice active on another device" with Take over; it sits
 *   above the composer, which stays usable (fixes W-1).
 * - `useVoiceDock(localId)`: the live props (snapshot, timers, level, actions), shared by the dock
 *   and the floating voice controls (`VoiceOverlay`), so both drive the call the same way.
 */
import { useCallback, useMemo } from 'react';
import { Button, IconButton } from '@/ui/controls';
import { Icon, cx } from '@/ui/primitives';
import { showSnackbar } from '@/stores';
import { isOwnerLive, startVoiceFromGesture, type VoiceSnapshot } from '@/voice';
import { dockText, ELSEWHERE_DETAIL, ELSEWHERE_TITLE, formatElapsed, RECONNECTING_DETAIL } from './copy';
import { LevelOrb, type OrbTone } from './LevelOrb';
import { useTicker, useVoiceUi } from './useVoiceUi';
import styles from './VoiceDock.module.css';

export interface VoiceDockActions {
  readonly onEnd: () => void;
  readonly onToggleMic: () => void;
  readonly onToggleSpeaker: () => void;
  readonly onInterrupt: () => void;
  readonly onRetry: () => void;
  readonly onReconnect: () => void;
  readonly onDismiss: () => void;
}

export interface VoiceDockViewProps extends VoiceDockActions {
  readonly snapshot: VoiceSnapshot;
  /** Clock for the timers (tests and the gallery pin it). */
  readonly now: number;
  /** RMS source for the orb. */
  readonly level?: () => number;
  /** Floating controls: the state text becomes a button that opens the Archie conversation. */
  readonly onOpenConversation?: () => void;
}

export function orbTone(s: VoiceSnapshot): OrbTone {
  if (s.link === 'lost') return 'recon';
  if (s.status === 'speaking') return 'speak';
  if (s.status === 'error' || s.status === 'ending') return 'idle';
  return 'listen';
}

export function VoiceDockView(p: VoiceDockViewProps) {
  const s = p.snapshot;

  if (s.link === 'lost') {
    const elapsed = s.linkLostAt !== null ? p.now - s.linkLostAt : 0;
    return (
      <div className={cx(styles.dock, styles.recon)} role="status" aria-live="polite" aria-label="Voice reconnecting" data-voice="reconnecting">
        <LevelOrb tone="recon" />
        <div className={styles.text}>
          <b className={styles.title}>
            Reconnecting…
            <span className={styles.timer} aria-label={`for ${formatElapsed(elapsed)}`}>
              {formatElapsed(elapsed)}
            </span>
          </b>
          <span className={cx(styles.detail, styles.wrap)}>{RECONNECTING_DETAIL}</span>
        </div>
        <IconButton icon="call_end" filled tone="error" aria-label="End voice" onClick={p.onEnd} />
      </div>
    );
  }

  if (s.link === 'restored') {
    return (
      <div className={cx(styles.dock, styles.mini, styles.ok)} role="status" aria-live="polite" data-voice="reconnected">
        <span className={styles.tile} aria-hidden="true">
          <Icon name="check" />
        </span>
        <div className={styles.text}>
          <b className={styles.title}>Reconnected</b>
          <span className={cx(styles.detail, styles.wrap)}>Back after {formatElapsed(s.linkRecoveredMs ?? 0)}</span>
        </div>
      </div>
    );
  }

  if (s.link === 'failed') {
    return (
      <div className={cx(styles.dock, styles.mini, styles.fail)} role="alert" data-voice="failed">
        <span className={styles.tile} aria-hidden="true">
          <Icon name="wifi_off" />
        </span>
        <div className={styles.text}>
          <b className={styles.title}>Couldn’t reconnect</b>
          <span className={cx(styles.detail, styles.wrap)}>Tried for 0:30 · check the connection</span>
        </div>
        <IconButton icon="close" size="small" aria-label="End voice" className={styles.inherit} onClick={p.onDismiss} />
        <div className={styles.failAction}>
          <Button variant="filled" tone="error" size="small" icon="refresh" onClick={p.onReconnect}>
            Reconnect
          </Button>
        </div>
      </div>
    );
  }

  if (s.status === 'error') {
    const e = s.error;
    return (
      <div className={cx(styles.dock, styles.errorDock)} role="alert" data-voice="error">
        <span className={cx(styles.tile, styles.tileError)} aria-hidden="true">
          <Icon name="error" />
        </span>
        <div className={styles.text}>
          <b className={cx(styles.title, styles.titleSmall)}>{e?.message ?? 'Voice stopped'}</b>
          <span className={cx(styles.detail, styles.wrap)}>
            {e?.hint ?? 'Try again, or keep typing below.'}
            {e?.docUrl ? (
              <>
                {' '}
                <a href={e.docUrl} target="_blank" rel="noopener noreferrer" className={styles.link}>
                  Details
                </a>
              </>
            ) : null}
          </span>
        </div>
        <IconButton icon="close" size="small" aria-label="Close voice" onClick={p.onDismiss} />
        <Button variant="tonal" size="small" icon="refresh" onClick={p.onRetry} className={styles.retry}>
          Retry
        </Button>
      </div>
    );
  }

  const t = dockText(s, p.now);
  const live = isOwnerLive(s) && s.status !== 'connecting';
  const tone = orbTone(s);
  const orb = <LevelOrb tone={tone} level={live ? p.level : undefined} still={s.status === 'ending'} />;
  return (
    <div className={styles.dock} role="group" aria-label="Voice controls" data-voice={s.status}>
      {s.status === 'speaking' ? (
        <button type="button" className={styles.orbButton} aria-label="Interrupt Archie" onClick={p.onInterrupt}>
          {orb}
        </button>
      ) : (
        orb
      )}
      {p.onOpenConversation ? (
        <button type="button" className={cx(styles.text, styles.textButton)} aria-label="Open the Archie conversation" onClick={p.onOpenConversation}>
          <span className={styles.textLive} aria-live="polite">
            <b className={styles.title}>{t.title}</b>
            <span className={styles.detail}>{t.detail}</span>
          </span>
        </button>
      ) : (
        <div className={styles.text} aria-live="polite">
          <b className={styles.title}>{t.title}</b>
          <span className={styles.detail}>{t.detail}</span>
        </div>
      )}
      <IconButton
        icon="mic"
        selectedIcon="mic_off"
        variant="tonal"
        selected={s.micMuted}
        disabled={!live}
        aria-label={s.micMuted ? 'Unmute microphone' : 'Mute microphone'}
        onClick={p.onToggleMic}
      />
      <IconButton
        icon="volume_up"
        selectedIcon="volume_off"
        variant="tonal"
        selected={s.speakerMuted}
        aria-label={s.speakerMuted ? 'Unmute speaker' : 'Mute speaker'}
        onClick={p.onToggleSpeaker}
      />
      <IconButton
        icon="call_end"
        filled
        tone="error"
        aria-label={s.status === 'connecting' ? 'Cancel voice' : 'End voice'}
        disabled={s.status === 'ending'}
        onClick={p.onEnd}
      />
    </div>
  );
}

export interface ActiveElsewhereViewProps {
  /** Hidden when this device cannot run voice. */
  readonly onTakeOver?: () => void;
}

export function ActiveElsewhereView({ onTakeOver }: ActiveElsewhereViewProps) {
  return (
    <div className={cx(styles.dock, styles.readOnly)} role="status" data-voice="elsewhere">
      <LevelOrb tone="idle" size={44} still />
      <div className={styles.text}>
        <b className={cx(styles.title, styles.titleSmall)}>{ELSEWHERE_TITLE}</b>
        <span className={styles.detail}>{ELSEWHERE_DETAIL}</span>
      </div>
      {onTakeOver ? (
        <Button variant="text" onClick={onTakeOver}>
          Take over
        </Button>
      ) : null}
    </div>
  );
}

/** The live props of one Archie conversation's dock: snapshot, timers, orb level and actions. */
export function useVoiceDock(localId: string): VoiceDockViewProps {
  const { snapshot: s, controller: c } = useVoiceUi(localId);
  const ticking = s.link === 'lost' || (s.status === 'active' && s.vad?.state === 'listening');
  const now = useTicker(ticking || s.status === 'active', 1000);
  const level = useCallback(() => {
    if (!c) return 0;
    const l = c.levels();
    const snap = c.snapshot;
    // A muted side is flat (as the legacy VolumeBars): the speaker analyser still hears muted audio.
    return snap.status === 'speaking' ? (snap.speakerMuted ? 0 : l.speaker) : snap.micMuted ? 0 : l.mic;
  }, [c]);
  const actions = useMemo<VoiceDockActions>(
    () => ({
      onEnd: () => c?.stop(),
      onToggleMic: () => c?.setMicMuted(!c.snapshot.micMuted),
      onToggleSpeaker: () => c?.setSpeakerMuted(!c.snapshot.speakerMuted),
      onInterrupt: () => c?.interrupt(),
      onRetry: () => {
        const reason = startVoiceFromGesture(localId);
        if (reason) showSnackbar(reason, { tone: 'error' });
      },
      onReconnect: () => c?.reconnect(),
      onDismiss: () => c?.dismissError(),
    }),
    [c, localId],
  );
  return { snapshot: s, now, level, ...actions };
}

/** The live dock of one Archie conversation (replaces the composer while voice is on). */
export function VoiceDock({ localId }: { localId: string }) {
  return <VoiceDockView {...useVoiceDock(localId)} />;
}
