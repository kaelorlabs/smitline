"""Serialize client-delegated coding-agent turns for one meeting."""
import asyncio

from providers.base import CodingAgentProvider, ProviderRequest
from providers.codex import CodexProvider


PROVIDER_CODEX = 'codex'


class DelegationRouter:
    def __init__(self, providers=None, default_provider=PROVIDER_CODEX, event_sink=None):
        self.providers = dict(providers or {})
        self.default_provider = default_provider
        if PROVIDER_CODEX not in self.providers:
            self.providers[PROVIDER_CODEX] = CodexProvider()
        self.event_sink = event_sink
        self._turn_lock = asyncio.Lock()
        self._results = {}
        self._closed = False

    def _emit(self, event_type, **payload):
        if self.event_sink is not None:
            self.event_sink(event_type, **payload)

    def _provider_id(self, request):
        requested = (request.provider or '').strip() or self.default_provider
        if requested in self.providers:
            return requested
        if requested != self.default_provider and self.default_provider in self.providers:
            return None
        return requested if requested in self.providers else None

    def close(self):
        self._closed = True

    async def execute(self, request, cancel=None):
        if not isinstance(request, ProviderRequest):
            raise TypeError('request must be a ProviderRequest')
        if request.delegation_id in self._results:
            return self._results[request.delegation_id]
        async with self._turn_lock:
            if request.delegation_id in self._results:
                return self._results[request.delegation_id]
            if self._closed or (cancel is not None and cancel.is_set()):
                result = {'error': 'cancelled'}
                self._results[request.delegation_id] = result
                self._emit('delegation.cancelled', delegationId=request.delegation_id,
                           reason='cancelled')
                return result
            if request.session_status not in ('joining', 'live'):
                result = {'error': 'delegation is only available during an active meeting'}
                self._results[request.delegation_id] = result
                self._emit('delegation.cancelled', delegationId=request.delegation_id,
                           reason='inactive_meeting')
                return result
            provider_id = self._provider_id(request)
            provider = None if provider_id is None else self.providers.get(provider_id)
            if not isinstance(provider, CodingAgentProvider):
                result = {'error': 'unsupported coding agent provider: ' +
                          (request.provider or self.default_provider)}
                self._results[request.delegation_id] = result
                self._emit('delegation.completed', delegationId=request.delegation_id)
                return result
            self._emit('delegation.started', delegationId=request.delegation_id)
            try:
                result = await provider.run(request, cancel)
            except Exception:
                result = {'error': 'delegated coding agent failed'}
            if not isinstance(result, dict):
                result = {'error': 'delegated coding agent returned an invalid result'}
            if self._closed or (cancel is not None and cancel.is_set()):
                result = {'error': 'cancelled'}
                self._emit('delegation.cancelled', delegationId=request.delegation_id,
                           reason='cancelled')
            else:
                if result.get('error') and result.get('error') != 'cancelled':
                    self._emit('delegation.progress', delegationId=request.delegation_id,
                               message=str(result.get('error'))[:200])
                self._emit('delegation.completed', delegationId=request.delegation_id)
            self._results[request.delegation_id] = result
            return result
