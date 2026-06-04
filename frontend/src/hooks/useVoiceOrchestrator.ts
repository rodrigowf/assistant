/**
 * useVoiceOrchestrator — drives a voice conversation against any
 * registered backend voice provider, dispatching on the
 * ``connection_type`` advertised in the ``session_started`` payload.
 *
 * Two transports are supported:
 *
 * - WebRTC (OpenAI today): browser holds a peer connection directly to
 *   the provider; data-channel events are mirrored to the orchestrator
 *   WS as ``voice_event`` payloads, and ``voice_command`` events from
 *   the orchestrator are forwarded back to the provider via the data
 *   channel. Audio bypasses the backend.
 *
 * - WebSocket (Qwen, Gemini Live, future locals): backend owns the
 *   upstream WS to the provider. The frontend captures mic at PCM16
 *   via an AudioWorklet and ships chunks over the orchestrator WS as
 *   ``voice_audio_in``; assistant audio arrives as ``voice_audio_out``
 *   chunks queued in :class:`PCMPlayer`. Provider events are
 *   delivered as ``voice_event`` server messages.
 *
 * The hook owns lifecycle + event normalisation; transports own the
 * per-protocol plumbing (see ``frontend/src/voice/transports/``).
 */

import { useState, useRef, useCallback, useEffect } from "react";
import { fetchEphemeralToken } from "../api/voice";
import { ChatSocket } from "../api/websocket";
import { VoiceRecorder } from "../voice/AudioRecorder";
import { connectWebRTCVoiceSession } from "../voice/transports/webrtc";
import { connectWebSocketVoiceSession } from "../voice/transports/websocket";
import type {
  AnyVoiceTransportHandles,
  WebRTCVoiceTransportHandles,
} from "../voice/transports/types";
import type {
  RealtimeEvent,
  ServerEvent,
  VoiceConnectionInfoPayload,
  VoiceStatus,
} from "../types";

// Voice debug logger — opt-in via URL flag (?debug=voice or ?debug=all).
// Off by default so the console isn't flooded.  When on, every voice
// state transition / sent / received event lands in the console with
// a `[voice]` prefix and a millisecond timestamp.
const VOICE_DEBUG: boolean = (() => {
  if (typeof window === "undefined") return false;
  try {
    const dbg = new URLSearchParams(window.location.search).get("debug") || "";
    return /\b(voice|all)\b/i.test(dbg);
  } catch {
    return false;
  }
})();
const VOICE_DEBUG_T0 = Date.now();
function vlog(...args: unknown[]): void {
  if (!VOICE_DEBUG) return;
  const t = ((Date.now() - VOICE_DEBUG_T0) / 1000).toFixed(2);
  // eslint-disable-next-line no-console
  console.log(`[voice t+${t}s]`, ...args);
}

interface UseVoiceOrchestratorOptions {
  localId?: string;
  resumeSdkId?: string | null;
  onUserTranscript?: (text: string) => void;
  onAssistantDelta?: (text: string) => void;
  onAssistantComplete?: (text: string) => void;
  onToolUse?: (callId: string, toolName: string, toolInput: Record<string, unknown>) => void;
  onTurnComplete?: () => void;
  onSessionStarted?: (sessionId: string) => void;
  onStatusChange?: (status: VoiceStatus) => void;
  onBeforeStart?: () => void;
  onAfterStop?: () => void;
}

export interface VoiceOrchestratorResult {
  voiceStatus: VoiceStatus;
  startVoice: () => Promise<void>;
  stopVoice: () => void;
  isActive: boolean;
  isMuted: boolean;
  toggleMute: () => void;
  isAssistantMuted: boolean;
  toggleAssistantMute: () => void;
  micLevel: number;
  speakerLevel: number;
  voiceError: string | null;
}

