/**
 * The voice signaling state machine (spec 13 §3.4 `core/VoiceController.ts`; spec 12 §7).
 *
 * One controller per live Archie conversation. Pure in the sense of spec 13: the orchestrator
 * socket (`VoicePort`), the transports, the clock, the cue player and the network source are
 * injected, so every rule below is unit-tested without audio.
 *
 * Rules (each one has a named test in `src/voice/__tests__/`):
 * - LOAD-BEARING inv02 §7 #11 (frontend/src/hooks/useVoiceOrchestrator.ts):
 *   · provider commands are queued until the transport is ready (V-4: FIFO, no cap, never drops);
 *   · `voice_session_update` is forwarded only by the initiator over WebRTC (V-5; :550-571);
 *   · 30 s wait for the connection info (:689-825, the Jetson summariser takes 10–20 s);
 *   · `response.cancel` only while a response is in flight (V-9; :389-410, DashScope closes on a stray cancel);
 *   · local playback flush on barge-in (V-9, V-10);
 *   · Gemini transcript coalescing and empty-text `turnComplete` finalisation live in the
 *     conversation reducer (§4.7); the controller only tracks state for them;
 *   · 5 s ending timeout (:827-858);
 *   · passive viewers act only on `voice_owner_active` / `session_started{voice_initiator:false}`
 *     and their own status stays `off` (fixes W-1, F-29).
 * - Voice tool cards come only from top-level `tool_use`/`tool_result` frames (the reducer, W-3);
 *   every end path delivers `voice_ended` or `voice_local_end` so running voice tools finish (VT-3).
 * - P-1: no lifecycle path sends `stop` or `close`; voice start never sends `stop` (fixes W-10).
 * - P-2: on a link drop: Reconnecting… with an elapsed timer and a repeating cue from the moment
 *   it is lost, automatic re-arm (`voice_start`, V-8), a distinct "reconnected" cue, or after the
 *   30 s budget (`LINK_RETRY_BUDGET_MS`) a failure state, a failure cue and a manual Reconnect.
 */
import { createStore, type StoreApi } from 'zustand/vanilla';
import type { ClientMessage, ServerFrame, SessionStartedFrame, VoiceStartMessage } from '@/protocol';
import { parseConnectionInfo } from './support';
import {
  OFF_SNAPSHOT,
  isOwnerLive,
  type Clock,
  type ConnectionInfo,
  type ConnectionType,
  type CuePlayer,
  type LinkSource,
  type NetworkSource,
  type TransportFactory,
  type VoiceErrorInfo,
  type VoicePort,
  type VoiceSnapshot,
  type VoiceStatus,
  type VoiceTransport,
} from './types';

export const CONNECTION_INFO_TIMEOUT_MS = 30_000;
export const ENDING_TIMEOUT_MS = 5_000;
/** P-2 retry budget (Rodrigo, 2026-10-04). */
export const LINK_RETRY_BUDGET_MS = 30_000;
/** How long the "Reconnected" confirmation stays before the dock returns to Listening. */
export const RESTORED_DISPLAY_MS = 2_500;
/** Delay between re-arm attempts while the socket is open but the transport keeps failing. */
export const TRANSPORT_RETRY_MS = 2_000;

/** Errors (top-level `error.error`) that answer a failed `voice_start`. */
const START_ERRORS = [
  'voice_restart_failed',
  'voice_config_busy',
  'orchestrator_stopping',
  'orchestrator_active',
  'cannot_switch_voice',
  'not_voice_session',
];
/** §7.7: a voice banner; voice keeps going. */
const SOFT_ERRORS = ['voice_event_failed', 'voice_audio_failed'];

export interface VoiceControllerDeps {
  readonly port: VoicePort;
  readonly createTransport: TransportFactory;
  readonly cues: CuePlayer;
  readonly clock?: Clock;
  readonly network?: NetworkSource;
  /** `null` when this transport can run here, else the human reason (spec 13 §2.5, P-8). */
  readonly unsupported?: (kind: ConnectionType) => string | null;
  /** V-3 fallback when `session_started` has no `voice_connection_info`. */
  readonly fetchConnectionInfo?: (f: SessionStartedFrame) => Promise<unknown>;
  readonly log?: (...args: unknown[]) => void;
}

interface Timers {
  connInfo: unknown;
  ending: unknown;
  budget: unknown;
  restored: unknown;
  retry: unknown;
}

type Rec = Record<string, unknown>;

function isRec(v: unknown): v is Rec {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}
function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

const LIVE: readonly VoiceStatus[] = ['active', 'speaking', 'thinking', 'tool_use'];

export class VoiceController {
  readonly store: StoreApi<VoiceSnapshot>;
  private readonly clock: Clock;
  private readonly port: VoicePort;

