"""Meeting browser participant with GPT-Live audio; no local speech models."""
import old_names
old_names.adopt_old_settings()

import asyncio
import base64
import contextlib
import dataclasses
import os
import re
import signal
import time
from contextlib import AsyncExitStack
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from call_record import CallRecord
from startup_input import clip_tokens, context_text, handoff_from_state, handoff_to_session_input
from runtime_config import RuntimeConfig, meeting_state_from_environ, resolve_meeting_url
from transcript_assembler import TranscriptAssembler
from listening import AudioTap
from voice_core import append_event, backend_usage_from, function_call_from, function_output_events
import call_requests
from meeting_control import ArchiveDoor, MeetingControl, add_routes, archive_grace, health_route
from meeting_connection import joined_meeting
from meeting_input import InputMonitor
from meeting_intro import (
    addressing_instructions, camera_caption, identity_instructions, introduce, intro_instructions,
    others_in, participants_note, speaker_note)
from adapters_base import AuthenticationRequired
from meeting_urls import platform_for_url
from participation import Participation
from presence_pack import load_presence_pack
from meeting_lifecycle import (
    BACKEND_LEAVING, LEAVE_ASK_SECONDS, LEAVE_MEETING_TOOL, LEAVING_INSTRUCTIONS, AloneWatch,
    LeaveJudge,
    asks_to_leave, is_idle, leave_end_reason, only_closing, says_goodbye, should_leave_alone)
import meeting_lifecycle
import meeting_voice
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
         'backend_status': 'idle', 'backend_tokens': {'input': 0, 'cached': 0, 'output': 0, 'webSearches': 0}}
state.update(cameraEnabled=True, cameraState='starting', visualState='joining')
stop = asyncio.Event()
record = None
presence = None
# How often the participant list is read; a change is told to the voice as quiet context.
PARTICIPANTS_EVERY = 20.0
# Both sides of the meeting as GPT-Live hears and speaks them, for the owner listening in.
listening = AudioTap('pcm16', 24000)
# The daemon's way in (/control) and the voice's requests out; main() sets it up.
control = MeetingControl('', state, owner='')
# GET /archive: the closed archive for a daemon that cannot read this machine's disk.
archive_door = ArchiveDoor()


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


PRIVATE_NAMES = frozenset({'MEETING_URL', 'MEETING_PASSCODE', 'TWILIO_ACCOUNT_SID'})
PRIVATE_SUFFIX = re.compile(r'(?:KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE)$')


def browser_environment(environ):
    """Everything in .env reaches this container; keep keys and meeting details from the browser."""
    return {k: v for k, v in environ.items() if k not in PRIVATE_NAMES and not PRIVATE_SUFFIX.search(k)}


class MeetingTranscript:
    """Writes transcript entries to the meeting archive and the live captions on /health."""

    def __init__(self, record, state, assembler=None):
        self.record = record
        self.state = state
        self.assembler = assembler or TranscriptAssembler()
        # Each caption's seq is its place among the archive's transcript events, so the daemon
        # can follow the meeting live and later take the rest from the archive without repeats.
        self.count = 0

    def note(self, event):
        entry = self.assembler.add_delta(event)
        if entry is None:
            return None
        speaker = 'meeting' if entry.source == 'input' else 'agent'
        # Who said it, when the meeting shows who is speaking (follow_speakers): best effort, as
        # the words arrive a moment after they are said.
        name = self.state.get('lastSpeaker') if speaker == 'meeting' else None
        named = {'name': name} if name else {}
        if self.record is not None:
            self.record.transcript(
                speaker,
                entry.text,
                bool(self.state.get('muted')),
                start_ms=entry.start_offset_ms,
                end_ms=entry.end_offset_ms,
                source=entry.source,
                event_id=entry.id,
                **named,
            )
        captions = self.state.setdefault('captions', [])
        captions.append({
            'seq': self.count,
            'speaker': speaker,
            'text': entry.text,
            'start_ms': entry.start_offset_ms,
            'end_ms': entry.end_offset_ms,
            **named,
        })
        self.state['captions'] = captions[-200:]
        self.count += 1
        return entry


