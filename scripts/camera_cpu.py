"""How much CPU the meeting camera takes with a presence pack: in a browser meeting, or a Daily room.

Run it inside the meeting image with the pack mounted; scripts/camera-cpu.sh runs both measures:

  docker run --rm --shm-size=1g --entrypoint /app/.venv/bin/python \\
    -v "$PACK:/pack:ro" -v "$PWD/scripts:/bench:ro" IMAGE /bench/camera_cpu.py browser --pack /pack
  docker run --rm --entrypoint /app/.venv/bin/python \\
    -v "$PACK:/pack:ro" -v "$PWD/scripts:/bench:ro" IMAGE /bench/camera_cpu.py daily --pack /pack

browser: Chromium as a meeting runs it (BrowserSession's flags, in a virtual display with its VNC
  server, software video), with the mascot camera (camera_feed.py) at the pack's size and frame
  rate, on a local page that sends the camera to itself through an RTCPeerConnection, so the video
  encoder runs as in a meeting. Reports CPU per process, the sender's WebRTC stats (frames encoded
  and sent, size, frame rate, bitrate, encoder, encode time, what limited quality) and the receiving
  end's decode time, which a meeting does not spend. A meeting site also runs its own page and
  decodes other people's video; this measures the camera alone.
daily: the Daily room camera (DailyCamera) drawing the pack into a device that discards the frames:
  decoding, the mouth, blends and the caption, without daily-python's encoder, which a live room adds.

Both cycle the presence states as a meeting does: the walk-in, then listening, speaking (a synthetic
voice moves the mouth, with pauses), thinking, and speaking again. The first --warmup seconds are
left out of the averages. Percentages are of one CPU core; the machine has os.cpu_count() of them.
The last line, RESULT {...}, is the same as JSON.
"""
import argparse
import asyncio
import json
import math
import os
import socket
import struct
import sys
import time

sys.path.insert(0, '/meeting-runtime')

from presence_pack import load_presence_pack  # noqa: E402
from voice_activity import VoiceActivity  # noqa: E402

TICK = os.sysconf('SC_CLK_TCK')
SCHEDULE = [('listening', 6), ('speaking', 8), ('working', 5), ('speaking', 6), ('listening', 4)]


class Sampler:
    """CPU and memory of every process on this machine, by role, sampled once a second."""

    def __init__(self):
        self.last = {}
        self.cpu = {}       # role -> [percent per second]
        self.rss = {}       # role -> peak MB
        self.machine = []   # percent of one core, all processes, per second
        self.last_machine = None
        self.last_at = None

    @staticmethod
    def role(pid):
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as handle:
                # Chrome's child processes rewrite theirs as one line, words apart by spaces.
                words = handle.read().replace(b'\0', b' ').decode(errors='replace').split()
        except OSError:
            return None
        if not words:
            return None
        if pid == os.getpid():
            return 'python (this script)'
        kind = next((w.split('=', 1)[1] for w in words if w.startswith('--type=')), None)
        name = os.path.basename(words[0])
        if kind is None and name not in ('chrome', 'chromium', 'headless_shell'):
            return name
        sub = next((w.split('=', 1)[1] for w in words if w.startswith('--utility-sub-type=')), '')
        return f'chrome {kind or "browser"}' + (f' ({sub.split(".")[0]})' if sub else '')

    def sample(self):
        now = time.monotonic()
        seen = {}
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f'/proc/{pid}/stat') as handle:
                    fields = handle.read().rsplit(')', 1)[1].split()
                ticks = int(fields[11]) + int(fields[12])
                rss = int(fields[21]) * os.sysconf('SC_PAGE_SIZE') / 1e6
            except (OSError, IndexError, ValueError):
                continue
            role = self.role(pid)
            if role is None:
                continue
            seen[pid] = (role, ticks, rss)
        with open('/proc/stat') as handle:
            fields = handle.readline().split()
            machine = sum(int(value) for value in fields[1:4] + fields[6:8])  # busy, not idle or wait
        if self.last_at is not None:
            elapsed = now - self.last_at
            per_role = {}
            for pid, (role, ticks, rss) in seen.items():
                before = self.last.get(pid)
                used = ticks - before[1] if before and before[0] == role else 0
                per_role[role] = per_role.get(role, 0) + used / TICK / elapsed * 100
                self.rss[role] = max(self.rss.get(role, 0), rss)
            for role, percent in per_role.items():
                self.cpu.setdefault(role, []).append(percent)
            self.machine.append((machine - self.last_machine) / TICK / elapsed * 100)
        self.last, self.last_machine, self.last_at = seen, machine, now

    def reset(self):
        self.cpu, self.machine = {}, []

    def summary(self):
        roles = {role: {'avg': round(sum(v) / len(v), 1), 'peak': round(max(v), 1),
                        'rssMB': round(self.rss.get(role, 0))}
                 for role, v in self.cpu.items() if v and max(v) > 0.05}
        total = round(sum(sum(v) / len(v) for v in self.cpu.values() if v), 1)
        machine = round(sum(self.machine) / len(self.machine), 1) if self.machine else None
        return {'processes': dict(sorted(roles.items(), key=lambda item: -item[1]['avg'])),
                'processesTotal': total, 'machine': machine, 'cores': os.cpu_count()}


