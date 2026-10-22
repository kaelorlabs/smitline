// Listening in: decode a call's live audio (from /api/calls/{id}/listen) and play both sides.
// The stream is NDJSON: {"type":"format","codec":"mulaw"|"pcm16","rate":8000|24000}, then
// {"side":"agent"|"other"|"meeting","audio":"<base64>"} chunks until the call ends.

// G.711 mu-law to a sample in [-1, 1].
export function mulawToFloat(byte) {
  const value = ~byte & 0xff;
  const sign = value & 0x80;
  const exponent = (value >> 4) & 0x07;
  const mantissa = value & 0x0f;
  const magnitude = ((mantissa << 3) + 0x84) << exponent;
  const sample = sign ? 0x84 - magnitude : magnitude - 0x84;
  return sample / 32768;
}

const MULAW = Float32Array.from({ length: 256 }, (_, byte) => mulawToFloat(byte));

function bytes(base64) {
  const binary = atob(base64);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
  return out;
}

/** One chunk's samples as floats in [-1, 1]. */
export function decodeChunk(codec, base64) {
  const data = bytes(base64);
  if (codec === 'mulaw') return Float32Array.from(data, (byte) => MULAW[byte]);
  if (codec === 'pcm16') {
    const count = data.length >> 1;
    const view = new DataView(data.buffer, data.byteOffset, count * 2);
    const out = new Float32Array(count);
    for (let i = 0; i < count; i += 1) out[i] = view.getInt16(i * 2, true) / 32768;
    return out;
  }
  throw new Error(`unknown codec ${codec}`);
}

/** Splits a byte stream into the JSON objects of its complete lines. */
export function lineReader() {
  const decoder = new TextDecoder();
  let rest = '';
  return (chunk, done = false) => {
    rest += decoder.decode(chunk || new Uint8Array(), { stream: !done });
    const lines = rest.split('\n');
    rest = done ? '' : lines.pop();
    const items = [];
    for (const line of lines) {
      if (!line.trim()) continue;
      try { items.push(JSON.parse(line)); } catch { /* a broken line is skipped */ }
    }
    return items;
  };
}

// Audio is scheduled this far ahead, so network jitter does not cut it up.
const LEAD_SECONDS = 0.25;

/**
 * Plays a listen stream: each side keeps its own play position, so the two sides overlap the
 * way they did on the call. listen(response) resolves when the stream ends; stop() ends it.
 */
export class ListenPlayer {
  constructor(AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext) {
    this.AudioContextClass = AudioContextClass;
    this.context = null;
    this.format = null;
    this.playheads = new Map();
    this.reader = null;
  }

  async listen(response) {
    this.context = new this.AudioContextClass();
    const read = lineReader();
    this.reader = response.body.getReader();
    try {
      for (;;) {
        const { value, done } = await this.reader.read();
        for (const item of read(value, done)) this.play(item);
        if (done) return;
      }
    } finally {
      this.close();
    }
  }

  play(item) {
    if (item.type === 'format') { this.format = item; return; }
    if (!this.format || !item.audio || !this.context) return;
    const samples = decodeChunk(this.format.codec, item.audio);
    if (!samples.length) return;
    const buffer = this.context.createBuffer(1, samples.length, this.format.rate);
    buffer.copyToChannel(samples, 0);
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    const now = this.context.currentTime;
    let at = this.playheads.get(item.side) || 0;
    if (at < now + 0.02) at = now + LEAD_SECONDS; // fell behind or just started: catch up
    source.start(at);
    this.playheads.set(item.side, at + buffer.duration);
  }

  stop() {
    this.reader?.cancel().catch(() => {});
    this.close();
  }

  close() {
    const context = this.context;
    this.context = null;
    this.playheads.clear();
    context?.close().catch(() => {});
  }
}
