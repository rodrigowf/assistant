/**
 * The conversation's composer slot (IA §6): W-11's Composer, or this package's VoiceDock while
 * this device has voice; with voice on another device, the read-only "Active elsewhere" dock
 * sits above the composer and the text input stays usable (fixes W-1).
 */
import type { ReactNode } from 'react';
import { getCapabilities } from '@/platform';
import { dockVisible, startVoiceFromGesture, transportForProvider, voiceUnsupportedReason } from '@/voice';
import { showSnackbar, useServerConfig } from '@/stores';
import { IconButton } from '@/ui/controls';
import { ActiveElsewhereView, VoiceDock } from './VoiceDock';
import { useVoiceUi } from './useVoiceUi';
import styles from './VoiceDock.module.css';

export function VoiceSlot({ localId, composer }: { localId: string; composer: ReactNode }) {
  const { snapshot: s, controller } = useVoiceUi(localId);
  const provider = useServerConfig((c) => c.config?.default_voice_provider ?? null);
  if (!controller) return <>{composer}</>;
  if (dockVisible(s)) return <VoiceDock localId={localId} />;
  if (s.remoteActive) {
    const canTakeOver = voiceUnsupportedReason(getCapabilities(), transportForProvider(s.remoteProvider ?? provider)) === null;
    return (
      <>
        <ActiveElsewhereView
          onTakeOver={
            canTakeOver
              ? () => {
                  const reason = startVoiceFromGesture(localId);
                  if (reason) showSnackbar(reason, { tone: 'error' });
                }
              : undefined
          }
        />
        {composer}
      </>
    );
  }
  return <>{composer}</>;
}

/** The compact app bar's trailing voice action (mockups (d)): the speaker toggle while this device has voice. */
export function VoiceAction({ localId }: { localId: string }) {
  const { snapshot: s, controller } = useVoiceUi(localId);
  if (!controller || s.status === 'off' || s.status === 'error') return null;
  return (
    <IconButton
      className={styles.barAction}
      icon="volume_up"
      selectedIcon="volume_off"
      selected={s.speakerMuted}
      aria-label={s.speakerMuted ? 'Speaker off' : 'Speaker on'}
      onClick={() => controller.setSpeakerMuted(!s.speakerMuted)}
    />
  );
}
