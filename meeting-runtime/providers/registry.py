"""Installed coding-agent provider registry with fail-closed unknown ids."""
from .base import CodingAgentProvider
from .capabilities import unavailable_capabilities
from .claude_code import ClaudeCodeProvider
from .codex import CodexProvider
from .cursor import CursorProvider


KNOWN_PROVIDERS = ('codex', 'cursor', 'claude-code')


class ProviderRegistry:
    def __init__(self, providers=None):
        self._providers = {}
        incoming = dict(providers or {})
        self._providers['codex'] = incoming.get('codex') or CodexProvider()
        self._providers['cursor'] = incoming.get('cursor') or CursorProvider()
        self._providers['claude-code'] = incoming.get('claude-code') or ClaudeCodeProvider()
        for key, value in incoming.items():
            if key not in self._providers:
                self._providers[key] = value

    def get(self, provider_id):
        if provider_id not in self._providers:
            return None
        found = self._providers[provider_id]
        return found if isinstance(found, CodingAgentProvider) else None

    def mapping(self):
        return dict(self._providers)

    def capabilities(self, provider_id=None):
        if provider_id:
            provider = self.get(provider_id)
            if provider is None:
                return unavailable_capabilities(provider_id, 'unknown_provider').to_dict()
            caps = provider.capabilities() if hasattr(provider, 'capabilities') else None
            if hasattr(caps, 'to_dict'):
                return caps.to_dict()
            if isinstance(caps, dict):
                return caps
            return unavailable_capabilities(provider_id, 'capability_unavailable').to_dict()
        return [self.capabilities(name) for name in KNOWN_PROVIDERS]
