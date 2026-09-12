"""Gate outgoing speech independently of the always-on meeting audio input."""
import asyncio
import contextlib


class SpeechGate:
    def __init__(self, microphone):
        self.microphone = microphone
        self.muted = True
        self.generation = 0
        self.queue = asyncio.Queue(maxsize=100)
        self.writing = None
        self.output_bytes = 0

    def offer(self, data):
        if not self.muted:
            self.queue.put_nowait((self.generation, data))

    async def set_muted(self, muted):
        if muted == self.muted:
            return
        self.muted = muted
        self.generation += 1
        if self.writing:
            self.writing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.writing
        while not self.queue.empty():
            self.queue.get_nowait()
        # Joinly's virtual microphone has a two-chunk paced queue. Clear it
        # while Zoom is muted so those chunks cannot play on the next unmute.
        pending = self.microphone._queue
        if pending is not None:
            while not pending.empty():
                pending.get_nowait()
                pending.task_done()

    async def run(self):
        try:
            while True:
                generation, data = await self.queue.get()
                if self.muted or generation != self.generation:
                    continue
                self.writing = asyncio.create_task(self.microphone.write(data))
                try:
                    await self.writing
                    self.output_bytes += len(data)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                finally:
                    self.writing = None
        finally:
            if self.writing:
                self.writing.cancel()