export function useVoiceOrchestrator(
  options: UseVoiceOrchestratorOptions = {},
): VoiceOrchestratorResult {
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>("off");
  const [voiceError, setVoiceError] = useState<string | null>(null);
  const transportRef = useRef<AnyVoiceTransportHandles | null>(null);
  const wsRef = useRef<ChatSocket | null>(null);

  // Queue WebRTC commands that arrive before the data channel is open.
  const pendingCommandsRef = useRef<RealtimeEvent[]>([]);
  const dcReadyRef = useRef(false);

  // Connection info from session_started — drives transport choice.
  const connInfoRef = useRef<VoiceConnectionInfoPayload | null>(null);
  // Whether recording is enabled (from session_started)
  const recordingEnabledRef = useRef(false);
  // Voice recorder for WebRTC sessions (audio bypasses backend)
  const recorderRef = useRef<VoiceRecorder | null>(null);

  // Tracks whether a provider response is currently in flight (between
  // response.created and response.done). For Qwen we must NOT send
  // response.cancel when nothing is active — DashScope rejects it with a
  // 400 ("InvalidParameter: The provided URL does not appear to be valid"
  // — misleading boilerplate for any malformed/unexpected request) and
  // closes the upstream WS, killing the voice session.
  const responseInFlightRef = useRef(false);
  // Gemini Live ``serverContent.inputTranscription`` arrives as
  // token-level deltas. Accumulate them and emit a single
  // ``onUserTranscript`` per turn (flushed on first output delta or
  // turnComplete). Without this, each fragment became its own user
  // bubble — one bubble per word.
  const pendingUserTranscriptRef = useRef("");

  // Mute state
  const [isMuted, setIsMuted] = useState(false);
  const [isAssistantMuted, setIsAssistantMuted] = useState(false);

  // Audio level analysis
  const [micLevel, setMicLevel] = useState(0);
  const [speakerLevel, setSpeakerLevel] = useState(0);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Safety timeout for the "Ending..." → "Off" transition. The backend
  // emits voice_ended once teardown completes; this fires only if the
  // ack never arrives (server crash, WS drop, etc.) so the UI doesn't
  // stay stuck on "Ending" forever.
  const endingTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const ENDING_ACK_TIMEOUT_MS = 5000;

  const optsRef = useRef(options);
  optsRef.current = options;

  const updateStatus = useCallback((status: VoiceStatus) => {
    vlog("status →", status);
    setVoiceStatus(status);
    optsRef.current.onStatusChange?.(status);
  }, []);

  // Send a raw provider event. WebRTC: data channel; WS: orchestrator WS mirror.
  //
  // Critically, ``session_started`` (which carries the backend-built
  // ``voice_session_update``) arrives BEFORE ``connectWebRTCVoiceSession``
  // returns and assigns ``transportRef.current``.  So when the orchestrator
  // hands us the session.update payload, the transport is still null —
  // dropping the event here would leave the OpenAI session running with
  // its defaults (no system prompt, no tools, no input transcription),
  // which is exactly the "voice mode is isolated from my architecture"
  // bug.  Queue when the transport isn't ready yet (or the data channel
  // isn't open yet) and let the WebRTC ``onConnected`` flush the queue.
  const sendProviderEvent = useCallback((event: RealtimeEvent) => {
    const t = transportRef.current;
    vlog("send", event.type, "transport=", t?.kind);
    if (t?.kind === "webrtc") {
      if (dcReadyRef.current) {
        t.sendProviderEvent(event);
      } else {
        pendingCommandsRef.current.push(event);
      }
    } else if (t?.kind === "websocket") {
      wsRef.current?.send({ type: "voice_event", event });
    } else {
      // Transport not yet created.  Queue; ``onConnected`` (WebRTC) or the
      // WS-transport startup path will drain it.
      pendingCommandsRef.current.push(event);
    }
  }, []);

  const stopAudioAnalysis = useCallback(() => {
    if (analyserIntervalRef.current) {
      clearInterval(analyserIntervalRef.current);
      analyserIntervalRef.current = null;
    }
    audioCtxRef.current?.close().catch(() => {});
    audioCtxRef.current = null;
    setMicLevel(0);
    setSpeakerLevel(0);
  }, []);

  const cleanup = useCallback(() => {
    vlog("cleanup (transport=", transportRef.current?.kind, ")");
    // Stop audio recorder if active
    if (recorderRef.current) {
      recorderRef.current.stop();
      recorderRef.current = null;
    }
    stopAudioAnalysis();
    transportRef.current?.disconnect();
    transportRef.current = null;
    wsRef.current?.close();
    wsRef.current = null;
    dcReadyRef.current = false;
    pendingCommandsRef.current = [];
    connInfoRef.current = null;
    recordingEnabledRef.current = false;
    setIsMuted(false);
    setIsAssistantMuted(false);
  }, [stopAudioAnalysis]);

  // Normalised provider-event handler — same logic for both transports.
  const handleProviderEvent = useCallback((event: RealtimeEvent) => {
    const eventType = event.type;

    // Backend-synthesised status for the upstream handshake (sent before
    // the WS provider's own greeting). "preparing" keeps the connecting
    // spinner up; "ready" flips to active so the user knows they can
    // start talking. Other voice_status payloads are reserved for future
    // states and ignored here.
    if (eventType === "voice_status") {
      const status = (event as { status?: string }).status;
      if (status === "preparing") {
        updateStatus("connecting");
      } else if (status === "ready") {
        updateStatus("active");
      }
      return;
    }

    // Gemini Live event shape: no top-level ``type`` field. Top-level
    // keys are camelCase (``setupComplete``, ``serverContent``,
    // ``toolCall``). Map to the same callbacks the OpenAI/Qwen branches
    // below drive so the UI renders transcripts + tool cards.
    if (!eventType) {
      const ev = event as unknown as Record<string, unknown>;
      const sc = ev.serverContent as Record<string, unknown> | undefined;
      if (sc) {
        const inputT = sc.inputTranscription as Record<string, unknown> | undefined;
        if (inputT) {
          // Accumulate — Gemini Live sends one event per word/fragment.
          // Flushed on the first output delta of this turn (model
          // started replying) or on turnComplete.
          const text = (inputT.text as string) || "";
          if (text) pendingUserTranscriptRef.current += text;
        }
        const flushPendingUser = () => {
          const staged = pendingUserTranscriptRef.current;
          if (staged) {
            pendingUserTranscriptRef.current = "";
            optsRef.current.onUserTranscript?.(staged);
          }
        };
        const outputT = sc.outputTranscription as Record<string, unknown> | undefined;
        if (outputT) {
          flushPendingUser();
          const text = (outputT.text as string) || "";
          if (text) optsRef.current.onAssistantDelta?.(text);
        }
        // Streaming text via modelTurn.parts[].text (half-cascade Live preview)
        const modelTurn = sc.modelTurn as Record<string, unknown> | undefined;
        if (modelTurn) {
          flushPendingUser();
          const parts = (modelTurn.parts as Array<Record<string, unknown>>) || [];
          for (const p of parts) {
            const t = p.text as string | undefined;
            if (t) optsRef.current.onAssistantDelta?.(t);
          }
        }
        if (sc.interrupted) {
          // Barge-in: Gemini server detected the user spoke over the
          // model. Drop the locally-buffered audio (chunks already
          // received and queued in PCMPlayer) so the assistant stops
          // talking immediately — without this the buffered tail keeps
          // playing to the end. Match the Qwen flushAudioOut() path.
          const t = transportRef.current;
          if (t?.kind === "websocket") t.flushAudioOut();
          responseInFlightRef.current = false;
          updateStatus("active");
        }
        if (sc.turnComplete) {
          // Failsafe: covers audio-only turns where neither
          // outputTranscription nor modelTurn fired.
          flushPendingUser();
          responseInFlightRef.current = false;
          updateStatus("active");
          optsRef.current.onAssistantComplete?.("");
          optsRef.current.onTurnComplete?.();
        }
      }
      const toolCall = ev.toolCall as Record<string, unknown> | undefined;
      if (toolCall) {
        const calls = (toolCall.functionCalls as Array<Record<string, unknown>>) || [];
        updateStatus("tool_use");
        for (const c of calls) {
          const callId = (c.id as string) || "";
          const name = (c.name as string) || "";
          const args = (c.args as Record<string, unknown>) || {};
          if (callId && name) optsRef.current.onToolUse?.(callId, name, args);
        }
      }
      return;
    }

    if (eventType === "error") {
      const err = event.error as Record<string, unknown> | undefined;
      const code = err?.code as string | undefined;
      const message = err?.message as string | undefined;
      vlog("ERR upstream", { code, message });
      if (code === "session_expired") {
        setVoiceError(message || "Voice session expired — please restart");
      } else {
        setVoiceError(message || `Voice error: ${code || "unknown"}`);
      }
      cleanup();
      updateStatus("error");
      optsRef.current.onAfterStop?.();
      return;
    }

    if (eventType === "response.created") {
      responseInFlightRef.current = true;
      updateStatus("speaking");
    } else if (eventType === "response.done") {
      responseInFlightRef.current = false;
      updateStatus("active");
      optsRef.current.onTurnComplete?.();
    } else if (eventType === "response.output_item.added") {
      const item = event.item as Record<string, unknown> | undefined;
      if (item?.type === "function_call") updateStatus("tool_use");
    } else if (eventType === "response.function_call_arguments.done") {
      updateStatus("thinking");
      const callId = (event.call_id as string) || "";
      const name = (event.name as string) || "";
      try {
        const args = JSON.parse((event.arguments as string) || "{}");
        optsRef.current.onToolUse?.(callId, name, args);
      } catch {
        optsRef.current.onToolUse?.(callId, name, {});
      }
    } else if (eventType === "input_audio_buffer.speech_started") {
      updateStatus("active");
      // Barge-in: stop the player immediately so audio that's already
      // queued (or currently playing) shuts up. Then ask the provider to
      // cancel any in-flight response — Qwen's auto-interrupt does not
      // reliably stop responses that have started streaming, so we send
      // response.cancel ourselves. WebRTC providers (OpenAI) handle this
      // server-side via their own VAD wiring.
      //
      // Critical: only send response.cancel if a response is actually
      // in flight. DashScope rejects a stray cancel with a misleading
      // 400 ("InvalidParameter: The provided URL does not appear to be
      // valid") and closes the upstream WS, killing the session. False
      // speech_started events fire from background noise / pauses
      // between turns, so this guard is required.
      const t = transportRef.current;
      if (t?.kind === "websocket") {
        t.flushAudioOut();
        if (responseInFlightRef.current) {
          sendProviderEvent({ type: "response.cancel" });
        }
      }
    } else if (eventType === "input_audio_buffer.speech_stopped") {
      updateStatus("thinking");
    }

    if (eventType === "conversation.item.input_audio_transcription.completed") {
      const transcript = (event.transcript as string) || "";
      if (transcript) optsRef.current.onUserTranscript?.(transcript);
    }

    if (eventType === "conversation.item.created") {
      const item = event.item as Record<string, unknown> | undefined;
      if (item?.role === "user") {
        const content = (item.content as Array<Record<string, unknown>>) || [];
        for (const c of content) {
          if (c.type === "input_text" && c.text) {
            optsRef.current.onUserTranscript?.(c.text as string);
          }
        }
      }
    }

    // GA gpt-realtime uses ``response.output_audio_transcript.*``;
    // legacy beta models and Qwen still use ``response.audio_transcript.*``.
    if (
      eventType === "response.output_audio_transcript.delta"
      || eventType === "response.audio_transcript.delta"
      || eventType === "response.text.delta"
    ) {
      optsRef.current.onAssistantDelta?.((event.delta as string) || "");
    } else if (
      eventType === "response.output_audio_transcript.done"
      || eventType === "response.audio_transcript.done"
    ) {
      optsRef.current.onAssistantComplete?.((event.transcript as string) || "");
    } else if (eventType === "response.text.done") {
      optsRef.current.onAssistantComplete?.((event.text as string) || "");
    }
  }, [updateStatus, cleanup]);

  // For WebRTC, every event also gets mirrored to the backend (so JSONL persistence works).
  const handleWebRTCEvent = useCallback((event: RealtimeEvent) => {
    wsRef.current?.send({ type: "voice_event", event });
    handleProviderEvent(event);
  }, [handleProviderEvent]);

  const handleConnectionClosed = useCallback(() => {
    if (transportRef.current) {
      setVoiceError((prev) => prev ?? "Voice connection lost");
      cleanup();
      updateStatus("error");
      optsRef.current.onAfterStop?.();
    }
  }, [cleanup, updateStatus]);

  // WebRTC-only: mic + remote-stream RMS analyser.
  const startWebRTCAudioAnalysis = useCallback((t: WebRTCVoiceTransportHandles) => {
    const ctx = new AudioContext();
    audioCtxRef.current = ctx;

    const micSource = ctx.createMediaStreamSource(t.micStream);
    const micAnalyser = ctx.createAnalyser();
    micAnalyser.fftSize = 256;
    micSource.connect(micAnalyser);

    let speakerAnalyser: AnalyserNode | null = null;
    const micData = new Uint8Array(micAnalyser.frequencyBinCount);
    let speakerData: Uint8Array | null = null;

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function rms(data: any): number {
      let sum = 0;
      for (let i = 0; i < data.length; i++) {
        const v = (data[i] - 128) / 128;
        sum += v * v;
      }
      return Math.sqrt(sum / data.length);
    }

    analyserIntervalRef.current = setInterval(() => {
      micAnalyser.getByteTimeDomainData(micData);
      setMicLevel(rms(micData));
      if (!speakerAnalyser && t.remoteStream) {
        try {
          const speakerSource = ctx.createMediaStreamSource(t.remoteStream);
          speakerAnalyser = ctx.createAnalyser();
          speakerAnalyser.fftSize = 256;
          speakerSource.connect(speakerAnalyser);
          speakerData = new Uint8Array(speakerAnalyser.frequencyBinCount);
        } catch { /* not ready yet */ }
      }
      if (speakerAnalyser && speakerData) {
        speakerAnalyser.getByteTimeDomainData(speakerData as Uint8Array<ArrayBuffer>);
        setSpeakerLevel(rms(speakerData));
      }
    }, 66);
  }, []);

  // WS-only: mic-only analyser; speaker level comes from PCMPlayer via setSpeakerLevel.
  const startWSMicAnalysis = useCallback((micStream: MediaStream) => {
    const ctx = new AudioContext();
    audioCtxRef.current = ctx;
    const micSource = ctx.createMediaStreamSource(micStream);
    const micAnalyser = ctx.createAnalyser();
    micAnalyser.fftSize = 256;
    micSource.connect(micAnalyser);
    const micData = new Uint8Array(micAnalyser.frequencyBinCount);
    analyserIntervalRef.current = setInterval(() => {
      micAnalyser.getByteTimeDomainData(micData);
      let sum = 0;
      for (let i = 0; i < micData.length; i++) {
        const v = (micData[i] - 128) / 128;
        sum += v * v;
      }
      setMicLevel(Math.sqrt(sum / micData.length));
    }, 66);
  }, []);

  // Orchestrator-WS server events.
  const handleServerEvent = useCallback((event: ServerEvent) => {
    // Log all non-frequent server events.  voice_audio_out (constant)
    // and voice_event with high-rate transcript deltas would flood.
    if (
      event.type !== "voice_audio_out" &&
      !(event.type === "voice_event" && (
        (event.event as RealtimeEvent | undefined)?.type === "response.output_audio_transcript.delta"
        || (event.event as RealtimeEvent | undefined)?.type === "response.audio_transcript.delta"
        || (event.event as RealtimeEvent | undefined)?.type === "response.text.delta"
      ))
    ) {
      vlog("recv", event.type, event.type === "voice_event" ? (event.event as RealtimeEvent).type : "");
    }
    switch (event.type) {
      // Heartbeat from backend (every 15s) — keeps the connection
      // warm against power-saving WiFi clients. No-op here; receiving
      // the bytes is the entire purpose.
      case "ping":
      case "pong":
        return;

      case "session_started": {
        optsRef.current.onSessionStarted?.(event.session_id);
        const info = event.voice_connection_info;
        if (info) connInfoRef.current = info;
        // Capture recording flag for WebRTC (audio bypasses backend)
        recordingEnabledRef.current = event.voice_recording_enabled ?? false;
        // For WebRTC, the frontend forwards session.update via the data channel.
        // For WebSocket, the backend already sent it upstream — no-op for us.
        if (event.voice_session_update && info?.connection_type === "webrtc") {
          sendProviderEvent(event.voice_session_update as RealtimeEvent);
        }
        if (event.voice_connection_error) {
          setVoiceError(event.voice_connection_error);
          updateStatus("error");
          optsRef.current.onAfterStop?.();
        }
        break;
      }

      case "voice_command":
        // Backend → provider command. WebRTC: forward via data channel.
        // WebSocket: the backend sends these directly; we ignore.
        if (connInfoRef.current?.connection_type === "webrtc") {
          sendProviderEvent(event.command as RealtimeEvent);
        }
        break;

      case "voice_event":
        // Provider event mirrored from backend (WebSocket providers only).
        handleProviderEvent(event.event);
        break;

      case "voice_audio_out": {
        const t = transportRef.current;
        if (t?.kind === "websocket") t.pushAudioOut(event.audio);
        break;
      }

      case "voice_ending":
        // Backend has begun teardown. Reflect it in the UI so the user
        // sees "Ending..." instead of a stuck "Active" until voice_ended
        // arrives. Idempotent if we already set Ending in stopVoice().
        if (endingTimeoutRef.current === null) {
          endingTimeoutRef.current = setTimeout(() => {
            console.warn("[voice-orchestrator] voice_ended ack timeout");
            cleanup();
            updateStatus("off");
            optsRef.current.onAfterStop?.();
            endingTimeoutRef.current = null;
          }, ENDING_ACK_TIMEOUT_MS);
        }
        updateStatus("ending");
        break;

      case "voice_ended":
      case "voice_stopped":  // legacy alias — remove after one release
        if (endingTimeoutRef.current !== null) {
          clearTimeout(endingTimeoutRef.current);
          endingTimeoutRef.current = null;
        }
        cleanup();
        updateStatus("off");
        optsRef.current.onAfterStop?.();
        break;

      case "error": {
        const detail = (event as { detail?: string }).detail;
        console.error("[voice-orchestrator] Server error:", event.error, detail);
        setVoiceError(detail || `Server error: ${event.error}`);
        cleanup();
        updateStatus("error");
        optsRef.current.onAfterStop?.();
        break;
      }

      case "status":
        if ((event as { status?: string }).status === "disconnected" && transportRef.current) {
          setVoiceError((prev) => prev ?? "Server connection lost");
          cleanup();
          updateStatus("error");
          optsRef.current.onAfterStop?.();
        }
        break;
    }
  }, [sendProviderEvent, handleProviderEvent, updateStatus, cleanup]);

  const toggleMute = useCallback(() => {
    const t = transportRef.current;
    if (!t) return;
    const newMuted = !isMuted;
    t.setMicEnabled(!newMuted);
    setIsMuted(newMuted);
  }, [isMuted]);

  const toggleAssistantMute = useCallback(() => {
    const t = transportRef.current;
    if (!t) return;
    const newMuted = !isAssistantMuted;
    t.setOutputMuted(newMuted);
    setIsAssistantMuted(newMuted);
  }, [isAssistantMuted]);

  const startVoice = useCallback(async () => {
    if (voiceStatus !== "off" && voiceStatus !== "error") return;

    setVoiceError(null);
    updateStatus("connecting");
    dcReadyRef.current = false;
    pendingCommandsRef.current = [];

    optsRef.current.onBeforeStart?.();

    // Open the orchestrator WS (used by both transports).
    const socket = new ChatSocket(handleServerEvent, "/api/orchestrator/chat");
    wsRef.current = socket;

    socket.connect(() => {
      const lid = optsRef.current.localId;
      const resumeId = optsRef.current.resumeSdkId;
      const payload: Record<string, unknown> = { type: "voice_start", local_id: lid };
      if (resumeId) payload.resume_sdk_id = resumeId;
      socket.send(payload);
    });

    // Wait up to 30s for session_started (which populates connInfoRef).
    // The backend may need to load the JSONL and run the history
    // summarizer (one LLM round-trip) before answering — on the Jetson
    // that can take 10–20s for large sessions.
    const startedAt = Date.now();
    while (!connInfoRef.current) {
      if (Date.now() - startedAt > 30_000) {
        setVoiceError("Voice session did not start (no connection_info from server)");
        cleanup();
        updateStatus("error");
        return;
      }
      await new Promise((r) => setTimeout(r, 50));
    }
    const info = connInfoRef.current;

    if (info.connection_type === "webrtc") {
      // OpenAI: get an ephemeral token + the canonical /v1/realtime/calls URL.
      let ephemeralKey: string;
      let callUrl: string;
      try {
        const tokenData = await fetchEphemeralToken();
        ephemeralKey = tokenData.client_secret.value;
        callUrl = tokenData.connection_info?.endpoint ?? info.endpoint;
      } catch (err) {
        setVoiceError(err instanceof Error ? err.message : String(err));
        cleanup();
        updateStatus("error");
        return;
      }

      try {
        const t = await connectWebRTCVoiceSession({
          ephemeralKey,
          callUrl,
          inputSampleRate: info.audio_in_format.sample_rate,
          onProviderEvent: handleWebRTCEvent,
          onConnected: () => {
            dcReadyRef.current = true;
            updateStatus("active");
            const pending = pendingCommandsRef.current.splice(0);
            for (const cmd of pending) t.sendProviderEvent(cmd);
          },
          onClose: handleConnectionClosed,
          onError: (msg) => {
            setVoiceError(msg);
            updateStatus("error");
          },
        });
        transportRef.current = t;
        startWebRTCAudioAnalysis(t);

        // Start audio recorder for WebRTC if recording is enabled
        // (WebRTC audio bypasses backend, so we record on frontend)
        if (recordingEnabledRef.current && wsRef.current) {
          const sessionId = optsRef.current.localId || "unknown";
          const recorder = new VoiceRecorder({
            sessionId,
            ws: wsRef.current,
            micStream: t.micStream,
            remoteStreamGetter: () => t.remoteStream,
            sampleRate: info.audio_in_format.sample_rate,
          });
          recorderRef.current = recorder;
          recorder.start().catch((err) => {
            console.error("[voice-orchestrator] Failed to start recorder:", err);
          });
        }
      } catch (err) {
        setVoiceError(err instanceof Error ? err.message : String(err));
        cleanup();
        updateStatus("error");
      }
    } else {
      // WebSocket providers: backend relays audio. We just capture + play.
      try {
        const t = await connectWebSocketVoiceSession({
          inputSampleRate: info.audio_in_format.sample_rate,
          outputSampleRate: info.audio_out_format.sample_rate,
          onAudioChunk: (b64) => {
            wsRef.current?.send({ type: "voice_audio_in", audio: b64 });
          },
          onSpeakerLevel: setSpeakerLevel,
        });
        transportRef.current = t;
        startWSMicAnalysis(t.micStream);
        // Drain any provider events that were queued while the transport
        // was being set up (e.g. session.update arriving on session_started
        // before connectWebSocketVoiceSession resolved).
        const pending = pendingCommandsRef.current.splice(0);
        for (const cmd of pending) {
          wsRef.current?.send({ type: "voice_event", event: cmd });
        }
        updateStatus("active");
      } catch (err) {
        setVoiceError(err instanceof Error ? err.message : String(err));
        cleanup();
        updateStatus("error");
      }
    }
  }, [
    voiceStatus,
    handleServerEvent,
    handleWebRTCEvent,
    handleConnectionClosed,
    startWebRTCAudioAnalysis,
    startWSMicAnalysis,
    cleanup,
    updateStatus,
  ]);

  const stopVoice = useCallback(() => {
    // Tell the backend to end ONLY the voice connection (keeping the
    // orchestrator session alive in the pool for re-arm), then wait
    // for the voice_ended ack before flipping to Off. Show "Ending..."
    // in the meantime so the user gets feedback that the request is in
    // flight. A safety timeout flips to Off after 5s in case the ack
    // never arrives (server crash, dropped WS). The previous design
    // sent {type:"stop"} which dropped the entire session — wrong: the
    // tab should survive, voice is one mode of interacting with it.
    if (wsRef.current) {
      wsRef.current.send({ type: "voice_stop" });
    } else {
      // No socket → nothing to await. Tear down locally.
      cleanup();
      updateStatus("off");
      optsRef.current.onAfterStop?.();
      return;
    }
    updateStatus("ending");
    if (endingTimeoutRef.current !== null) {
      clearTimeout(endingTimeoutRef.current);
    }
    endingTimeoutRef.current = setTimeout(() => {
      console.warn(
        "[voice-orchestrator] voice_ended ack timeout — forcing local Off",
      );
      cleanup();
      updateStatus("off");
      optsRef.current.onAfterStop?.();
      endingTimeoutRef.current = null;
    }, ENDING_ACK_TIMEOUT_MS);
  }, [cleanup, updateStatus]);

  useEffect(() => {
    return () => {
      if (endingTimeoutRef.current !== null) {
        clearTimeout(endingTimeoutRef.current);
        endingTimeoutRef.current = null;
      }
      cleanup();
    };
  }, [cleanup]);

  return {
    voiceStatus,
    startVoice,
    stopVoice,
    isActive: voiceStatus !== "off" && voiceStatus !== "error",
    isMuted,
    toggleMute,
    isAssistantMuted,
    toggleAssistantMute,
    micLevel,
    speakerLevel,
    voiceError,
  };
}
