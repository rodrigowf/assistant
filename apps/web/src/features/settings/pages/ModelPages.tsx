/**
 * Archie (server) → Conversation model and Agent sessions (inv02 F-31; spec 12 §8.1).
 *
 * P-9 / O-7: the Settings `default_model` now decides the model of new Archie conversations
 * (`SETTINGS_DEFAULT_MODEL_FIRST = True`); retired ids (`gpt-4o-audio-preview`, OpenAI 404) are
 * skipped by the server, so the page says so instead of showing a dead choice as healthy.
 *
 * Two models (2026-10-04): no OpenAI chat model takes both typed text and audio, so Archie uses
 * the **text model** (`default_model`) for typed messages and the **audio model**
 * (`default_audio_model`, "" = server default) for voice messages. Each has its own provider
 * select (the audio one offers "Server default" like the summarizer). Live voice is the Voice page.
 */
import { useState } from 'react';
import { loadHarnessCatalogs, type HarnessInfo, type ServerConfig } from '@/services';
import { useServerConfig } from '@/stores';
import { Button, Disclosure, Select, type SelectOption } from '@/ui/controls';
import { saveSetting } from '../controller';
import { globalModelPatch, globalOptionPatch, harnessDefaultsSummary, harnessInfo } from '../harness';
import { ClaudeInChromeField, HarnessFields, HarnessWarnings } from '../HarnessFields';
import {
  audioModels,
  findModel,
  modelAvailability,
  modelProviderLabel,
  modelProviders,
  modelTraits,
  textModels,
} from '../logic';
import { Field, FieldStack, Notice } from '../parts';
import { useSaving, WithConfig } from './shared';
import type { ModelInfo } from '@/protocol';
import styles from '../settings.module.css';

function modelOptions(models: readonly ModelInfo[], provider: string, current: string): SelectOption[] {
  const opts: SelectOption[] = models
    .filter((m) => m.provider === provider && m.model_id)
    .map((m) => {
      const traits = modelTraits(m);
      return { value: m.model_id as string, label: m.display_name ?? (m.model_id as string), description: traits ? `${m.model_id} · ${traits}` : m.model_id };
    });
  if (current && !opts.some((o) => o.value === current)) opts.unshift({ value: current, label: `${current} (unavailable)`, disabled: true });
  return opts;
}

function firstModelOf(models: readonly ModelInfo[], provider: string): string | null {
  return models.find((m) => m.provider === provider && m.model_id)?.model_id ?? null;
}

const SERVER_DEFAULT = '';

export function ConversationModelPage() {
  return <WithConfig>{(cfg) => <ConversationModelForm cfg={cfg} />}</WithConfig>;
}

