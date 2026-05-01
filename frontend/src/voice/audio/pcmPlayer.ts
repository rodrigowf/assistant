/**
 * PCMPlayer — schedules incoming base64-encoded PCM16 chunks for gapless
 * playback through Web Audio API. Used by WebSocket voice providers
 * (Qwen, Gemini Live, future locals) where assistant audio is delivered
 * as discrete chunks rather than a WebRTC media track.
 *
 * Each pushed chunk decodes to a Float32 AudioBuffer; sources are
 * scheduled tail-to-tail using a running ``nextStartTime`` cursor so
 * there are no audible gaps between chunks even if the network jitters.
 */

interface PCMPlayerOptions {
  sampleRate: number;
  /** Optional callback fired ~15fps with the current speaker RMS level (0–1). */
  onLevel?: (level: number) => void;
}

export class PCMPlayer {
  private ctx: AudioContext;
  private gainNode: GainNode;
  private analyser: AnalyserNode;
  private nextStartTime = 0;
  private muted = false;
  private levelInterval: ReturnType<typeof setInterval> | null = null;
  private analyserData: Uint8Array;
  private sampleRate: number;
  private destroyed = false;
  // All scheduled-but-not-yet-finished sources, so flush() can stop them.
  private active: Set<AudioBufferSourceNode> = new Set();

  constructor(opts: PCMPlayerOptions) {
    this.sampleRate = opts.sampleRate;
    // The output context can run at a different rate than the chunks —
    // AudioBufferSourceNode resamples on the fly when its buffer's rate
    // differs from the destination's.
    this.ctx = new AudioContext();
    this.gainNode = this.ctx.createGain();
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 256;
    this.analyserData = new Uint8Array(this.analyser.frequencyBinCount);
    this.gainNode.connect(this.analyser);
    this.analyser.connect(this.ctx.destination);

    if (opts.onLevel) {
      const cb = opts.onLevel;
      this.levelInterval = setInterval(() => {
        // Cast workaround: getByteTimeDomainData expects strictly typed array.
        this.analyser.getByteTimeDomainData(this.analyserData as Uint8Array<ArrayBuffer>);
        let sum = 0;
        for (let i = 0; i < this.analyserData.length; i++) {
          const v = (this.analyserData[i] - 128) / 128;
          sum += v * v;
        }
        cb(Math.sqrt(sum / this.analyserData.length));
      }, 66);
    }
  }

  /** Schedule a base64 PCM16 chunk for playback at the cursor. */
  push(b64: string): void {
    if (this.destroyed) return;
    // Decode base64 → Int16 → Float32.
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const i16 = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 0x8000;

    const buf = this.ctx.createBuffer(1, f32.length, this.sampleRate);
    buf.getChannelData(0).set(f32);

    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.gainNode);

    const now = this.ctx.currentTime;
    // If we're behind (long pause / first chunk), start immediately;
    // otherwise queue at the running cursor for gapless playback.
    const startAt = Math.max(now, this.nextStartTime);
    src.start(startAt);
    this.nextStartTime = startAt + buf.duration;

    // Track for flush(); auto-remove when finished.
    this.active.add(src);
    src.onended = () => { this.active.delete(src); };
  }

  /** Drop any pending audio (used on barge-in interruption).
   *
   * Stops every scheduled source — including the one currently playing —
   * so the assistant goes silent immediately when the user starts talking.
   * Without this, in-flight AudioBufferSourceNodes keep playing and chunks
   * that were already scheduled at future startAt times still fire.
   */
  flush(): void {
    for (const src of this.active) {
      try { src.stop(); } catch { /* already stopped */ }
      try { src.disconnect(); } catch { /* fine */ }
    }
    this.active.clear();
    this.nextStartTime = this.ctx.currentTime;
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    this.gainNode.gain.value = muted ? 0 : 1;
  }

  isMuted(): boolean {
    return this.muted;
  }

  async destroy(): Promise<void> {
    if (this.destroyed) return;
    this.destroyed = true;
    if (this.levelInterval) {
      clearInterval(this.levelInterval);
      this.levelInterval = null;
    }
    try {
      await this.ctx.close();
    } catch {
      // ignore
    }
  }
}