  /** `startMessage()` returns a `voice_start` (owner, or re-arming after a drop). */
  private armed = false;
  /** A `voice_start` was sent and its `voice_owner_active{active:true}` has not come back (§7.5). */
  private pendingStart = false;
  /** A `voice_start` was sent and its `session_started` has not come back. */
  private awaitingStarted = false;
  /** The call reached ready at least once since `start()` (P-2 applies only to an established call). */
  private everReady = false;
  /**
   * We gave up on a start whose answer may still come (End while connecting, 30 s timeout): its
   * `voice_owner_active{active:true}` broadcast is ours, not another device taking voice.
   */
  private swallowOwnerActive = false;
  private transport: VoiceTransport | null = null;
  /** Ignores callbacks from a transport that was already replaced. */
  private gen = 0;
  private transportReady = false;
  /** WS relay: the server said `voice_status: ready` (mic chunks before it are dropped server-side). */
  private relayReady = false;
  private info: ConnectionInfo | null = null;
  private recordRequested = false;
  /** V-4: provider-bound frames waiting for the transport. FIFO, unbounded, never dropped. */
  private queue: Rec[] = [];
  private responseInFlight = false;
  private cancelSent = false;
  /** A fatal error waiting for `voice_ended` (§7.7 fatal path). */
  private pendingFatal: VoiceErrorInfo | null = null;
  private timers: Timers = { connInfo: null, ending: null, budget: null, restored: null, retry: null };
  private unsubscribeNetwork: (() => void) | null = null;
  private disposed = false;

  constructor(private readonly deps: VoiceControllerDeps) {
    this.port = deps.port;
    this.clock = deps.clock ?? {
      now: () => Date.now(),
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (h) => {
        clearTimeout(h as ReturnType<typeof setTimeout>);
      },
    };
    this.store = createStore<VoiceSnapshot>(() => ({
      ...OFF_SNAPSHOT,
      // hooks attached after the conversation already learned that voice is live: passive
      remoteActive: deps.port.conversationVoiceActive(),
    }));
    if (deps.network) this.unsubscribeNetwork = deps.network.subscribe((online) => this.onNetwork(online));
  }

  get snapshot(): VoiceSnapshot {
    return this.store.getState();
  }

  /** Test/debug view of the V-4 queue. */
  get queuedFrames(): readonly Rec[] {
    return this.queue;
  }

  get localId(): string {
    return this.port.localId;
  }

  private patch(p: Partial<VoiceSnapshot>): void {
    if (this.disposed) return;
    this.store.setState(p);
  }

  private log(...args: unknown[]): void {
    this.deps.log?.(...args);
  }

  // ═════════════════════════ user actions ═════════════════════════

  /** §7.3 owner start. Allowed from `off` and `error` (inv02 F-26). */
  start(): void {
    const s = this.snapshot;
    if (this.disposed || (s.status !== 'off' && s.status !== 'error')) return;
    this.resetCall();
    this.patch({
      ...OFF_SNAPSHOT,
      status: 'connecting',
      phase: 'starting',
      remoteActive: false,
      speakerMuted: s.speakerMuted,
    });
    this.armed = true;
    this.port.requestStart(); // calls startMessage(): voice_start, never `stop` first (W-10, P-1)
  }

  /** Take over voice from another device (mockups "Take over"): a plain owner start. */
  takeOver(): void {
    if (this.snapshot.status === 'off') this.start();
  }

  /** P-2 manual Reconnect after the budget ran out: a fresh budget, cues and timer. */
  reconnect(): void {
    const s = this.snapshot;
    if (this.disposed || (s.status !== 'error' && s.status !== 'off')) return;
    this.resetCall();
    this.patch({ ...OFF_SNAPSHOT, status: 'connecting', phase: 'starting', speakerMuted: s.speakerMuted });
    this.armed = true;
    this.everReady = true; // an established call being restored: drops go through P-2
    this.linkLost('socket', true);
    this.port.requestStart();
  }

  /** §7.6 End. */
  stop(): void {
    const s = this.snapshot;
    if (s.status === 'off') return;
    if (s.status === 'error') {
      this.dismissError();
      return;
    }
    if (s.status === 'ending') return;
    this.armed = false;
    if (s.link === 'lost') {
      // the server already ended (or will end) voice for the dropped socket (G-31)
      this.port.send({ type: 'voice_stop' });
      this.finishEnd(true);
      return;
    }
    const sent = this.port.send({ type: 'voice_stop' });
    if (!sent) {
      this.finishEnd(true);
      return;
    }
    this.enterEnding();
  }

