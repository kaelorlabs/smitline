"""Does Smitline hear a Daily room? A live check that needs only the room's link, no API key.

Inside the meeting image (the room must exist: daily_rooms.py create makes one):

    docker run --rm --entrypoint /app/.venv/bin/python -e ROOM_URL=https://TEAM.daily.co/ROOM \\
        smitline-meeting:TAG /meeting-runtime/daily_hearing_check.py

A talker joins the room from a process of its own and plays bursts of a voice-like tone through a
virtual microphone. This process joins as a meeting does (daily_room.joined_room: DailyRoom and
DailySpeaker), takes the room's audio chunk by chunk as bridge.send_audio would hand it to
GPT-Live, and prints one JSON line: the bursts heard and how late each came in, the voiced
milliseconds against those played, the level, and the speaker's own counts. GPT-Live is not
involved. Both join as "Hearing check", for about half a minute, and leave.

Exit status: 0 every burst was heard; 2 some were; 1 nothing was (the bot is deaf); 3 the check
could not run (a join failed, or the talker never played).
"""
import argparse
import asyncio
import contextlib
import json
import math
import os
import statistics
import struct
import sys
import time
from types import SimpleNamespace

from meeting_input import BYTES_PER_SECOND, VOICED_DB, InputMonitor, pcm_level

BOT_NAME = 'Hearing check (bot)'
TALKER_NAME = 'Hearing check (talker)'
TALKER_MICROPHONE = 'hearing-check-talker'
BURSTS = 5
BURST_SECONDS = 1.0
GAP_SECONDS = 1.0
JOIN_SECONDS = 60
SETTLE_SECONDS = 2.0  # once both are in, the talker waits this long for the audio to flow
TAIL_SECONDS = 1.5    # the bot keeps listening this long after the last burst
DEADLINE_SECONDS = 120
ONSET_QUIET_MS = 200  # voiced audio after this much quiet is a burst coming in
MATCH_SECONDS = 1.5   # a burst coming in later than this after it was played was not heard


def burst(seconds, sample_rate=24000):
    """A voice-like sound: a 220 Hz buzz with two overtones that swells four times a second,
    like syllables. Never so quiet that a 20 ms piece of it is not voiced."""
    count = int(seconds * sample_rate)
    values = []
    for n in range(count):
        t = n / sample_rate
        swell = 0.55 + 0.45 * math.sin(2 * math.pi * 4 * t)
        wave = (math.sin(2 * math.pi * 220 * t) + 0.5 * math.sin(2 * math.pi * 440 * t)
                + 0.25 * math.sin(2 * math.pi * 660 * t)) / 1.75
        values.append(int(12000 * swell * wave))
    return struct.pack(f'<{count}h', *values)


def say(message):
    print(f'hearing check: {message}', file=sys.stderr, flush=True)


class Listener:
    """The room's audio as the bot gets it: what came in (InputMonitor), the level of the voiced
    chunks, and when (wall clock) each voiced stretch after a quiet one began."""

    def __init__(self):
        self.monitor = InputMonitor(clock=time.time)
        self.onsets = []
        self.levels = []
        self._quiet_ms = ONSET_QUIET_MS

    def feed(self, data, at):
        level, _peak = pcm_level(data)
        self.monitor.feed(data)
        ms = len(data) * 1000 // BYTES_PER_SECOND
        if level > VOICED_DB:
            if self._quiet_ms >= ONSET_QUIET_MS:
                self.onsets.append(at - ms / 1000)  # when this chunk began
            self._quiet_ms = 0
            self.levels.append(level)
        else:
            self._quiet_ms += ms


def report(played, listener, speaker=None, seconds=BURST_SECONDS, failure=None):
    """The JSON report and the exit status (see the module's docstring)."""
    latencies = []
    for each in played:
        start = each['start']
        onset = next((at for at in listener.onsets if start - 0.1 <= at <= start + MATCH_SECONDS),
                     None)
        latencies.append(None if onset is None else max(0, round((onset - start) * 1000)))
    heard = [ms for ms in latencies if ms is not None]
    monitor = listener.monitor
    result = {
        'heard': bool(played) and len(heard) == len(played),
        'bursts_played': len(played),
        'bursts_heard': len(heard),
        'latency_ms': latencies,
        'latency_median_ms': round(statistics.median(heard)) if heard else None,
        'latency_max_ms': max(heard) if heard else None,
        'voiced_ms': monitor.voiced_ms,
        'played_ms': int(len(played) * seconds * 1000),
        'level_db': round(statistics.median(listener.levels), 1) if listener.levels else None,
        'peak': monitor.peak,
        'received_ms': monitor.bytes_in * 1000 // BYTES_PER_SECOND,
        'speaker': speaker,
    }
    if failure:
        result['failure'] = failure
    if not played:
        return result, 3
    if result['heard']:
        return result, 0
    return result, 2 if heard or monitor.voiced_ms else 1


class TalkerProcess:
    """The talker, in a process of its own (daily-python serves one speaker per process), and
    the JSON lines it prints."""

    def __init__(self, process):
        self.process = process
        self.events = asyncio.Queue()
        self._reader = asyncio.create_task(self._read())

    async def _read(self):
        async for line in self.process.stdout:
            with contextlib.suppress(ValueError):
                await self.events.put(json.loads(line))
        await self.events.put({'talker': 'exited', 'code': await self.process.wait()})

    async def stop(self):
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.process.wait(), 15)
        self._reader.cancel()


async def start_talker_process(url, bursts, seconds, gap):
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-u', os.path.abspath(__file__), '--talker', '--bursts', str(bursts),
        '--seconds', str(seconds), '--gap', str(gap), stdout=asyncio.subprocess.PIPE,
        env={**os.environ, 'ROOM_URL': url})  # the link stays out of the command line
    return TalkerProcess(process)


