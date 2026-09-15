import asyncio
import unittest
from speech_gate import SpeechGate

class Mic:
    def __init__(self):
        self._queue = asyncio.Queue()
        self.played = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
    async def write(self, data):
        self.started.set()
        await self.release.wait()
        self.played.append(data)

class SpeechGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_muted_audio_is_discarded_and_unmute_accepts_fresh_audio(self):
        mic = Mic(); gate = SpeechGate(mic)
        gate.offer(b'old')
        self.assertTrue(gate.queue.empty())
        await gate.set_muted(False)
        task = asyncio.create_task(gate.run())
        gate.offer(b'fresh')
        await asyncio.wait_for(mic.started.wait(), 1)
        mic.release.set()
        for _ in range(10): await asyncio.sleep(0)
        self.assertEqual(mic.played, [b'fresh'])
        task.cancel(); await asyncio.gather(task, return_exceptions=True)

    async def test_remute_cancels_inflight_and_clears_both_queues(self):
        mic = Mic(); gate = SpeechGate(mic)
        await gate.set_muted(False)
        task = asyncio.create_task(gate.run())
        gate.offer(b'inflight')
        await asyncio.wait_for(mic.started.wait(), 1)
        gate.offer(b'queued')
        mic._queue.put_nowait(b'paced')
        await gate.set_muted(True)
        self.assertTrue(gate.queue.empty())
        self.assertTrue(mic._queue.empty())
        gate.offer(b'muted')
        await gate.set_muted(False)
        mic.release.set(); gate.offer(b'new')
        for _ in range(10): await asyncio.sleep(0)
        self.assertEqual(mic.played, [b'new'])
        task.cancel(); await asyncio.gather(task, return_exceptions=True)

if __name__ == '__main__': unittest.main()
