/**
 * Per-session WebSocket resume checkpoint.
 *
 * The backend stamps every broadcast event with a monotonic ``seq`` within
 * a ``stream_id`` (which changes when the SDK subprocess (re)connects).
 * We persist the most-recently-observed ``(stream_id, seq)`` to
 * ``sessionStorage`` so a reconnecting tab can ask the backend to replay
 * events newer than that seq.
 *
 * Why ``sessionStorage`` (not ``localStorage``):
 *  - It survives intra-tab full page reloads (Cmd-R / F5), which is the
 *    main case we want to recover.
 *  - It's per-tab, so two tabs of the same session don't trample each
 *    other's checkpoints.
 *  - It's automatically cleared when the tab closes, which is fine —
 *    a brand-new tab opens a fresh session anyway.
 *
 * Why ``localId`` (not ``sdk_session_id``) as the key:
 *  - ``localId`` is the stable per-tab UUID the frontend already uses
 *    everywhere; ``sdk_session_id`` may not be assigned yet at the
 *    moment we first need to write a checkpoint (it arrives via
 *    ``session_started`` or later).
 *
 * All accessors are no-ops when ``sessionStorage`` is unavailable
 * (private mode, embedded WebViews, quota exceeded). The replay
 * protocol degrades cleanly: no checkpoint → backend treats the client
 * as fresh → full REST refetch on next ``start``.
 */

const STORAGE_PREFIX = "ws-resume-checkpoint:";

/**
 * The checkpoint a client persists between reconnects.  Sent verbatim in
 * the ``resume_from`` field of the ``start`` handshake.
 */
export interface ResumeCheckpoint {
  stream_id: string;
  seq: number;
}

/**
 * Read the persisted checkpoint for a session, or ``null`` if none exists
 * or storage isn't available.
 */
export function readCheckpoint(localId: string): ResumeCheckpoint | null {
  if (!localId) return null;
  const storage = safeSessionStorage();
  if (!storage) return null;
  try {
    const raw = storage.getItem(STORAGE_PREFIX + localId);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (
      !parsed ||
      typeof parsed !== "object" ||
      typeof (parsed as ResumeCheckpoint).stream_id !== "string" ||
      typeof (parsed as ResumeCheckpoint).seq !== "number"
    ) {
      // Malformed payload — clear it and behave as no checkpoint.
      storage.removeItem(STORAGE_PREFIX + localId);
      return null;
    }
    return parsed as ResumeCheckpoint;
  } catch {
    return null;
  }
}

/**
 * Persist the checkpoint for a session.  Monotonic: ignores writes that
 * would move the seq backwards within the same ``stream_id`` (defensive
 * against out-of-order delivery between replay batches and live events).
 *
 * A different ``stream_id`` always overwrites — that signals a backend
 * restart and the old checkpoint is meaningless.
 */
export function writeCheckpoint(
  localId: string,
  checkpoint: ResumeCheckpoint,
): void {
  if (!localId) return;
  const storage = safeSessionStorage();
  if (!storage) return;

  const existing = readCheckpoint(localId);
  if (
    existing &&
    existing.stream_id === checkpoint.stream_id &&
    existing.seq >= checkpoint.seq
  ) {
    return;
  }
  try {
    storage.setItem(STORAGE_PREFIX + localId, JSON.stringify(checkpoint));
  } catch {
    // Quota or disabled storage — ignore.  We'll re-attempt on every
    // event; first successful write wins.
  }
}

/**
 * Drop a session's checkpoint — call this after ``replay_overflow`` (the
 * backend told us our checkpoint is stale and we've already done a full
 * REST refetch to recover).
 */
export function clearCheckpoint(localId: string): void {
  if (!localId) return;
  const storage = safeSessionStorage();
  if (!storage) return;
  try {
    storage.removeItem(STORAGE_PREFIX + localId);
  } catch {
    // ignore
  }
}

/**
 * Pull the (seq, stream_id) pair from a wire-event if both are present.
 * Returns ``null`` for events from a non-protocol-aware backend or for
 * events that aren't subject to seq tracking (``ping``/``pong``).
 *
 * Centralised so the ``useChatInstance`` event handler doesn't have to
 * know the protocol's field names — change the wire shape here only.
 *
 * Accepts any object (typically a discriminated-union ``ServerEvent``);
 * indexed lookups handle variants that don't declare the optional
 * fields without TypeScript complaining about excess-property checks.
 */
export function checkpointFromEvent(
  event: Record<string, unknown>,
): ResumeCheckpoint | null {
  const seq = event["seq"];
  const streamId = event["stream_id"];
  if (
    typeof seq === "number" &&
    typeof streamId === "string" &&
    streamId.length > 0
  ) {
    return { stream_id: streamId, seq };
  }
  return null;
}

function safeSessionStorage(): Storage | null {
  try {
    if (typeof window === "undefined") return null;
    return window.sessionStorage;
  } catch {
    return null;
  }
}
