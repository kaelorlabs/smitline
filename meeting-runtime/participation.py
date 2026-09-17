"""Transmit GPT-Live speech without making conversational decisions locally."""
import asyncio
import array
import contextlib
import time

from speech_gate import SpeechGate


def audible_pcm(data, threshold=160):
    """Return whether signed 16-bit PCM contains meaningful audio."""
    samples = array.array('h', data[:len(data) // 2 * 2])
    return any(abs(sample) > threshold for sample in samples)


class Participation:
    def __init__(self, adapter, microphone, state, quiet_seconds=.8, clock=time.monotonic,
                 on_presence=None):
        self.adapter, self.state = adapter, state
        self.gate = SpeechGate(microphone)
        self.quiet_seconds = quiet_seconds
        self.clock = clock
        self.on_presence = on_presence
        self.last_output_audio = float('-inf')
        self.queue = asyncio.Queue(maxsize=500)
        self.lock = asyncio.Lock()
        self.platform_ready = False
        self.state.update(
            muted=True,
            microphoneState='muted',
            floorState='listening',
            generated_audio_bytes=0,
            discarded_audio_bytes=0,
            audible_audio_bytes=0,
        )

    def offer(self, data):
        """Queue speech exactly when GPT-Live chooses to produce it."""
        self.state['generated_audio_bytes'] += len(data)
        audible = audible_pcm(data, threshold=80)
        now = self.clock()
        if audible:
            self.last_output_audio = now
            self.state['audible_audio_bytes'] += len(data)
        if not audible and self.queue.empty() and self.gate.muted:
            return
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            self.state['discarded_audio_bytes'] += len(data)

    def _discard_pending(self):
        while not self.queue.empty():
            data = self.queue.get_nowait()
            self.state['discarded_audio_bytes'] += len(data)

    async def _open_platform_microphone(self):
        try:
            await self.adapter.unmute()
            if await self.adapter.get_microphone_state() != 'open':
                raise RuntimeError('Microphone opening could not be confirmed')
            self.platform_ready = True
            self.state['microphoneState'] = 'open'
            self.state.pop('error', None)
        except Exception:
            self.platform_ready = False
            self.state.update(microphoneState='blocked', floorState='platform_muted')
            self.state['error'] = 'Microphone unavailable. Check the meeting microphone permissions.'
            if self.on_presence:
                self.on_presence()

    async def platform_microphone_changed(self, actual):
        """Respect a host or participant mute instead of reopening it automatically."""
        if self.platform_ready and actual != 'open':
            self.platform_ready = False
            async with self.lock:
                self._discard_pending()
                await self.gate.set_muted(True)
                self.state.update(muted=True, microphoneState=actual, floorState='platform_muted')
            self.state['error'] = 'The meeting microphone was muted. Colleague AI will not override it.'
            if self.on_presence:
                self.on_presence()
            return True
        return False

    async def stop_output(self, mute_platform=False):
        async with self.lock:
            self._discard_pending()
            await self.gate.set_muted(True)
            self.state['muted'] = True
            if mute_platform:
                with contextlib.suppress(Exception):
                    await self.adapter.mute()
                self.platform_ready = False
                self.state['microphoneState'] = 'muted'

    async def run(self):
        writer = asyncio.create_task(self.gate.run())
        await self._open_platform_microphone()
        try:
            while True:
                try:
                    data = await asyncio.wait_for(self.queue.get(), .05)
                except TimeoutError:
                    if (self.clock() - self.last_output_audio > self.quiet_seconds and
                            self.gate.queue.empty() and not self.gate.writing and not self.gate.muted):
                        async with self.lock:
                            pending = self.gate.microphone._queue
                            if pending is not None:
                                await pending.join()
                            await asyncio.sleep(.04)
                            if (self.queue.empty() and
                                    self.clock() - self.last_output_audio > self.quiet_seconds):
                                await self.gate.set_muted(True)
                                self.state.update(muted=True, floorState='listening')
                                if self.on_presence:
                                    self.on_presence()
                    continue
                async with self.lock:
                    if not self.platform_ready:
                        self.state['discarded_audio_bytes'] += len(data)
                        continue
                    actual = await self.adapter.get_microphone_state()
                    if actual != 'open':
                        self.platform_ready = False
                        self.state['discarded_audio_bytes'] += len(data)
                        self.state.update(muted=True, microphoneState=actual, floorState='platform_muted')
                        self.state['error'] = 'The meeting microphone was muted. Colleague AI will not override it.'
                        if self.on_presence:
                            self.on_presence()
                        continue
                    if self.gate.muted:
                        await self.gate.set_muted(False)
                        self.state.update(muted=False, floorState='speaking')
                        if self.on_presence:
                            self.on_presence()
                    self.gate.offer(data)
                self.state['output_bytes'] = self.gate.output_bytes
        finally:
            await self.stop_output(mute_platform=True)
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