BACKEND_WORKING = frozenset({'response.created', 'response.in_progress'})
BACKEND_DONE = frozenset({
    'response.completed', 'response.failed', 'response.incomplete', 'response.cancelled',
})


def note_backend_event(event, state):
    """Track Responses-delegation progress and token usage from a response.event envelope."""
    inner = event.get('event') if isinstance(event.get('event'), dict) else {}
    kind = inner.get('type')
    if kind in BACKEND_WORKING:
        state['backend_status'] = 'working'
    elif kind in BACKEND_DONE:
        state['backend_status'] = 'idle'
    usage = backend_usage_from(event)
    if usage:
        tokens = state.setdefault('backend_tokens', {})
        for key, value in usage.items():
            tokens[key] = tokens.get(key, 0) + value
    return usage


def backend_instructions(runtime, meeting_state=None):
    return meeting_voice.backend_instructions(
        runtime.participant_name, runtime.owner_name, can_do=(meeting_state or {}).get('canDo'),
        guidance=runtime.meeting_instructions,
        context=context_text(handoff_from_state(meeting_state)))


def meeting_delegation_config(runtime, meeting_state=None):
    """Responses delegation: GPT-Live hands hard questions to a backend model, as phone calls do."""
    responses = {
        'model': runtime.backend_model,
        'instructions': backend_instructions(runtime, meeting_state),
        'tool_choice': 'auto',
        'tools': [LEAVE_MEETING_TOOL],
    }
    tools = []
    if (meeting_state or {}).get('canDo'):
        tools.append(call_requests.ask_agent_tool(
            runtime.owner_name or 'the person who invited them'))
    if runtime.web_search:
        tools.append({'type': 'web_search'})
    responses['tools'].extend(tools)
    return {'type': 'responses', 'responses': responses}


def build_session_config(runtime, meeting_state=None):
    meeting_state = meeting_state or {}
    config = {'model': 'gpt-live-1', 'store': False,
              'audio': {'format': {'type': 'audio/pcm', 'rate': 24000}},
              'instructions': meeting_voice.policy(runtime.participant_name),
              'delegation': meeting_delegation_config(runtime, meeting_state)}
    config['instructions'] += addressing_instructions(runtime.participant_name)
    config['instructions'] += LEAVING_INSTRUCTIONS
    config['instructions'] += identity_instructions(runtime.owner_name)
    if meeting_state.get('canDo'):
        config['instructions'] += call_requests.voice_instructions(
            runtime.owner_name or 'the person who invited them', meeting_state['canDo'],
            meeting=True)
    if runtime.voice:
        config['audio']['output'] = {'voice': runtime.voice}
    if runtime.meeting_intro:
        config['instructions'] += intro_instructions(runtime.owner_name,
                                                     bool(meeting_state.get('canDo')))
    if runtime.persona:
        config['instructions'] += '\nWho you are in this meeting: ' + runtime.persona
    if runtime.meeting_instructions:
        config['instructions'] += (
            '\nOrganizer-provided meeting guidance: ' + runtime.meeting_instructions +
            '\nUse this guidance to understand the meeting and your role where it is compatible with the policies above.')
    incoming = handoff_to_session_input(handoff_from_state(meeting_state))
    if incoming:
        config['input'] = incoming
    return config


# Seconds a goodbye gets to be heard before the agent leaves.
LEAVE_SPEECH_TIMEOUT = 10.0
# Who is speaking: asked this often (an adapter may set speaker_poll_seconds), and told to the
# voice once someone has held the floor this long, so a passing "yeah" does not count.
SPEAKER_POLL_SECONDS = 1.0
SPEAKER_HOLD_SECONDS = 0.8


