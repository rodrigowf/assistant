/**
 * One service on Settings → Accounts: status chip, what it is signed in with (method, account,
 * plan, expiry), warnings, and one button per sign-in method. A method opens its panel under the
 * card: the link flow (URL with Open / Copy, device code, pasted-back code, live status, Cancel),
 * the credentials paste (with the exact file to copy and where it lands on the server), or the
 * env-key fields. Sign out asks first.
 */
import { useState } from 'react';
import type { AccountMethod, AccountService, LoginFlow } from '@/services';
import { copyText } from '@/platform';
import { showSnackbar } from '@/stores';
import { Button, IconButton, TextField } from '@/ui/controls';
import { ConfirmDialog } from '@/ui/overlays';
import { Icon, Spinner, type IconName } from '@/ui/primitives';
import {
  cancelLogin,
  clearAccountError,
  dismissFlow,
  refreshService,
  saveCredentials,
  signOut,
  startLogin,
  submitCode,
  useAccounts,
  verifyService,
} from './accountsStore';
import { EnvFieldRow, SecretField } from './EnvKeys';
import { FLOW_LABEL, STATE_LABEL, flowActive, formatExpiry, jsonError, stateTone } from './logic';
import styles from './accounts.module.css';

const SERVICE_ICON: Record<string, IconName> = {
  claude: 'smart_toy',
  codex: 'terminal',
  gemini: 'star_shine',
  qwen: 'code',
  modelstudio: 'hub',
  openai: 'record_voice_over',
  google_ai: 'graphic_eq',
  dashscope: 'graphic_eq',
  anthropic: 'forum',
  browser: 'language',
  google_oauth: 'shield',
};

const METHOD_ICON: Record<AccountMethod['kind'], IconName> = {
  link: 'login',
  credentials: 'content_paste',
  env: 'key',
  signout: 'logout',
};

const STATE_ICON: Record<ReturnType<typeof stateTone>, IconName> = { ok: 'check_circle', off: 'account_circle', bad: 'error', unknown: 'help' };

