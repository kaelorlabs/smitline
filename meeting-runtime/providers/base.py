"""Provider adapter boundary for delegated coding-agent work."""


class ProviderRequest:
    def __init__(self, *, delegation_id, request_text, handoff=None, transcript='', model=None,
                 permissions=None, workspace=None, provider=None, session_status='live',
                 session_id=None, continuity=None, authorize_model=False, meeting_id=None,
                 on_progress=None, source=None, approval_gate=None):
        self.delegation_id = delegation_id
        self.request_text = request_text
        self.handoff = handoff
        self.transcript = transcript
        self.model = model
        self.permissions = permissions
        self.workspace = workspace
        self.provider = provider
        self.session_status = session_status
        self.session_id = session_id
        self.continuity = continuity
        self.authorize_model = authorize_model
        self.meeting_id = meeting_id
        self.on_progress = on_progress
        self.source = source
        self.approval_gate = approval_gate


class CodingAgentProvider:
    async def validate_session(self, ref):
        from session_continuity import validate_agent_session
        mode = validate_agent_session(ref)
        return {'ok': True, 'continuity': mode}

    async def acquire(self, ref, meeting_id=None):
        return None

    async def run(self, request, cancel):
        raise NotImplementedError

    async def cancel(self, delegation_id):
        return None

    async def append_handoff(self, request, handoff, cancel=None):
        return {'error': 'append_handoff is not implemented'}

    async def release(self, ref):
        return None