function ConversationModelForm({ cfg }: { cfg: ServerConfig }) {
  const catalog = useServerConfig((s) => s.orchestratorModels);
  const saving = useSaving();
  const models = catalog?.models ?? [];
  const typed = textModels(models);
  const providers = modelProviders(typed);
  const current = cfg.default_model;
  const provider = findModel(models, current)?.provider ?? providers[0] ?? '';
  const availability = modelAvailability(current, catalog);
  const currentIsAudioOnly = findModel(models, current)?.supports_audio === true;
  const providerOptions: SelectOption[] = providers.map((p) => ({ value: p, label: modelProviderLabel(p) }));

  // The audio model has its own provider: audio-capable models don't exist for every text
  // provider (none at Anthropic), so it can't follow the text provider. Same shape as the summarizer.
  const audio = audioModels(models);
  const audioProviders = modelProviders(audio);
  const audioCurrent = cfg.default_audio_model ?? '';
  const audioProvider = audioCurrent ? (findModel(models, audioCurrent)?.provider ?? audioProviders[0] ?? '') : SERVER_DEFAULT;
  const serverAudio = catalog?.default_audio_model;
  const serverDefault: SelectOption =
    serverAudio && !audioCurrent
      ? { value: SERVER_DEFAULT, label: 'Server default', description: `Now ${serverAudio}` }
      : { value: SERVER_DEFAULT, label: 'Server default' };
  const audioProviderOptions: SelectOption[] = [serverDefault, ...audioProviders.map((p) => ({ value: p, label: modelProviderLabel(p) }))];
  const audioSupported = cfg.default_audio_model !== undefined;

  const summ = cfg.summarizer_model;
  const summProvider = summ ? (findModel(models, summ)?.provider ?? providers[0] ?? '') : SERVER_DEFAULT;
  const summProviderOptions: SelectOption[] = [{ value: SERVER_DEFAULT, label: 'Server default' }].concat(providerOptions);

  return (
    <>
      {availability !== 'ok' ? (
        <Notice tone="warning" title={availability === 'retired' ? 'This model was retired' : 'This model is not in the catalog'}>
          <p>
            “{current}” {availability === 'retired' ? 'answers 404 at OpenAI' : 'is not offered by the server'}, so new conversations skip it and use
            the server&apos;s fallback. Pick another model below.
          </p>
        </Notice>
      ) : null}
      {currentIsAudioOnly ? (
        <Notice tone="info" title="The text model is an audio model">
          <p>
            “{current}” can&apos;t answer typed messages, so the server answers them with gpt-4o. Pick a text model below, and choose the
            audio model separately.
          </p>
        </Notice>
      ) : null}
      <FieldStack label="Archie">
        <Field
          help="Answers typed messages. New Archie conversations start on it."
          info={
            <>
              <p>You can still switch models inside a conversation.</p>
              <p>If this model can&apos;t be used, the server falls back to its ORCHESTRATOR_MODEL setting, then gpt-audio.</p>
            </>
          }
        >
          <Select
            label="Text model provider"
            options={providerOptions}
            value={provider}
            disabled={saving || !providers.length}
            onChange={(p) => {
              const first = firstModelOf(typed, p);
              if (first && p !== provider) void saveSetting({ default_model: first }, 'default_model');
            }}
          />
          <Select
            label="Text model"
            options={modelOptions(typed, provider, current)}
            value={current}
            disabled={saving || !models.length}
            supportingText={catalog ? undefined : 'Loading the model list…'}
            onChange={(id) => {
              if (id !== current) void saveSetting({ default_model: id }, 'default_model');
            }}
          />
        </Field>
        <Field
          help="Answers voice messages (recorded clips)."
          info={
            <>
              <p>Audio models take audio but refuse typed messages, and text models refuse audio, so Archie uses one of each.</p>
              <p>Live voice conversations use the Voice page instead.</p>
            </>
          }
        >
          <Select
            label="Audio model provider"
            options={audioProviderOptions}
            value={audioProvider}
            disabled={saving || !audioSupported || !models.length}
            supportingText={audioSupported ? undefined : 'This server has no separate audio model setting yet.'}
            onChange={(p) => {
              if (p === audioProvider) return;
              if (p === SERVER_DEFAULT) void saveSetting({ default_audio_model: '' }, 'default_audio_model');
              else {
                const first = firstModelOf(audio, p);
                if (first) void saveSetting({ default_audio_model: first }, 'default_audio_model');
              }
            }}
          />
          {audioProvider !== SERVER_DEFAULT ? (
            <Select
              label="Audio model"
              options={modelOptions(audio, audioProvider, audioCurrent)}
              value={audioCurrent}
              disabled={saving || !audioSupported}
              onChange={(id) => {
                if (id !== audioCurrent) void saveSetting({ default_audio_model: id }, 'default_audio_model');
              }}
            />
          ) : null}
        </Field>
      </FieldStack>
      <FieldStack label="History summaries">
        <Field help="Summarizes older history for Archie's voice prompt.">
          <Select
            label="Summarizer provider"
            options={summProviderOptions}
            value={summProvider}
            disabled={saving}
            onChange={(p) => {
              if (p === summProvider) return;
              if (p === SERVER_DEFAULT) void saveSetting({ summarizer_model: '' }, 'summarizer_model');
              else {
                const first = firstModelOf(models, p);
                if (first) void saveSetting({ summarizer_model: first }, 'summarizer_model');
              }
            }}
          />
          {summProvider !== SERVER_DEFAULT ? (
            <Select
              label="Summarizer model"
              options={modelOptions(models, summProvider, summ)}
              value={summ}
              disabled={saving}
              onChange={(id) => {
                if (id !== summ) void saveSetting({ summarizer_model: id }, 'summarizer_model');
              }}
            />
          ) : null}
        </Field>
      </FieldStack>
    </>
  );
}

export function AgentSessionsPage() {
  return <WithConfig>{(cfg) => <AgentSessionsForm cfg={cfg} />}</WithConfig>;
}