export function ServiceCard({ service }: { service: AccountService }) {
  const busy = useAccounts((s) => s.busy[service.id]);
  const error = useAccounts((s) => s.errors[service.id]);
  const [open, setOpen] = useState<string | null>(null);
  const [confirmSignOut, setConfirmSignOut] = useState(false);
  const tone = stateTone(service.state);
  const methods = service.methods.filter((m) => m.kind !== 'signout');
  const signout = service.methods.find((m) => m.kind === 'signout');
  const flow = service.flow;
  const expiry = formatExpiry(service.expires_at);
  const facts: [string, string][] = [];
  if (service.method) facts.push(['Signed in with', service.method]);
  if (service.account) facts.push(['Account', service.account]);
  if (service.plan) facts.push(['Plan', service.plan]);
  if (expiry) facts.push(['Valid', expiry]);
  const openMethod = methods.find((m) => m.id === open) ?? null;

  return (
    <section className={styles.card} aria-labelledby={`acct-${service.id}`} data-account={service.id} data-state={service.state}>
      <div className={styles.head}>
        <Icon name={SERVICE_ICON[service.id] ?? 'key'} size={24} className={styles.headIcon} />
        <div className={styles.headText}>
          <h4 id={`acct-${service.id}`} className={styles.title}>
            {service.label}
          </h4>
          <p className={styles.desc}>{service.description}</p>
        </div>
        <div className={styles.headEnd}>
          <span className={styles.state} data-tone={tone}>
            <Icon name={STATE_ICON[tone]} size={16} />
            {STATE_LABEL[service.state]}
          </span>
          <IconButton icon="refresh" size="small" aria-label={`Check ${service.label} again`} loading={busy === 'refresh'} onClick={() => void refreshService(service.id)} />
        </div>
      </div>

      {facts.length ? (
        <dl className={styles.facts}>
          {facts.map(([k, v]) => (
            <div key={k}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {service.detail || service.warnings.length || service.verified ? (
        <ul className={styles.lines}>
          {service.detail ? (
            <li className={styles.line}>
              <Icon name="info" size={16} />
              <span>{service.detail}</span>
            </li>
          ) : null}
          {service.warnings.map((w) => (
            <li key={w} className={styles.line} data-tone="warning">
              <Icon name="warning" size={16} />
              <span>{w}</span>
            </li>
          ))}
          {service.verified ? (
            <li className={styles.line} data-tone={service.verified.ok ? 'ok' : 'error'} data-verified="">
              <Icon name={service.verified.ok ? 'check_circle' : 'error'} size={16} />
              <span>{service.verified.message}</span>
            </li>
          ) : null}
        </ul>
      ) : null}

      {error ? (
        <p className={`${styles.alert} ${styles.spaced}`} role="alert">
          {error}
        </p>
      ) : null}

      {flow ? <FlowPanel key={flow.id} service={service} flow={flow} busy={busy} /> : null}

      <div className={styles.methods}>
        {methods.map((m) =>
          m.available ? (
            <Button
              key={m.id}
              variant={open === m.id ? 'filled' : m.recommended ? 'tonal' : 'outlined'}
              size="small"
              icon={METHOD_ICON[m.kind]}
              aria-expanded={m.kind === 'link' ? undefined : open === m.id}
              disabled={m.kind === 'link' && flowActive(flow)}
              loading={m.kind === 'link' && busy === 'login' && open === m.id}
              onClick={() => {
                clearAccountError(service.id);
                if (m.kind === 'link') {
                  setOpen(m.id);
                  void startLogin(service.id, m.id);
                } else setOpen(open === m.id ? null : m.id);
              }}
            >
              {m.label}
            </Button>
          ) : (
            <span key={m.id} className={styles.line} data-unavailable={m.id}>
              <Icon name="close" size={16} />
              <span>
                {m.label}: {m.unavailable_reason}
              </span>
            </span>
          ),
        )}
        {service.can_verify ? (
          <Button variant="text" size="small" icon="network_check" loading={busy === 'verify'} onClick={() => void verifyService(service.id)}>
            Test
          </Button>
        ) : null}
        {signout && !signout.available && service.state === 'signed_in' && signout.unavailable_reason ? (
          <span className={styles.line} data-signout-unavailable="">
            <Icon name="info" size={16} />
            <span>{signout.unavailable_reason}</span>
          </span>
        ) : null}
        {signout?.available ? (
          <Button variant="text" size="small" tone="error" icon="logout" onClick={() => setConfirmSignOut(true)}>
            {signout.label}
          </Button>
        ) : null}
      </div>

      {openMethod && openMethod.kind !== 'link' ? <MethodPanel service={service} method={openMethod} busy={busy} onDone={() => setOpen(null)} /> : null}
      {openMethod && openMethod.kind === 'link' && !flow ? <p className={styles.panelText}>{openMethod.description}</p> : null}

      {signout ? (
        <ConfirmDialog
          open={confirmSignOut}
          title={`${signout.label}?`}
          confirmLabel="Sign out"
          destructive
          busy={busy === 'logout'}
          onCancel={() => setConfirmSignOut(false)}
          onConfirm={() => {
            void signOut(service.id).then(() => setConfirmSignOut(false));
          }}
        >
          {signout.description}
        </ConfirmDialog>
      ) : null}
    </section>
  );
}

function MethodPanel({ service, method, busy, onDone }: { service: AccountService; method: AccountMethod; busy: string | undefined; onDone: () => void }) {
  if (method.kind === 'credentials') return <CredentialsPanel service={service} method={method} busy={busy} onDone={onDone} />;
  return (
    <div className={styles.panel} data-method={method.id}>
      <p className={styles.panelTitle}>
        <Icon name="key" size={18} />
        {method.label}
      </p>
      {method.description ? <p className={styles.panelText}>{method.description}</p> : null}
      <div className={styles.envList}>
        {method.fields.map((f) => (
          <EnvFieldRow key={f.name} serviceId={service.id} field={f} busy={busy === `env:${f.name}`} />
        ))}
      </div>
    </div>
  );
}

function CredentialsPanel({ service, method, busy, onDone }: { service: AccountService; method: AccountMethod; busy: string | undefined; onDone: () => void }) {
  const [text, setText] = useState('');
  const [localError, setLocalError] = useState<string | null>(null);
  const saving = busy === `credentials:${method.id}`;
  const secret = method.input === 'secret';
  const submit = (): void => {
    const err = secret ? (text.trim() ? null : 'Paste the key') : jsonError(text);
    setLocalError(err);
    if (err) return;
    void saveCredentials(service.id, method.id, text).then((ok) => {
      if (ok) {
        setText('');
        onDone();
      }
    });
  };
  return (
    <form
      className={styles.panel}
      data-method={method.id}
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <p className={styles.panelTitle}>
        <Icon name={secret ? 'key' : 'content_paste'} size={18} />
        {method.label}
      </p>
      {method.description ? <p className={styles.panelText}>{method.description}</p> : null}
      {method.source_hint ? (
        <ol className={styles.steps}>
          <li>
            On a machine where it is signed in, open <code>{method.source_hint}</code>.
          </li>
          <li>Copy the whole file and paste it below.</li>
        </ol>
      ) : null}
      {method.warning ? <p className={styles.caution}>{method.warning}</p> : null}
      {secret ? (
        <SecretField
          label={method.label}
          value={text}
          onValueChange={(v) => {
            setText(v);
            setLocalError(null);
          }}
          placeholder={method.placeholder}
          error={localError ?? undefined}
          supportingText={method.path ? <>Stored in <span className={styles.mono}>{method.path}</span></> : undefined}
        />
      ) : (
        <TextField
          label="File contents (JSON)"
          multiline
          rows={5}
          maxRows={10}
          value={text}
          placeholder={method.placeholder}
          spellCheck={false}
          autoCapitalize="off"
          autoComplete="off"
          onValueChange={(v) => {
            setText(v);
            setLocalError(null);
          }}
          error={localError ?? undefined}
          supportingText={
            method.path ? (
              <>
                Saved on the server as <span className={styles.mono}>{method.path}</span> (mode 600; the previous file is kept as a backup).
              </>
            ) : undefined
          }
        />
      )}
      <div className={styles.actions}>
        <Button type="submit" variant="filled" icon="check" loading={saving} disabled={!text.trim()}>
          Save
        </Button>
        <Button variant="text" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

function FlowPanel({ service, flow, busy }: { service: AccountService; flow: LoginFlow; busy: string | undefined }) {
  const [code, setCode] = useState('');
  const active = flowActive(flow);
  const method = service.methods.find((m) => m.id === flow.method);
  const statusIcon: IconName | null =
    flow.status === 'succeeded' ? 'check_circle' : flow.status === 'failed' || flow.status === 'expired' ? 'error' : flow.status === 'cancelled' ? 'close' : null;
  const copy = (text: string, what: string): void => {
    void copyText(text).then((ok) => showSnackbar(ok ? `${what} copied` : "Couldn't copy; select it and copy by hand", { durationMs: 2500 }));
  };
  return (
    <div className={styles.panel} data-flow={flow.status} aria-live="polite">
      <p className={styles.panelTitle}>
        {statusIcon ? <Icon name={statusIcon} size={18} /> : <Spinner size={16} />}
        <span>
          {method?.label ?? 'Sign in'} · {FLOW_LABEL[flow.status]}
        </span>
      </p>
      {active && method?.warning ? (
        <p className={styles.caution} data-flow-warning="">
          {method.warning}
        </p>
      ) : null}
      {flow.url && active ? (
        <>
          <ol className={styles.steps}>
            <li>Open the link on any device and sign in.</li>
            {flow.user_code ? <li>Enter the code below when asked.</li> : null}
            {flow.needs_code ? <li>{flow.code_help || `Copy the ${flow.code_label.toLowerCase()} you get and paste it here.`}</li> : <li>Approve; this page updates by itself.</li>}
          </ol>
          <div className={styles.linkBox} data-flow-url="">
            {flow.url}
          </div>
          <div className={styles.actions}>
            <Button variant="filled" size="small" trailingIcon="open_in_new" onClick={() => window.open(flow.url ?? '', '_blank', 'noopener,noreferrer')}>
              Open link
            </Button>
            <Button variant="outlined" size="small" icon="content_copy" onClick={() => copy(flow.url ?? '', 'Link')}>
              Copy link
            </Button>
          </div>
        </>
      ) : null}
      {flow.user_code && active ? (
        <div className={styles.actions}>
          <span className={styles.code} data-user-code="">
            {flow.user_code}
          </span>
          <IconButton icon="content_copy" aria-label="Copy the code" onClick={() => copy(flow.user_code ?? '', 'Code')} />
        </div>
      ) : null}
      {flow.needs_code && (flow.status === 'waiting' || flow.status === 'verifying') ? (
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            if (code.trim()) void submitCode(service.id, code.trim());
          }}
        >
          <TextField
            label={flow.code_label}
            value={code}
            autoComplete="off"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            onValueChange={setCode}
            disabled={flow.status === 'verifying'}
          />
          <div className={`${styles.actions} ${styles.spaced}`}>
            <Button type="submit" variant="filled" icon="check" loading={busy === 'code' || flow.status === 'verifying'} disabled={!code.trim()}>
              Finish sign-in
            </Button>
          </div>
        </form>
      ) : null}
      <p className={styles.panelText} data-flow-message="">
        {flow.message}
      </p>
      <div className={styles.actions}>
        {active ? (
          <Button variant="text" size="small" icon="close" loading={busy === 'cancel'} onClick={() => void cancelLogin(service.id)}>
            Cancel sign-in
          </Button>
        ) : (
          <Button variant="text" size="small" onClick={() => dismissFlow(service.id)}>
            {flow.status === 'succeeded' ? 'Done' : 'Dismiss'}
          </Button>
        )}
      </div>
    </div>
  );
}
