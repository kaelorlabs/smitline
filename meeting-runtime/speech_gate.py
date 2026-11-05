"""Gate outgoing speech independently of the always-on meeting audio input."""
import asyncio
import contextlib
import time


class SpeechGate:
    def __init__(self, microphone, clock=time.monotonic):
        self.microphone = microphone
        self.clock = clock
        self.muted = True
        self.generation = 0
        self.queue = asyncio.Queue(maxsize=100)
        self.writing = None
        self.output_bytes = 0
        # Audible speech, as opposed to the silence GPT-Live streams between replies: how many
        # pieces wait in the queue, whether the one being written is speech, and when the last
        # one reached the microphone. Participation.speaking() goes by these.
        self.audible_queued = 0
        self.writing_audible = False
        self.last_audible_written = float('-inf')
        # Called with each piece once the microphone has taken it (for a microphone that cannot
        # say itself what it writes; see Participation).
        self.on_written = None

    def offer(self, data, audible=False):
        if not self.muted:
            self.queue.put_nowait((self.generation, data, audible))
            if audible:
                self.audible_queued += 1

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
        self.audible_queued = 0
        # Clear paced output while muted so old chunks cannot play on the next
        # unmute. Writers that buffer audio expose this explicitly; simple test
        # or third-party writers may write synchronously and need no cleanup.
        discard = getattr(self.microphone, 'discard_pending', None)
        if discard is not None:
            discard()

    async def drain(self):
        """Wait until accepted audio has reached the output device."""
        drain = getattr(self.microphone, 'drain', None)
        if drain is not None:
            await drain()

    async def run(self):
        try:
            while True:
                generation, data, audible = await self.queue.get()
                if audible:
                    self.audible_queued = max(0, self.audible_queued - 1)
                if self.muted or generation != self.generation:
                    continue
                self.writing_audible = audible
                self.writing = asyncio.create_task(self.microphone.write(data))
                try:
                    await self.writing
                    self.output_bytes += len(data)
                    if audible:
                        self.last_audible_written = self.clock()
                    if self.on_written is not None:
                        with contextlib.suppress(Exception):  # never let it stop the voice
                            self.on_written(data)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                finally:
                    self.writing = None
                    self.writing_audible = False
        finally:
            if self.writing:
                self.writing.cancel()
