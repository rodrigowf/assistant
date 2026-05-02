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

  // Tracks whether a provider response is currently in flight (between
  // response.created and response.done). For Qwen we must NOT send
  // response.cancel when nothing is active — DashScope rejects it with a
  // 400 ("InvalidParameter: The provided URL does not appear to be valid"
  // — misleading boilerplate for any malformed/unexpected request) and
  // closes the upstream WS, killing the voice session.
  const responseInFlightRef = useRef(false);

  // Mute state
  const [isMuted, setIsMuted] = useState(false);
  const [isAssistantMuted, setIsAssistantMuted] = useState(false);

  // Audio level analysis
  const [micLevel, setMicLevel] = useState(0);
  const [speakerLevel, setSpeakerLevel] = useState(0);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const optsRef = useRef(options);
  optsRef.current = options;

  const updateStatus = useCallback((status: VoiceStatus) => {
    vlog("status →", status);
    setVoiceStatus(status);
    optsRef.current.onStatusChange?.(status);
  }, []);

  // Send a raw provider event. WebRTC: data channel; WS: orchestrator WS mirror.
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
    stopAudioAnalysis();
    transportRef.current?.disconnect();
    transportRef.current = null;
    wsRef.current?.close();
    wsRef.current = null;
    dcReadyRef.current = false;
    pendingCommandsRef.current = [];
    connInfoRef.current = null;
    setIsMuted(false);
    setIsAssistantMuted(false);
  }, [stopAudioAnalysis]);

  // Normalised provider-event handler — same logic for both transports.
  const handleProviderEvent = useCallback((event: RealtimeEvent) => {
    const eventType = event.type;

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

    if (eventType === "response.audio_transcript.delta" || eventType === "response.text.delta") {
      optsRef.current.onAssistantDelta?.((event.delta as string) || "");
    } else if (eventType === "response.audio_transcript.done") {
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
        (event.event as RealtimeEvent | undefined)?.type === "response.audio_transcript.delta"
        || (event.event as RealtimeEvent | undefined)?.type === "response.text.delta"
      ))
    ) {
      vlog("recv", event.type, event.type === "voice_event" ? (event.event as RealtimeEvent).type : "");
    }
    switch (event.type) {
      case "session_started": {
        optsRef.current.onSessionStarted?.(event.session_id);
        const info = event.voice_connection_info;
        if (info) connInfoRef.current = info;
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

      case "voice_stopped":
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

    // Wait up to 10s for session_started (which populates connInfoRef).
    const startedAt = Date.now();
    while (!connInfoRef.current) {
      if (Date.now() - startedAt > 10_000) {
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
    if (wsRef.current) wsRef.current.send({ type: "stop" });
    cleanup();
    updateStatus("off");
    optsRef.current.onAfterStop?.();
  }, [cleanup, updateStatus]);

  useEffect(() => {
    return () => { cleanup(); };
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
