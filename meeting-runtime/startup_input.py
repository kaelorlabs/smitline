"""Deterministic GPT-Live startup input from a ContextHandoff."""
from context_handoff import ContextHandoff


MESSAGE_LIMIT = 128
TOKEN_LIMIT = 8192


def estimate_tokens(text):
    return max(1, (len(str(text or '')) + 3) // 4) if text else 0


def clip_tokens(text, limit=500):
    text = str(text or '')
    if estimate_tokens(text) <= limit:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= limit:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip()


def _part(role, text):
    kind = 'output_text' if role == 'assistant' else 'input_text'
    return {'type': 'message', 'role': role, 'content': [{'type': kind, 'text': text}]}


def _message_tokens(message):
    return sum(estimate_tokens(part.get('text', '')) for part in message.get('content') or ())


def _developer_text(handoff):
    lines = [
        'Objective: ' + (handoff.objective or ''),
        'Current task: ' + (handoff.current_task or ''),
        'Summary: ' + (handoff.summary or ''),
    ]
    if handoff.constraints:
        lines.append('Constraints: ' + '; '.join(handoff.constraints))
    if handoff.decisions:
        lines.append('Decisions: ' + '; '.join(handoff.decisions))
    if handoff.important_files:
        lines.append('Important files: ' + ', '.join(handoff.important_files))
    return '\n'.join(line for line in lines if line.split(': ', 1)[-1])


def context_text(handoff):
    """The handoff as plain text for backend instructions; empty when there is none."""
    if handoff is None:
        return ''
    lines = [_developer_text(handoff)]
    if handoff.open_questions:
        lines.append('Open questions: ' + '; '.join(handoff.open_questions))
    return '\n'.join(line for line in lines if line)


def handoff_from_state(meeting_state):
    """The meeting's ContextHandoff from its runtime state, or None."""
    payload = (meeting_state or {}).get('context')
    if payload is None or isinstance(payload, ContextHandoff):
        return payload
    try:
        return ContextHandoff.from_dict(payload)
    except (TypeError, ValueError):
        return None


def handoff_to_session_input(handoff):
    if handoff is None:
        return []
    if not isinstance(handoff, ContextHandoff):
        try:
            handoff = ContextHandoff.from_dict(handoff)
        except (TypeError, ValueError):
            return []
    messages = []
    developer = _developer_text(handoff)
    if developer:
        messages.append(_part('developer', developer))
    for turn in handoff.recent_conversation:
        role = turn.role if hasattr(turn, 'role') else turn.get('role')
        text = turn.text if hasattr(turn, 'text') else turn.get('text')
        if role in ('user', 'assistant') and text:
            messages.append(_part(role, text))
    if handoff.open_questions:
        messages.append(_part('user', 'Open questions: ' + '; '.join(handoff.open_questions)))
    return truncate_session_input(messages)


def truncate_session_input(messages):
    selected = list(messages[:MESSAGE_LIMIT])
    while selected and sum(_message_tokens(item) for item in selected) > TOKEN_LIMIT:
        if len(selected) > 1:
            selected.pop(1 if selected[0]['role'] == 'developer' else 0)
        else:
            text = selected[0]['content'][0]['text']
            selected[0]['content'][0]['text'] = clip_tokens(text, TOKEN_LIMIT)
            break
    while len(selected) > MESSAGE_LIMIT:
        selected.pop(1 if selected and selected[0]['role'] == 'developer' else 0)
    return selected