  /** Close the error / "Couldn't reconnect" dock and go back to the composer. */
  dismissError(): void {
    if (this.snapshot.status !== 'error') return;
    this.clearTimer('restored');
    this.patch({ status: 'off', phase: null, error: null, link: 'ok', linkLostAt: null, linkSource: null, linkRecoveredMs: null, banner: null });
  }

  setMicMuted(muted: boolean): void {
    this.transport?.setMicMuted(muted);
    this.patch({ micMuted: muted });
  }

  setSpeakerMuted(muted: boolean): void {
    this.transport?.setSpeakerMuted(muted);
    this.patch({ speakerMuted: muted });
  }

  /** Tap the orb while Archie speaks ("Tap the orb to interrupt"). Same guard as barge-in (V-9). */
  interrupt(): void {
    const t = this.transport;
    if (!t || !this.responseInFlight || this.cancelSent) return;
    t.flushPlayback();
    this.cancelSent = true;
    this.sendProvider({ type: 'response.cancel' });
    if (t.kind === 'webrtc') this.sendProvider({ type: 'output_audio_buffer.clear' });
  }

  levels(): { mic: number; speaker: number } {
    if (!this.transport || !this.transportReady || this.snapshot.link === 'lost') return { mic: 0, speaker: 0 };
    const l = this.transport.levels();
    return { mic: this.snapshot.micMuted ? 0 : l.mic, speaker: this.snapshot.speakerMuted ? 0 : l.speaker };
  }

  /** Release local media (tab closed, app teardown). Sends nothing (P-1). */
  dispose(): void {
    if (this.disposed) return;
    this.closeTransport();
    this.deps.cues.stopLoop();
    this.clearAllTimers();
    this.unsubscribeNetwork?.();
    this.unsubscribeNetwork = null;
    this.armed = false;
    this.disposed = true;
  }

  // ═════════════════════════ VoiceHooks (ArchieRuntime) ═════════════════════════

  /** The start the runtime sends on every socket open / resync while this device owns voice (T-11, V-8). */
  startMessage(): VoiceStartMessage | null {
    if (!this.armed || this.disposed) return null;
    this.pendingStart = true;
    this.awaitingStarted = true;
    this.armTimer('connInfo', CONNECTION_INFO_TIMEOUT_MS, () => this.onConnInfoTimeout());
    const msg: VoiceStartMessage = { type: 'voice_start', local_id: this.port.localId };
    const sdk = this.port.sdkId();
    if (sdk) msg.resume_sdk_id = sdk;
    return msg; // V-1: no voice_* fields → server defaults; on re-arm "keep the previous value" (V-2)
  }

  onFrame(f: ServerFrame): void {
    if (this.disposed) return;
    switch (f.type) {
      case 'session_started':
        return this.onSessionStarted(f);
      case 'voice_owner_active':
        return this.onOwnerActive(f.active === true);
      case 'voice_event':
        return this.onProviderEvent(f.event, false);
      case 'voice_command':
        // V-5: only the owner executes; WS providers get commands server-side
        if (this.isOwner() && this.snapshot.status !== 'error' && this.info?.connectionType !== 'websocket') this.sendProvider(f.command);
        return;
      case 'voice_audio_out':
        return this.onAudioOut(f.audio);
      case 'voice_connection_error':
        if (this.isOwner() && this.snapshot.status === 'connecting') this.startFailed(str(f.detail) || 'Voice could not connect');
        return;
      case 'voice_ending':
        if (!this.isOwner() || this.snapshot.link === 'lost' || this.awaitingStarted) return;
        if (f.reason === 'switch') return; // §6.11a SW-3: quiet, the call continues in the resumed conversation
        if (this.snapshot.status !== 'ending') this.enterEnding();
        return;
      case 'voice_ended':
      case 'voice_stopped':
        // a quiet end for every reason, `switch` included (SW-3): no cue, no notice
        return this.onVoiceEnded();
      case 'error':
        return this.onErrorFrame(f.error, f.detail ?? null);
      default:
        return;
    }
  }

  /** The orchestrator socket dropped. The server ends voice for its owner at once (G-31). */
  onSocketClosed(): void {
    if (this.disposed) return;
    const s = this.snapshot;
    if (!this.isOwner()) return;
    this.pendingStart = false;
    this.awaitingStarted = false;
    this.clearTimer('connInfo');
    if (s.status === 'ending') {
      this.finishEnd(true);
      return;
    }
    if (s.status === 'error') return;
    if (this.everReady) {
      this.linkLost('socket', false);
      return;
    }
    // still bringing the call up: drop the half-built transport; the runtime reconnects and,
    // armed, re-sends voice_start (a fresh 30 s connection-info wait starts then)
    this.closeTransport();
    this.patch({ phase: 'starting' });
  }

  // ═════════════════════════ frames ═════════════════════════