async def run_voice(speaker, microphone, page, api_key, runtime, adapter, meeting_state=None):
    meeting_state = meeting_state or {}
    config = build_session_config(runtime, meeting_state)
    record.event('session_config', config=config)
    async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=30)) as client:
        async with client.ws_connect('wss://api.openai.com/v1/live/sessions', headers={'Authorization': f'Bearer {api_key}'}, max_msg_size=8*1024*1024) as ws:
            await ws.send_json({'type': 'session.start', 'session': config})
            ready = asyncio.Event()
            finished = asyncio.Event()
            participation = Participation(
                adapter, microphone, state, on_presence=_sync_presence, record=record)
            gate = participation
            transcript = MeetingTranscript(record, state)
            room_input = InputMonitor()  # the room's audio as it goes to GPT-Live
            last_speech = [time.monotonic()]
            leaving = []
            # Asked to leave, then a goodbye: the meeting is left once that has been heard.
            turns = {'meeting': '', 'agent': '', 'last': None}
            asked_to_leave = [None]  # when someone asked the agent to leave in so many words
            goodbye = [None]  # when the agent last said goodbye
            judged = [None]  # the goodbye the leave check last decided to stay after
            judge = LeaveJudge(api_key)
            said_after_goodbye = ['']
            heard_at = [None]  # when the meeting was last heard

            async def leave(reason, why):
                # Every way out goes through stop: GPT-Live closes (final usage) and the bot leaves.
                state['endReason'] = why
                record.event('auto_leave' if reason is None else 'agent_left',
                             reason=why if reason is None else reason)
                try:
                    if reason is not None:
                        await participation.finish_speaking(LEAVE_SPEECH_TIMEOUT)
                    await gate.stop_output()
                finally:
                    stage('meeting_ended')
                    stop.set()

            heard = [None]  # when someone in the meeting was last heard; the intro waits for a pause
            requests = []

            async def send_note(kind, text):
                payload = append_event(f'session.{kind}.append', text)
                if payload is None or not ready.is_set() or stop.is_set():
                    return False
                await ws.send_json(payload)
                record.event('control_note', note=kind)
                return True

            async def answer_request(call):
                # Who asked: the backend's name for them, else whoever the meeting shows speaking.
                try:
                    speaker = await adapter.get_active_speaker()
                except Exception:
                    speaker = None
                said = call_requests.context_lines(state.get('captions', []))
                output = control.ask(call['arguments'], asked_by=speaker or '', context=said)
                record.event('agent_request', status=output.get('status'),
                             request=output.get('requestId'))
                try:
                    for payload in function_output_events(call['call_id'],
                                                          call_requests.tool_output(output)):
                        await ws.send_json(payload)
                except Exception as error:
                    # The session is closing; the request itself already went out on /health.
                    record.event('agent_request_unanswered', error=type(error).__name__)

            async def pass_on_claims():
                """The safety net: a request the voice said it passed on, without its backend."""
                await ready.wait()
                while not stop.is_set():
                    await asyncio.sleep(1)
                    if not control.claim_pending():
                        continue
                    try:
                        speaker = await adapter.get_active_speaker()
                    except Exception:
                        speaker = None
                    message = control.claims_due(asked_by=speaker or '')
                    if message is not None:
                        record.event('agent_request', status='from_speech', request=message['id'])

            control.adapter = adapter
            control.record = record.event
            control.send = send_note
            # Seconds since anyone in the meeting, or the voice itself, was last heard.
            control.quiet = lambda: time.monotonic() - max(heard[0] or float('-inf'),
                                                           participation.last_output_audio,
                                                           last_speech[0] or float('-inf'))

            async def send_audio():
                await ready.wait()
                try:
                    while not stop.is_set():
                        chunk = await speaker.read()
                        room_input.feed(chunk.data)
                        state['input_bytes'] += len(chunk.data)
                        state['input'] = room_input.stats(speaker)
                        audio = base64.b64encode(chunk.data).decode()
                        await ws.send_json({'type': 'session.input_audio.append', 'audio': audio})
                        listening.push('meeting', audio)
                finally:
                    # Nobody reads the room any more: what still comes in is not lost speech.
                    stop_listening = getattr(speaker, 'stop_listening', None)
                    if stop_listening is not None:
                        stop_listening()
                    # What came in, in the archive before it closes (a machine of its own hands
                    # the archive over before process_stopped is written).
                    record.event('input_audio', **room_input.stats(speaker))

            async def watch_microphone():
                # Also accepts a host's explicit request to unmute (Participation.accept_host_unmute).
                await ready.wait()
                while not stop.is_set():
                    await asyncio.sleep(.2)
                    actual = await adapter.get_microphone_state()
                    await gate.platform_microphone_changed(actual)

            def last_heard():
                # Someone talking in the room's audio, or their words: the words come a second or
                # more after the audio, so going by them alone the hello cut in over people.
                times = [at for at in (room_input.last_voiced_at, heard[0]) if at is not None]
                return max(times) if times else None

            async def introduce_once():
                if await introduce(ws.send_json, runtime, participation, ready, stop,
                                   heard_at=last_heard,
                                   takes_requests=bool(meeting_state.get('canDo'))):
                    state['introduced'] = True
                    record.event('ai_disclosure_cued')

            participants = {'names': None, 'read_at': None, 'said_none': False}

            async def tell_who_is_here():
                """Read who is in the meeting now and then; tell the voice when it changes."""
                now = time.monotonic()
                if participants['read_at'] is not None and now - participants['read_at'] < PARTICIPANTS_EVERY:
                    return
                participants['read_at'] = now
                try:
                    names = others_in(await adapter.get_participant_names(), runtime.participant_name)
                except Exception:
                    names = []
                if not names and not participants['said_none']:
                    participants['said_none'] = True  # once: the list could not be read here
                    record.event('participants_unavailable', platform=state.get('platform'))
                if names and names != participants['names']:
                    participants['names'] = names
                    if await send_note('thinking', participants_note(
                            names, runtime.owner_name, runtime.participant_name)):
                        record.event('participants', count=len(names))

            async def follow_speakers():
                """Tell the voice who is speaking once that has held for a moment, so a reply can
                go to the right person by name; the transcript names them too (lastSpeaker)."""
                await ready.wait()
                poll = getattr(adapter, 'speaker_poll_seconds', SPEAKER_POLL_SECONDS)
                current, since, told = None, 0.0, None
                while not stop.is_set():
                    await asyncio.sleep(poll)
                    try:
                        name = await adapter.get_active_speaker()
                    except Exception:
                        name = None
                    if not isinstance(name, str) or not name.strip() or name == runtime.participant_name:
                        name = None
                    state['activeSpeaker'] = name
                    if name:
                        state['lastSpeaker'] = name
                    silent = room_input.speaker(name)
                    if silent:  # the meeting shows people talking, and nothing comes in
                        record.event('input_silent', **silent)
                    now = time.monotonic()
                    if name != current:
                        current, since = name, now
                        continue
                    if name and name != told and now - since >= SPEAKER_HOLD_SECONDS:
                        if await send_note('thinking', speaker_note(name, runtime.owner_name)):
                            told = name
                            record.event('speaker_noted')

            async def watch_meeting():
                await ready.wait()
                alone = AloneWatch()
                while not stop.is_set():
                    await asyncio.sleep(3)
                    state['microphoneState'] = await adapter.get_microphone_state()
                    state['chatAvailable'] = await adapter.chat_available()
                    if await adapter.has_ended():
                        stage('meeting_ended')
                        stop.set()
                        return
                    await tell_who_is_here()
                    try:
                        count = await adapter.get_participant_count()
                    except Exception:
                        count = None
                    state['participantCount'] = count
                    if leaving:
                        return
                    if alone.update(count):
                        await leave(None, 'alone_in_meeting')
                        return
                    if is_idle(last_speech[0], time.monotonic(), runtime.idle_leave_seconds):
                        await leave(None, 'idle')
                        return

            def note_turn(kind, delta):
                """Follow who said what, for leaving once asked and after the goodbye."""
                if not delta.strip():
                    return
                now = time.monotonic()
                side = 'meeting' if kind == 'session.input_transcript.delta' else 'agent'
                if turns['last'] != side:
                    turns[side] = ''
                turns[side] += delta
                turns['last'] = side
                if side == 'meeting':
                    heard_at[0] = now
                    if asks_to_leave(turns['meeting'][-240:]):
                        asked_to_leave[0], goodbye[0] = now, None
                    elif goodbye[0] is not None:
                        said_after_goodbye[0] += delta
                elif goodbye[0] is None and says_goodbye(turns['agent']):
                    goodbye[0], said_after_goodbye[0] = now, ''

            async def leave_after_goodbye():
                """After the agent's goodbye has been heard and the meeting is quiet for a moment,
                leave without waiting for the backend's leave_meeting: at once when someone asked
                it to leave, otherwise when the leave check of the end of the meeting says so."""
                await ready.wait()
                quiet_since = None
                while not stop.is_set() and not leaving:
                    await asyncio.sleep(.2)
                    now = time.monotonic()
                    if asked_to_leave[0] is not None and now - asked_to_leave[0] > LEAVE_ASK_SECONDS:
                        asked_to_leave[0] = None
                    if goodbye[0] is None:
                        quiet_since = None
                        continue
                    if not says_goodbye(turns['agent']) or not only_closing(said_after_goodbye[0]):
                        goodbye[0], quiet_since = None, None  # it said more, or they carried on
                        continue
                    if participation.speaking() or (heard_at[0] is not None and now - heard_at[0] < 1.0):
                        quiet_since = None
                        continue
                    quiet_since = quiet_since or now
                    if now - quiet_since < meeting_lifecycle.LEAVE_QUIET_SECONDS:
                        continue
                    reason = 'asked_to_leave' if asked_to_leave[0] is not None else None
                    if reason is None:
                        if judged[0] == goodbye[0]:
                            continue  # it already decided to stay after this goodbye
                        said_at = judged[0] = goodbye[0]
                        decided, why = await judge.decide(state.get('captions') or [],
                                                          runtime.participant_name)
                        record.event('leave_judged', leave=decided, reason=why)
                        if (not decided or leaving or goodbye[0] != said_at
                                or not only_closing(said_after_goodbye[0])):
                            continue
                        reason = why
                    record.event('goodbye_leave')
                    leaving.append(asyncio.create_task(
                        leave(reason, leave_end_reason(reason)), name='agent_leave'))
                    return

            async def closer():
                await stop.wait()
                state['finalizing'] = True
                _sync_presence()
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
                ('ai_disclosure', introduce_once),
                ('meeting_lifecycle', watch_meeting),
                ('speaker_notes', follow_speakers),
                ('leave_after_goodbye', leave_after_goodbye),
                ('request_safety_net', pass_on_claims),
                ('session_closer', closer),
            ]
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
                        last_speech[0] = time.monotonic()
                        ready.set()
                        state['listening'] = True
                        stage('live')
                    elif kind == 'session.output_audio.delta':
                        listening.push('agent', event['delta'])
                        data = base64.b64decode(event['delta'])
                        # Fail rather than silently accumulate seconds of stale speech.
                        gate.offer(data)
                    elif kind in ('session.input_transcript.delta', 'session.output_transcript.delta'):
                        note_turn(kind, event.get('delta') or '')
                        if (event.get('delta') or '').strip():
                            last_speech[0] = time.monotonic()
                        if kind == 'session.input_transcript.delta' and (event.get('delta') or '').strip():
                            heard[0] = time.monotonic()
                            # Talked over, the voice stops, as on a phone call (participation).
                            participation.heard(event.get('start_ms'), event.get('end_ms'))
                        elif (event.get('delta') or '').strip():
                            participation.said()
                        control.heard('agent' if kind == 'session.output_transcript.delta' else 'meeting',
                                      event.get('delta') or '')
                        transcript.note(event)
                    elif kind == 'response.event':
                        before = state.get('backend_status')
                        note_backend_event(event, state)
                        call = function_call_from(event)
                        if call and call['name'] == 'ask_agent' and control.desk is not None:
                            requests.append(asyncio.create_task(
                                answer_request(call), name='agent_request'))
                        elif call and call['name'] == 'leave_meeting' and not leaving:
                            # Leaving needs no tool result or further backend turn.
                            reason = call['arguments'].get('reason') or 'other'
                            leaving.append(asyncio.create_task(
                                leave(reason, leave_end_reason(reason)), name='agent_leave'))
                            leaving[0].add_done_callback(task_failed)
                        if state.get('backend_status') != before:
                            _sync_presence()
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
                        error = event.get('error') or {}
                        state['api_error'] = error.get('code') or 'live_error'
                        record.event('api_error', code=state['api_error'],
                                     message=str(error.get('message') or '')[:300])
                        if not ready.is_set():
                            stage('api_error')
                            raise RuntimeError('GPT-Live rejected session startup')
                        # After start-up an error refuses one event (a tool result, a note),
                        # and the session goes on: the meeting continues, as a phone call does.
                    # Unknown future events are ignored so audio and transcripts continue.
            finally:
                control.send = None
                control.quiet = None
                for task in tasks + requests + leaving:
                    task.cancel()
                await asyncio.gather(*tasks, *requests, *leaving, return_exceptions=True)


