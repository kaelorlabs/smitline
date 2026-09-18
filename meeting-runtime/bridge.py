"""Meeting browser participant with GPT-Live audio; no local speech models."""
import asyncio
import base64
import contextlib
import os
import re
import signal
from contextlib import AsyncExitStack
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from call_record import CallRecord
from client_delegation import ClientDelegation, handoff_from_state, permissions_from_state
from context_tool import context_available, load_context
from codex_tool import CODEX_MODELS, CodexJobClient
from speech_gate import SpeechGate
from startup_input import handoff_to_session_input
from runtime_config import RuntimeConfig, meeting_state_from_environ, resolve_meeting_url
from session_continuity import continuity_from_payload
from meeting_connection import joined_meeting
from adapters_base import AuthenticationRequired
from meeting_urls import platform_for_url
from participation import Participation
from meeting_lifecycle import should_leave_alone
from pathlib import Path
from joinly.providers.browser.browser_session import BrowserSession
from joinly.providers.browser.devices.pulse_server import PulseServer
from joinly.providers.browser.devices.virtual_display import VirtualDisplay
from joinly.providers.browser.devices.virtual_speaker import VirtualSpeaker
from joinly.providers.browser.devices.virtual_microphone import VirtualMicrophone
from joinly.providers.browser.camera_feed import CameraFeed
from visual_presence import Presence, apply_platform_camera, presence_public_fields

state = {'stage': 'starting', 'model': 'gpt-live-1', 'input_bytes': 0, 'output_bytes': 0,
         'muted': True, 'listening': False, 'admissionState': 'pending', 'authenticationState': 'guest', 'microphoneState': 'muted', 'captions': [], 'usage_seconds': 0, 'finalized': False,
         'web_search': {'enabled': True, 'implementation': 'local_function', 'provider_configured': bool(os.environ.get('TAVILY_API_KEY')), 'started': 0, 'completed': 0}, 'backend_status': 'idle', 'sources': []}
state['codex'] = {'enabled': True, 'implementation': 'host_codex_cli', 'models': list(CODEX_MODELS),
                  'default_model': 'gpt-5.6-terra', 'session_scope': 'meeting',
                  'session_active': False, 'worker_connected': False, 'started': 0, 'completed': 0}
state['context'] = {'enabled': False, 'implementation': 'local_retrieval', 'source_count': 0,
                    'started': 0, 'completed': 0, 'last_sources': []}
state.update(cameraEnabled=True, cameraState='starting', visualState='joining')
stop = asyncio.Event()
record = None
presence = None


def _sync_presence():
    if presence is not None:
        presence.sync()


def stage(value):
    if state['stage'] != value:
        state['stage'] = value
        if value in ('joining', 'waiting_for_admission', 'admitted', 'authentication_required'):
            state['admissionState'] = value
        if record:
            record.event('stage', stage=value)
        print(value, flush=True)
    if presence is not None:
        presence.sync()


def build_session_config(runtime, meeting_state=None):
    meeting_state = meeting_state or {}
    config = {'model': 'gpt-live-1', 'store': False,
              'audio': {'format': {'type': 'audio/pcm', 'rate': 24000}},
              'instructions': f'''You are {runtime.participant_name}, an AI participant in a real meeting. Follow the conversation continuously and retain the context needed to help.
Participation policy: Default to listening silently. Respond when someone directly addresses you by name, explicitly asks you a question, asks you to perform a task, or requests a result from the backend. You may briefly correct a material factual error only when the correction is important to the current decision and you can establish the correct fact. Otherwise keep listening. Do not respond to general discussion, rhetorical questions, greetings between other participants, unfinished thoughts, background conversation, ordinary pauses, or questions clearly directed to somebody else. If it is unclear whether someone addressed you, remain silent. Do not produce acknowledgements, backchannels, or listening sounds such as "mm-hmm." Keep spoken responses concise and natural. Continue through brief listener backchannels such as "mm-hmm," "yeah," "okay," or other non-substantive sounds. Stop speaking when a participant asks you to stop, makes a substantive interruption, or begins a new sentence that takes the floor.
Capability policy: Help with any meeting task you can handle reliably, including questions, explanations, brainstorming, planning, summaries, decisions, calculations, and conversation. Ask one concise clarification when a missing detail would materially change the answer. Clearly distinguish known facts, verified backend results, and inference. Never invent access, results, sources, actions, or capabilities.
Delegation policy: Delegate technical, workspace, repository, data, planning, and current-fact work to the backend instead of answering from guesswork. Delegate only for an explicit actionable request addressed to you, or to verify a material factual correction that meets the participation policy. Never delegate merely because the conversation mentions a related topic. Continue listening and handle simple unrelated conversation while backend work runs. You may briefly acknowledge that you are checking. Never invent a pending technical result. If a participant cancels or corrects the request, follow the latest spoken request. Do not claim that you browsed the web, edited files, or ran tools yourself. Identify yourself as an AI if asked.''',
              'delegation': {'type': 'client'}}
    if runtime.meeting_instructions:
        config['instructions'] += (
            '\nOrganizer-provided meeting guidance: ' + runtime.meeting_instructions +
            '\nUse this guidance to understand the meeting and your role where it is compatible with the policies above.')
    if runtime.charts_enabled:
        config['instructions'] += (
            ' For chart or plot requests, delegate so the backend can query the data. '
            'The application may render a PNG and try to attach it to meeting chat. '
            'Do not read plot JSON aloud. Explain any sharing error honestly.')
    incoming = handoff_to_session_input(
        handoff_from_state(meeting_state),
        permissions_from_state(meeting_state, runtime) if meeting_state else None)
    if incoming:
        config['input'] = incoming
    return config


