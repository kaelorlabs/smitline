"""Daily rooms: a meeting agent joins a Daily room through daily-python, with no browser.

Daily (daily.co) hosts the room and its join page, so people need only the link. The agent joins
from this process as an ordinary participant: GPT-Live's voice goes out through a virtual
microphone and the room comes in through a virtual speaker. Only microphones are received; remote
cameras and screen shares are never decoded, which is most of what a browser spends its CPU on.
The camera tile is drawn here (DailyCamera): a presence pack's clips, or the still mark.

DailyRoom gives bridge.run_voice, Participation and meeting_control what the browser adapters give
them, and joined_room is the Daily counterpart of meeting_connection.joined_meeting.
"""
import asyncio
import contextlib
from contextlib import asynccontextmanager
import io
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from adapters_base import Capabilities, MeetingPlatformAdapter, leave_meeting
from joinly.types import AudioChunk, AudioFormat
from presence_pack import load_presence_pack
from visual_presence import VISUAL_STATES
from voice_activity import VoiceActivity

SAMPLE_RATE = 24000
BYTE_DEPTH = 2
SPEAKER_FRAMES = 480          # 20 ms: the chunk bridge.send_audio hands GPT-Live
SPEAKER_QUEUE_CHUNKS = 50     # 1 s; a reader that falls behind loses the oldest audio first
MIC_CHUNK_MS = 10
MIC_LEAD_SECONDS = 0.04       # how far ahead of real time the voice is handed to daily-python
JOIN_TIMEOUT = 60
CALL_TIMEOUT = 10
CAMERA_OFF_TIMEOUT = 3
# The browser adapters wait this long in a lobby; a room waits as long for its first person.
ADMISSION_TIMEOUT = 600
ADMISSION_POLL = 0.5
MICROPHONE = 'smitline-microphone'
SPEAKER = 'smitline-speaker'
CAMERA = 'smitline-camera'
STILL_SIZE = (640, 360)
STILL_FPS = 2
# A new clip blends in from the last one over this long, so a change of state never jumps; into and
# out of a clip with a mouth (speaking) it is quicker, so the mouth moves with the first word.
BLEND_SECONDS = 0.25
QUICK_BLEND_SECONDS = 0.08
# A looping clip with at most this many distinct pictures (the speaking clip) is decoded once and
# kept; other frames are decoded as they are shown, which keeps a 1280x720 pack's memory small.
KEEP_PICTURES = 12
# With no presence pack, the still picture stays this long once the meeting has ended.
ENDED_HOLD_SECONDS = 1.0
# One sharp layer for the camera tile. With simulcast, a room can be sent the smallest layer
# (160x90 for a 640x360 pack, seen in a test on 2026-11-05) and show it blown up. The cap leaves
# the fleece room at 24 fps; WebRTC sends less when the network has less. A 1280x720 pack gets more.
CAMERA_BITRATE = 1_500_000
CAMERA_BITRATE_HD = 2_500_000
# The agent listens: microphones and screen-share audio only, never anyone's video.
SUBSCRIPTIONS = {'base': {'camera': 'unsubscribed', 'screenVideo': 'unsubscribed',
                          'microphone': 'subscribed', 'screenAudio': 'subscribed'}}
# GPT-Live's voice arrives clean; a browser's processing of a real microphone would only change it.
RAW_AUDIO = {'autoGainControl': {'exact': False}, 'echoCancellation': {'exact': False},
             'noiseSuppression': {'exact': False}}
MICROPHONE_INPUT = {'isEnabled': True,
                    'settings': {'deviceId': MICROPHONE, 'customConstraints': RAW_AUDIO}}
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'

_initialized = False


def camera_bitrate(width, height):
    """The camera's one layer: CAMERA_BITRATE up to 640x360, CAMERA_BITRATE_HD above."""
    return CAMERA_BITRATE_HD if width * height > 640 * 360 else CAMERA_BITRATE


def daily_sdk():
    """daily-python, initialized once per process and quiet below warnings."""
    global _initialized
    import daily
    if not _initialized:
        daily.Daily.init()
        with contextlib.suppress(Exception):
            daily.Daily.set_log_level(daily.LogLevel.Warn)
        _initialized = True
    return daily


