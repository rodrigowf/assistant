/**
 * useVoiceOrchestrator — self-contained hook that manages:
 *   1. A WebSocket to /api/orchestrator/chat (sends voice_start)
 *   2. A WebRTC connection to OpenAI Realtime (via useVoiceSession)
 *   3. Bidirectional event bridging between the two
 *
 * Events from OpenAI → mirrored to backend via voice_event WS message
 * voice_command from backend → forwarded to OpenAI via data channel
 *
 * Voice transcripts are dispatched to the message list via onEvent callbacks.
 *
 * This hook is activated only for orchestrator tabs in voice mode. It runs
 * alongside (not instead of) the normal text orchestrator if one exists.
 * Since only one orchestrator can be active at a time on the backend, the
 * caller must stop any active text session before starting voice.
 */

import { useState, useRef, useCallback, useEffect } from "react";
import { useVoiceSession, type VoiceSessionHandles } from "./useVoiceSession";
import { ChatSocket } from "../api/websocket";
import type { RealtimeEvent, ServerEvent, VoiceStatus } from "../types";

interface UseVoiceOrchestratorOptions {
  /** The stable local_id of the orchestrator tab — used as pool key. */
  localId?: string;
  /** The original session ID for JSONL continuity when resuming from history. */
  resumeSdkId?: string | null;
  /** Called for each user transcript received (to add to message list). */
  onUserTranscript?: (text: string) => void;
  /** Called for each assistant transcript delta. */
  onAssistantDelta?: (text: string) => void;
  /** Called when assistant transcript is complete. */
  onAssistantComplete?: (text: string) => void;
  /** Called when a tool call starts. */
  onToolUse?: (callId: string, toolName: string, toolInput: Record<string, unknown>) => void;
  /** Called when turn completes. */
  onTurnComplete?: () => void;
  /** Called when session starts. */
  onSessionStarted?: (sessionId: string) => void;
  /** Called when voice status changes (for tab status sync). */
  onStatusChange?: (status: VoiceStatus) => void;
  /** Called before voice starts — use to stop the text orchestrator session. */
  onBeforeStart?: () => void;
  /** Called after voice stops — use to allow the text orchestrator to reconnect. */
  onAfterStop?: () => void;
}

export interface VoiceOrchestratorResult {
  voiceStatus: VoiceStatus;
  startVoice: () => Promise<void>;
  stopVoice: () => void;
  isActive: boolean;
  /** Whether the microphone is muted. */
  isMuted: boolean;
  /** Toggle microphone mute on/off. */
  toggleMute: () => void;
  /** Mic input audio level (0–1). */
  micLevel: number;
  /** Remote speaker audio level (0–1). */
  speakerLevel: number;
}