  private isOwner(): boolean {
    const st = this.snapshot.status;
    return st !== 'off';
  }

  private onSessionStarted(f: SessionStartedFrame): void {
    if (f.voice !== true) {
      if (this.armed && this.awaitingStarted) {
        // our voice_start was answered without voice: treat as a failed start
        this.awaitingStarted = false;
        this.pendingStart = false;
        if (this.snapshot.link === 'lost') this.scheduleRetry();
        else this.startFailed('Voice did not start');
        return;
      }
      if (!this.isOwner()) this.patch({ remoteActive: false, remoteProvider: null });
      return;
    }
    if (f.voice_initiator === false) {
      if (this.isOwner() && this.snapshot.status !== 'error') {
        this.ownershipLost(f.voice_provider ?? null);
        return;
      }
      if (this.snapshot.status === 'off') this.patch({ remoteActive: true, remoteProvider: f.voice_provider ?? null });
      return;
    }
    // a late answer to a start we already gave up on (its voice_stop is queued behind it)
    if (!this.armed) return;
    if (!this.awaitingStarted && this.transport && this.transport.healthy()) return; // duplicate answer
    this.awaitingStarted = false;
    this.clearTimer('connInfo');
    this.recordRequested = f.voice_recording_enabled === true;
    this.patch({ provider: f.voice_provider ?? null });
    if (f.voice_connection_error) {
      if (this.snapshot.link === 'lost') this.scheduleRetry();
      else this.startFailed(f.voice_connection_error);
      return;
    }
    // A resync / repeated voice_start on a healthy call: keep the running transport.
    if (this.transport && this.transport.healthy() && this.transportReady) return;
    const raw = f.voice_connection_info;
    const info = parseConnectionInfo(raw);
    if (info) {
      this.useConnectionInfo(info, f.voice_session_update ?? null);
      return;
    }
    const fetchInfo = this.deps.fetchConnectionInfo;
    if (!fetchInfo) {
      this.startFailed('Voice did not start (no connection info from the server)');
      return;
    }
    const gen = ++this.gen;
    fetchInfo(f).then(
      (r) => {
        if (gen !== this.gen || this.disposed || !this.isOwner()) return;
        const parsed = parseConnectionInfo(r);
        if (parsed) this.useConnectionInfo(parsed, f.voice_session_update ?? null);
        else this.startFailed('Voice did not start (no connection info from the server)');
      },
      (err: unknown) => {
        if (gen !== this.gen || this.disposed || !this.isOwner()) return;
        this.startFailed(err instanceof Error ? err.message : 'Voice did not start');
      },
    );
  }

  private useConnectionInfo(info: ConnectionInfo, sessionUpdate: Rec | null): void {
    const reason = this.deps.unsupported?.(info.connectionType) ?? null;
    if (reason) {
      this.startFailed(reason, 'Change the voice provider in Settings → Voice.');
      return;
    }
    this.info = info;
    this.patch({ connectionType: info.connectionType });
    // V-5: only the initiator forwards session.update, and only over WebRTC (the backend sends
    // it upstream itself for WS providers). It MUST be the first provider frame (V-4).
    if (sessionUpdate && info.connectionType === 'webrtc') this.queue.unshift(sessionUpdate);
    this.openTransport(info);
  }

  private openTransport(info: ConnectionInfo): void {
    this.closeTransport();
    const gen = ++this.gen;
    const live = (): boolean => gen === this.gen && !this.disposed;
    this.transportReady = false;
    this.relayReady = false;
    if (info.connectionType === 'websocket' && this.snapshot.status === 'connecting') this.patch({ phase: 'preparing' });
    const t = this.deps.createTransport(info.connectionType, {
      info,
      record: this.recordRequested && info.connectionType === 'webrtc',
      events: {
        ready: () => {
          if (live()) this.onTransportReady();
        },
        providerEvent: (ev) => {
          if (!live()) return;
          // §7.3: every data-channel event is mirrored to the backend (JSONL persistence),
          // and inbound ones feed the conversation reducer (§4.7)
          this.port.send({ type: 'voice_event', event: ev });
          this.port.feedDataChannelEvent(ev);
          this.onProviderEvent(ev, true);
        },
        outbound: (ev) => {
          if (live()) this.port.send({ type: 'voice_event', event: ev });
        },
        audioChunk: (b64) => {
          // only after `ready`: earlier chunks are dropped by the server anyway (§7.3)
          if (live() && this.relayReady && this.snapshot.link !== 'lost') this.port.send({ type: 'voice_audio_in', audio: b64 });
        },
        linkDown: () => {
          if (live()) this.linkLost('transport', true);
        },
        linkUp: () => {
          if (live() && this.snapshot.link === 'lost' && this.snapshot.linkSource === 'transport') this.linkRestored();
        },
        failed: (message) => {
          if (live()) this.onTransportFailed(message);
        },
        recordingChunk: (channel, b64) => {
          if (live()) this.port.send({ type: 'voice_recording_chunk', channel, audio: b64 });
        },
        recordingEnd: () => {
          this.port.send({ type: 'voice_recording_end' });
        },
      },
    });
    this.transport = t;
    t.setMicMuted(this.snapshot.micMuted);
    t.setSpeakerMuted(this.snapshot.speakerMuted);
    this.patch({ recording: this.recordRequested && info.connectionType === 'webrtc' });
    t.start().catch((err: unknown) => {
      if (live()) this.onTransportFailed(err instanceof Error ? err.message : String(err));
    });
  }

