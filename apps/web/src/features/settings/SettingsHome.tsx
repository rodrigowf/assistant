/**
 * Settings home (mockup phone (e)): three groups exactly as IA §7, each row showing its current
 * value. The server group carries the server name and state and is disabled while offline.
 */
import { authSummary, backendHost, useAuth } from '@/features/auth';
import { useConnection, usePrefs, useServerConfig } from '@/stores';
import { StatusDot } from '@/ui/controls';
import {
  coerceWorkingDirectories,
  formatGain,
  formatMs,
  formatThreshold,
  languageLabel,
  mcpSummary,
  modelName,
  modelProviderLabel,
  findModel,
  voiceCatalog,
  voiceProviderLabel,
  workingDirectoriesSummary,
} from './logic';
import { useAccounts } from './accounts/accountsStore';
import { accountsSummary } from './accounts/logic';
import { SETTINGS_PAGES, type SettingsPageDef, type SettingsPageId } from './pages';
import { SettingsGroup, SettingsRow } from './parts';
import { TEXT_SIZE_LABELS, THEME_LABELS, notificationsSummary, useNotifyPermission } from './pages/DevicePages';
import { mcpNames } from './pages/McpServersPage';
import styles from './settings.module.css';

function useServerSummaries(): Partial<Record<SettingsPageId, string>> {
  const s = useServerConfig((x) => x);
  const auth = useAuth((a) => a);
  const services = useAccounts((a) => a.services);
  // Every service once Accounts has been opened; until then the Claude check the gate does anyway.
  const out: Partial<Record<SettingsPageId, string>> = { account: services ? accountsSummary(services) : authSummary(auth) };
  const cfg = s.config;
  if (!cfg) {
    const msg = s.error ? "Couldn't load" : 'Loading…';
    for (const p of SETTINGS_PAGES) if (p.group === 'server' && p.id !== 'account') out[p.id] = msg;
    return out;
  }
  const models = s.orchestratorModels?.models ?? [];
  const textModel = findModel(models, cfg.default_model);
  const summ = cfg.summarizer_model;
  const audioId = cfg.default_audio_model || s.orchestratorModels?.default_audio_model || '';
  out['conversation-model'] =
    `${textModel?.provider ? `${modelProviderLabel(textModel.provider)} · ` : ''}${modelName(models, cfg.default_model)}` +
    (audioId ? ` · voice messages by ${modelName(models, audioId)}` : '') +
    ` · summaries by ${summ ? modelName(models, summ) : 'server default'}`;
  const entry = voiceCatalog(s.voiceModels, s.googleVoiceModels[cfg.default_voice_endpoint])[cfg.default_voice_provider]?.find(
    (m) => m.id === cfg.default_voice_model,
  );
  out.voice = [
    voiceProviderLabel(cfg.default_voice_provider),
    cfg.default_voice_model,
    cfg.default_voice_name,
    languageLabel(entry, cfg.default_voice_transcription_language ?? ''),
  ]
    .filter(Boolean)
    .join(' · ');
  out['voice-tuning'] = `VAD ${formatThreshold(cfg.voice_vad_threshold)} · silence ${formatMs(cfg.voice_vad_min_silence_ms)} · gain ${formatGain(cfg.voice_mic_gain)}`;
  const harness = s.providers?.find((p) => p.id === cfg.provider)?.label ?? cfg.provider;
  out['agent-sessions'] = `${harness} by default · Chrome ${cfg.chrome_extension ? 'on' : 'off'}`;
  out['working-directories'] = workingDirectoriesSummary(coerceWorkingDirectories(cfg.working_directory_history), cfg.working_directory);
  out['mcp-servers'] = s.mcpServers ? mcpSummary(cfg.enabled_mcps, mcpNames(s.mcpServers)) : cfg.enabled_mcps?.length ? `${cfg.enabled_mcps.length} enabled` : 'All enabled';
  return out;
}

export interface SettingsHomeProps {
  /** Highlighted row (two-pane). */
  selected?: SettingsPageId | null;
  onOpen: (id: SettingsPageId) => void;
}

export function SettingsHome({ selected, onOpen }: SettingsHomeProps) {
  const theme = usePrefs((p) => p.theme);
  const textSize = usePrefs((p) => p.textSize);
  const notify = usePrefs((p) => p.notifyAgentTurns);
  const [notifyPerm] = useNotifyPermission();
  const backend = useConnection((c) => c.backend);
  const summaries = useServerSummaries();
  const offline = backend === 'offline';
  const host = backendHost();
  const row = (p: SettingsPageDef, summary: string, disabled = false) => (
    <SettingsRow
      key={p.id}
      icon={p.icon}
      title={p.title}
      summary={summary}
      selected={selected === p.id}
      disabled={disabled}
      disabledReason="Offline"
      onOpen={() => onOpen(p.id)}
    />
  );
  const pages = (g: SettingsPageDef['group']) => SETTINGS_PAGES.filter((p) => p.group === g);
  return (
    <nav aria-label="Settings" className={styles.home}>
      <SettingsGroup title="This device">
        {pages('device').map((p) =>
          row(p, p.id === 'notifications' ? notificationsSummary(notify, notifyPerm) : `${THEME_LABELS[theme]} · ${TEXT_SIZE_LABELS[textSize]}`),
        )}
      </SettingsGroup>
      <SettingsGroup
        title="Archie (server)"
        meta={
          <>
            <StatusDot status={offline ? 'disconnected' : backend === 'online' ? 'idle' : 'off'} aria-hidden="true" />
            <span>
              {host} · {offline ? 'offline' : backend === 'online' ? 'online' : 'connecting'}
            </span>
          </>
        }
      >
        {pages('server').map((p) => row(p, summaries[p.id] ?? '', offline))}
      </SettingsGroup>
      <SettingsGroup title="About">
        {pages('about').map((p) => row(p, `App ${__APP_VERSION__} (${__TARGET__}) · backend on ${host}`))}
      </SettingsGroup>
    </nav>
  );
}
