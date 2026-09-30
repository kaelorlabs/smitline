from dataclasses import dataclass, field


class ProviderNotSupportedError(Exception):
    """Raised when a provider does not support a requested feature."""


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """Properties of pcm audio.

    Attributes:
        sample_rate (int): The sample rate of the audio stream in Hz.
        byte_depth (int): The byte depth of the audio stream in bytes.
    """

    sample_rate: int
    byte_depth: int


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """A chunk of audio data.

    Attributes:
        data (bytes): The raw PCM audio data.
        time_ns (int): The timestamp of the audio chunk in nanoseconds.
        speaker (str | None): The (main) speaker of the audio chunk, if available.
    """

    data: bytes
    time_ns: int
    speaker: str | None = None


@dataclass(frozen=True, slots=True)
class MeetingChatMessage:
    """A chat message in a meeting."""

    text: str
    timestamp: str | None = None
    sender: str | None = None


@dataclass(frozen=True, slots=True)
class MeetingChatHistory:
    """The chat history of a meeting."""

    messages: list[MeetingChatMessage] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MeetingParticipant:
    """A participant in a meeting."""

    name: str
    email: str | None = None
    infos: list[str] = field(default_factory=list)