  private onTransportReady(): void {
    this.transportReady = true;
    const t = this.transport;
    if (!t) return;
    if (t.kind === 'webrtc') {
      this.flushQueue(); // dc.onopen: session.update first, then the rest in order
      this.becameReady();
    } else if (this.relayReady) {
      this.flushQueue();
      this.becameReady();
    }
  }

  /** WS relay `voice_status: ready` (§7.3). */
  private onRelayReady(): void {
    this.relayReady = true;
    this.patch({ banner: null });
    if (this.transportReady) {
      this.flushQueue();
      this.becameReady();
    } else if (this.snapshot.link === 'lost' && this.snapshot.linkSource === 'provider') {
      this.linkRestored();
    }
  }

  private becameReady(): void {
    this.everReady = true;
    const s = this.snapshot;
    if (s.status === 'connecting' || s.status === 'error' || s.link === 'lost') this.patch({ status: 'active', phase: null, error: null });
    if (s.link === 'lost') this.linkRestored();
  }

  private onTransportFailed(message: string): void {
    const s = this.snapshot;
    if (s.status === 'ending' || s.status === 'off' || s.status === 'error') {
      this.closeTransport();
      return;
    }
    if (this.everReady) {
      // an established call: P-2 (re-arm on the open socket, budget running)
      this.linkLost('transport', false);
      this.scheduleRetry();
      return;
    }
    this.startFailed(message);
  }

  private onOwnerActive(active: boolean): void {
    if (active) {
      if (this.pendingStart) {
        this.pendingStart = false; // the broadcast that answers our own voice_start
        return;
      }
      if (this.swallowOwnerActive) {
        this.swallowOwnerActive = false;
        return;
      }
      const st = this.snapshot.status;
      if (st === 'off') {
        this.patch({ remoteActive: true });
        return;
      }
      if (st === 'error' || this.snapshot.link === 'lost') return;
      this.ownershipLost(null); // V-13: another device took over
      return;
    }
    this.patch({ remoteActive: false, remoteProvider: null });
    // the end of a voice session; the owner normally already saw voice_ended
    if (this.isOwner() && !this.awaitingStarted && this.snapshot.link === 'ok' && this.snapshot.status !== 'error') this.finishEnd(false);
  }

  private onVoiceEnded(): void {
    if (!this.isOwner()) {
      this.patch({ remoteActive: false, remoteProvider: null });
      return;
    }
    const s = this.snapshot;
    // stale ends: from a previous session before our session_started, or while re-arming (P-2)
    if (this.awaitingStarted || s.link === 'lost' || s.status === 'error') return;
    this.finishEnd(false);
  }

  private onErrorFrame(code: string, detail: string | null): void {
    if (!this.isOwner()) return;
    if (SOFT_ERRORS.indexOf(code) >= 0) {
      this.patch({ banner: { kind: 'warning', message: detail ?? 'Voice hiccup; still listening' } });
      return;
    }
    if (code === 'voice_relay_failed') {
      this.fatal({ message: detail ?? 'The voice connection failed', hint: null, category: null, docUrl: null });
      return;
    }
    if (START_ERRORS.indexOf(code) >= 0 && this.awaitingStarted) {
      this.awaitingStarted = false;
      this.pendingStart = false;
      if (this.snapshot.link === 'lost') this.scheduleRetry();
      else this.startFailed(detail ?? code);
    }
  }

  private onAudioOut(b64: string): void {
    const t = this.transport;
    if (!t || t.kind !== 'websocket' || !this.isOwner()) return; // V-12: passive never plays
    t.playAudio(b64);
    this.providerSpoke(); // G-35: Qwen sends no ready after a reconnect
    const s = this.snapshot;
    if (s.status === 'active' || s.status === 'thinking') this.patch({ status: 'speaking' });
  }

  // ═════════════════════════ provider events (§4.7 / §7.4 / §7.7) ═════════════════════════