def presence_pack_for(runtime):
    """The presence pack for the mascot style, or None. A pack that cannot be read leaves the
    still picture: a meeting never fails over its picture."""
    if runtime.camera_style != 'mascot':
        return None
    try:
        if not runtime.camera_pack:
            raise ValueError('SMITLINE_MEETING_CAMERA_STYLE=mascot needs SMITLINE_MEETING_CAMERA_PACK, '
                             'the folder of a presence pack')
        return load_presence_pack(runtime.camera_pack)
    except ValueError as exc:
        print(f'{exc}. The camera shows the still picture instead.', flush=True)
        return None


def with_persona(runtime, pack, meeting_state=None):
    """The agent appears as the pack's character, so it takes the character's persona, and its
    voice unless the brief named one (a brief's voice reaches the meeting as `voice`)."""
    if pack is None:
        return runtime
    changes = {}
    if pack.persona:
        changes['persona'] = pack.persona
    if pack.voice and not (meeting_state or {}).get('voice'):
        from call_brief import available_voices
        if pack.voice in available_voices(os.environ):
            changes['voice'] = pack.voice
        else:
            print(f'The presence pack names voice {pack.voice!r}, which GPT-Live does not have; '
                  'the meeting keeps its own.', flush=True)
    return dataclasses.replace(runtime, **changes) if changes else runtime


