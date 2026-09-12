"""Run Joinly's bundled mock browser meeting and publish measured results."""
import asyncio
import json
import signal
from pathlib import Path

from aiohttp import web
import jiwer
from joinly.container import SessionContainer
from joinly.providers.browser.meeting_provider import PLATFORMS
from tests.utils.mockup_browser_controller import MockupBrowserPlatformController
from tests.utils.mockup_browser_meeting import _create_mockup_meeting_html

DATA = Path('/source/tests/data/speech_audio')
OUTPUT = Path('/demo/mock-output')
report = {'status': 'starting', 'kind': 'recorded-audio mock meeting'}


async def main():
    OUTPUT.mkdir(exist_ok=True)
    sample = json.loads((DATA / 'test_samples.json').read_text())[0]
    app = web.Application()

    async def index(request):
        return web.Response(text=_create_mockup_meeting_html(), content_type='text/html')

    async def audio(request):
        return web.FileResponse(DATA / sample['filename'])

    async def result(request):
        return web.json_response(report, dumps=lambda value: json.dumps(value, indent=2))

    app.router.add_get('/', index)
    app.router.add_get('/speech_audio', audio)
    app.router.add_get('/result', result)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 8090).start()
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, done.set)

    PLATFORMS.insert(0, MockupBrowserPlatformController)
    try:
        async with SessionContainer() as meeting:
            await meeting.join_meeting(meeting_url='http://127.0.0.1:8090/', participant_name='Local Demo')
            report.update(status='listening', sample_seconds=sample['duration'])
            print(json.dumps(report), flush=True)
            for _ in range(sample['duration'] + 5):
                await asyncio.sleep(1)
                report['transcript'] = meeting.transcript.text
            actual = meeting.transcript.text
            error_rate = jiwer.wer(sample['transcription'].lower(), actual.lower())
            report.update(status='passed' if actual and error_rate <= 0.2 else 'failed',
                          transcript=actual, expected=sample['transcription'],
                          word_error_rate=round(error_rate, 4), maximum_error_rate=0.2,
                          segments=len(meeting.transcript.segments))
            (OUTPUT / 'transcript.txt').write_text(actual + '\n')
            await meeting.leave_meeting()
    except Exception as exc:
        report.update(status='failed', error=str(exc))
    finally:
        PLATFORMS.remove(MockupBrowserPlatformController)
        (OUTPUT / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)
    try:
        await done.wait()
    finally:
        await runner.cleanup()


asyncio.run(main())