  private setLive(status: VoiceStatus): void {
    if (LIVE.indexOf(this.snapshot.status) >= 0 && this.snapshot.status !== status) this.patch({ status });
  }

  private onProviderEvent(ev: Readonly<Rec>, fromDataChannel: boolean): void {
    // V-12: a passive device never changes its own state from mirrored events
    if (!this.isOwner() || this.snapshot.status === 'error') return;
    const type = ev.type;
    if (typeof type !== 'string') {
      this.onGeminiEvent(ev);
      return;
    }
    switch (type) {
      case 'voice_status':
        return this.onVoiceStatus(ev);
      case 'voice_vad_state':
        this.patch({
          vad: { state: str(ev.state), durationMs: typeof ev.duration_ms === 'number' ? ev.duration_ms : 0, at: this.clock.now() },
        });
        return;
      case 'voice_error': {
        const e = isRec(ev.error) ? ev.error : {};
        const info: VoiceErrorInfo = {
          message: str(e.message) || 'Voice error',
          hint: str(e.recovery_hint) || null,
          category: str(e.category) || null,
          docUrl: str(e.provider_doc_url) || null,
        };
        if (e.recoverable === false) this.fatal(info);
        else this.patch({ banner: { kind: 'warning', message: info.hint ? `${info.message} · ${info.hint}` : info.message } });
        return;
      }
      case 'error': {
        // legacy relay error: `error` is an object here (G-28)
        const e = isRec(ev.error) ? ev.error : {};
        const code = str(e.code);
        const message =
          code === 'session_expired' ? 'Voice session expired. Start voice again.' : str(e.message) || `Voice error${code ? `: ${code}` : ''}`;
        this.fatal({ message, hint: null, category: code || null, docUrl: null });
        return;
      }
      case 'response.created':
        this.responseInFlight = true;
        this.cancelSent = false;
        this.setLive('speaking');
        return;
      case 'response.done':
        this.responseInFlight = false;
        this.cancelSent = false;
        this.setLive('active');
        return;
      case 'response.output_item.added':
        if (isRec(ev.item) && ev.item.type === 'function_call') this.setLive('tool_use');
        return;
      case 'response.function_call_arguments.done':
        this.setLive('thinking');
        return;
      case 'input_audio_buffer.speech_started':
        this.setLive('active');
        this.bargeIn(fromDataChannel);
        return;
      case 'input_audio_buffer.speech_stopped':
        this.setLive('thinking');
        return;
      case 'response.output_audio_transcript.delta':
      case 'response.audio_transcript.delta':
      case 'response.output_text.delta':
      case 'response.text.delta':
      case 'conversation.item.input_audio_transcription.completed':
        this.providerSpoke();
        return;
      default:
        return;
    }
  }

  /** V-9 (WS relay): flush local playback; cancel only while a response is in flight. V-11: WebRTC sends nothing. */
  private bargeIn(fromDataChannel: boolean): void {
    const t = this.transport;
    if (!t || t.kind !== 'websocket' || fromDataChannel) return;
    t.flushPlayback();
    if (this.responseInFlight && !this.cancelSent) {
      this.cancelSent = true;
      this.sendProvider({ type: 'response.cancel' });
    }
  }

  private onGeminiEvent(ev: Readonly<Rec>): void {
    const sc = ev.serverContent;
    if (isRec(sc)) {
      const out = sc.outputTranscription;
      if (isRec(out) && str(out.text)) {
        this.providerSpoke();
        this.setLive('speaking');
      }
      if (isRec(sc.inputTranscription) && str(sc.inputTranscription.text)) this.providerSpoke();
      if (sc.interrupted === true) {
        this.transport?.flushPlayback(); // V-10: no cancel frame
        this.responseInFlight = false;
        this.setLive('active');
      }
      if (sc.turnComplete === true) {
        this.responseInFlight = false;
        this.setLive('active');
      }
    }
    if (isRec(ev.toolCall)) this.setLive('tool_use');
  }

  /** G-35: Qwen sends no `ready` after a provider reconnect; the next transcript clears the banner. */
  private providerSpoke(): void {
    const s = this.snapshot;
    if (s.link !== 'lost' || s.linkSource !== 'provider') return;
    if (this.transport?.kind === 'websocket') this.relayReady = true;
    this.flushQueue();
    this.linkRestored();
  }

