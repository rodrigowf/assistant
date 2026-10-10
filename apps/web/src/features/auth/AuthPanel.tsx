/**
 * The Claude sign-in flows (inv02 §1.11) of the AuthGate screen:
 *
 * - **Sign in with a link** (any server, also headless): `POST /api/accounts/claude/login` runs
 *   `claude setup-token` on the server; the panel shows its URL (Open / Copy), the user signs in on
 *   any device and pastes back the code; the 1-year token is saved on the server. Older backends
 *   without `/api/accounts` fall back to the blocking `POST /api/auth/login` (server with a screen).
 * - **Paste credentials**: `~/.claude/.credentials.json` from a signed-in machine
 *   (`POST /api/auth/credentials`).
 *
 * Settings → Accounts has every method of every service.
 */
import { useEffect, useRef, useState } from 'react';
import { copyText } from '@/platform';
import { showSnackbar } from '@/stores';
import { Button, TextField } from '@/ui/controls';
import { Icon } from '@/ui/primitives';
import { cancelLinkSignIn, clearAuthError, linkFlowActive, pollLinkSignIn, startLinkSignIn, submitCredentials, submitLinkCode } from './authActions';
import { useAuth } from './authStore';
import styles from './auth.module.css';

export interface AuthPanelProps {
  /** Server host for the copy ("…saved on 192.168.0.200"). */
  host: string;
  /** Start in the paste view. */
  startWithPaste?: boolean;
  /** Called after a successful sign-in or credentials save. */
  onSignedIn?: () => void;
}

export const LINK_POLL_MS = 2000;

export function AuthPanel({ host, startWithPaste = false, onSignedIn }: AuthPanelProps) {
  const status = useAuth((s) => s.status);
  const phase = useAuth((s) => s.phase);
  const error = useAuth((s) => s.actionError);
  const flow = useAuth((s) => s.flow);
  const [paste, setPaste] = useState(startWithPaste);
  const [text, setText] = useState('');
  const [code, setCode] = useState('');
  const field = useRef<HTMLTextAreaElement | HTMLInputElement>(null);
  const active = linkFlowActive(flow);

  useEffect(() => () => clearAuthError(), []);
  useEffect(() => {
    if (!active) return undefined;
    const t = setInterval(() => void pollLinkSignIn(), LINK_POLL_MS);
    return () => clearInterval(t);
  }, [active]);

  const done = (): void => {
    showSnackbar('Signed in to Claude', { durationMs: 2500 });
    setText('');
    onSignedIn?.();
  };

  if (!paste) {
    const url = active ? flow?.url : null;
    return (
      <div className={styles.panel}>
        {!flow || !active ? (
          <p className={styles.lead}>
            Sign in with your Claude subscription: open a link on any device, sign in, and paste back the code it shows. The token is saved on <b>{host}</b>.
          </p>
        ) : null}
        {active && !url ? (
          <p className={styles.waiting} role="status">
            <Icon name="hourglass_top" size={18} /> Starting the sign-in on {host}…
          </p>
        ) : null}
        {url ? (
          <>
            <ol className={styles.steps}>
              <li>Open the link and sign in with your Claude account.</li>
              <li>Copy the code the page shows and paste it below.</li>
            </ol>
            <p className={styles.link} data-flow-url="">
              {url}
            </p>
            <div className={styles.actions}>
              <Button variant="filled" trailingIcon="open_in_new" onClick={() => window.open(url, '_blank', 'noopener,noreferrer')}>
                Open link
              </Button>
              <Button
                variant="outlined"
                icon="content_copy"
                onClick={() => {
                  void copyText(url).then((ok) => showSnackbar(ok ? 'Link copied' : "Couldn't copy; select it and copy by hand", { durationMs: 2500 }));
                }}
              >
                Copy link
              </Button>
            </div>
            <form
              noValidate
              className={styles.panel}
              onSubmit={(e) => {
                e.preventDefault();
                if (code.trim()) void submitLinkCode(code).then(() => setCode(''));
              }}
            >
              <TextField
                label="Code"
                value={code}
                autoComplete="off"
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
                onValueChange={setCode}
                disabled={flow?.status === 'verifying'}
              />
              <div className={styles.actions}>
                <Button type="submit" variant="filled" icon="check" loading={phase === 'signing-in' || flow?.status === 'verifying'} disabled={!code.trim()}>
                  Finish sign-in
                </Button>
                <Button variant="text" onClick={() => void cancelLinkSignIn()}>
                  Cancel
                </Button>
              </div>
            </form>
          </>
        ) : null}
        {phase === 'signing-in' && !flow ? (
          <p className={styles.waiting} role="status">
            <Icon name="hourglass_top" size={18} /> Waiting for {host}…
          </p>
        ) : null}
        {flow?.status === 'succeeded' ? (
          <p className={styles.waiting} role="status">
            <Icon name="check_circle" size={18} /> {flow.message}
          </p>
        ) : null}
        {error ? (
          <p className={styles.error} role="alert">
            {error}
          </p>
        ) : null}
        {!active ? (
          <div className={styles.actions}>
            <Button variant="filled" icon="login" loading={phase === 'signing-in'} onClick={() => void startLinkSignIn()}>
              Sign in with Claude
            </Button>
            <Button
              variant="text"
              icon="content_paste"
              disabled={phase === 'signing-in'}
              onClick={() => {
                clearAuthError();
                setPaste(true);
              }}
            >
              Paste credentials instead
            </Button>
          </div>
        ) : null}
      </div>
    );
  }

  const submit = (): void => {
    void submitCredentials(text).then((ok) => {
      if (ok) done();
      else field.current?.focus();
    });
  };

  return (
    <form
      className={styles.panel}
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <ol className={styles.steps}>
        <li>
          On a computer where Claude Code is signed in, open <code>~/.claude/.credentials.json</code>.
        </li>
        <li>Copy the whole file and paste it below.</li>
      </ol>
      <p className={styles.note}>
        <Icon name="info" size={16} /> Don&apos;t paste a login another machine keeps using: refresh tokens rotate and one of the two stops working.
      </p>
      <TextField
        ref={field}
        label="Credentials JSON"
        multiline
        rows={5}
        maxRows={10}
        value={text}
        spellCheck={false}
        autoCapitalize="off"
        autoComplete="off"
        onValueChange={(v) => {
          setText(v);
          if (error) clearAuthError();
        }}
        error={error ?? undefined}
        supportingText={error ? undefined : 'Saved on the server as .credentials.json (only readable by the server user).'}
      />
      <div className={styles.actions}>
        <Button type="submit" variant="filled" icon="check" loading={phase === 'saving'} disabled={!text.trim()}>
          Set credentials
        </Button>
        {status?.auth_url ? (
          <Button variant="text" trailingIcon="open_in_new" onClick={() => window.open(status.auth_url ?? '', '_blank', 'noopener,noreferrer')}>
            Claude Console
          </Button>
        ) : null}
        {!startWithPaste ? (
          <Button
            variant="text"
            icon="arrow_back"
            onClick={() => {
              clearAuthError();
              setPaste(false);
            }}
          >
            Back
          </Button>
        ) : null}
      </div>
    </form>
  );
}