def camera_feed_for(runtime, microphone, pack=None):
    """The browser meeting camera for these settings (SMITLINE_MEETING_CAMERA_STYLE)."""
    style = runtime.camera_style if runtime.camera_style != 'mascot' or pack else 'still'
    return CameraFeed(
        microphone,
        enabled=runtime.camera_enabled,
        logo_src=runtime.camera_logo_data_uri or None,
        style=style,
        caption=camera_caption(runtime.owner_name),
        pack=pack.page_payload() if pack is not None else None,
    )


async def main():
    global record, presence, control
    runtime = RuntimeConfig.from_environ()
    # Meet and Teams find the active speaker by skipping the bot's own tile, by this name.
    os.environ['JOINLY_NAME'] = runtime.participant_name
    meeting_state = meeting_state_from_environ()
    pack = presence_pack_for(runtime)
    runtime = with_persona(runtime, pack, meeting_state)
    state['participant_name'] = runtime.participant_name
    state['meeting_guidance_configured'] = bool(runtime.meeting_instructions)
    state['cameraEnabled'] = runtime.camera_enabled
    state['cameraState'] = 'off' if not runtime.camera_enabled else 'starting'
    state['visualState'] = 'joining'
    state['backend'] = {'type': 'responses', 'model': runtime.backend_model,
                        'web_search': runtime.web_search}
    control = MeetingControl(os.environ.get('SMITLINE_CONTROL_TOKEN'), state,
                             owner=runtime.owner_name or 'the person who invited them',
                             can_do=meeting_state.get('canDo'))
    control.stop = stop.set  # what SIGTERM does, for a daemon on another machine
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
    record.event('started')
    api_key = os.environ['OPENAI_API_KEY']
    url = resolve_meeting_url()
    state['platform'] = platform_for_url(url)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    app = web.Application()

    def public_state():
        payload = dict(state)
        payload.update(presence_public_fields(state))
        return payload
    # On a machine of its own the port is reachable over a network, so /health takes the token.
    app.router.add_get('/health', health_route(
        control, public_state, token_required=os.environ.get('SMITLINE_HEALTH_TOKEN') == '1'))

    # /listen, /control and /archive: for the daemon that started this meeting alone
    # (meeting_control.py).
    add_routes(app, control, listening, archive=archive_door)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 8094).start()
    if state['platform'] == 'daily':
        # A Daily room needs no browser, display or PulseAudio: daily-python joins it (daily_room.py).
        from daily_room import joined_room
        try:
            async with joined_room(url, runtime, stop, stage, state, pack=pack,
                                   caption=camera_caption(runtime.owner_name)) as (adapter, speaker, camera_feed):
                presence = Presence(state, camera_feed)
                presence.sync()
                state['admissionState'] = 'admitted'
                await apply_platform_camera(adapter, enabled=runtime.camera_enabled,
                                            default_on=runtime.camera_default_on, state=state)
                presence.sync()
                stage('connecting_audio')
                await adapter.connect_audio()
                state['chatAvailable'] = await adapter.chat_available()
                await run_voice(speaker, camera_feed.audio_writer, None, api_key, runtime, adapter, meeting_state)
        except Exception as exc:
            state['error'] = type(exc).__name__ + ': ' + str(exc)[:300]
            stage('needs_attention')
            print(state['error'], flush=True)
            if archive_grace(os.environ):
                close_record(runtime)  # a daemon elsewhere fetches it before stopping this one
            await stop.wait()
        finally:
            listening.close()
            # The archive is complete before the server stops, so /archive can hand it over.
            try:
                close_record(runtime)
                await archive_door.wait_fetched(archive_grace(os.environ))
            finally:
                await runner.cleanup()
        return
    # The browser and display subprocesses do not inherit project secrets.
    env = browser_environment(os.environ)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(PulseServer(env=env))
        await stack.enter_async_context(VirtualDisplay(env=env, use_vnc_server=True, vnc_port=5900))
        speaker = await stack.enter_async_context(VirtualSpeaker(env=env, sample_rate=24000, byte_depth=2, frames_per_chunk=480))
        microphone = await stack.enter_async_context(VirtualMicrophone(env=env, sample_rate=24000, byte_depth=2))
        camera_feed = camera_feed_for(runtime, microphone, pack)
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
            if archive_grace(os.environ):
                close_record(runtime)  # a daemon elsewhere fetches it before stopping this one
            await stop.wait()
        finally:
            listening.close()
            drain_task.cancel()
            viewer.terminate()
            await viewer.wait()
            # The archive is complete before the server stops, so /archive can hand it over.
            try:
                close_record(runtime)
                await archive_door.wait_fetched(archive_grace(os.environ))
            finally:
                await runner.cleanup()