async def run_voice(speaker, microphone, page, api_key, runtime, adapter, meeting_state=None,
                   router=None):
    meeting_state = meeting_state or {}
    config = build_session_config(runtime, meeting_state)
    record.event('session_config', config=config)
    async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=30)) as client:
        async with client.ws_connect('wss://api.openai.com/v1/live/sessions', headers={'Authorization': f'Bearer {api_key}'}, max_msg_size=8*1024*1024) as ws:
            await ws.send_json({'type': 'session.start', 'session': config})
            ready = asyncio.Event()
            finished = asyncio.Event()
            participation = Participation(adapter, microphone, state, on_presence=_sync_presence)
            gate = participation
            delegations = ClientDelegation(
                send=ws.send_json, record=record, state=state, runtime=runtime,
                meeting_state=meeting_state, router=router, page=page, adapter=adapter,
                stop_event=stop, on_presence=_sync_presence)
            capture_loop = None
            if runtime.screen_share_enabled:
                from screen_share_pipeline import ScreenShareBus, ScreenShareCaptureLoop
                meeting_id = (meeting_state or {}).get('meetingId') or getattr(record, 'meeting_id', None)
                jobs = os.environ.get('CODEX_JOBS_DIR', '/meeting-runtime/jobs')
                if meeting_id:
                    capture_loop = ScreenShareCaptureLoop(
                        adapter=adapter,
                        settings=runtime.screen_share_settings,
                        bus=ScreenShareBus(jobs, meeting_id),
                        state=state,
                        stop=stop,
                        send=ws.send_json,
                    )
            codex_client = CodexJobClient()

            async def send_audio():
                await ready.wait()
                while not stop.is_set():
                    chunk = await speaker.read()
                    state['input_bytes'] += len(chunk.data)
                    await ws.send_json({'type': 'session.input_audio.append', 'audio': base64.b64encode(chunk.data).decode()})

            async def watch_microphone():
                await ready.wait()
                while not stop.is_set():
                    await asyncio.sleep(.2)
                    actual = await adapter.get_microphone_state()
                    await gate.platform_microphone_changed(actual)

            async def watch_meeting():
                await ready.wait()
                while not stop.is_set():
                    await asyncio.sleep(3)
                    state['codex']['worker_connected'] = codex_client.worker_connected()
                    state['microphoneState'] = await adapter.get_microphone_state()
                    state['chatAvailable'] = await adapter.chat_available()
                    if await adapter.has_ended():
                        stage('meeting_ended')
                        stop.set()
                        return
                    try:
                        count = await adapter.get_participant_count()
                    except Exception:
                        count = None
                    state['participantCount'] = count
                    if should_leave_alone(count):
                        state['endReason'] = 'alone_in_meeting'
                        record.event('auto_leave', reason='alone_in_meeting')
                        try:
                            await gate.stop_output()
                        finally:
                            stage('meeting_ended')
                            stop.set()
                        return

            async def closer():
                await stop.wait()
                state['finalizing'] = True
                _sync_presence()
                await delegations.close()
                await ws.send_json({'type': 'session.close'})
                try:
                    await asyncio.wait_for(finished.wait(), 15)
                except TimeoutError:
                    stage('closed_without_final_usage')
                    await ws.close()

            task_specs = [
                ('meeting_audio_input', send_audio),
                ('reply_audio_output', gate.run),
                ('microphone_monitor', watch_microphone),
                ('meeting_lifecycle', watch_meeting),
                ('session_closer', closer),
            ]
            if capture_loop is not None:
                async def run_capture():
                    await ready.wait()
                    await capture_loop.run()
                async def drain_share():
                    await ready.wait()
                    while not stop.is_set():
                        await capture_loop.drain_observations()
                        await asyncio.sleep(0.5)
                task_specs.extend((
                    ('screen_share_capture', run_capture),
                    ('screen_share_observations', drain_share),
                ))
            tasks = [asyncio.create_task(fn(), name=name) for name, fn in task_specs]
            def task_failed(task):
                if not task.cancelled() and task.exception():
                    error = task.exception()
                    state['error'] = f'{task.get_name()} failed: {type(error).__name__}: {str(error)[:200]}'
                    record.event('background_task_failed', task=task.get_name(), error=type(error).__name__, detail=str(error)[:300])
                    stop.set()
            for task in tasks: task.add_done_callback(task_failed)
            try:
                async for msg in ws:
                    if msg.type != WSMsgType.TEXT:
                        continue
                    event = msg.json()
                    kind = event.get('type')
                    if kind == 'session.started':
                        ready.set()
                        state['listening'] = True
                        stage('live')
                    elif kind == 'session.output_audio.delta':
                        data = base64.b64decode(event['delta'])
                        # Fail rather than silently accumulate seconds of stale speech.
                        gate.offer(data)
                    elif kind in ('session.input_transcript.delta', 'session.output_transcript.delta'):
                        delegations.note_transcript(event)
                    elif kind == 'session.delegation.created':
                        delegations.submit(event)
                    elif kind == 'session.usage.updated':
                        state['usage_seconds'] = event.get('usage', {}).get('seconds', 0)
                    elif kind == 'session.closed':
                        record.event('session_closed', usage=event.get('usage', {}))
                        state['listening'] = False
                        state['finalized'] = True
                        state['usage_seconds'] = event.get('usage', {}).get('seconds', 0)
                        finished.set()
                        stage('finished')
                        break
                    elif kind == 'error':
                        state['api_error'] = event.get('error', {}).get('code') or 'live_error'
                        stage('api_error')
                        if not ready.is_set():
                            raise RuntimeError('GPT-Live rejected session startup')
                    # Unknown future events are ignored so audio and transcripts continue.
            finally:
                await delegations.close()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


