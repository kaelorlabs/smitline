"""Exact vs context continuity for originating coding-agent sessions."""
from types import MappingProxyType


CONTEXT_SESSION_IDS = frozenset({'local-portal'})
FORBIDDEN_SESSION_IDS = frozenset({'--last', 'last', 'latest'})
EXACT = 'exact'
CONTEXT = 'context'


def _metadata(ref):
    value = getattr(ref, 'metadata', None)
    if value is None and isinstance(ref, dict):
        value = ref.get('metadata')
    if isinstance(value, MappingProxyType):
        return dict(value)
    return dict(value or {})


def session_id_of(ref):
    if ref is None:
        return None
    if isinstance(ref, dict):
        return ref.get('sessionId') or ref.get('session_id')
    return getattr(ref, 'session_id', None)


def _source(ref):
    return str(_metadata(ref).get('source') or '')


def is_forbidden_session_id(session_id):
    if not isinstance(session_id, str) or not session_id.strip():
        return True
    if session_id in FORBIDDEN_SESSION_IDS or session_id.startswith('-'):
        return True
    return False


def continuity_mode(ref):
    requested = str(_metadata(ref).get('continuity') or '').strip()
    if requested in (EXACT, CONTEXT):
        return requested
    if _source(ref) == 'local-portal' or session_id_of(ref) in CONTEXT_SESSION_IDS:
        return CONTEXT
    if not session_id_of(ref):
        return CONTEXT
    return EXACT


def validate_agent_session(ref):
    session_id = session_id_of(ref)
    if is_forbidden_session_id(session_id):
        raise ValueError('Codex session id must be an explicit originating thread id')
    mode = continuity_mode(ref)
    if mode == EXACT and (
            session_id in CONTEXT_SESSION_IDS or _source(ref) == 'local-portal'):
        raise ValueError('exact Codex session continuity requires a real sessionId')
    return mode


def continuity_from_payload(payload):
    if not payload:
        return CONTEXT
    if payload.get('continuity') in (EXACT, CONTEXT):
        if payload['continuity'] == EXACT:
            session_id = payload.get('sessionId') or payload.get('session_id')
            source = str((payload.get('metadata') or {}).get('source')
                         if isinstance(payload.get('metadata'), dict)
                         else payload.get('source') or '')
            if session_id in CONTEXT_SESSION_IDS or source == 'local-portal':
                return CONTEXT
        return payload['continuity']
    return continuity_mode(payload)
