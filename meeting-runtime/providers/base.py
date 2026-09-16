"""Provider adapter boundary for delegated coding-agent work."""


class ProviderRequest:
    def __init__(self, *, delegation_id, request_text, handoff=None, transcript='', model=None,
                 permissions=None, workspace=None, provider=None, session_status='live'):
        self.delegation_id = delegation_id
        self.request_text = request_text
        self.handoff = handoff
        self.transcript = transcript
        self.model = model
        self.permissions = permissions
        self.workspace = workspace
        self.provider = provider
        self.session_status = session_status


class CodingAgentProvider:
    async def run(self, request, cancel):
        raise NotImplementedError

    async def cancel(self, delegation_id):
        return None
