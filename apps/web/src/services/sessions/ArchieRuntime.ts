/**
 * The orchestrator ("Archie") conversation (spec 13 §3.4): attaches to the single orchestrator
 * socket (T-6), model info, `send_audio`, `inject_text` for shares, agent approvals, and the
 * bridge the voice engine (W-12) plugs into.
 *
 * Voice belongs to W-12. This runtime only exposes `VoiceBridge`: the voice controller provides
 * the `voice_start` that replaces `start` while it owns voice (T-11, V-8), receives the voice
 * frames (audio frames bypass the conversation queue, L-3), sends voice frames on the shared
 * socket, and feeds data-channel events / local voice ends into the reducer.
 */
import {
  initialConversation,
  type ClientMessage,
  type ModelInfo,
  type OrchestratorModelInfo,
  type ServerFrame,
  type VoiceStartMessage,
} from '@/protocol';
import { createSessionStore } from '@/stores';
import { ConversationRuntime, EMPTY_PAGE, type RuntimeHooks } from './ConversationRuntime';
import type { ChannelClient, OrchestratorChannel } from './orchestratorChannel';

/** Implemented by the voice engine (W-12). */
export interface VoiceHooks {
  /** The `voice_start` to send instead of `start` (owner re-arm, T-11/V-8), or null. */
  startMessage(): VoiceStartMessage | null;
  /** Every frame on the orchestrator socket that voice cares about, after the reducer saw it. */
  onFrame(frame: ServerFrame): void;
  /** The socket dropped (the server ends voice for its owner at once, G-31). */
  onSocketClosed?(): void;
}

/** What W-12 calls on the runtime. */
export interface VoiceBridge {
  setVoiceHooks(hooks: VoiceHooks | null): void;
  /** Raw send on the shared orchestrator socket (`voice_start`, `voice_stop`, `voice_event`, `voice_audio_in`, recording). */
  sendVoice(msg: ClientMessage): boolean;
  /** OpenAI data-channel inbound event, owner only (§4.7). */
  feedDataChannelEvent(event: Readonly<Record<string, unknown>>): void;
  /** The controller ended voice locally (timeout, fatal error, §7.6). */
  voiceLocalEnd(): void;
}

const VOICE_FRAMES: readonly string[] = [
  'session_started',
  'voice_event',
  'voice_command',
  'voice_audio_out',
  'voice_owner_active',
  'voice_connection_error',
  'voice_ending',
  'voice_ended',
  'voice_stopped',
  'error',
];

export interface ArchieRuntimeOptions {
  localId: string;
  /** The JSONL id when resuming (G-14); `null` for a brand-new orchestrator. */
  sdkId?: string | null;
  /** Opened from the server's pool, not by a user action: every `start` reattaches (OPEN-2). */
  reattach?: boolean;
  voiceActive?: boolean;
  hidden?: boolean;
  readOnly?: boolean;
}

export class ArchieRuntime extends ConversationRuntime implements ChannelClient, VoiceBridge {
  private voice: VoiceHooks | null = null;
  private coldOpenPending = false;
  private audioTurnPending = false;
  readonly readOnly: boolean;

  constructor(
    opts: ArchieRuntimeOptions,
    hooks: RuntimeHooks,
    private readonly channel: OrchestratorChannel,
  ) {
    const conv = initialConversation({
      localId: opts.localId,
      kind: 'orchestrator',
      sdkId: opts.sdkId ?? null,
      reattach: opts.reattach,
      voiceActive: opts.voiceActive === true,
    });
    super(conv, createSessionStore({ localId: opts.localId, conv, readOnly: opts.readOnly, hidden: opts.hidden }), hooks);
    this.readOnly = opts.readOnly === true;
  }

  /** §6.11 attach / new, with the §5.2 cold open (orchestrator history is lossy, §5.7). */
  open(): void {
    if (this.readOnly) {
      if (this.conv.ref.sdkId) void this.reload();
      return;
    }
    if (this.conv.ref.sdkId) {
      this.step({ type: 'begin_reload' });
      this.coldOpenPending = true;
    } else {
      this.step({ type: 'history_page', mode: 'replace', response: EMPTY_PAGE });
    }
    this.channel.attach(this);
  }

  // ───────────────────────── channel client ─────────────────────────

  onSocketOpen(): void {
    const start = this.voice?.startMessage() ?? undefined;
    this.step(start ? { type: 'socket_open', start } : { type: 'socket_open' });
  }

