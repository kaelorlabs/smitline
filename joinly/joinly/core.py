from typing import Protocol

from joinly.types import AudioChunk, AudioFormat


class AudioReader(Protocol):
    """Protocol for audio stream sources.

    Defines the interface for objects that provide audio data.

    Attributes:
        audio_format (AudioFormat): The format of the audio data being read.
    """

    audio_format: AudioFormat

    async def read(self) -> AudioChunk:
        """Read a chunk of audio data.

        Returns:
            AudioChunk: A chunk of audio data.
        """
        ...


class AudioWriter(Protocol):
    """Protocol for audio output destinations.

    Defines the interface for objects that consume audio data.

    Attributes:
        audio_format (AudioFormat): The format of the audio data being written.
        chunk_size (int): The smallest accepted size of an audio chunk in bytes.
    """

    audio_format: AudioFormat
    chunk_size: int

    async def write(self, data: bytes) -> None:
        """Write audio data to the sink.

        Args:
            data: Raw PCM audio data.
        """
        ...