def process_stopped_details(state):
    """The meeting process's last event: where it stopped, and what its audio did both ways.
    `input` is what came in from the meeting (InputMonitor.stats): a meeting that heard nobody
    shows voiced_ms 0."""
    return {
        'stage': state['stage'],
        'finalized': state['finalized'],
        'microphone_state': state.get('microphoneState'),
        'generated_audio_bytes': state.get('generated_audio_bytes', 0),
        'audible_audio_bytes': state.get('audible_audio_bytes', 0),
        'discarded_audio_bytes': state.get('discarded_audio_bytes', 0),
        'output_audio_bytes': state.get('output_bytes', 0),
        'input': dict(state.get('input') or {'bytes': state.get('input_bytes', 0)}),
        'interruptions_followed': state.get('interruptionsFollowed', 0),
    }


def close_record(runtime):
    """Close the meeting's record with what it used, once the meeting is over."""
    if archive_door.closed:
        return
    if not record:
        archive_door.close(None)
        return
    usage = {'usageSeconds': state.get('usage_seconds', 0)}
    tokens = state.get('backend_tokens') or {}
    if any(tokens.values()):
        usage['backendTokens'] = dict(tokens)
        usage['backendModel'] = runtime.backend_model
    record.close(
        usage=usage,
        end_reason=state.get('endReason') or state.get('stage'),
        stage=state.get('stage'),
    )
    archive = state.get('archive')
    if isinstance(archive, dict):
        archive['closed'] = True
        archive['handoffStatus'] = 'local'
    archive_door.close(record.directory)


if __name__ == '__main__':
    try:
        mode = os.environ.get('SMITLINE_AUTH_MODE')
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
            record.event('process_stopped', **process_stopped_details(state))