def voice_chunk(t):
    """10 ms of a synthetic voice at time t: syllables about 4.5 a second, a pause every 2.2 s."""
    if t % 2.2 > 1.85:
        return bytes(480)
    loud = 0.04 + 0.3 * max(0.0, math.sin(2 * math.pi * 4.5 * t)) ** 0.7
    return struct.pack('<240h', *(round(32767 * loud * math.sin(2 * math.pi * 180 * (t + i / 24000)))
                                  for i in range(240)))


# 2.2 s of it, made once, so the measure spends nothing on it.
VOICE = [voice_chunk(i / 100) for i in range(220)]


async def run_schedule(camera, voice, sampler, seconds, warmup):
    """Cycle the presence states, feed the voice while speaking, sample the CPU every second."""
    started = time.monotonic()
    spoken = 0
    camera.set_visual_state('joining')
    await asyncio.sleep(2)
    step, step_ends = 0, 0.0
    state = None
    next_sample = time.monotonic()
    warmed = False
    while time.monotonic() - started < seconds + 2:
        now = time.monotonic()
        if now >= step_ends:
            state, length = SCHEDULE[step % len(SCHEDULE)]
            step += 1
            step_ends = now + length
            camera.set_visual_state(state)
        if state == 'speaking':
            voice.feed(VOICE[spoken % len(VOICE)])
            spoken += 1
        if now >= next_sample:
            sampler.sample()
            next_sample += 1
            if not warmed and now - started >= warmup + 2:
                sampler.reset()
                warmed = True
                yield 'warm'
        await asyncio.sleep(0.01)
    yield 'done'


class Discard:
    """A camera device that takes frames and keeps none."""
    def __init__(self):
        self.frames = 0

    def write_frame(self, frame):
        self.frames += 1


class Writer:
    audio_format = object()
    chunk_size = 480


HARNESS = '<!doctype html><title>camera benchmark</title>'
START = """async ({codec, bitrate}) => {
    const stream = await navigator.mediaDevices.getUserMedia({video: true});
    const pc1 = new RTCPeerConnection(), pc2 = new RTCPeerConnection();
    pc1.onicecandidate = (e) => e.candidate && pc2.addIceCandidate(e.candidate);
    pc2.onicecandidate = (e) => e.candidate && pc1.addIceCandidate(e.candidate);
    const track = stream.getVideoTracks()[0];
    const sender = pc1.addTrack(track, stream);
    if (codec) {
        const transceiver = pc1.getTransceivers().find((t) => t.sender === sender);
        const all = RTCRtpSender.getCapabilities('video').codecs;
        const first = all.filter((c) => c.mimeType.toLowerCase() === 'video/' + codec.toLowerCase());
        if (first.length) transceiver.setCodecPreferences([...first, ...all.filter((c) => !first.includes(c))]);
    }
    pc2.ontrack = () => {};
    const offer = await pc1.createOffer();
    await pc1.setLocalDescription(offer);
    await pc2.setRemoteDescription(offer);
    const answer = await pc2.createAnswer();
    await pc2.setLocalDescription(answer);
    await pc1.setRemoteDescription(answer);
    if (bitrate) {
        const parameters = sender.getParameters();
        if (!parameters.encodings || !parameters.encodings.length) parameters.encodings = [{}];
        parameters.encodings[0].maxBitrate = bitrate * 1000;
        await sender.setParameters(parameters);
    }
    window.__bench = {pc1, pc2};
    return sender.track.contentHint;
}"""
STATS = """async () => {
    const out = {at: performance.now()};
    for (const [pc, side] of [[window.__bench.pc1, 'send'], [window.__bench.pc2, 'receive']]) {
        const report = await pc.getStats();
        const codecs = {};
        report.forEach((s) => { if (s.type === 'codec') codecs[s.id] = s.mimeType; });
        report.forEach((s) => {
            if (s.kind !== 'video') return;
            if (side === 'send' && s.type === 'outbound-rtp') out.send = {...s, codec: codecs[s.codecId]};
            if (side === 'receive' && s.type === 'inbound-rtp') out.receive = {...s, codec: codecs[s.codecId]};
        });
    }
    return out;
}"""


