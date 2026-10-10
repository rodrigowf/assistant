/**
 * `context/.env` editing (spec 12 §8.1 "Environment keys"): the key fields inside a service card
 * (`EnvFieldRow`) and the full list (`EnvKeysSection`). Values are masked everywhere; a full value
 * is fetched only when the eye is pressed or a key is edited (`POST /api/env/{name}/reveal`) and
 * lives in component state only.
 */
import { useEffect, useId, useState, type ReactNode } from 'react';
import type { AccountEnvField, EnvKey } from '@/services';
import { Button, IconButton, Select, TextField } from '@/ui/controls';
import { ConfirmDialog, Dialog } from '@/ui/overlays';
import { Icon } from '@/ui/primitives';
import {
  clearEnvError,
  createEnvKey,
  deleteEnvKey,
  loadEnv,
  removeServiceKey,
  revealEnv,
  setServiceKey,
  updateEnvKey,
  useAccounts,
} from './accountsStore';
import { envNameError } from './logic';
import styles from './accounts.module.css';

/** A key's state as listed; a revealed value is shown only while this still matches. */
const keySig = (k: EnvKey): string => `${k.preview}|${k.length}|${k.line}|${k.duplicates}`;

/** A password-style field with a show/hide toggle (the 16 px rule: always a TextField). */
export function SecretField({
  label,
  value,
  onValueChange,
  placeholder,
  supportingText,
  error,
  initiallyVisible = false,
}: {
  label: string;
  value: string;
  onValueChange: (v: string) => void;
  placeholder?: string;
  supportingText?: ReactNode;
  error?: ReactNode;
  initiallyVisible?: boolean;
}) {
  const [visible, setVisible] = useState(initiallyVisible);
  return (
    <TextField
      label={label}
      type={visible ? 'text' : 'password'}
      value={value}
      placeholder={placeholder}
      autoComplete="off"
      autoCapitalize="off"
      autoCorrect="off"
      spellCheck={false}
      onValueChange={onValueChange}
      supportingText={supportingText}
      error={error}
      trailing={
        <IconButton
          icon={visible ? 'visibility_off' : 'visibility'}
          size="small"
          aria-label={visible ? 'Hide value' : 'Show value'}
          aria-pressed={visible}
          onClick={() => setVisible((v) => !v)}
        />
      }
    />
  );
}

// ───────────────────────── in a service card ─────────────────────────

export function EnvFieldRow({ serviceId, field, busy }: { serviceId: string; field: AccountEnvField; busy: boolean }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [confirm, setConfirm] = useState(false);
  if (field.choices) {
    const options = field.choices.map((c) => ({ value: c.value, label: c.label }));
    return (
      <div className={styles.envRow} data-env-field={field.name}>
        <div className={styles.envEdit}>
          <Select
            label={field.label}
            options={options}
            value={field.value ?? ''}
            disabled={busy}
            supportingText={field.help || <span className={styles.mono}>{field.name}</span>}
            onChange={(v) => {
              if (v === (field.value ?? '')) return;
              void (v === '' ? removeServiceKey(serviceId, field.name) : setServiceKey(serviceId, field.name, v));
            }}
          />
        </div>
      </div>
    );
  }
  const shown = field.set ? (field.secret ? field.preview || '••••' : field.value) : 'Not set';
  return (
    <div className={styles.envRow} data-env-field={field.name}>
      <div className={styles.envMain}>
        <span className={styles.envText}>
          <span className={styles.envLabel}>
            {field.label} <span className={styles.mono}>· {field.name}</span>
          </span>
          <span className={styles.envValue} data-unset={field.set ? undefined : ''}>
            {shown}
          </span>
          {field.help ? <span className={styles.envHelp}>{field.help}</span> : null}
        </span>
        {!editing ? (
          <span className={styles.envActions}>
            <IconButton
              icon={field.set ? 'edit' : 'add'}
              size="small"
              aria-label={`${field.set ? 'Change' : 'Set'} ${field.name}`}
              disabled={busy}
              onClick={() => {
                setDraft(field.secret ? '' : field.value ?? '');
                setEditing(true);
              }}
            />
            {field.set ? <IconButton icon="delete" size="small" aria-label={`Remove ${field.name}`} disabled={busy} onClick={() => setConfirm(true)} /> : null}
          </span>
        ) : null}
      </div>
      {editing ? (
        <form
          className={styles.envEdit}
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            if (!draft.trim()) return;
            void setServiceKey(serviceId, field.name, draft.trim()).then((ok) => {
              if (ok) {
                setEditing(false);
                setDraft('');
              }
            });
          }}
        >
          {field.secret ? (
            <SecretField label={field.label} value={draft} onValueChange={setDraft} placeholder={field.placeholder} supportingText="Saved in context/.env on the server." />
          ) : (
            <TextField
              label={field.label}
              value={draft}
              placeholder={field.placeholder}
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
             
              onValueChange={setDraft}
            />
          )}
          <div className={styles.actions}>
            <Button type="submit" variant="filled" icon="check" loading={busy} disabled={!draft.trim()}>
              Save
            </Button>
            <Button variant="text" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </form>
      ) : null}
      <ConfirmDialog
        open={confirm}
        title={`Remove ${field.name}?`}
        confirmLabel="Remove"
        destructive
        busy={busy}
        onCancel={() => setConfirm(false)}
        onConfirm={() => {
          void removeServiceKey(serviceId, field.name).then(() => setConfirm(false));
        }}
      >
        It is deleted from context/.env (a backup is kept on the server). What uses it stops working until it is set again.
      </ConfirmDialog>
    </div>
  );
}

