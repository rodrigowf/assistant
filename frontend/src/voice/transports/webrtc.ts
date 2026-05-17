/**
 * WebRTC voice transport — used by OpenAI Realtime today. Establishes a
 * peer connection directly with the provider, capturing mic audio and
 * routing remote audio to a hidden ``<audio>`` element. Provider events
 * arrive on a data channel; the orchestrator hook mirrors them to the
 * backend and dispatches to UI callbacks.
 *
 * Backend supplies:
 *   - ``ephemeralKey`` — short-lived bearer for SDP exchange
 *   - ``callUrl``     — canonical /v1/realtime/calls URL with ?model=...
 *
 * On WebRTC, audio bypasses the backend entirely. The data channel is
 * the only thing carrying provider events to/from us.
 */

import { exchangeSDP } from "../../api/voice";
import type { RealtimeEvent } from "../../types";
import type { WebRTCVoiceTransportHandles } from "./types";

interface ConnectOptions {
  ephemeralKey: string;
  callUrl: string;
  /** Sample rate to request from getUserMedia (24kHz for OpenAI). */
  inputSampleRate: number;
  /** Called for every event received from the provider data channel. */
  onProviderEvent: (event: RealtimeEvent) => void;
  /** Called when the data channel opens (transport ready for sending). */
  onConnected: () => void;
  /** Called on connection drop / data channel close. */
  onClose: () => void;
  /** Called on error. */
  onError: (error: string) => void;
}

export async function connectWebRTCVoiceSession(
  opts: ConnectOptions,
): Promise<WebRTCVoiceTransportHandles> {
  let pc: RTCPeerConnection | null = null;
  let audioEl: HTMLAudioElement | null = null;
  let micStream: MediaStream | null = null;
  let remoteStream: MediaStream | null = null;

  const cleanup = () => {
    micStream?.getTracks().forEach((t) => t.stop());
    pc?.close();
    audioEl?.remove();
  };

  try {
    pc = new RTCPeerConnection();

    audioEl = document.createElement("audio");
    audioEl.autoplay = true;
    audioEl.style.display = "none";
    document.body.appendChild(audioEl);

    pc.ontrack = (e) => {
      remoteStream = e.streams[0];
      if (audioEl) audioEl.srcObject = remoteStream;
    };

    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        sampleRate: opts.inputSampleRate,
      },
    });
    micStream.getTracks().forEach((track) => pc!.addTrack(track, micStream!));

    const dc = pc.createDataChannel("oai-events");

    dc.onopen = () => opts.onConnected();
    dc.onmessage = (e) => {
      try {
        const event: RealtimeEvent = JSON.parse(e.data);
        opts.onProviderEvent(event);
      } catch {
        // unparseable payloads ignored
      }
    };
    dc.onerror = (e) => opts.onError(`Data channel error: ${e}`);
    dc.onclose = () => opts.onClose();

    pc.onconnectionstatechange = () => {
      if (pc!.connectionState === "failed") {
        opts.onError("Connection failed");
      } else if (pc!.connectionState === "disconnected" || pc!.connectionState === "closed") {
        opts.onClose();
      }
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    const answerSdp = await exchangeSDP(opts.ephemeralKey, offer.sdp!, opts.callUrl);
    await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });

    return {
      kind: "webrtc",
      sendProviderEvent: (event) => {
        if (dc.readyState === "open") {
          dc.send(JSON.stringify(event));
        }
      },
      disconnect: cleanup,
      micStream: micStream!,
      get remoteStream() { return remoteStream; },
      audioElement: audioEl!,
      setMicEnabled: (enabled) => {
        micStream!.getAudioTracks().forEach((t) => { t.enabled = enabled; });
      },
      setOutputMuted: (muted) => {
        if (audioEl) audioEl.muted = muted;
      },
    };
  } catch (err) {
    cleanup();
    throw err;
  }
}
