"""Versioned context handoff: what the meeting agent should know before it joins."""
from dataclasses import dataclass

from schema_validation import (
    reject_unknown_fields, require_enum, require_field, require_mapping, require_object_list,
    require_string, require_string_list, require_version,
)


CONVERSATION_ROLES = ('user', 'assistant')
HANDOFF_FIELDS = (
    'version', 'objective', 'currentTask', 'summary', 'decisions', 'constraints',
    'openQuestions', 'importantFiles', 'recentConversation',
)


@dataclass(frozen=True)
class ConversationTurn:
    role: str
    text: str

    def to_dict(self):
        return {'role': self.role, 'text': self.text}

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'recentConversation item')
        reject_unknown_fields(payload, ('role', 'text'), 'recentConversation item')
        return cls(
            role=require_enum(require_field(payload, 'role', 'recentConversation item'),
                              'recentConversation item.role', CONVERSATION_ROLES),
            text=require_string(require_field(payload, 'text', 'recentConversation item'),
                                'recentConversation item.text', allow_newlines=True),
        )


@dataclass(frozen=True)
class ContextHandoff:
    version: int
    objective: str
    current_task: str
    summary: str
    decisions: tuple
    constraints: tuple
    open_questions: tuple
    important_files: tuple
    recent_conversation: tuple

    def to_dict(self):
        return {
            'version': self.version,
            'objective': self.objective,
            'currentTask': self.current_task,
            'summary': self.summary,
            'decisions': list(self.decisions),
            'constraints': list(self.constraints),
            'openQuestions': list(self.open_questions),
            'importantFiles': list(self.important_files),
            'recentConversation': [turn.to_dict() for turn in self.recent_conversation],
        }

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'context')
        reject_unknown_fields(payload, HANDOFF_FIELDS, 'context')
        return cls(
            version=require_version(require_field(payload, 'version', 'context')),
            objective=require_string(require_field(payload, 'objective', 'context'), 'objective',
                                     allow_newlines=True),
            current_task=require_string(require_field(payload, 'currentTask', 'context'),
                                        'currentTask', allow_newlines=True),
            summary=require_string(require_field(payload, 'summary', 'context'), 'summary',
                                   allow_empty=True, allow_newlines=True),
            decisions=require_string_list(require_field(payload, 'decisions', 'context'), 'decisions'),
            constraints=require_string_list(require_field(payload, 'constraints', 'context'),
                                            'constraints'),
            open_questions=require_string_list(require_field(payload, 'openQuestions', 'context'),
                                               'openQuestions'),
            important_files=require_string_list(require_field(payload, 'importantFiles', 'context'),
                                                'importantFiles'),
            recent_conversation=require_object_list(
                require_field(payload, 'recentConversation', 'context'),
                'recentConversation', ConversationTurn.from_dict, max_items=128),
        )
