/**
 * Frontend audio recorder for WebRTC voice sessions.
 *
 * Captures mic and speaker streams and sends them to the backend for storage.
 * Used when voice_recording_enabled is true and the transport is WebRTC
 * (where audio bypasses the backend).
 *
 * For WebSocket transports, the backend handles recording directly since
 * audio flows through it anyway.
 */

import type { ChatSocket } from "../api/websocket";

interface RecordingOptions {
  sessionId: string;
  ws: ChatSocket;
  micStream: MediaStream;
  remoteStreamGetter: () => MediaStream | null;
  sampleRate?: number;
}

export class VoiceRecorder {
  private sessionId: string;
  private ws: ChatSocket;
  private micStream: MediaStream;
  private remoteStreamGetter: () => MediaStream | null;
  private sampleRate: number;

  private micWorklet: AudioWorkletNode | null = null;
  private speakerWorklet: AudioWorkletNode | null = null;
  private ctx: AudioContext | null = null;
  private isRecording = false;

  // Accumulate base64 chunks for periodic sending
  private micChunks: string[] = [];
  private speakerChunks: string[] = [];
  private flushInterval: number | null = null;

  constructor(opts: RecordingOptions) {
    this.sessionId = opts.sessionId;
    this.ws = opts.ws;
    this.micStream = opts.micStream;
    this.remoteStreamGetter = opts.remoteStreamGetter;
    this.sampleRate = opts.sampleRate ?? 24000;
  }

  async start(): Promise<void> {
    if (this.isRecording) return;

    try {
      // Create audio context at the desired sample rate
      this.ctx = new AudioContext({ sampleRate: this.sampleRate });

      // Load the PCM capture worklet if not already loaded
      const workletUrl = new URL("/pcm-capture-worklet.js", window.location.origin).href;
      await this.ctx.audioWorklet.addModule(workletUrl);

      // Set up mic recording
      await this.setupMicRecording();

      // Set up speaker recording (may not be available immediately)
      await this.setupSpeakerRecording();

      this.isRecording = true;

      // Flush chunks to backend every 5 seconds
      this.flushInterval = window.setInterval(() => this.flushChunks(), 5000);

      console.log("[VoiceRecorder] Started recording for session", this.sessionId);
    } catch (err) {
      console.error("[VoiceRecorder] Failed to start recording:", err);
      this.cleanup();
    }
  }

  private async setupMicRecording(): Promise<void> {
    if (!this.ctx) return;

    const source = this.ctx.createMediaStreamSource(this.micStream);
    this.micWorklet = new AudioWorkletNode(this.ctx, "pcm-capture-processor");

    this.micWorklet.port.onmessage = (e) => {
      const msg = e.data;
      if (msg?.type === "pcm" && msg.buffer instanceof ArrayBuffer) {
        const bytes = new Uint8Array(msg.buffer);
        const b64 = this.arrayBufferToBase64(bytes);
        this.micChunks.push(b64);
      }
    };

    source.connect(this.micWorklet);
    // Don't connect to destination - we just want to capture
  }

  private async setupSpeakerRecording(): Promise<void> {
    if (!this.ctx) return;

    const remoteStream = this.remoteStreamGetter();
    if (!remoteStream) {
      // Remote stream not ready yet - try again later
      console.log("[VoiceRecorder] Remote stream not ready, will retry...");
      setTimeout(() => this.setupSpeakerRecording(), 1000);
      return;
    }

    const source = this.ctx.createMediaStreamSource(remoteStream);
    this.speakerWorklet = new AudioWorkletNode(this.ctx, "pcm-capture-processor");

    this.speakerWorklet.port.onmessage = (e) => {
      const msg = e.data;
      if (msg?.type === "pcm" && msg.buffer instanceof ArrayBuffer) {
        const bytes = new Uint8Array(msg.buffer);
        const b64 = this.arrayBufferToBase64(bytes);
        this.speakerChunks.push(b64);
      }
    };

    source.connect(this.speakerWorklet);
    console.log("[VoiceRecorder] Speaker recording started");
  }

  private flushChunks(): void {
    if (!this.ws || !this.isRecording) return;

    if (this.micChunks.length > 0) {
      const combined = this.micChunks.join("");
      this.micChunks = [];
      this.sendAudioChunk("user", combined);
    }

    if (this.speakerChunks.length > 0) {
      const combined = this.speakerChunks.join("");
      this.speakerChunks = [];
      this.sendAudioChunk("assistant", combined);
    }
  }

  private sendAudioChunk(channel: "user" | "assistant", audioB64: string): void {
    try {
      this.ws.send({
        type: "voice_recording_chunk",
        session_id: this.sessionId,
        channel,
        audio: audioB64,
      });
    } catch (err) {
      console.error("[VoiceRecorder] Failed to send audio chunk:", err);
    }
  }

  stop(): void {
    if (!this.isRecording) return;

    // Flush any remaining chunks
    this.flushChunks();

    // Notify backend that recording is done
    try {
      this.ws.send({
        type: "voice_recording_end",
        session_id: this.sessionId,
      });
    } catch (err) {
      console.error("[VoiceRecorder] Failed to send recording end:", err);
    }

    this.cleanup();
    console.log("[VoiceRecorder] Stopped recording for session", this.sessionId);
  }

  private cleanup(): void {
    this.isRecording = false;

    if (this.flushInterval) {
      clearInterval(this.flushInterval);
      this.flushInterval = null;
    }

    if (this.micWorklet) {
      this.micWorklet.disconnect();
      this.micWorklet = null;
    }

    if (this.speakerWorklet) {
      this.speakerWorklet.disconnect();
      this.speakerWorklet = null;
    }

    if (this.ctx) {
      this.ctx.close().catch(() => {});
      this.ctx = null;
    }

    this.micChunks = [];
    this.speakerChunks = [];
  }

  private arrayBufferToBase64(buffer: Uint8Array): string {
    let binary = "";
    const bytes = buffer;
    const len = bytes.byteLength;
    for (let i = 0; i < len; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
  }
}
