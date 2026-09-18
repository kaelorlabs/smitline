"""Blocking Python SDK example for Codex or Cursor host integrations.

Pass the originating thread id explicitly. Colleague AI never infers ``--last``
or hashes the meeting URL into a session id.
"""

import asyncio
import os

from colleague_ai import Colleague


async def main():
    colleague = Colleague()
    session = {
        'provider': 'codex',
        'sessionId': os.environ['CODEX_THREAD_ID'],
        'workspace': os.getcwd(),
    }
    model = os.environ.get('CODEX_MODEL')
    if model:
        session['model'] = model
    meeting = await colleague.join_meeting({
        'url': os.environ['MEETING_URL'],
        'agentSession': session,
        'permissions': {
            'workspace': 'read-only',
            'commands': 'approval-required',
            'edits': 'disabled',
            'network': 'approval-required',
            'commits': 'disabled',
            'pushes': 'disabled',
        },
    })

    async for event in meeting.events():
        kind = event.get('type') or ''
        if kind.startswith('transcript.'):
            continue
        print(kind, flush=True)
        if kind == 'handoff.ready':
            break

    handoff = await meeting.finished()
    print({
        'meetingId': meeting.id,
        'handoffId': handoff.get('handoffId'),
        'archivePath': handoff.get('archivePath'),
    })


if __name__ == '__main__':
    asyncio.run(main())