  onSocketFrame(f: ServerFrame): void {
    if (f.type === 'voice_audio_out') {
      this.voice?.onFrame(f); // L-3: never through the conversation queue
      return;
    }
    this.step({ type: 'frame', frame: f });
    this.trackModel(f);
    if (this.coldOpenPending && !this.disposed && (f.type === 'session_started' || f.type === 'error')) {
      this.coldOpenPending = false;
      void this.runReload();
    }
    if (this.voice && VOICE_FRAMES.indexOf(f.type) >= 0) this.voice.onFrame(f);
  }

  onSocketClosed(): void {
    this.step({ type: 'socket_closed' });
    if (this.coldOpenPending) {
      this.coldOpenPending = false;
      void this.runReload();
    }
    this.voice?.onSocketClosed?.();
  }

  onResync(): void {
    this.resendStart();
  }

  resendStart(): void {
    const start = this.voice?.startMessage() ?? undefined;
    this.step(start ? { type: 'resend_start', start } : { type: 'resend_start' });
  }

  protected transportSend(msg: ClientMessage): boolean {
    return this.channel.send(msg);
  }

  protected onSubscribed(): void {
    this.channel.markHealthy(); // T-13
  }

  protected closeTransport(): void {
    this.channel.detach(this); // the socket stays open as the pool watcher
  }

  /** Banner "Retry": restart the handshake on the open socket, or reconnect now. */
  retry(): void {
    if (this.channel.isOpen) this.retryHandshake(this.voice?.startMessage() ?? undefined);
    else this.channel.retry();
  }

  private trackModel(f: ServerFrame): void {
    let info: OrchestratorModelInfo | null | undefined;
    if (f.type === 'session_started' || f.type === 'model_info' || f.type === 'model_changed') info = f.model_info;
    if (info) this.handle.patch({ modelInfo: info });
    if (f.type === 'models_list' && Array.isArray(f.models)) this.handle.patch({ models: f.models as ModelInfo[] });
  }

  get modelInfo(): OrchestratorModelInfo | null {
    return this.handle.store.getState().modelInfo;
  }

  // ───────────────────────── user actions (§6) ─────────────────────────

  /** §6.2 */
  send(text: string): void {
    if (this.readOnly || !text.trim()) return;
    this.step({ type: 'local_send', text });
    this.sendWhenSubscribed({ type: 'send', text });
  }

  /** §6.16 talk-mode audio message, on the orchestrator WS only (A-8.6). */
  sendAudio(audio: string, format: string, text?: string): void {
    if (this.readOnly) return;
    this.step({ type: 'local_send_audio', text: text ?? '' });
    this.audioTurnPending = true;
    this.sendWhenSubscribed(text ? { type: 'send_audio', audio, format, text } : { type: 'send_audio', audio, format });
  }

  /** §6.15 / §7.9: shared text or an uploaded-file line. Kept until `session_started` if not subscribed. */
  inject(text: string): void {
    if (this.readOnly || !text) return;
    this.step({ type: 'local_inject', text });
    this.sendWhenSubscribed({ type: 'inject_text', text });
  }

  /** §6.3. In voice mode the Stop control is `voice_stop` (W-12), not this. */
  interrupt(): void {
    if (!this.readOnly) this.transportSend({ type: 'interrupt' });
  }

  compact(): void {
    if (this.readOnly) return;
    this.step({ type: 'local_compact' });
    this.sendWhenSubscribed({ type: 'compact' });
  }

  setModel(model: string): void {
    this.sendWhenSubscribed({ type: 'set_model', model });
  }

  requestModel(): void {
    this.sendWhenSubscribed({ type: 'get_model' });
  }

  requestModels(): void {
    this.sendWhenSubscribed({ type: 'get_models' });
  }

  /** §6.16: the server may switch to an audio model without `model_changed`; re-read it after the turn. */
  afterTurnEnded(): void {
    if (this.audioTurnPending) {
      this.audioTurnPending = false;
      this.requestModel();
    }
  }

  /** PM-5 cleanup when an agent view saw its turn end (§6.9). */
  clearAgentApprovals(localId: string): void {
    this.step({ type: 'clear_agent_approvals', localId });
  }

  // ───────────────────────── voice bridge (W-12) ─────────────────────────

  setVoiceHooks(hooks: VoiceHooks | null): void {
    this.voice = hooks;
  }

  sendVoice(msg: ClientMessage): boolean {
    if (this.readOnly) return false;
    return this.transportSend(msg);
  }

  feedDataChannelEvent(event: Readonly<Record<string, unknown>>): void {
    this.step({ type: 'datachannel_event', event });
  }

  voiceLocalEnd(): void {
    this.step({ type: 'voice_local_end' });
  }

  override dispose(): void {
    this.voice = null;
    super.dispose();
  }
}