async def check(url, *, start_talker=start_talker_process, sdk=None, bursts=BURSTS,
                seconds=BURST_SECONDS, gap=GAP_SECONDS, deadline=DEADLINE_SECONDS):
    """Join as the bot, let the talker play, and report what came in."""
    from daily_room import joined_room
    loop = asyncio.get_running_loop()
    ends = loop.time() + deadline
    talker = await start_talker(url, bursts, seconds, gap)
    played, problems = [], []
    stop, done = asyncio.Event(), asyncio.Event()
    listener, speaker_stats = Listener(), None

    async def follow_talker():
        while True:
            event = await talker.events.get()
            if 'burst' in event:
                played.append(event)
            elif event.get('talker') == 'done':
                done.set()
            elif event.get('talker') in ('failed', 'exited'):
                if not done.is_set():
                    problems.append(event.get('error') or f'the talker stopped ({event.get("code")})')
                    stop.set()
                    done.set()
                return

    follower = asyncio.create_task(follow_talker())
    runtime = SimpleNamespace(camera_enabled=False, camera_style='still', camera_pack='',
                              participant_name=BOT_NAME)
    try:
        async with joined_room(url, runtime, stop, lambda _stage: None, {}, pack=None,
                               sdk=sdk) as (_room, speaker, _camera):
            say('the bot is in the room, listening')
            done_at = None
            while loop.time() < ends and not stop.is_set():
                chunk = await asyncio.wait_for(speaker.read(), 2)
                listener.feed(chunk.data, time.time())
                if done.is_set():
                    done_at = done_at or loop.time()
                    if loop.time() - done_at >= TAIL_SECONDS:
                        break
            else:
                if not stop.is_set():
                    problems.append('the check ran out of time')
            speaker_stats = speaker.stats()
    except Exception as error:
        problems.append(f'{type(error).__name__}: {str(error)[:200]}')
    finally:
        follower.cancel()
        await asyncio.gather(follower, return_exceptions=True)
        await talker.stop()
    return report(played, listener, speaker_stats, seconds, '; '.join(problems) or None)


async def talk(url, bursts, seconds, gap, out=sys.stdout):
    """The talker: join with a virtual microphone, wait for the bot, play the bursts."""
    from daily_room import RAW_AUDIO, DailyMicrophone, _Completion, daily_sdk, room_and_token

    def tell(**event):
        print(json.dumps(event), file=out, flush=True)

    daily = daily_sdk()
    device = daily.Daily.create_microphone_device(TALKER_MICROPHONE, sample_rate=24000,
                                                  channels=1, non_blocking=True)
    microphone = DailyMicrophone(device)
    client = daily.CallClient()
    client.set_user_name(TALKER_NAME)
    client.update_subscription_profiles({'base': {
        'camera': 'unsubscribed', 'microphone': 'unsubscribed',
        'screenVideo': 'unsubscribed', 'screenAudio': 'unsubscribed'}})
    loop = asyncio.get_running_loop()
    room_url, token = room_and_token(url)
    joined = _Completion(loop)
    client.join(room_url, meeting_token=token, client_settings={'inputs': {
        'camera': False,
        'microphone': {'isEnabled': True, 'settings': {
            'deviceId': TALKER_MICROPHONE, 'customConstraints': RAW_AUDIO}}}}, completion=joined)
    error = None
    try:
        try:
            _data, error = await joined.wait(JOIN_SECONDS)
        except TimeoutError:
            error = 'no answer from Daily within a minute'
        if error:
            tell(talker='failed', error=f'the talker could not join: {str(error)[:200]}')
            return 3
        tell(talker='joined')
        until = loop.time() + JOIN_SECONDS
        while not any((person.get('info') or {}).get('userName') == BOT_NAME
                      for key, person in (client.participants() or {}).items() if key != 'local'):
            if loop.time() > until:
                tell(talker='failed', error='the bot never came into the room')
                return 3
            await asyncio.sleep(0.2)
        microphone.start()
        await asyncio.sleep(SETTLE_SECONDS)
        sound = burst(seconds)
        for index in range(bursts):
            start = time.time()
            await microphone.write(sound)
            await microphone.drain()
            tell(burst=index, start=start, end=time.time())
            await asyncio.sleep(gap)
        tell(talker='done')
        return 0
    finally:
        await microphone.close()
        if not error:  # in the room: leave it
            left = _Completion(loop)
            with contextlib.suppress(Exception):
                client.leave(completion=left)
                await left.wait(10)
        with contextlib.suppress(Exception):
            client.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Check that Smitline hears a Daily room.')
    parser.add_argument('url', nargs='?', default=os.environ.get('ROOM_URL'),
                        help='the room link (or set ROOM_URL)')
    parser.add_argument('--bursts', type=int, default=BURSTS)
    parser.add_argument('--seconds', type=float, default=BURST_SECONDS)
    parser.add_argument('--gap', type=float, default=GAP_SECONDS)
    parser.add_argument('--talker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.url:
        parser.error('give the room link, or set ROOM_URL')
    if args.talker:
        return asyncio.run(talk(args.url, args.bursts, args.seconds, args.gap))
    result, code = asyncio.run(check(args.url, bursts=args.bursts, seconds=args.seconds,
                                     gap=args.gap))
    print(json.dumps(result), flush=True)
    say({0: 'every burst was heard', 1: 'nothing was heard: the bot is deaf',
         2: 'some bursts were not heard', 3: 'the check could not run'}[code])
    return code


if __name__ == '__main__':
    sys.exit(main())
