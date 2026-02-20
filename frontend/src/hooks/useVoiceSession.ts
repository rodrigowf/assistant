/**
 * useVoiceSession — manages the WebRTC connection to OpenAI Realtime API.
 *
 * Responsibilities:
 * - Fetch ephemeral token from backend
 * - Create RTCPeerConnection with mic audio
 * - Exchange SDP offer/answer with OpenAI
 * - Expose data channel for sending events to OpenAI
 * - Fire onEvent callback for every event received from OpenAI
 * - Pipe remote audio to <audio> element (server-side VAD, autoplay)
 */

import { useRef, useCallback } from "react";
import { fetchEphemeralToken, exchangeSDP } from "../api/voice";
import type { RealtimeEvent } from "../types";

export interface VoiceSessionHandles {
  /** Send an event to OpenAI via the data channel. */
  sendToOpenAI: (event: RealtimeEvent) => void;
  /** Tear down the WebRTC connection. */
  disconnect: () => void;
  /** The local microphone MediaStream (for muting and audio analysis). */
  micStream: MediaStream;
  /** The remote audio MediaStream (for audio analysis). May be null until ontrack fires. */
  remoteStream: MediaStream | null;
}

interface UseVoiceSessionOptions {
  /** Called for every event received from OpenAI data channel. */
  onEvent: (event: RealtimeEvent) => void;
  /** Called when connection is established and ready. */
  onConnected: () => void;
  /** Called on error. */
  onError: (error: string) => void;
}

export function useVoiceSession(options: UseVoiceSessionOptions): {
  connect: () => Promise<VoiceSessionHandles | null>;
} {
  const { onEvent, onConnected, onError } = options;

  // Keep stable refs so connect() closure doesn't go stale
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;
  const onConnectedRef = useRef(onConnected);
  onConnectedRef.current = onConnected;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  const connect = useCallback(async (): Promise<VoiceSessionHandles | null> => {
    let pc: RTCPeerConnection | null = null;
    let audioEl: HTMLAudioElement | null = null;
    let micStream: MediaStream | null = null;
    let remoteStream: MediaStream | null = null;

    function cleanup() {
      micStream?.getTracks().forEach((t) => t.stop());
      pc?.close();
      audioEl?.remove();
    }

    try {
      // 1. Get ephemeral token from backend
      const tokenData = await fetchEphemeralToken();
      const ephemeralKey = tokenData.client_secret.value;

      // 2. Create RTCPeerConnection
      pc = new RTCPeerConnection();

      // 3. Set up remote audio output
      audioEl = document.createElement("audio");
      audioEl.autoplay = true;
      audioEl.style.display = "none";
      document.body.appendChild(audioEl);

      pc.ontrack = (e) => {
        remoteStream = e.streams[0];
        if (audioEl) audioEl.srcObject = remoteStream;
      };

      // 4. Get mic with echo cancellation (server-side VAD — no push-to-talk)
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          sampleRate: 24000,
        },
      });
      micStream.getTracks().forEach((track) => pc!.addTrack(track, micStream!));

      // 5. Create data channel for OpenAI Realtime events
      const dc = pc.createDataChannel("oai-events");

      dc.onopen = () => {
        onConnectedRef.current();
      };

      dc.onmessage = (e) => {
        try {
          const event: RealtimeEvent = JSON.parse(e.data);
          onEventRef.current(event);
        } catch {
          // Ignore unparseable messages
        }
      };

      dc.onerror = (e) => {
        onErrorRef.current(`Data channel error: ${e}`);
      };

      // 6. Create SDP offer
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      // 7. Exchange SDP with OpenAI
      const answerSdp = await exchangeSDP(ephemeralKey, offer.sdp!);
      await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });

      // Return handles for the caller
      const handles: VoiceSessionHandles = {
        sendToOpenAI: (event: RealtimeEvent) => {
          if (dc.readyState === "open") {
            dc.send(JSON.stringify(event));
          }
        },
        disconnect: () => {
          cleanup();
        },
        micStream: micStream!,
        get remoteStream() { return remoteStream; },
      };

      return handles;
    } catch (err) {
      cleanup();
      onErrorRef.current(err instanceof Error ? err.message : String(err));
      return null;
    }
  }, []);

  return { connect };
}