def room_and_token(url):
    """A link may carry a meeting token (?t=...), which daily-python takes apart from the room."""
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    token = (query.pop('t', None) or [None])[0] or None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), '')), token


class _Completion:
    """A daily-python completion callback as something to await; it may fire on Daily's threads."""

    def __init__(self, loop):
        self.loop = loop
        self.future = loop.create_future()

    def __call__(self, *args):
        with contextlib.suppress(RuntimeError):  # the event loop has closed: nobody is waiting
            self.loop.call_soon_threadsafe(self._resolve, args)

    def _resolve(self, args):
        if not self.future.done():
            self.future.set_result(args)

    async def wait(self, timeout):
        return await asyncio.wait_for(self.future, timeout)


class DailyRoom(MeetingPlatformAdapter):
    """A Daily room through daily-python: the adapter methods the voice loop and Participation use."""
    platform_id = 'daily'
    capabilities = Capabilities(text_chat=True, participant_discovery=True, camera=True)
    # Daily says who is speaking without a page to read, so the voice loop can ask often.
    speaker_poll_seconds = 0.4

    def __init__(self, stop, stage, *, sdk=None, camera_size=STILL_SIZE):
        super().__init__(None, stop, stage)
        self.sdk = sdk or daily_sdk()
        daily = self.sdk.Daily
        self.microphone_device = daily.create_microphone_device(
            MICROPHONE, sample_rate=SAMPLE_RATE, channels=1, non_blocking=True)
        self.speaker_device = daily.create_speaker_device(
            SPEAKER, sample_rate=SAMPLE_RATE, channels=1, non_blocking=True)
        daily.select_speaker_device(SPEAKER)
        self.camera_device = None
        self.camera_size = camera_size
        if camera_size:
            width, height = camera_size
            self.camera_device = daily.create_camera_device(
                CAMERA, width=width, height=height, color_format='RGBA')
        self.name = ''
        self.loop = None
        self.admission_timeout = ADMISSION_TIMEOUT
        self.admission_poll = ADMISSION_POLL
        self.call_state = 'initialized'
        self.ended = False
        self.leaving = False
        self.released = False
        self.errors = []
        room = self

        class Events(self.sdk.EventHandler):
            def on_call_state_updated(self, call_state):
                room._from_daily(room._call_state_changed, call_state)

            def on_error(self, message):
                room._from_daily(room.errors.append, str(message)[:300])

        self.client = self.sdk.CallClient(event_handler=Events())

    def _from_daily(self, handler, *args):
        """Run an event's handler on the event loop; daily-python calls from its own threads."""
        loop = self.loop
        if loop is None or loop.is_closed():
            handler(*args)
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(handler, *args)

    def _call_state_changed(self, call_state):
        self.call_state = call_state
        if call_state == 'left' and not self.leaving:
            # Ejected, the room expired, or the connection gave up: the meeting is over for us.
            self.ended = True

    async def join(self, url, name, passcode=''):
        self.validate_url(url)
        self.loop = asyncio.get_running_loop()
        self.name = name
        room_url, token = room_and_token(url)
        self.stage('opening_meeting')
        self.client.set_user_name(name)
        self.client.update_subscription_profiles(SUBSCRIPTIONS)
        camera = {'isEnabled': False}
        if self.camera_device is not None:
            camera['settings'] = {'deviceId': CAMERA}
        settings = {'inputs': {'camera': camera, 'microphone': MICROPHONE_INPUT}}
        self.stage('joining')
        done = _Completion(self.loop)
        self.client.join(room_url, meeting_token=token, client_settings=settings, completion=done)
        try:
            _data, error = await done.wait(JOIN_TIMEOUT)
        except TimeoutError:
            raise RuntimeError('Daily did not let Smitline into the room within a minute') from None
        if error:
            raise RuntimeError(f'Daily refused to join the room: {str(error)[:200]}')
        self.call_state = 'joined'
        await self._wait_for_someone()
        self.stage('admitted')

    def _others(self):
        """The people in the room besides the agent; Daily lists hidden participants (its own
        recorders, bots joined hidden) only in participant_counts, so they never count."""
        try:
            people = self.client.participants() or {}
        except Exception:
            return []
        return [person for key, person in people.items()
                if key != 'local' and not (person.get('info') or {}).get('isLocal')]

    async def _wait_for_someone(self):
        """The agent is usually in a room before the people it sent the link to. It waits for one
        of them before it counts as admitted, so the voice, the paid part, starts only when someone
        is there, and leaving when alone works as in any meeting once they have gone."""
        if self._others():
            return
        self.stage('waiting_for_admission')
        deadline = self.loop.time() + self.admission_timeout
        while not self.stop.is_set() and self.loop.time() < deadline:
            if self.ended:
                raise RuntimeError('The Daily room ended before anyone joined')
            await asyncio.sleep(self.admission_poll)
            if self._others():
                return
        raise RuntimeError('Nobody joined the Daily room in time, or it was stopped')

    async def _update_inputs(self, inputs):
        done = _Completion(self.loop)
        self.client.update_inputs(inputs, completion=done)
        (error,) = await done.wait(CALL_TIMEOUT)
        if error:
            raise RuntimeError(f'Daily refused the change: {str(error)[:200]}')

    async def send_one_sharp_layer(self, fps):
        """Publish the camera as a single full-size layer instead of simulcast's three, so nobody
        is sent the 160x90 one. Best effort: Daily keeps its defaults if it refuses."""
        if self.camera_device is None:
            return False
        # Daily numbers layers up from "low"; one layer is "low" alone, at full size.
        settings = {'camera': {'sendSettings': {
            'maxQuality': 'low',
            'allowAdaptiveLayers': False,
            'encodings': {'low': {
                'maxBitrate': camera_bitrate(*self.camera_size), 'maxFramerate': fps,
                'scaleResolutionDownBy': 1}},
        }}}
        done = _Completion(self.loop)
        try:
            self.client.update_publishing(settings, completion=done)
            answer = await done.wait(CALL_TIMEOUT)
            error = answer[-1] if answer else None
        except Exception as exc:
            error = exc
        if error:
            self.errors.append(f'camera publishing: {str(error)[:200]}')
            return False
        return True

    def _input_enabled(self, kind):
        try:
            return bool(((self.client.inputs() or {}).get(kind) or {}).get('isEnabled'))
        except Exception:
            return None

    def _participants(self):
        try:
            return list((self.client.participants() or {}).values())
        except Exception:
            return None

    async def get_microphone_state(self):
        if self.call_state != 'joined':
            return 'unknown'
        enabled = self._input_enabled('microphone')
        if enabled is None:
            return 'unknown'
        # An owner muting the agent from the room turns its microphone input off; respected.
        return 'open' if enabled else 'muted'

    async def mute(self):
        await self._update_inputs({'microphone': {'isEnabled': False}})

    async def unmute(self):
        await self._update_inputs({'microphone': MICROPHONE_INPUT})

    async def connect_audio(self):
        if self.call_state != 'joined':
            raise RuntimeError('Smitline is not in the Daily room')

    async def get_camera_state(self):
        if self.camera_device is None:
            return 'off'
        enabled = self._input_enabled('camera')
        return 'unknown' if enabled is None else ('on' if enabled else 'off')

    async def enable_camera(self):
        if self.camera_device is None:
            raise RuntimeError('This room has no camera picture')
        await self._update_inputs({'camera': {'isEnabled': True, 'settings': {'deviceId': CAMERA}}})

    async def disable_camera(self):
        await self._update_inputs({'camera': {'isEnabled': False}})

    async def turn_camera_off(self):
        """The camera goes off when the agent is done, so nobody is left looking at a still frame.
        Only while in the room and not leaving; never raises."""
        if self.camera_device is None or self.call_state != 'joined' or self.leaving:
            return
        if self._input_enabled('camera') is False:
            return  # already off
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.disable_camera(), CAMERA_OFF_TIMEOUT)

    async def get_participant_count(self):
        if self.call_state != 'joined':
            return None
        people = self._participants()
        return None if people is None else len(people)

    async def get_participant_names(self):
        names = [((person.get('info') or {}).get('userName') or '').strip()
                 for person in self._participants() or []]
        return [name for name in names if name]

    async def get_active_speaker(self):
        try:
            speaker = self.client.active_speaker() or {}
        except Exception:
            return None
        info = speaker.get('info') or {}
        if info.get('isLocal'):
            return None
        return (info.get('userName') or '').strip() or None

    async def chat_available(self):
        return self.call_state == 'joined'

    async def send_chat_message(self, text):
        done = _Completion(self.loop)
        self.client.send_prebuilt_chat_message(str(text), user_name=self.name or None, completion=done)
        (error,) = await done.wait(CALL_TIMEOUT)
        if error:
            raise RuntimeError(f'Daily refused the chat message: {str(error)[:200]}')

    async def has_ended(self):
        return self.ended

    async def leave(self):
        """Leave the room; raises when Daily does not confirm it. Once out (left, ejected, never
        in), returns at once. release() frees daily-python after the devices have stopped."""
        self.leaving = True  # from here a 'left' call state is ours, not an ejection
        if self.loop is None or self.call_state not in ('joining', 'joined'):
            return
        done = _Completion(self.loop)
        self.client.leave(completion=done)
        answer = await done.wait(CALL_TIMEOUT)
        error = answer[-1] if answer else None
        if error:
            raise RuntimeError(f'Daily did not let Smitline leave the room: {str(error)[:200]}')
        self.call_state = 'left'

    def release(self):
        """Free daily-python's resources, once; never raises."""
        if self.released:
            return
        self.released = True
        with contextlib.suppress(Exception):
            self.client.release()