def webrtc_summary(first, last):
    send, before = last.get('send') or {}, first.get('send') or {}
    receive, received = last.get('receive') or {}, first.get('receive') or {}
    seconds = (last['at'] - first['at']) / 1000

    def rate(key, source, start):
        return round((source.get(key, 0) - start.get(key, 0)) / seconds, 1) if seconds > 0 else None

    def per_frame(time_key, count_key, source, start):
        frames = source.get(count_key, 0) - start.get(count_key, 0)
        return round((source.get(time_key, 0) - start.get(time_key, 0)) / frames * 1000, 2) if frames else None
    return {
        'codec': send.get('codec'), 'encoder': send.get('encoderImplementation'),
        'size': f"{send.get('frameWidth')}x{send.get('frameHeight')}",
        'framesEncodedPerSecond': rate('framesEncoded', send, before),
        'framesSentPerSecond': rate('framesSent', send, before),
        'framesEncoded': send.get('framesEncoded'), 'framesSent': send.get('framesSent'),
        'keyFrames': send.get('keyFramesEncoded'),
        'kbitPerSecond': round(rate('bytesSent', send, before) * 8 / 1000) if seconds > 0 else None,
        'encodeMsPerFrame': per_frame('totalEncodeTime', 'framesEncoded', send, before),
        'qualityLimitation': send.get('qualityLimitationReason'),
        'qualityLimitationSeconds': send.get('qualityLimitationDurations'),
        'contentHint': None,
        'receiverDecodeMsPerFrame': per_frame('totalDecodeTime', 'framesDecoded', receive, received),
        'receiverDecodedPerSecond': rate('framesDecoded', receive, received),
    }


async def browser(args):
    from aiohttp import web
    from joinly.providers.browser.browser_session import BrowserSession
    from joinly.providers.browser.camera_feed import CameraFeed
    from joinly.providers.browser.devices.virtual_display import VirtualDisplay

    pack = load_presence_pack(args.pack)

    async def page_route(_request):
        return web.Response(text=HARNESS, content_type='text/html')
    app = web.Application()
    app.router.add_get('/', page_route)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    await web.SockSite(runner, sock).start()
    env = dict(os.environ)
    sampler = Sampler()
    try:
        async with VirtualDisplay(env=env, use_vnc_server=not args.no_vnc, vnc_port=5900), \
                BrowserSession(env=env) as session:
            page = await session.get_page()
            voice = VoiceActivity()
            feed = CameraFeed(Writer(), style='mascot', pack=pack.page_payload(), voice=voice,
                              caption=args.caption)
            await feed.install(page)
            await page.goto(f'http://127.0.0.1:{sock.getsockname()[1]}/')
            hint = await page.evaluate(START, {'codec': args.codec, 'bitrate': args.bitrate})
            first = None
            async for mark in run_schedule(feed, voice, sampler, args.total, args.warmup):
                if mark == 'warm':
                    first = await page.evaluate(STATS)
            last = await page.evaluate(STATS)
            rtc = webrtc_summary(first or last, last)
            rtc['contentHint'] = hint
            await feed.turn_off()
    finally:
        await runner.cleanup()
    return {'mode': 'browser', 'pack': pack_summary(pack), 'webrtc': rtc, 'cpu': sampler.summary()}


async def daily(args):
    from daily_room import DailyCamera
    pack = load_presence_pack(args.pack)
    device = Discard()
    if args.daily_device:
        from daily_room import daily_sdk
        device = daily_sdk().Daily.create_camera_device(
            'benchmark-camera', width=pack.width, height=pack.height, color_format='RGBA')
    voice = VoiceActivity()
    camera = DailyCamera(device, None, pack=pack, caption=args.caption, voice=voice)
    sampler = Sampler()
    camera.start()
    frames_at_warm = 0
    async for mark in run_schedule(camera, voice, sampler, args.total, args.warmup):
        if mark == 'warm':
            frames_at_warm = camera.frames_written
    camera._closed.set()
    await asyncio.sleep(0.2)
    own = sampler.summary()
    return {'mode': 'daily', 'pack': pack_summary(pack),
            'camera': {'framesWritten': camera.frames_written - frames_at_warm,
                       'framesPerSecond': round((camera.frames_written - frames_at_warm) / args.seconds, 1),
                       'lateFrames': camera.late_frames, 'longestGapMs': round(camera.longest_gap * 1000),
                       'failedWrites': camera.failures, 'dailyDevice': bool(args.daily_device)},
            'cpu': own}


def pack_summary(pack):
    mouths = {name: len(clip.mouth.levels) for name, clip in pack.clips.items() if clip.mouth}
    return {'name': pack.name, 'version': pack.version, 'size': f'{pack.width}x{pack.height}',
            'fps': pack.fps, 'pictures': len(pack.pictures()),
            'MB': round(pack.total_bytes / 1024 / 1024, 2), 'mouthLevels': mouths}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('mode', choices=('browser', 'daily'))
    parser.add_argument('--pack', required=True, help='the presence pack folder')
    parser.add_argument('--seconds', type=float, default=60, help='measured time, after the warm-up')
    parser.add_argument('--warmup', type=float, default=10)
    parser.add_argument('--caption', default="Arun's AI assistant")
    parser.add_argument('--codec', default='', help='browser: vp8, vp9, h264 or av1 first (default: Chromium\'s)')
    parser.add_argument('--bitrate', type=int, default=0, help='browser: cap the sender, kbit/s')
    parser.add_argument('--no-vnc', action='store_true', help='browser: without the VNC server a meeting runs')
    parser.add_argument('--daily-device', action='store_true',
                        help="daily: write to daily-python's virtual camera (no call, so no encoder)")
    args = parser.parse_args()
    args.total = args.seconds + args.warmup
    result = asyncio.run(browser(args) if args.mode == 'browser' else daily(args))
    print(json.dumps(result, indent=2))
    print('RESULT ' + json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