  private onVoiceStatus(ev: Readonly<Rec>): void {
    const status = str(ev.status);
    const s = this.snapshot;
    switch (status) {
      case 'summarizing':
        if (s.status === 'connecting') this.patch({ phase: 'summarizing' });
        return;
      case 'preparing':
        if (s.status === 'connecting') this.patch({ phase: 'preparing' });
        return;
      case 'ready':
        this.onRelayReady();
        return;
      case 'reconnect_warning':
        this.patch({ banner: { kind: 'reconnect_warning', timeLeftS: typeof ev.time_left === 'number' ? ev.time_left : null } });
        return;
      case 'reconnecting':
        // the server's provider relay reconnects; keep our transport (§7.7)
        if (LIVE.indexOf(s.status) >= 0) {
          this.relayReady = this.transport?.kind !== 'websocket';
          this.linkLost('provider', true);
        }
        return;
      default:
        return;
    }
  }

  // ═════════════════════════ V-4 queue ═════════════════════════

  private transportCanSend(): boolean {
    const t = this.transport;
    if (!t || !this.transportReady || this.snapshot.link === 'lost') return false;
    return t.kind === 'webrtc' ? true : this.relayReady;
  }

  private deliver(ev: Rec): boolean {
    const t = this.transport;
    if (!t) return false;
    if (t.kind === 'webrtc') return t.send(ev);
    return this.port.send({ type: 'voice_event', event: ev });
  }

  private sendProvider(ev: Rec): void {
    if (this.queue.length === 0 && this.transportCanSend() && this.deliver(ev)) return;
    this.queue.push(ev);
  }

  private flushQueue(): void {
    while (this.queue.length > 0) {
      const head = this.queue[0] as Rec;
      if (!this.deliver(head)) return; // keep it (and the rest) for the next ready
      this.queue.shift();
    }
  }

  // ═════════════════════════ P-2 link loss ═════════════════════════

  private linkLost(source: LinkSource, keepTransport: boolean): void {
    const s = this.snapshot;
    if (s.link !== 'lost') {
      this.clearTimer('restored');
      this.patch({ link: 'lost', linkLostAt: this.clock.now(), linkSource: source, linkRecoveredMs: null });
      this.deps.cues.startLoop(); // from the moment the link is lost
      if (source !== 'provider') this.armTimer('budget', LINK_RETRY_BUDGET_MS, () => this.onBudgetExhausted());
    } else if (source !== 'provider' && s.linkSource === 'provider') {
      // a client-side drop on top of a provider reconnect: the client budget applies now
      this.patch({ linkSource: source });
      this.armTimer('budget', LINK_RETRY_BUDGET_MS, () => this.onBudgetExhausted());
    }
    if (!keepTransport) {
      this.closeTransport();
      this.queue = []; // V-4: the queue lives until the transport is ready or torn down
      this.responseInFlight = false;
      this.cancelSent = false;
      this.armed = true; // V-8: re-arm with voice_start on the next open
    }
  }

  private linkRestored(): void {
    const s = this.snapshot;
    if (s.link !== 'lost') return;
    const lostAt = s.linkLostAt ?? this.clock.now();
    this.clearTimer('budget');
    this.clearTimer('retry');
    this.deps.cues.stopLoop();
    this.deps.cues.play('reconnected');
    this.patch({ link: 'restored', linkRecoveredMs: this.clock.now() - lostAt, linkSource: null });
    this.armTimer('restored', RESTORED_DISPLAY_MS, () => {
      if (this.snapshot.link === 'restored') this.patch({ link: 'ok', linkLostAt: null, linkRecoveredMs: null });
    });
  }

  private onBudgetExhausted(): void {
    if (this.snapshot.link !== 'lost') return;
    this.deps.cues.stopLoop();
    this.deps.cues.play('failed');
    this.closeTransport();
    this.clearTimer('retry');
    this.clearTimer('connInfo');
    this.armed = false;
    this.pendingStart = false;
    this.awaitingStarted = false;
    // our re-arm may have left voice live server-side; end it (an explicit end, not a lifecycle stop)
    this.port.send({ type: 'voice_stop' });
    this.port.voiceLocalEnd();
    this.patch({
      status: 'error',
      phase: null,
      link: 'failed',
      linkSource: null,
      error: { message: 'Couldn’t reconnect', hint: 'Check the connection, then tap Reconnect.', category: 'network', docUrl: null },
      banner: null,
      vad: null,
    });
  }

  private scheduleRetry(): void {
    if (this.snapshot.link !== 'lost') return;
    this.armed = true;
    this.armTimer('retry', TRANSPORT_RETRY_MS, () => {
      if (this.snapshot.link === 'lost' && !this.awaitingStarted) this.port.requestStart();
    });
  }

  private onNetwork(online: boolean): void {
    if (this.disposed || !this.everReady) return;
    const s = this.snapshot;
    if (!online) {
      if (LIVE.indexOf(s.status) >= 0 || s.status === 'connecting') this.linkLost('network', true);
      return;
    }
    if (s.link === 'lost' && s.linkSource === 'network' && this.transport?.healthy() && this.transportReady) this.linkRestored();
  }