function AgentSessionsForm({ cfg }: { cfg: ServerConfig }) {
  const harnesses = useServerConfig((s) => s.harnesses);
  const saving = useSaving();
  const list = harnesses ?? [];
  const options: SelectOption[] = list.map((p) => ({ value: p.id, label: p.label || p.id }));
  if (cfg.provider && !options.some((o) => o.value === cfg.provider)) options.unshift({ value: cfg.provider, label: cfg.provider });
  const selected = harnessInfo(list, cfg.provider);
  const others = list.filter((h) => h.id !== cfg.provider);

  return (
    <>
      <FieldStack label="New sessions">
        <Field help="Applies to new agent tabs. Per session: ⋮ → Session settings.">
          <Select
            label="Default harness"
            options={options}
            value={cfg.provider}
            disabled={saving || !options.length}
            supportingText={harnesses ? selected?.description : 'Loading the harness list…'}
            onChange={(id) => {
              if (id !== cfg.provider) void saveSetting({ provider: id }, 'provider');
            }}
          />
        </Field>
      </FieldStack>
      {selected ? (
        <>
          <HarnessWarnings harness={selected} />
          <FieldStack label={`${selected.label} defaults`}>
            <HarnessDefaults cfg={cfg} harness={selected} disabled={saving} />
            <RefreshCatalogs />
          </FieldStack>
        </>
      ) : null}
      {others.length ? (
        <div className={styles.stackBlock}>
          <h3 className={styles.stackLabel}>Other harnesses</h3>
          <p className={styles.help}>Defaults for sessions you switch to another harness (⋮ → Session settings).</p>
          <div className={styles.harnessList}>
            {others.map((h) => (
              <OtherHarness key={h.id} cfg={cfg} harness={h} disabled={saving} />
            ))}
          </div>
        </div>
      ) : null}
    </>
  );
}

/** A collapsed block of a non-default harness; its fields mount when opened (so controls measure their real width). */
function OtherHarness({ cfg, harness, disabled }: { cfg: ServerConfig; harness: HarnessInfo; disabled: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <Disclosure
      summary={harness.label || harness.id}
      meta={harnessDefaultsSummary(harness.catalog, cfg.harness_model?.[harness.id] ?? '', cfg.harness_options?.[harness.id])}
      className={styles.harnessGroup}
      bodyClassName={styles.harnessGroupBody}
      open={open}
      onOpenChange={setOpen}
    >
      {open ? (
        <>
          <HarnessWarnings harness={harness} />
          <div className={styles.fieldStack}>
            <HarnessDefaults cfg={cfg} harness={harness} disabled={disabled} />
          </div>
        </>
      ) : null}
    </Disclosure>
  );
}

/**
 * Model + options of one harness, each saved on its own (`harness_model` / `harness_options` partial
 * PUTs); Claude Code adds its "Claude in Chrome" switch (`chrome_extension`).
 */
function HarnessDefaults({ cfg, harness, disabled }: { cfg: ServerConfig; harness: HarnessInfo; disabled: boolean }) {
  const p = harness.id;
  return (
    <>
      <HarnessFields
        harness={harness}
        scope="global"
        model={cfg.harness_model?.[p] ?? ''}
        options={cfg.harness_options?.[p] ?? null}
        disabled={disabled}
        onModel={(m) => {
          void saveSetting(globalModelPatch(p, m ?? ''), 'harness_model');
        }}
        onOption={(key, st) => {
          void saveSetting(globalOptionPatch(p, key, st), 'harness_options');
        }}
      />
      {p === 'claude' ? (
        <ClaudeInChromeField
          scope="global"
          value={cfg.chrome_extension}
          disabled={disabled}
          onChange={(v) => {
            void saveSetting({ chrome_extension: v === true }, 'chrome_extension');
          }}
        />
      ) : null}
    </>
  );
}

/** "Refresh models": rebuild the server's catalogs (a model added to a CLI's settings, a new live model). */
function RefreshCatalogs() {
  const [busy, setBusy] = useState(false);
  return (
    <Field help="Model lists are cached on the server for a few minutes.">
      <div className={styles.actionsRow}>
        <Button
          variant="text"
          size="small"
          icon="refresh"
          loading={busy}
          onClick={() => {
            setBusy(true);
            void loadHarnessCatalogs(true).finally(() => {
              setBusy(false);
            });
          }}
        >
          Refresh models
        </Button>
      </div>
    </Field>
  );
}