export function useVoiceOrchestrator(
  options: UseVoiceOrchestratorOptions = {}
): VoiceOrchestratorResult {
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>("off");
  const voiceHandlesRef = useRef<VoiceSessionHandles | null>(null);
  const wsRef = useRef<ChatSocket | null>(null);
  // Queue commands that arrive before data channel opens
  const pendingCommandsRef = useRef<RealtimeEvent[]>([]);
  // Whether data channel is open
  const dcReadyRef = useRef(false);

  // Mute state
  const [isMuted, setIsMuted] = useState(false);

  // Audio level analysis
  const [micLevel, setMicLevel] = useState(0);
  const [speakerLevel, setSpeakerLevel] = useState(0);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Stable callback refs
  const optsRef = useRef(options);
  optsRef.current = options;

  const updateStatus = useCallback((status: VoiceStatus) => {
    setVoiceStatus(status);
    optsRef.current.onStatusChange?.(status);
  }, []);

  // Send a raw event to OpenAI via data channel (or queue if not yet open)
  const sendToOpenAI = useCallback((event: RealtimeEvent) => {
    if (dcReadyRef.current && voiceHandlesRef.current) {
      voiceHandlesRef.current.sendToOpenAI(event);
    } else {
      pendingCommandsRef.current.push(event);
    }
  }, []);

  // Handle server events from the orchestrator WebSocket
  const handleServerEvent = useCallback((event: ServerEvent) => {
    switch (event.type) {
      case "session_started":
        optsRef.current.onSessionStarted?.(event.session_id);
        // Forward session.update to OpenAI as soon as data channel is open
        if (event.voice_session_update) {
          sendToOpenAI(event.voice_session_update as RealtimeEvent);
        }
        break;

      case "voice_command":
        // Backend is sending a command to forward to OpenAI (e.g. function_call_output)
        sendToOpenAI(event.command as RealtimeEvent);
        break;

      case "error":
        console.error("[voice-orchestrator] Server error:", event.error, (event as { detail?: string }).detail);
        updateStatus("error");
        break;
    }
  }, [sendToOpenAI, updateStatus]);

  // Handle events from OpenAI data channel
  const handleOpenAIEvent = useCallback((event: RealtimeEvent) => {
    const eventType = event.type;

    // Mirror every event to the backend
    wsRef.current?.send({ type: "voice_event", event });

    // Update status and dispatch UI callbacks
    if (eventType === "response.created") {
      updateStatus("speaking");
    } else if (eventType === "response.done") {
      updateStatus("active");
      optsRef.current.onTurnComplete?.();
    } else if (eventType === "response.output_item.added") {
      const item = event.item as Record<string, unknown> | undefined;
      if (item?.type === "function_call") {
        updateStatus("tool_use");
      }
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
    } else if (eventType === "input_audio_buffer.speech_stopped") {
      updateStatus("thinking");
    }

    // User speech transcript — arrives after transcription completes
    if (eventType === "conversation.item.input_audio_transcription.completed") {
      const transcript = (event.transcript as string) || "";
      if (transcript) {
        optsRef.current.onUserTranscript?.(transcript);
      }
    }

    // User text input (typed text in voice session, if ever used)
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

    // Assistant transcript streaming
    if (eventType === "response.audio_transcript.delta") {
      optsRef.current.onAssistantDelta?.((event.delta as string) || "");
    } else if (eventType === "response.audio_transcript.done") {
      optsRef.current.onAssistantComplete?.((event.transcript as string) || "");
    }
  }, [updateStatus]);

  // Data channel connected → drain queue
  const handleVoiceConnected = useCallback(() => {
    dcReadyRef.current = true;
    updateStatus("active");
    const pending = pendingCommandsRef.current.splice(0);
    for (const cmd of pending) {
      voiceHandlesRef.current?.sendToOpenAI(cmd);
    }
  }, [updateStatus]);

  const { connect } = useVoiceSession({
    onEvent: handleOpenAIEvent,
    onConnected: handleVoiceConnected,
    onError: (err) => {
      console.error("[voice-orchestrator] WebRTC error:", err);
      updateStatus("error");
    },
  });

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

  const startAudioAnalysis = useCallback((handles: VoiceSessionHandles) => {
    const ctx = new AudioContext();
    audioCtxRef.current = ctx;

    // Mic analyser
    const micSource = ctx.createMediaStreamSource(handles.micStream);
    const micAnalyser = ctx.createAnalyser();
    micAnalyser.fftSize = 256;
    micSource.connect(micAnalyser);

    // Speaker analyser (lazy — remote stream may arrive later)
    let speakerAnalyser: AnalyserNode | null = null;

    const micData = new Uint8Array(micAnalyser.frequencyBinCount);
    let speakerData: Uint8Array | null = null;

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function computeRMS(data: any): number {
      let sum = 0;
      for (let i = 0; i < data.length; i++) {
        const v = (data[i] - 128) / 128;
        sum += v * v;
      }
      return Math.sqrt(sum / data.length);
    }

    analyserIntervalRef.current = setInterval(() => {
      // Mic level
      micAnalyser.getByteTimeDomainData(micData);
      setMicLevel(computeRMS(micData));

      // Speaker level (lazy init when remote stream is available)
      if (!speakerAnalyser && handles.remoteStream) {
        try {
          const speakerSource = ctx.createMediaStreamSource(handles.remoteStream);
          speakerAnalyser = ctx.createAnalyser();
          speakerAnalyser.fftSize = 256;
          speakerSource.connect(speakerAnalyser);
          speakerData = new Uint8Array(speakerAnalyser.frequencyBinCount);
        } catch {
          // Remote stream not ready yet
        }
      }
      if (speakerAnalyser && speakerData) {
        speakerAnalyser.getByteTimeDomainData(speakerData as Uint8Array<ArrayBuffer>);
        setSpeakerLevel(computeRMS(speakerData));
      }
    }, 66); // ~15fps
  }, []);

  const cleanup = useCallback(() => {
    stopAudioAnalysis();
    voiceHandlesRef.current?.disconnect();
    voiceHandlesRef.current = null;
    wsRef.current?.close();
    wsRef.current = null;
    dcReadyRef.current = false;
    pendingCommandsRef.current = [];
    setIsMuted(false);
  }, [stopAudioAnalysis]);

  const toggleMute = useCallback(() => {
    const handles = voiceHandlesRef.current;
    if (!handles) return;
    const newMuted = !isMuted;
    handles.micStream.getAudioTracks().forEach((track) => {
      track.enabled = !newMuted;
    });
    setIsMuted(newMuted);
  }, [isMuted]);

  const startVoice = useCallback(async () => {
    if (voiceStatus !== "off" && voiceStatus !== "error") return;

    updateStatus("connecting");
    dcReadyRef.current = false;
    pendingCommandsRef.current = [];

    // Notify the text session it's about to be replaced (disconnects its WS).
    // The backend handles the text→voice transition atomically — no need to
    // wait for an explicit stop acknowledgment.
    optsRef.current.onBeforeStart?.();

    // 1. Open orchestrator WebSocket in voice mode
    const socket = new ChatSocket(handleServerEvent, "/api/orchestrator/chat");
    wsRef.current = socket;
    socket.connect(() => {
      // WebSocket open → send voice_start with local_id (pool key) and
      // resume_sdk_id (original JSONL session ID for history continuity).
      const lid = optsRef.current.localId;
      const resumeId = optsRef.current.resumeSdkId;
      const payload: Record<string, unknown> = { type: "voice_start", local_id: lid };
      if (resumeId) {
        payload.resume_sdk_id = resumeId;
      }
      socket.send(payload);
    });

    // 2. Establish WebRTC connection in parallel
    const handles = await connect();
    if (!handles) {
      cleanup();
      updateStatus("error");
      return;
    }
    voiceHandlesRef.current = handles;
    // Start audio level analysis
    startAudioAnalysis(handles);
    // Drain any pending commands that arrived before DC opened
    if (dcReadyRef.current && pendingCommandsRef.current.length > 0) {
      const pending = pendingCommandsRef.current.splice(0);
      for (const cmd of pending) {
        handles.sendToOpenAI(cmd);
      }
    }
  }, [voiceStatus, connect, handleServerEvent, cleanup, updateStatus, startAudioAnalysis]);

  const stopVoice = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.send({ type: "stop" });
    }
    cleanup();
    updateStatus("off");
    // Allow text orchestrator to reconnect
    optsRef.current.onAfterStop?.();
  }, [cleanup, updateStatus]);

  // Clean up on unmount
  useEffect(() => {
    return () => {
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
    micLevel,
    speakerLevel,
  };
}
