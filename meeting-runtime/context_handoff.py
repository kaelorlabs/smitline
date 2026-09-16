"""Versioned context handoff from a coding-agent conversation into a meeting."""
from dataclasses import dataclass

from schema_validation import (
    omit_none, optional_bool, optional_field, optional_string, reject_unknown_fields,
    require_enum, require_field, require_mapping, require_object_list, require_string,
    require_string_list, require_version,
)


CONVERSATION_ROLES = ('user', 'assistant')
GIT_FIELDS = ('branch', 'commit', 'dirty')
HANDOFF_FIELDS = (
    'version', 'objective', 'currentTask', 'summary', 'decisions', 'constraints',
    'openQuestions', 'importantFiles', 'recentConversation', 'git',
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
class GitState:
    branch: str = None
    commit: str = None
    dirty: bool = None

    def to_dict(self):
        return omit_none({
            'branch': self.branch,
            'commit': self.commit,
            'dirty': self.dirty,
        })

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'git')
        reject_unknown_fields(payload, GIT_FIELDS, 'git')
        return cls(
            branch=optional_string(optional_field(payload, 'branch'), 'git.branch', max_length=256),
            commit=optional_string(optional_field(payload, 'commit'), 'git.commit', max_length=64),
            dirty=optional_bool(optional_field(payload, 'dirty'), 'git.dirty'),
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
    git: GitState = None

    def to_dict(self):
        payload = {
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
        if self.git is not None:
            payload['git'] = self.git.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data):
        payload = require_mapping(data, 'context')
        reject_unknown_fields(payload, HANDOFF_FIELDS, 'context')
        git_value = optional_field(payload, 'git')
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
            git=None if git_value is None else GitState.from_dict(git_value),
        )