// ───────────────────────── the full list ─────────────────────────

export function EnvKeysSection() {
  const keys = useAccounts((s) => s.envKeys);
  const path = useAccounts((s) => s.envPath);
  const loading = useAccounts((s) => s.envLoading);
  const error = useAccounts((s) => s.envError);
  const busy = useAccounts((s) => s.envBusy);
  // Revealed values, each tagged with the key's state when it was fetched: after any change to the
  // key (new preview / length / line) the stale value is no longer shown.
  const [revealed, setRevealed] = useState<Record<string, { value: string; sig: string }>>({});
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  useEffect(() => {
    void loadEnv();
    return () => clearEnvError();
  }, []);
  const forget = (name: string): void =>
    setRevealed((r) => {
      const next: Record<string, { value: string; sig: string }> = {};
      for (const k of Object.keys(r)) {
        const v = r[k];
        if (k !== name && v) next[k] = v;
      }
      return next;
    });
  return (
    <div data-env-section="">
      <p className={styles.sectionHelp}>
        The keys in <code>{path || 'context/.env'}</code> on the server. Values stay hidden until you reveal one. Changes apply to the server right away and to agent
        sessions started afterwards; the file syncs to your other machines with <code>context/</code>.
      </p>
      {error ? (
        <p className={styles.alert} role="alert">
          {error}
        </p>
      ) : null}
      {keys === null ? (
        <p className={styles.panelText} role="status">
          {loading ? 'Loading keys…' : '—'}
        </p>
      ) : keys.length === 0 ? (
        <p className={styles.panelText}>No keys yet.</p>
      ) : (
        <div className={styles.envList} role="list" aria-label="Environment keys">
          {keys.map((k) => (
            <EnvKeyRow
              key={k.name}
              k={k}
              value={revealed[k.name]?.sig === keySig(k) ? revealed[k.name]?.value : undefined}
              busy={busy === k.name}
              onReveal={(v) => setRevealed((r) => ({ ...r, [k.name]: { value: v, sig: keySig(k) } }))}
              onHide={() => forget(k.name)}
              onDelete={() => setRemoving(k.name)}
            />
          ))}
        </div>
      )}
      <div className={`${styles.actions} ${styles.envToolbar}`}>
        <Button variant="tonal" icon="add" onClick={() => setAdding(true)}>
          Add key
        </Button>
        <Button variant="text" icon="refresh" loading={loading} onClick={() => void loadEnv()}>
          Reload
        </Button>
      </div>
      {adding ? <AddKeyDialog existing={(keys ?? []).map((k) => k.name)} onClose={() => setAdding(false)} /> : null}
      <ConfirmDialog
        open={removing !== null}
        title={`Delete ${removing ?? ''}?`}
        confirmLabel="Delete"
        destructive
        busy={busy !== null}
        onCancel={() => setRemoving(null)}
        onConfirm={() => {
          const name = removing;
          if (!name) return;
          void deleteEnvKey(name).then((ok) => {
            if (ok) forget(name);
            setRemoving(null);
          });
        }}
      >
        Every line that sets it is removed from context/.env (a backup is kept on the server). The running server forgets it too.
      </ConfirmDialog>
    </div>
  );
}

