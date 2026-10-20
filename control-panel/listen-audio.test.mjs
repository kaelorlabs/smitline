import assert from 'node:assert/strict';
import test from 'node:test';
import { ListenPlayer, decodeChunk, lineReader, mulawToFloat } from './listen-audio.mjs';

test('mu-law decodes to the G.711 values', () => {
  assert.equal(mulawToFloat(0xff), 0);
  assert.equal(mulawToFloat(0x7f), 0);
  assert.equal(mulawToFloat(0x00), -32124 / 32768);
  assert.equal(mulawToFloat(0x80), 32124 / 32768);
  assert.ok(mulawToFloat(0xfe) > 0 && mulawToFloat(0xfe) < 0.001);
});

test('chunks decode from base64 for both codecs', () => {
  assert.deepEqual([...decodeChunk('mulaw', Buffer.from([0xff, 0x80]).toString('base64'))], [0, 32124 / 32768]);
  const pcm = Buffer.alloc(6);
  pcm.writeInt16LE(16384, 0);
  pcm.writeInt16LE(-32768, 2);
  pcm.writeInt16LE(0, 4);
  assert.deepEqual([...decodeChunk('pcm16', pcm.toString('base64'))], [0.5, -1, 0]);
  assert.throws(() => decodeChunk('opus', ''), /unknown codec/);
});

test('lines are read across chunk boundaries and broken lines skipped', () => {
  const read = lineReader();
  const bytes = (text) => new TextEncoder().encode(text);
  assert.deepEqual(read(bytes('{"type":"format","codec":"mulaw"')), []);
  assert.deepEqual(read(bytes(',"rate":8000}\n{"side":"other",')), [{ type: 'format', codec: 'mulaw', rate: 8000 }]);
  assert.deepEqual(read(bytes('"audio":"AA"}\nnot json\n\n')), [{ side: 'other', audio: 'AA' }]);
  assert.deepEqual(read(bytes('{"side":"agent","audio":"BB"}'), true), [{ side: 'agent', audio: 'BB' }]);
});

class FakeContext {
  constructor() { this.currentTime = 10; this.started = []; this.closed = false; }
  createBuffer(channels, length, rate) {
    return { duration: length / rate, copyToChannel() {} };
  }
  createBufferSource() {
    const context = this;
    return { connect() {}, start(at) { context.started.push(Number(at.toFixed(4))); } };
  }
  get destination() { return {}; }
  async close() { this.closed = true; }
}

test('each side plays back to back, and catches up after falling behind', () => {
  const player = new ListenPlayer(FakeContext);
  player.context = new FakeContext();
  const ms20 = Buffer.alloc(160, 0xff).toString('base64'); // 20 ms of mu-law at 8 kHz
  player.play({ side: 'other', audio: ms20 }); // no format yet: ignored
  assert.deepEqual(player.context.started, []);
  player.play({ type: 'format', codec: 'mulaw', rate: 8000 });
  player.play({ side: 'other', audio: ms20 });
  player.play({ side: 'other', audio: ms20 });
  player.play({ side: 'agent', audio: ms20 }); // the other side has its own position
  assert.deepEqual(player.context.started, [10.25, 10.27, 10.25]);
  player.context.currentTime = 20; // the network stalled
  player.play({ side: 'other', audio: ms20 });
  assert.equal(player.context.started.at(-1), 20.25);
});

test('listen plays a stream to its end and closes the audio', async () => {
  const lines = [{ type: 'format', codec: 'mulaw', rate: 8000 }, { side: 'other', audio: Buffer.alloc(80, 0xff).toString('base64') }];
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(lines.map((line) => JSON.stringify(line)).join('\n') + '\n'));
      controller.close();
    },
  });
  const contexts = [];
  class Recording extends FakeContext { constructor() { super(); contexts.push(this); } }
  const player = new ListenPlayer(Recording);
  await player.listen({ body });
  assert.equal(contexts[0].started.length, 1);
  assert.equal(contexts[0].closed, true);
  assert.equal(player.context, null);
});
