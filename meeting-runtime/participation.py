"""Transmit GPT-Live speech without making conversational decisions locally."""
import asyncio
import array
import contextlib
import time

from speech_gate import SpeechGate
from voice_activity import VoiceActivity

BYTES_PER_SECOND = 24000 * 2  # GPT-Live's meeting voice: 24 kHz, 16-bit, mono
# GPT-Live stops talking when someone talks over it but sends no event for it, and it makes
# speech faster than it plays, so the rest of what it was saying can still be waiting here. As on
# a phone call (phone_line.INTERRUPT_*): once someone in the meeting has talked this long while
# this much of the voice's speech is still to be heard, and the voice says nothing new for
# INTERRUPT_SETTLE seconds, it has stopped for them, and the rest is dropped.
INTERRUPT_MIN_SPEECH_MS = 500
INTERRUPT_MIN_QUEUED = 0.25
INTERRUPT_SETTLE = 0.35


def _waiting(queue):
    """What an asyncio.Queue holds, oldest first, without taking it out."""
    return tuple(getattr(queue, '_queue', ()))


def audible_pcm(data, threshold=160):
    """Return whether signed 16-bit PCM contains meaningful audio."""
    samples = array.array('h', data[:len(data) // 2 * 2])
    return any(abs(sample) > threshold for sample in samples)


class Participation:
    def __init__(self, adapter, microphone, state, quiet_seconds=.8, clock=time.monotonic,
                 on_presence=None, record=None):
        self.adapter, self.state = adapter, state
        self.gate = SpeechGate(microphone, clock=clock)
        # The talking picture (floorState) follows the voice as the meeting hears it: the
        # microphone's VoiceActivity, fed by each chunk it writes, or else what the gate writes.
        self.voice = getattr(microphone, 'voice', None)
        if self.voice is None:
            self.voice = VoiceActivity()
            self.gate.on_written = self.voice.feed
        self.voice.on_speaking.append(self._voice_changed)
        self.quiet_seconds = quiet_seconds
        self.clock = clock
        self.on_presence = on_presence
        self.record = record
        self.last_output_audio = float('-inf')
        # Queued as (audio, audible); audible_queued counts the pieces of speech among them.
        self.queue = asyncio.Queue(maxsize=500)
        self.audible_queued = 0
        self.lock = asyncio.Lock()
        self.platform_ready = False
        self.interrupt_settle = INTERRUPT_SETTLE
        self._their_words = None  # [start_ms, end_ms] of what someone in the meeting says now
        self._interrupt_check = None
        self.state.update(
            muted=True,
            microphoneState='muted',
            floorState='listening',
            generated_audio_bytes=0,
            discarded_audio_bytes=0,
            audible_audio_bytes=0,
            interruptionsFollowed=0,
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
            self.queue.put_nowait((data, audible))
        except asyncio.QueueFull:
            self.state['discarded_audio_bytes'] += len(data)
            return
        if audible:
            self.audible_queued += 1

    def _discard_pending(self):
        while not self.queue.empty():
            data, audible = self.queue.get_nowait()
            self.state['discarded_audio_bytes'] += len(data)
            if audible:
                self.audible_queued = max(0, self.audible_queued - 1)

    def _arm_platform_microphone(self):
        self.platform_ready = True
        self.state['microphoneState'] = 'open'
        self.state.pop('error', None)

    async def _open_platform_microphone(self):
        try:
            await self.adapter.unmute()
            if await self.adapter.get_microphone_state() != 'open':
                raise RuntimeError('Microphone opening could not be confirmed')
            self._arm_platform_microphone()
        except Exception:
            self.platform_ready = False
            self.state.update(microphoneState='blocked', floorState='platform_muted')
            self.state['error'] = 'Microphone unavailable. Check the meeting microphone permissions.'
            if self.on_presence:
                self.on_presence()

    async def platform_microphone_changed(self, actual):
        """Respect a host mute. Resume playback only after the operator unmutes."""
        if actual == 'open':
            if self.platform_ready:
                return False
            self._arm_platform_microphone()
            if self.on_presence:
                self.on_presence()
            return True
        if not self.platform_ready:
            return await self.accept_host_unmute(actual)
        self.platform_ready = False
        async with self.lock:
            self._discard_pending()
            await self.gate.set_muted(True)
            self.state.update(muted=True, microphoneState=actual, floorState='platform_muted')
        self.state['error'] = 'The meeting microphone was muted. Smitline will not override it.'
        if self.on_presence:
            self.on_presence()
        return True

    async def accept_host_unmute(self, actual):
        """Unmute only when the host explicitly asks, e.g. Zoom's "Ask to unmute" dialog."""
        accept = getattr(self.adapter, 'accept_unmute_request', None)
        if actual not in ('muted', 'blocked') or accept is None:
            return False
        try:
            if not await accept():
                return False
            for _ in range(20):
                if await self.adapter.get_microphone_state() == 'open':
                    break
                await asyncio.sleep(.1)
            else:
                return False
        except Exception:
            return False
        self._arm_platform_microphone()
        if self.record is not None:
            self.record.event('host_unmute_accepted')
        if self.on_presence:
            self.on_presence()
        return True

    def _voice_changed(self, speaking):
        """floorState is speaking from the first audible chunk written to the meeting until
        voice_activity.RELEASE_SECONDS after the last, whatever GPT-Live has delivered: it
        delivers faster than real time and streams silence between replies."""
        floor = self.state.get('floorState')
        if speaking and floor != 'speaking':
            self.state['floorState'] = 'speaking'
        elif not speaking and floor == 'speaking':
            self.state['floorState'] = 'listening'
        else:
            return
        if self.on_presence:
            self.on_presence()

    def speaking(self):
        """Speech is still waiting, being written, or was produced or written within quiet_seconds.

        Only audible audio counts. GPT-Live streams silence between replies, which keeps the
        queues and the paced writer busy; counting that as speech meant a meeting agent that
        said goodbye never left (the 2026-11-05 Zoom trial).
        """
        gate = self.gate
        last_speech = max(self.last_output_audio, gate.last_audible_written)
        return (self.audible_queued > 0 or gate.audible_queued > 0 or gate.writing_audible
                or self.clock() - last_speech <= self.quiet_seconds)

    async def finish_speaking(self, timeout, settle=1.0):
        """Wait for what the voice is saying (a goodbye) to be heard, at most `timeout` seconds.
        `settle` gives a reply that is still being generated time to start arriving."""
        await asyncio.sleep(settle)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, timeout - settle)
        while self.speaking() and loop.time() < deadline:
            await asyncio.sleep(.05)

    async def stop_output(self, mute_platform=False):
        async with self.lock:
            self._discard_pending()
            await self.gate.set_muted(True)
            self.state['muted'] = True
            if mute_platform:
                # Once the agent has left the meeting there is no microphone there to mute
                # (adapters_base.leave_meeting); trying took Zoom three seconds.
                if not getattr(self.adapter, 'left_meeting', False):
                    with contextlib.suppress(Exception):
                        await self.adapter.mute()
                self.platform_ready = False
                self.state['microphoneState'] = 'muted'

    def unsaid_seconds(self):
        """How much of the voice's speech is still to be heard: what waits at the gate, then
        here, up to its last audible piece (silence after that does not count)."""
        waiting = [(data, audible) for _generation, data, audible in _waiting(self.gate.queue)]
        waiting += _waiting(self.queue)
        total = unsaid = 0
        for data, audible in waiting:
            total += len(data)
            if audible:
                unsaid = total
        return unsaid / BYTES_PER_SECOND

    def heard(self, start_ms=None, end_ms=None):
        """Someone in the meeting is talking: a piece of GPT-Live's input transcript, with its
        times. Once they have talked for INTERRUPT_MIN_SPEECH_MS while the voice still has
        INTERRUPT_MIN_QUEUED seconds to say, it is checked INTERRUPT_SETTLE seconds later; if it
        has said nothing new, it stopped for them, and the rest is dropped (drop_unsaid). A short
        "mhm" does not stop it, and neither does talk it carries on over."""
        words = self._their_words
        if words is None:
            words = self._their_words = [start_ms, end_ms]
        elif end_ms is not None:
            words[1] = end_ms
        if self._interrupt_check is not None and not self._interrupt_check.done():
            return
        if self.unsaid_seconds() < INTERRUPT_MIN_QUEUED:
            return
        if None not in words and words[1] - words[0] < INTERRUPT_MIN_SPEECH_MS:
            return
        self._interrupt_check = asyncio.create_task(
            self._follow_interruption(self.clock()), name='meeting_interruption')

    def said(self):
        """A piece of the voice's own transcript: what the meeting says next starts anew."""
        self._their_words = None

    async def _follow_interruption(self, spoke_at):
        await asyncio.sleep(self.interrupt_settle)
        if self.last_output_audio > spoke_at:
            return  # it carried on talking: GPT-Live chose to
        if self.unsaid_seconds() < INTERRUPT_MIN_QUEUED:
            return
        unsaid = await self.drop_unsaid()
        self.state['interruptionsFollowed'] = self.state.get('interruptionsFollowed', 0) + 1
        if self.record is not None:
            self.record.event('interrupted', unsaid_ms=int(unsaid * 1000))

    async def drop_unsaid(self):
        """Someone talked over the voice and it stopped: drop what it has not said yet, here, at
        the gate and in the microphone's buffer. Its next reply opens the gate again. Returns the
        seconds of speech dropped."""
        async with self.lock:
            unsaid = self.unsaid_seconds()
            self._discard_pending()
            await self.gate.set_muted(True)
            self.state['muted'] = True
            if self.state.get('floorState') == 'speaking':
                self.state['floorState'] = 'listening'
        if self.on_presence:
            self.on_presence()
        return unsaid

    async def run(self):
        writer = asyncio.create_task(self.gate.run())
        await self._open_platform_microphone()
        try:
            while True:
                try:
                    data, audible = await asyncio.wait_for(self.queue.get(), .05)
                except TimeoutError:
                    if (self.clock() - self.last_output_audio > self.quiet_seconds and
                            self.gate.queue.empty() and not self.gate.writing and not self.gate.muted):
                        async with self.lock:
                            await self.gate.drain()
                            self.state['output_bytes'] = self.gate.output_bytes
                            await asyncio.sleep(.04)
                            if (self.queue.empty() and
                                    self.clock() - self.last_output_audio > self.quiet_seconds):
                                await self.gate.set_muted(True)
                                self.state.update(muted=True, floorState='listening')
                                if self.on_presence:
                                    self.on_presence()
                    continue
                try:
                    async with self.lock:
                        actual = await self.adapter.get_microphone_state()
                        if actual == 'open' and not self.platform_ready:
                            self._arm_platform_microphone()
                        if not self.platform_ready:
                            self.state['discarded_audio_bytes'] += len(data)
                            continue
                        if actual != 'open':
                            self.platform_ready = False
                            self.state['discarded_audio_bytes'] += len(data)
                            self.state.update(muted=True, microphoneState=actual, floorState='platform_muted')
                            self.state['error'] = 'The meeting microphone was muted. Smitline will not override it.'
                            if self.on_presence:
                                self.on_presence()
                            continue
                        if self.gate.muted:
                            await self.gate.set_muted(False)
                            self.state['muted'] = False
                        # The gate counts this piece of speech from here on.
                        self.gate.offer(data, audible=audible)
                finally:
                    if audible:
                        self.audible_queued = max(0, self.audible_queued - 1)
                self.state['output_bytes'] = self.gate.output_bytes
        finally:
            if self._interrupt_check is not None:
                self._interrupt_check.cancel()
            await self.stop_output(mute_platform=True)
            self.state['output_bytes'] = self.gate.output_bytes
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