class DailySpeaker:
    """The room's audio for GPT-Live: joinly AudioChunks of 20 ms, 24 kHz 16-bit mono.

    GPT-Live keeps time by its input, so the stream never stops: every 20 ms it takes what Daily's
    non-blocking speaker has (nothing at all while nobody in the room has a microphone on, because
    Daily then plays nothing) and fills the rest with silence, as a sound card would. Without that
    the voice never spoke and GPT-Live closed the session ("context_injection_incomplete")."""

    def __init__(self, device, *, frames=SPEAKER_FRAMES, max_chunks=SPEAKER_QUEUE_CHUNKS):
        self.device = device
        self.frames = frames
        self.audio_format = AudioFormat(sample_rate=SAMPLE_RATE, byte_depth=BYTE_DEPTH)
        self.chunk_size = frames * BYTE_DEPTH
        self.period = frames / SAMPLE_RATE
        self.dropped = 0
        self._queue = asyncio.Queue(maxsize=max_chunks)
        self._task = None
        self._pending = b''
        self._time_ns = 0
        self._chunk_ns = frames * 1_000_000_000 // SAMPLE_RATE

    def start(self):
        self._task = asyncio.create_task(self._pace(), name='daily-speaker')

    def close(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def next_chunk(self):
        """One chunk: what Daily has played since the last one, then silence to fill it."""
        try:
            data = self.device.read_frames(self.frames)
        except Exception:
            data = b''
        pending = self._pending + bytes(data or b'')
        # Never fall more than ten chunks behind the room: older audio is dropped first.
        if len(pending) > self.chunk_size * 10:
            pending = pending[-self.chunk_size * 10:]
        chunk, self._pending = pending[:self.chunk_size], pending[self.chunk_size:]
        return chunk + b'\x00' * (self.chunk_size - len(chunk))

    async def _pace(self):
        loop = asyncio.get_running_loop()
        due = loop.time()
        while True:
            due += self.period
            delay = due - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.2:
                due = loop.time()  # the loop stalled: carry on from now rather than burst
            self._put(self.next_chunk())

    def _put(self, data):
        if self._queue.full():
            self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(data)

    async def read(self):
        data = await self._queue.get()
        chunk = AudioChunk(data=data, time_ns=self._time_ns)
        self._time_ns += self._chunk_ns
        return chunk


class DailyMicrophone:
    """GPT-Live's voice into the room, paced like joinly's VirtualMicrophone.

    write() returns once its audio is queued, at most two 10 ms chunks ahead, so the speech gate's
    "writing" follows what is playing. Chunks go to daily-python's non-blocking microphone no more
    than MIC_LEAD_SECONDS ahead of real time; between turns nothing is written and Daily sends
    silence. on_chunk, like VirtualMicrophone's, sees each chunk as it goes to Daily."""

    def __init__(self, device, *, chunk_ms=MIC_CHUNK_MS, queue_size=2, lead=MIC_LEAD_SECONDS):
        self.device = device
        self.audio_format = AudioFormat(sample_rate=SAMPLE_RATE, byte_depth=BYTE_DEPTH)
        self.chunk_size = SAMPLE_RATE * chunk_ms // 1000 * BYTE_DEPTH
        self.period = chunk_ms / 1000
        self.lead = lead
        self.on_chunk = None
        self._queue = asyncio.Queue(maxsize=queue_size)
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._pace(), name='daily-microphone')

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def write(self, data):
        view = memoryview(data)
        while len(view) >= self.chunk_size:
            await self._queue.put(bytes(view[:self.chunk_size]))
            view = view[self.chunk_size:]
        if view:
            await self._queue.put(bytes(view) + b'\x00' * (self.chunk_size - len(view)))

    async def drain(self):
        await self._queue.join()

    def discard_pending(self):
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()

    async def _pace(self):
        loop = asyncio.get_running_loop()
        playhead = 0.0  # loop time up to which audio has been handed over
        while True:
            chunk = await self._queue.get()
            try:
                now = loop.time()
                playhead = max(playhead, now)
                if playhead - now > self.lead:
                    await asyncio.sleep(playhead - now - self.lead)
                if self.on_chunk is not None:
                    # The chunks queued behind it are read from the queue's deque, not taken.
                    with contextlib.suppress(Exception):
                        self.on_chunk(chunk, tuple(getattr(self._queue, '_queue', ())))
                self.device.write_frames(chunk)
                playhead += self.period
            finally:
                self._queue.task_done()


