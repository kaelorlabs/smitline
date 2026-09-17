"""GPT-Live client-delegation controller: transcript lookup, progress, and results."""
import asyncio
import inspect
from uuid import uuid4

from context_handoff import ContextHandoff
from plot_share import create_and_share_plot
from permissions import permission_mode
from providers.base import ProviderRequest
from session_continuity import CONTEXT, continuity_from_payload
from startup_input import clip_tokens
from transcript_assembler import TranscriptAssembler

from delegation_router import DelegationRouter


def permissions_from_state(meeting_state, runtime):
    payload = (meeting_state or {}).get('permissions')
    if isinstance(payload, dict) and payload:
        return payload
    return {
        'workspace': 'read-only',
        'commands': 'disabled',
        'edits': 'disabled',
        'network': 'allowed' if runtime.web_search_enabled else 'disabled',
        'commits': 'disabled',
        'pushes': 'disabled',
    }


def handoff_from_state(meeting_state):
    payload = (meeting_state or {}).get('context')
    if payload is None:
        return None
    if isinstance(payload, ContextHandoff):
        return payload
    try:
        return ContextHandoff.from_dict(payload)
    except (TypeError, ValueError):
        return payload


def speakable_result(result, plot_share=None):
    if result is None or not isinstance(result, dict):
        return 'I could not complete that request. Please try again.'
    error = result.get('error')
    if error == 'cancelled':
        return 'I stopped that request.'
    if error == 'no spoken request was available at the delegation offset':
        return 'I did not catch a request I can act on. Please repeat the question.'
    if error in ('approval_denied', 'denied'):
        return 'That action was denied.'
    if error in ('approval_expired', 'expired'):
        return 'That approval expired before it was decided.'
    if error in ('approval_cancelled', 'cancelled_approval'):
        return 'That approval was cancelled.'
    if error in ('conflict',):
        return 'I saved the proposed patch without changing your local files because they were edited.'
    if error in ('unsupported', 'commits_disabled', 'pushes_disabled'):
        return 'That action is not available in this meeting.'
    if result.get('summary') and result.get('status') in (
            'completed', 'failed', 'conflict', 'unsupported', 'denied', 'cancelled'):
        return clip_tokens(str(result.get('summary')), 500)
    if error:
        return clip_tokens('I could not complete that request. ' + str(error), 500)
    text = (result.get('text') or '').strip()
    if plot_share:
        status = plot_share.get('status') or plot_share.get('plot_share', {}).get('status')
        if status == 'saved_locally':
            text = (text + ' A chart was saved locally but not sent to meeting chat.').strip()
        elif status == 'upload_submitted':
            text = (text + ' A chart was submitted to meeting chat; delivery is unconfirmed.').strip()
    if not text:
        return 'The workspace lookup finished with no additional detail.'
    return clip_tokens(text, 500)


