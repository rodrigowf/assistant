// PCM capture worklet — runs on the AudioWorklet thread.
//
// Captures mono mic audio at the AudioContext's native rate (typically
// 48 kHz on desktop browsers), resamples to a target rate, packs as
// signed 16-bit little-endian PCM, and posts each chunk's raw bytes to
// the main thread roughly every chunkMs ms. The main thread does the
// base64-encode (AudioWorkletGlobalScope has no `btoa`).
//
// Mounted by the main thread via:
//   await audioContext.audioWorklet.addModule('/pcm-capture-worklet.js');
//   const node = new AudioWorkletNode(ctx, 'pcm-capture', {
//     processorOptions: { targetSampleRate: 16000, chunkMs: 100 }
//   });

class PCMCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetSampleRate = opts.targetSampleRate || 16000;
    // Resampler ratio: how many input samples per output sample.
    this.ratio = sampleRate / this.targetSampleRate;
    // Fractional read pointer into the input buffer.
    this.readPos = 0;
    // Accumulated output samples (Float32) before we emit a chunk.
    this.chunkSize = Math.round((this.targetSampleRate * (opts.chunkMs || 100)) / 1000);
    this.outBuf = new Float32Array(this.chunkSize);
    this.outIdx = 0;
    // Carry-over from previous render block (so resampling is continuous).
    this.tail = null;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;

    const channel = input[0];
    if (!channel || channel.length === 0) return true;

    // Concatenate the carry-over tail with this block's samples so the
    // resampler can interpolate across block boundaries.
    let src;
    if (this.tail && this.tail.length > 0) {
      src = new Float32Array(this.tail.length + channel.length);
      src.set(this.tail, 0);
      src.set(channel, this.tail.length);
    } else {
      src = channel;
    }

    // Linear-interpolate to the target rate. readPos walks through src.
    while (this.readPos + 1 < src.length) {
      const i = Math.floor(this.readPos);
      const frac = this.readPos - i;
      const sample = src[i] * (1 - frac) + src[i + 1] * frac;
      this.outBuf[this.outIdx++] = sample;
      this.readPos += this.ratio;

      if (this.outIdx === this.chunkSize) {
        this._emitChunk();
      }
    }

    // Anything left over (one or two samples) becomes the tail for the
    // next block; reset readPos relative to the new tail's start.
    const consumed = Math.floor(this.readPos);
    if (consumed < src.length) {
      this.tail = src.slice(consumed);
      this.readPos -= consumed;
    } else {
      this.tail = null;
      this.readPos = 0;
    }
    return true;
  }

  _emitChunk() {
    // Float32 → int16 little-endian. Allocate a fresh ArrayBuffer each
    // time so structured-clone keeps it intact for postMessage transfer.
    const buf = new ArrayBuffer(this.chunkSize * 2);
    const view = new DataView(buf);
    for (let k = 0; k < this.chunkSize; k++) {
      let v = this.outBuf[k];
      if (v > 1) v = 1; else if (v < -1) v = -1;
      view.setInt16(k * 2, v < 0 ? Math.round(v * 0x8000) : Math.round(v * 0x7fff), true);
    }
    // Transfer the buffer to the main thread; base64 encoding happens there
    // (AudioWorkletGlobalScope doesn't expose `btoa`).
    this.port.postMessage(
      { type: 'pcm', buffer: buf, sampleRate: this.targetSampleRate },
      [buf]
    );
    this.outIdx = 0;
  }
}

registerProcessor('pcm-capture', PCMCaptureProcessor);