# The states in which the character is on stage; the first of them brings it on with the arrival.
PRESENT_STATES = frozenset({'listening', 'working', 'speaking', 'needs_attention'})


class ClipPlayer:
    """Which frame of which clip a presence pack shows, one tick at a time.

    A state's clip starts from its first frame and a looping clip repeats. A one-shot clip (the
    arrival, a goodbye) is never cut short: the newest state's clip waits until it ends, and with
    nothing after it the last frame holds. A state that shows the clip already on screen lets it
    carry on. The arrival plays once, the first time the agent is present; a character that never
    arrived stays off stage when the meeting ends. `settled` is true once the clip shown last has
    played to its end with nothing after it. The browser camera (camera_feed.py) follows the same
    rules."""

    def __init__(self, pack):
        self.pack = pack
        self.state = 'joining'
        self.clip = pack.clip_for('joining')
        self.index = 0
        self.queue = []
        self.arrived = False
        self.settled = False

    def _length(self, clip):
        return len(self.pack.clips[clip].frames)

    def _one_shot_running(self):
        return not self.pack.clips[self.clip].loop and self.index + 1 < self._length(self.clip)

    def _clips_for(self, state):
        if state in PRESENT_STATES:
            clips = [self.pack.clip_for(state)]
            if not self.arrived:
                self.arrived = True
                if self.pack.arrival:
                    clips.insert(0, self.pack.arrival)
            return clips
        if not self.arrived:
            return [self.pack.clip_for('joining')]  # it never walked in, so it cannot leave
        return [self.pack.clip_for(state)]

    def set_state(self, state):
        if state == self.state:
            return
        self.state = state
        self.settled = False
        clips = self._clips_for(state)
        if clips[0] == self.clip:
            self.queue = clips[1:]  # carries on: a loop where it is, a one-shot to its end
        elif self._one_shot_running():
            self.queue = clips
        else:
            self.clip, self.index, self.queue = clips[0], 0, clips[1:]

    def tick(self):
        """The (clip, frame index) to show now; then moves on by one frame."""
        shown = (self.clip, self.index)
        last = self.index + 1 >= self._length(self.clip)
        self.settled = last and not self.queue
        if not last:
            self.index += 1
        elif self.queue:
            self.clip, self.index = self.queue.pop(0), 0
        elif self.pack.clips[self.clip].loop:
            self.index = 0
        return shown