class ClientDelegation:
    def __init__(self, *, send, record, state, runtime, meeting_state=None, assembler=None,
                 router=None, page=None, adapter=None, stop_event=None, progress_interval=8.0,
                 on_presence=None, approval_gate=None):
        self.send = send
        self.record = record
        self.state = state
        self.runtime = runtime
        self.meeting_state = meeting_state or {}
        self.assembler = assembler or TranscriptAssembler()
        self.page = page
        self.adapter = adapter
        self.stop_event = stop_event
        self.progress_interval = progress_interval
        self.on_presence = on_presence
        self.approval_gate = approval_gate
        self._closed = False
        self._seen = set()
        self._cancels = {}
        self._tasks = set()
        if isinstance(state, dict):
            state.setdefault('delegationsOpen', True)
            state.setdefault('acceptingDelegations', True)
        self.router = router or DelegationRouter(event_sink=self._sink)
        if router is not None:
            previous = router.event_sink
            if previous is None:
                router.event_sink = self._sink
            elif previous is not self._sink:
                def combined(event_type, **payload):
                    self._sink(event_type, **payload)
                    previous(event_type, **payload)
                router.event_sink = combined

    def _sink(self, event_type, **payload):
        if self.record is not None:
            self.record.event(event_type, **payload)
        if event_type == 'delegation.started':
            self.state['backend_status'] = 'working'
        elif event_type in ('delegation.completed', 'delegation.cancelled'):
            if not self._open_work():
                self.state['backend_status'] = 'idle'
                self.state['awaitingApproval'] = False
        if self.on_presence:
            self.on_presence()

    def _open_work(self):
        return any(not task.done() for task in self._tasks)

    def closed(self):
        if self._closed:
            return True
        return bool(self.stop_event is not None and self.stop_event.is_set())

    async def _send(self, payload):
        if self.closed():
            return
        await self.send(payload)

    async def append(self, kind, delegation_id, content):
        text = clip_tokens(content, 500)
        if not text:
            return
        await self._send({
            'type': kind,
            'event_id': kind.split('.')[1] + '-' + uuid4().hex[:12],
            'delegation_id': delegation_id,
            'content': text,
        })

    def note_transcript(self, event):
        entry = self.assembler.add_delta(event)
        if entry is None:
            return None
        if self.record is not None:
            self.record.transcript(
                'meeting' if entry.source == 'input' else 'agent',
                entry.text,
                bool(self.state.get('muted')),
                start_ms=entry.start_offset_ms,
                end_ms=entry.end_offset_ms,
                source=entry.source,
                event_id=entry.id,
            )
        captions = self.state.setdefault('captions', [])
        captions.append({
            'speaker': 'meeting' if entry.source == 'input' else 'agent',
            'text': entry.text,
            'start_ms': entry.start_offset_ms,
            'end_ms': entry.end_offset_ms,
        })
        self.state['captions'] = captions[-200:]
        return entry

    def submit(self, event):
        if self.closed():
            return None
        delegation = event.get('delegation') if isinstance(event.get('delegation'), dict) else {}
        target = delegation.get('target')
        delegation_id = delegation.get('id')
        if target != 'client' or not isinstance(delegation_id, str) or not delegation_id:
            return None
        if delegation_id in self._seen:
            return None
        self._seen.add(delegation_id)
        cancel = asyncio.Event()
        self._cancels[delegation_id] = cancel
        task = asyncio.create_task(self._run(event, delegation_id, cancel),
                                   name='delegation-' + delegation_id)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        self._tasks.discard(task)
        if not self._open_work() and self.state.get('backend_status') in ('working', 'waiting_approval'):
            self.state['backend_status'] = 'idle'
            self.state['awaitingApproval'] = False
            if self.on_presence:
                self.on_presence()

    def _request(self, event, delegation_id):
        offset = event.get('offset_ms')
        spoken = self.assembler.relevant_request(offset) or ''
        transcript = self.assembler.bounded_transcript(offset)
        provider = self.meeting_state.get('provider') or 'codex'
        workspace = self.meeting_state.get('workspace') or self.runtime.workspace
        model = self.meeting_state.get('defaultCodexModel') or self.runtime.default_codex_model
        status = 'live'
        if self.state.get('stage') in ('joining', 'waiting_for_admission'):
            status = 'joining'
        elif self.state.get('stage') in ('finished', 'meeting_ended'):
            status = 'ended'
        continuity = self.meeting_state.get('continuity') or continuity_from_payload(self.meeting_state)
        session_id = self.meeting_state.get('sessionId')
        if continuity == CONTEXT and session_id == 'local-portal':
            session_id = None

        def on_progress(message):
            text = clip_tokens(str(message or ''), 80)
            if text:
                self.router._emit('delegation.progress', delegationId=delegation_id, message=text)

        return ProviderRequest(
            delegation_id=delegation_id,
            request_text=spoken,
            handoff=handoff_from_state(self.meeting_state),
            transcript=transcript,
            model=model,
            permissions=permissions_from_state(self.meeting_state, self.runtime),
            workspace=workspace,
            provider=provider,
            session_status=status,
            session_id=session_id,
            continuity=continuity,
            authorize_model=bool(self.meeting_state.get('authorizeModel')),
            meeting_id=self.meeting_state.get('meetingId'),
            on_progress=on_progress,
            source=self.meeting_state.get('source'),
            approval_gate=self._provider_gate(delegation_id),
        )

    def _provider_gate(self, delegation_id):
        async def gate(payload):
            category = (payload or {}).get('category') or (payload or {}).get('permission')
            return await self.await_approval(
                category,
                (payload or {}).get('summary') or 'Requested action needs approval',
                scope=(payload or {}).get('scope'),
                delegation_id=delegation_id,
            )
        return gate

    async def await_approval(self, category, summary, *, scope=None, delegation_id=None):
        mode = permission_mode(permissions_from_state(self.meeting_state, self.runtime), category)
        if mode == 'allowed':
            return {'status': 'approved', 'mode': 'allowed'}
        if mode != 'approval-required':
            return {'status': 'denied', 'error': 'approval_denied', 'mode': mode}
        if self.approval_gate is None:
            return {'status': 'denied', 'error': 'approval_denied', 'mode': mode}
        previous = self.state.get('backend_status')
        self.state['awaitingApproval'] = True
        self.state['backend_status'] = 'waiting_approval'
        if delegation_id:
            self.router._emit(
                'delegation.progress', delegationId=delegation_id, message='needs approval')
        if self.on_presence:
            self.on_presence()
        try:
            result = self.approval_gate({
                'category': category,
                'summary': summary,
                'scope': scope or {},
                'delegationId': delegation_id,
                'meetingId': self.meeting_state.get('meetingId'),
            })
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            result = {'status': 'denied', 'error': 'approval_denied'}
        finally:
            self.state['awaitingApproval'] = False
            if self._open_work():
                self.state['backend_status'] = previous if previous == 'working' else 'working'
            else:
                self.state['backend_status'] = 'idle'
            if self.on_presence:
                self.on_presence()
        if not isinstance(result, dict):
            return {'status': 'denied', 'error': 'approval_denied'}
        status = result.get('status') or result.get('decision')
        if status == 'approved':
            return result
        if status == 'expired':
            return {'status': 'expired', 'error': 'approval_expired'}
        if status == 'cancelled':
            return {'status': 'cancelled', 'error': 'approval_cancelled'}
        return {'status': 'denied', 'error': 'approval_denied'}

    async def _progress_loop(self, delegation_id, cancel):
        await asyncio.sleep(self.progress_interval)
        while not self.closed() and not cancel.is_set():
            self.router._emit('delegation.progress', delegationId=delegation_id,
                              message='still working')
            await self.append('session.thinking.append', delegation_id,
                              'Still working on that request.')
            await asyncio.sleep(self.progress_interval)

    async def _run(self, event, delegation_id, cancel):
        request = self._request(event, delegation_id)
        progress = None
        try:
            if not self.runtime.codex_enabled:
                result = {'error': 'workspace lookup is disabled for this meeting'}
                self.router._emit('delegation.started', delegationId=delegation_id)
                self.router._emit('delegation.completed', delegationId=delegation_id)
                if not self.closed():
                    await self.append('session.commentary.append', delegation_id,
                                      speakable_result(result))
                return
            if not (request.request_text or '').strip():
                result = {'error': 'no spoken request was available at the delegation offset'}
                self.router._emit('delegation.started', delegationId=delegation_id)
                self.router._emit('delegation.completed', delegationId=delegation_id)
                if not self.closed():
                    await self.append('session.commentary.append', delegation_id,
                                      speakable_result(result))
                return
            await self.append('session.thinking.append', delegation_id,
                              'Looking that up. I will speak the result when it is ready.')
            progress = asyncio.create_task(self._progress_loop(delegation_id, cancel))
            result = await self.router.execute(request, cancel)
            if self.closed() or cancel.is_set():
                return
            shared = None
            if (self.runtime.charts_enabled and self.page is not None and self.record is not None
                    and isinstance(result, dict) and result.get('text')):
                deliver = getattr(getattr(self.adapter, 'capabilities', None), 'file_delivery', False)
                shared = await create_and_share_plot(
                    self.page, result['text'], self.record.directory, deliver=deliver)
                if shared:
                    result = dict(result)
                    result['plot_share'] = shared
                    self.state['last_plot'] = shared
                    if self.record is not None:
                        self.record.event('plot', **shared)
            await self.append('session.commentary.append', delegation_id,
                              speakable_result(result, shared))
        except asyncio.CancelledError:
            cancel.set()
            raise
        except Exception:
            if not self.closed():
                await self.append('session.commentary.append', delegation_id,
                                  'I could not complete that request. Please try again.')
        finally:
            if progress is not None:
                progress.cancel()
                await asyncio.gather(progress, return_exceptions=True)

    def cancel_all(self, reason='session_closed'):
        self._closed = True
        self.router.close()
        for cancel in self._cancels.values():
            cancel.set()
        if isinstance(self.state, dict):
            self.state['delegationsOpen'] = False
            self.state['acceptingDelegations'] = False

    async def close(self):
        self.cancel_all()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
