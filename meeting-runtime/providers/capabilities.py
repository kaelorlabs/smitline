"""Public coding-agent capability records. Unknown or undetectable fields stay false."""
from dataclasses import dataclass

from schema_validation import omit_none, reject_secrets


CAPABILITY_FIELDS = (
    'id', 'installed', 'usable', 'authenticated', 'exactSessionResume', 'contextContinuity',
    'structuredProgress', 'cancellation', 'handoffAppend', 'workspaceRead', 'workspaceActions',
    'supportedModels', 'reasonUnavailable', 'detectedBinary', 'noninteractiveFlag', 'resumeFlag',
    'modelFlag',
)
UNAVAILABLE_REASONS = (
    'missing_binary', 'authentication_required', 'unsupported_version',
    'exact_resume_unsupported', 'capability_unavailable', 'unknown_provider',
)


@dataclass(frozen=True)
class ProviderCapabilities:
    id: str
    installed: bool = False
    usable: bool = False
    authenticated: bool | None = None
    exactSessionResume: bool = False
    contextContinuity: bool = False
    structuredProgress: bool = False
    cancellation: bool = False
    handoffAppend: bool = False
    workspaceRead: bool = False
    workspaceActions: bool = False
    supportedModels: tuple = ()
    reasonUnavailable: str | None = None
    detectedBinary: str | None = None
    noninteractiveFlag: str | None = None
    resumeFlag: str | None = None
    modelFlag: str | None = None

    def to_dict(self):
        payload = {
            'id': self.id,
            'installed': bool(self.installed),
            'usable': bool(self.usable),
            'exactSessionResume': bool(self.exactSessionResume),
            'contextContinuity': bool(self.contextContinuity),
            'structuredProgress': bool(self.structuredProgress),
            'cancellation': bool(self.cancellation),
            'handoffAppend': bool(self.handoffAppend),
            'workspaceRead': bool(self.workspaceRead),
            'workspaceActions': bool(self.workspaceActions),
            'supportedModels': list(self.supportedModels),
        }
        if self.authenticated is not None:
            payload['authenticated'] = bool(self.authenticated)
        if self.reasonUnavailable:
            payload['reasonUnavailable'] = self.reasonUnavailable
        reject_secrets(payload, 'provider capabilities')
        return omit_none(payload)


def unavailable_capabilities(provider_id, reason='missing_binary'):
    return ProviderCapabilities(id=provider_id, reasonUnavailable=reason)