  private onConnInfoTimeout(): void {
    if (!this.awaitingStarted) return;
    this.awaitingStarted = false;
    this.swallowOwnerActive = this.pendingStart;
    this.pendingStart = false;
    if (this.snapshot.link === 'lost') return; // the budget timer decides
    this.startFailed('Voice did not start (no answer from the server in 30 s)');
  }

  // ═════════════════════════ endings ═════════════════════════

  private enterEnding(): void {
    this.patch({ status: 'ending', phase: null });
    this.armTimer('ending', ENDING_TIMEOUT_MS, () => {
      if (this.snapshot.status === 'ending') this.finishEnd(true); // §7.6 timer → voice_local_end
    });
  }

  /** §7.7 fatal: voice_stop, wait ≤ 5 s for voice_ended, else voice_local_end; then `error`. */
  private fatal(err: VoiceErrorInfo): void {
    if (this.snapshot.status === 'off' || this.snapshot.status === 'error') return;
    this.pendingFatal = err;
    this.armed = false;
    this.deps.cues.stopLoop();
    this.clearTimer('budget');
    if (this.snapshot.status === 'ending') return;
    if (this.port.send({ type: 'voice_stop' })) this.enterEnding();
    else this.finishEnd(true);
  }

  private finishEnd(local: boolean): void {
    this.closeTransport();
    this.clearAllTimers();
    this.deps.cues.stopLoop();
    this.armed = false;
    if (this.pendingStart) this.swallowOwnerActive = true;
    this.pendingStart = false;
    this.awaitingStarted = false;
    this.queue = [];
    if (local) this.port.voiceLocalEnd();
    const err = this.pendingFatal;
    this.pendingFatal = null;
    const keep = { micMuted: false, speakerMuted: this.snapshot.speakerMuted };
    if (err) this.patch({ ...OFF_SNAPSHOT, ...keep, status: 'error', error: err });
    else this.patch({ ...OFF_SNAPSHOT, ...keep });
  }

  /** The start failed before the call was established: show it, leave nothing live. */
  private startFailed(message: string, hint: string | null = null): void {
    this.closeTransport();
    this.clearAllTimers();
    this.deps.cues.stopLoop();
    const hadVoice = this.armed || this.pendingStart;
    this.armed = false;
    if (this.pendingStart) this.swallowOwnerActive = true;
    this.pendingStart = false;
    this.awaitingStarted = false;
    this.queue = [];
    if (hadVoice) this.port.send({ type: 'voice_stop' }); // no-op server-side unless voice came up
    this.port.voiceLocalEnd();
    this.patch({
      status: 'error',
      phase: null,
      error: { message, hint, category: null, docUrl: null },
      link: 'ok',
      linkLostAt: null,
      linkSource: null,
      banner: null,
      vad: null,
      recording: false,
    });
  }

  /** V-13: another device took over. Tear down locally, never `voice_stop`. */
  private ownershipLost(provider: string | null): void {
    this.closeTransport();
    this.clearAllTimers();
    this.deps.cues.stopLoop();
    this.armed = false;
    this.pendingStart = false;
    this.awaitingStarted = false;
    this.queue = [];
    this.pendingFatal = null;
    this.patch({ ...OFF_SNAPSHOT, speakerMuted: this.snapshot.speakerMuted, remoteActive: true, remoteProvider: provider });
  }

  private resetCall(): void {
    this.closeTransport();
    this.clearAllTimers();
    this.deps.cues.stopLoop();
    this.queue = [];
    this.responseInFlight = false;
    this.cancelSent = false;
    this.pendingFatal = null;
    this.everReady = false;
    this.swallowOwnerActive = false;
    this.info = null;
  }

  private closeTransport(): void {
    const t = this.transport;
    this.transport = null;
    this.transportReady = false;
    this.relayReady = false;
    this.gen += 1;
    if (t) {
      try {
        t.close();
      } catch (err) {
        this.log('transport close failed', err);
      }
    }
  }

  // ═════════════════════════ timers ═════════════════════════

  private armTimer(name: keyof Timers, ms: number, fn: () => void): void {
    this.clearTimer(name);
    this.timers[name] = this.clock.setTimeout(() => {
      this.timers[name] = null;
      if (!this.disposed) fn();
    }, ms);
  }

  private clearTimer(name: keyof Timers): void {
    const h = this.timers[name];
    if (h !== null) this.clock.clearTimeout(h);
    this.timers[name] = null;
  }

  private clearAllTimers(): void {
    (Object.keys(this.timers) as (keyof Timers)[]).forEach((k) => this.clearTimer(k));
  }
}

/** True when the snapshot means "this device runs a call" (dock with live controls). */
export { isOwnerLive };
export type { ClientMessage };