function EnvKeyRow({
  k,
  value,
  busy,
  onReveal,
  onHide,
  onDelete,
}: {
  k: EnvKey;
  value: string | undefined;
  busy: boolean;
  onReveal: (v: string) => void;
  onHide: () => void;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [fetching, setFetching] = useState(false);
  const shown = value !== undefined;
  const fetchValue = async (): Promise<string | null> => {
    if (value !== undefined) return value;
    setFetching(true);
    const v = await revealEnv(k.name);
    setFetching(false);
    if (v !== null) onReveal(v);
    return v;
  };
  return (
    <div className={styles.envRow} role="listitem" data-env-key={k.name}>
      <div className={styles.envMain}>
        <span className={styles.envText}>
          <span className={styles.envName}>
            {k.name}
            {k.exported ? <span className={styles.tag}>export</span> : null}
            {k.duplicates ? (
              <span className={styles.tag} data-tone="warning" title="Set more than once; the last line wins">
                ×{k.duplicates + 1}
              </span>
            ) : null}
            {!k.in_process ? (
              <span className={styles.tag} data-tone="warning" title="The running server has a different value (edited by hand since it started)">
                not loaded
              </span>
            ) : null}
          </span>
          <span className={styles.envValue} data-unset={k.set ? undefined : ''} aria-live="polite">
            {shown ? value || '(empty)' : k.set ? `${k.preview} · ${k.length} chars` : '(empty)'}
          </span>
        </span>
        {!editing ? (
          <span className={styles.envActions}>
            <IconButton
              icon={shown ? 'visibility_off' : 'visibility'}
              size="small"
              aria-label={shown ? `Hide ${k.name}` : `Reveal ${k.name}`}
              aria-pressed={shown}
              loading={fetching}
              disabled={!k.set && !shown}
              onClick={() => {
                if (shown) onHide();
                else void fetchValue();
              }}
            />
            <IconButton
              icon="edit"
              size="small"
              aria-label={`Edit ${k.name}`}
              disabled={busy}
              onClick={() => {
                void fetchValue().then((v) => {
                  if (v === null) return;
                  setDraft(v);
                  setEditing(true);
                });
              }}
            />
            <IconButton icon="delete" size="small" aria-label={`Delete ${k.name}`} disabled={busy} onClick={onDelete} />
          </span>
        ) : null}
      </div>
      {editing ? (
        <form
          className={styles.envEdit}
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void updateEnvKey(k.name, draft).then((ok) => {
              if (!ok) return;
              onHide();
              setEditing(false);
            });
          }}
        >
          <SecretField label="Value" value={draft} onValueChange={setDraft} initiallyVisible={shown} />
          <div className={styles.actions}>
            <Button type="submit" variant="filled" icon="check" loading={busy}>
              Save
            </Button>
            <Button
              variant="text"
              onClick={() => {
                onHide(); // it was revealed only to prefill the editor
                setEditing(false);
              }}
            >
              Cancel
            </Button>
          </div>
        </form>
      ) : null}
    </div>
  );
}

function AddKeyDialog({ existing, onClose }: { existing: string[]; onClose: () => void }) {
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [touched, setTouched] = useState(false);
  const busy = useAccounts((s) => s.envBusy !== null);
  const serverError = useAccounts((s) => s.envError);
  const formId = useId();
  const nameErr = envNameError(name, existing);
  const submit = (): void => {
    setTouched(true);
    if (nameErr) return;
    void createEnvKey(name.trim(), value).then((ok) => {
      if (ok) onClose();
    });
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title="Add a key"
      fullScreen="compact"
      maxWidth={520}
      actions={
        <>
          <Button variant="text" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" form={formId} variant="text" loading={busy} disabled={!name.trim()}>
            Add
          </Button>
        </>
      }
    >
      <form
        id={formId}
        noValidate
        className={styles.formGrid}
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <button type="submit" hidden tabIndex={-1} aria-hidden="true" />
        <TextField
          label="Name"
          required
          value={name}
          placeholder="MY_API_KEY"
          autoCapitalize="characters"
          autoCorrect="off"
          spellCheck={false}
         
          onValueChange={(v) => setName(v.toUpperCase())}
          onBlur={() => setTouched(true)}
          error={touched && nameErr ? nameErr : undefined}
          supportingText="Capital letters, digits and underscores."
        />
        <SecretField label="Value" value={value} onValueChange={setValue} supportingText="Saved in context/.env on the server." />
        {serverError ? (
          <p className={styles.line} data-tone="error" role="alert">
            <Icon name="error" size={16} /> {serverError}
          </p>
        ) : null}
      </form>
    </Dialog>
  );
}