async def main():
    global record, presence
    runtime = RuntimeConfig.from_environ()
    meeting_state = meeting_state_from_environ()
    state['participant_name'] = runtime.participant_name
    state['meeting_guidance_configured'] = bool(runtime.meeting_instructions)
    state['cameraEnabled'] = runtime.camera_enabled
    state['cameraState'] = 'off' if not runtime.camera_enabled else 'starting'
    state['visualState'] = 'joining'
    from screen_share import default_settings, public_status
    share_settings = runtime.screen_share_settings or default_settings()
    state['screenShare'] = public_status({
        'enabled': runtime.screen_share_enabled,
        'available': False,
        'active': False,
        'paused': False,
        'capturing': False,
        'degradedReason': None if runtime.screen_share_enabled else 'disabled',
        'captureIntervalMs': share_settings['captureIntervalMs'],
        'analyzerAvailable': False,
        'retention': {
            'maxFrames': share_settings['maxFrames'],
            'maxBytes': share_settings['maxBytes'],
            'retentionSeconds': share_settings['retentionSeconds'],
        },
    })
    sources = load_context()
    state['context']['enabled'] = bool(sources)
    state['context']['source_count'] = len(sources)
    state['web_search']['enabled'] = runtime.web_search_enabled
    state['codex']['enabled'] = runtime.codex_enabled
    state['charts_enabled'] = runtime.charts_enabled
    state['workspace'] = runtime.workspace or '/meeting-runtime/codex-workspace'
    state['codex']['default_model'] = runtime.default_codex_model
    continuity = continuity_from_payload(meeting_state)
    state['codex']['continuity'] = continuity
    state['codex']['session_id'] = meeting_state.get('sessionId')
    state['codex']['session_scope'] = 'originating' if continuity == 'exact' else 'meeting'
    meeting_id = meeting_state.get('meetingId')
    try:
        record = CallRecord(
            os.path.join(os.path.dirname(__file__), 'recordings'), meeting_id=meeting_id)
    except (TypeError, ValueError):
        record = CallRecord(os.path.join(os.path.dirname(__file__), 'recordings'))
    state['recording'] = str(record.directory)
    state['archive'] = {
        'meetingId': record.meeting_id,
        'name': record.directory.name,
        'closed': False,
        'handoffStatus': 'recording',
    }
    state['acceptingDelegations'] = True
    state['delegationsOpen'] = True
    record.event('started')
    api_key = os.environ['OPENAI_API_KEY']
    url = resolve_meeting_url()
    state['platform'] = platform_for_url(url)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    app = web.Application()
    async def status(_request):
        payload = dict(state)
        payload.update(presence_public_fields(state))
        share = payload.get('screenShare')
        if isinstance(share, dict):
            from screen_share import public_status
            payload['screenShare'] = public_status(share)
        payload.pop('last_plot', None)
        return web.json_response(payload, headers={'Cache-Control': 'no-store'})
    app.router.add_get('/health', status)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 8094).start()
    # The browser and display subprocesses do not inherit project secrets.
    env = {k: v for k, v in os.environ.items() if k not in ('OPENAI_API_KEY', 'MEETING_URL', 'MEETING_PASSCODE', 'BRAVE_SEARCH_API_KEY', 'TAVILY_API_KEY')}
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(PulseServer(env=env))
        await stack.enter_async_context(VirtualDisplay(env=env, use_vnc_server=True, vnc_port=5900))
        speaker = await stack.enter_async_context(VirtualSpeaker(env=env, sample_rate=24000, byte_depth=2, frames_per_chunk=480))
        microphone = await stack.enter_async_context(VirtualMicrophone(env=env, sample_rate=24000, byte_depth=2))
        camera_feed = CameraFeed(
            microphone,
            enabled=runtime.camera_enabled,
            logo_src=runtime.camera_logo_data_uri or None,
        )
        presence = Presence(state, camera_feed)
        presence.sync()
        viewer = await asyncio.create_subprocess_exec('/usr/bin/websockify', '--web=/usr/share/novnc', '6080', '127.0.0.1:5900', env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        # Drain the speaker while waiting for admission, before starting billed audio.
        async def drain():
            while True:
                await speaker.read()
        drain_task = asyncio.create_task(drain())
        try:
            async with joined_meeting(url, runtime.participant_name, os.environ.get('MEETING_PASSCODE', ''), env, stop, stage, state, camera_feed=camera_feed) as (page, adapter):
                state['admissionState'] = 'admitted'
                await apply_platform_camera(
                    adapter,
                    enabled=runtime.camera_enabled,
                    default_on=runtime.camera_default_on,
                    state=state,
                )
                presence.sync()
                stage('connecting_audio')
                await adapter.connect_audio()
                state['chatAvailable'] = await adapter.chat_available()
                drain_task.cancel()
                await asyncio.gather(drain_task, return_exceptions=True)
                await run_voice(speaker, camera_feed.audio_writer, page, api_key, runtime, adapter, meeting_state)
        except Exception as exc:
            state['error'] = type(exc).__name__ + ': ' + str(exc).split('Call log:')[0][:300]
            stage('authentication_required' if isinstance(exc, AuthenticationRequired) else 'needs_attention')
            print(state['error'], flush=True)
            await stop.wait()
        finally:
            drain_task.cancel()
            viewer.terminate()
            await viewer.wait()
            await runner.cleanup()
            if record:
                record.close(
                    usage={'usageSeconds': state.get('usage_seconds', 0)},
                    end_reason=state.get('stage'),
                    stage=state.get('stage'),
                )
                archive = state.get('archive')
                if isinstance(archive, dict):
                    archive['closed'] = True
                    archive['handoffStatus'] = 'local'


if __name__ == '__main__':
    try:
        mode = os.environ.get('COLLEAGUE_AUTH_MODE')
        if mode == 'teams':
            from teams_account import connect
            asyncio.run(connect())
        elif mode == 'google':
            from google_account import connect
            asyncio.run(connect())
        else:
            asyncio.run(main())
    finally:
        if record:
            record.event(
                'process_stopped',
                stage=state['stage'],
                finalized=state['finalized'],
                microphone_state=state.get('microphoneState'),
                generated_audio_bytes=state.get('generated_audio_bytes', 0),
                audible_audio_bytes=state.get('audible_audio_bytes', 0),
                discarded_audio_bytes=state.get('discarded_audio_bytes', 0),
                output_audio_bytes=state.get('output_bytes', 0),
            )