def _caption_font(size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(FONT, size)
    except OSError:
        return ImageFont.load_default(size=size)


class DailyCamera:
    """The camera tile in a Daily room, drawn here and written to Daily's virtual camera.

    It stands where CameraFeed stands for a browser: Presence calls set_visual_state, and run_voice
    writes the voice through audio_writer. With a presence pack it plays the pack's clips; without
    one (or for the animated style, which needs a browser) it shows the still mark. Frames are
    decoded as they are shown (a few milliseconds), so a change of clip never stalls the picture;
    a small looping clip with a mouth (speaking) and its mouth pictures are decoded once, before the
    first frame, so the first word never waits. A new clip blends in over BLEND_SECONDS, or
    QUICK_BLEND_SECONDS into and out of a clip with a mouth. While such a clip shows, `voice` (a
    VoiceActivity) picks each frame's mouth picture. Once the meeting has ended and its last clip has
    played, the frames stop and `turn_off` turns the room's camera off, so nobody is left with a
    still frame. Frames go out from a thread of their own at the pack's frame rate."""

    def __init__(self, device, writer, *, enabled=True, pack=None, caption=None, voice=None,
                 turn_off=None):
        self.device = device
        self.audio_writer = writer
        self.enabled = bool(enabled and device is not None)
        self.pack = pack
        self.caption = caption
        self.voice = voice
        self.turn_off = turn_off
        self.player = ClipPlayer(pack) if pack is not None else None
        self.fps = pack.fps if pack is not None else STILL_FPS
        self.size = (pack.width, pack.height) if pack is not None else STILL_SIZE
        self.blend_frames = max(1, round(BLEND_SECONDS * self.fps))
        self.quick_blend_frames = max(1, round(QUICK_BLEND_SECONDS * self.fps))
        self.failures = 0
        self.off = False
        self.frames_written, self.late_frames, self.longest_gap = 0, 0, 0.0
        self._state = 'joining'
        self._ended_frames = 0      # frames shown since the meeting ended
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._loop = None
        self._kept = {}             # picture bytes -> its decoded picture, for clips decoded once
        self._mouths = {}           # clip -> its decoded mouth pictures
        self._still = None
        self._caption_band = None   # (top, layer): the caption over the frame's bottom band
        self._shown = None          # (clip, index) last shown
        self._last_picture = None   # its picture, before the caption
        self._fade_from = None      # the picture a new clip blends in over
        self._fade_left = 0
        self._fade_frames = self.blend_frames
        self._blended = False       # whether the last frame was a blend of two clips

    def start(self):
        if self.enabled:
            with contextlib.suppress(RuntimeError):
                self._loop = asyncio.get_running_loop()
            threading.Thread(target=self._run, name='daily-camera', daemon=True).start()

    async def install(self, _page):
        return None

    async def stop(self):
        """No more frames, and the room's camera off unless the end of the meeting turned it off."""
        self._closed.set()
        if not self.off:
            self.off = True
            if self.enabled and self.turn_off is not None:
                with contextlib.suppress(Exception):
                    await self.turn_off()

    def set_visual_state(self, name):
        with self._lock:
            self._state = name if name in VISUAL_STATES else 'listening'

    def next_frame(self):
        """The RGBA bytes to show now."""
        with self._lock:
            state = self._state
        self._ended_frames = self._ended_frames + 1 if state == 'ended' else 0
        if self.player is None:
            return self._still_frame()
        self.player.set_state(state)
        clip, index = self.player.tick()
        if self._shown is not None and clip != self._shown[0] and self._last_picture is not None:
            # A new clip blends in from wherever the last one was; quickly to or from a mouth.
            quick = self._mouth(clip) is not None or self._mouth(self._shown[0]) is not None
            self._fade_frames = self.quick_blend_frames if quick else self.blend_frames
            self._fade_from, self._fade_left = self._last_picture, self._fade_frames
        self._shown = (clip, index)
        picture = self._with_mouth(clip, self._picture(self.pack.clips[clip].frames[index]))
        self._last_picture = picture
        self._blended = self._fade_left > 0 and self._fade_from is not None
        if self._blended:
            from PIL import Image
            weight = 1 - self._fade_left / (self._fade_frames + 1)
            picture = Image.blend(self._fade_from, picture, weight)
            self._fade_left -= 1
        return self._with_caption(picture)

    def finished(self):
        """The meeting has ended and the last clip has played, its last frame shown unblended
        (the still picture: for a moment)."""
        if not self._ended_frames:
            return False
        if self.player is None:
            return self._ended_frames >= ENDED_HOLD_SECONDS * self.fps
        return self.player.settled and not self._blended

    def _run(self):
        try:
            self._prepare()
        except Exception:
            self.failures += 1
        period = 1 / self.fps
        due = time.monotonic()
        last = None
        while not self._closed.is_set():
            try:
                self.device.write_frame(self.next_frame())
                now = time.monotonic()
                if last is not None:
                    self.longest_gap = max(self.longest_gap, now - last)
                last = now
                self.frames_written += 1
            except Exception:
                self.failures += 1
            if self.finished():
                self._switch_off()
                break
            due += period
            delay = due - time.monotonic()
            if delay > 0:
                self._closed.wait(delay)
            else:
                if delay < -period:
                    self.late_frames += 1
                due = time.monotonic()
        # One line for the container log: whether the picture kept its pace.
        print(f'camera: {self.frames_written} frames at {self.fps} fps, {self.late_frames} late, '
              f'longest gap {self.longest_gap * 1000:.0f} ms, {self.failures} failed writes'
              + (', then off' if self.off else ''), flush=True)

    def _switch_off(self):
        """From the camera's thread: no more frames, and the room's camera goes off."""
        if self.off:
            return
        self.off = True
        loop = self._loop
        if self.turn_off is not None and loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                asyncio.run_coroutine_threadsafe(self.turn_off(), loop)

    def _mouth(self, clip):
        return getattr(self.pack.clips[clip], 'mouth', None)

    def _prepare(self):
        """Decode, before the first frame, the clips kept decoded: a small looping clip with a mouth
        and the empty stage the camera starts on, and the mouth pictures."""
        if self.pack is None:
            return
        first = self.pack.clip_for('joining')
        for name, clip in self.pack.clips.items():
            mouth = self._mouth(name)
            keep = clip.loop and (mouth is not None or name == first)
            if keep and len(set(clip.frames)) <= KEEP_PICTURES:
                for data in clip.frames:
                    if data not in self._kept:
                        self._kept[data] = self._decode(data)
            if mouth is not None:
                self._mouths[name] = self._mouth_pictures(mouth)

    def _mouth_pictures(self, mouth):
        return [self._decode(data, (mouth.width, mouth.height)) for data in mouth.levels]

    def _picture(self, data):
        picture = self._kept.get(data)
        return picture if picture is not None else self._decode(data)

    def _with_mouth(self, clip, picture):
        """The frame with the mouth the voice has now (the frame's own mouth is the closed one)."""
        mouth = self._mouth(clip)
        if mouth is None or self.voice is None:
            return picture
        level = self.voice.mouth(len(mouth.levels))
        if not level:
            return picture
        shapes = self._mouths.get(clip)
        if shapes is None:
            shapes = self._mouths[clip] = self._mouth_pictures(mouth)
        picture = picture.copy()
        picture.paste(shapes[level - 1], (mouth.x, mouth.y))
        return picture

    def _decode(self, data, size=None):
        from PIL import Image
        size = size or self.size
        picture = Image.open(io.BytesIO(data)).convert('RGBA')
        if picture.size != size:
            picture = picture.resize(size)
        return picture

    def _with_caption(self, picture):
        """The frame's RGBA bytes with the caption drawn over its bottom band (made once)."""
        if self.caption:
            if self._caption_band is None:
                colour = (self.pack.caption_color if self.pack else None) or '#FFFFFF'
                self._caption_band = self._caption_over_band(colour)
            top, layer = self._caption_band
            if layer is not None:
                band = picture.crop((0, top, self.size[0], self.size[1]))
                band.alpha_composite(layer)
                picture = picture.copy()
                picture.paste(band, (0, top))
        return picture.tobytes()

    def _caption_over_band(self, colour):
        """(top, layer): the caption, cut to the band of the frame it covers."""
        layer = self._caption_picture(colour)
        box = layer.getbbox()
        if box is None:
            return 0, None
        top = max(0, box[1] - 2)
        return top, layer.crop((0, top, self.size[0], self.size[1]))

    def _caption_picture(self, colour):
        from PIL import Image, ImageDraw
        width, height = self.size
        layer = Image.new('RGBA', self.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        red, green, blue = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
        draw.text((width / 2, height * 0.9), self.caption, font=_caption_font(round(height * 0.06)),
                  fill=(red, green, blue, 217), anchor='mm')
        return layer

    def _draw_caption(self, picture, colour):
        if self.caption:
            picture.alpha_composite(self._caption_picture(colour))

    def _still_frame(self):
        """The still mark, like the browser's still style: three bars on slate, the caption below."""
        if self._still is None:
            from PIL import Image, ImageDraw
            width, height = self.size
            picture = Image.new('RGBA', self.size, (0x30, 0x32, 0x3B, 255))
            draw = ImageDraw.Draw(picture)
            scale = height * 0.35 / 128
            left, top = width / 2 - 64 * scale, height / 2 - 64 * scale
            for x, y, h in ((34, 44, 40), (58, 28, 72), (82, 44, 40)):
                draw.rounded_rectangle(
                    (left + x * scale, top + y * scale, left + (x + 12) * scale, top + (y + h) * scale),
                    radius=4 * scale, fill=(255, 255, 255, 255))
            self._draw_caption(picture, '#FFFFFF')
            self._still = picture.tobytes()
        return self._still


_LOAD_PACK = object()


@asynccontextmanager
async def joined_room(url, runtime, stop, stage, state, *, pack=_LOAD_PACK, caption=None, sdk=None):
    """Join a Daily room for bridge.main; yields (room, speaker, camera) and always leaves.

    `pack` is the presence pack bridge.main already read (None for none); left out, it is read
    here from the runtime's settings."""
    if pack is _LOAD_PACK:
        pack = None
        if runtime.camera_style == 'mascot' and runtime.camera_pack:
            try:
                pack = load_presence_pack(runtime.camera_pack)
            except ValueError as error:
                # A pack that cannot be read leaves the still picture, not the meeting without a camera.
                state['cameraPackError'] = str(error)[:300]
    if not runtime.camera_enabled:
        pack = None
    size = (pack.width, pack.height) if pack is not None else STILL_SIZE
    room = DailyRoom(stop, stage, sdk=sdk, camera_size=size if runtime.camera_enabled else None)
    state['capabilities'] = room.capabilities.public()
    state['authenticationState'] = 'guest'
    speaker = DailySpeaker(room.speaker_device)
    microphone = DailyMicrophone(room.microphone_device)
    # The talking picture and the mouth follow the voice as it goes into the room.
    voice = VoiceActivity()
    voice.attach(microphone)
    camera = DailyCamera(room.camera_device, microphone, enabled=runtime.camera_enabled,
                         pack=pack, caption=caption, voice=voice, turn_off=room.turn_camera_off)
    microphone.start()
    camera.start()
    try:
        await room.join(url, runtime.participant_name)
        state['cameraOneLayer'] = await room.send_one_sharp_layer(camera.fps)
        speaker.start()
        yield room, speaker, camera
    finally:
        # Out of the room first (bridge.run_voice has usually left it already), so the agent's
        # tile goes at once rather than freezing as its devices stop; daily-python goes last.
        await leave_meeting(room, state)
        speaker.close()
        await camera.stop()
        await microphone.close()
        room.release()
