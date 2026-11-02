"""A call's own recording: both sides as they pass through Smitline, in one two-channel WAV.

Phone providers can record a call, but SignalWire stops recording when its answering-machine
check reports (on 2026-11-02 a call that ran 2 min 35 s left 7 seconds), and a provider's
recordings sit behind its own URLs and account credentials. Every relayed call's audio already
passes through the line as 20 ms mu-law frames at 8 kHz: what the other person says comes in,
what the voice says goes out. The recorder writes the other person on the left channel and the
voice on the right. Incoming frames keep time (the provider sends them steadily, silence
included); the voice's frames are lined up against them.
"""
import array
import base64
import binascii
import math
import os
import struct
import sys
from pathlib import Path

RATE = 8000
SILENCE = b'\xff'  # mu-law zero
# The voice's audio waits for the incoming clock. When the incoming side stalls for this many
# samples (2 s), what the voice said is written anyway, against silence.
MAX_AHEAD = RATE * 2


def _linear(byte):
    """G.711 mu-law to a 16-bit sample."""
    byte = ~byte & 0xFF
    magnitude = (((byte & 0x0F) << 3) + 0x84) << ((byte >> 4) & 0x07)
    return 0x84 - magnitude if byte & 0x80 else magnitude - 0x84


ULAW = array.array('h', (_linear(byte) for byte in range(256)))


def _header(frames):
    data = frames * 4  # two channels of 16-bit samples
    return (b'RIFF' + struct.pack('<I', 36 + data) + b'WAVE'
            + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 2, RATE, RATE * 4, 4, 16)
            + b'data' + struct.pack('<I', data))


def _decode(payload):
    try:
        return base64.b64decode(payload or '')
    except (binascii.Error, ValueError):
        return b''


class CallRecorder:
    """Writes `path` (mode 0600) once the call ends; a .partial file until then."""

    def __init__(self, path):
        self.path = Path(path)
        self._partial = self.path.with_name(self.path.name + '.partial')
        fd = os.open(self._partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self._file = os.fdopen(fd, 'wb')
        self._file.write(_header(0))
        self.frames = 0
        self._ahead = bytearray()  # the voice's audio not yet written
        self.closed = False

    @property
    def seconds(self):
        return self.frames / RATE

    def other(self, payload):
        """A frame from the other person: it keeps time for both channels."""
        audio = _decode(payload)
        if self.closed or not audio:
            return
        voice = bytes(self._ahead[:len(audio)])
        del self._ahead[:len(audio)]
        self._write(audio, voice.ljust(len(audio), SILENCE))

    def agent(self, payload):
        """A frame of the voice, as sent to the provider."""
        audio = _decode(payload)
        if self.closed or not audio:
            return
        self._ahead += audio
        if len(self._ahead) > MAX_AHEAD:
            spill = bytes(self._ahead[:len(self._ahead) - MAX_AHEAD])
            del self._ahead[:len(spill)]
            self._write(SILENCE * len(spill), spill)

    def _write(self, left, right):
        stereo = array.array('h', [0]) * (2 * len(left))
        stereo[0::2] = array.array('h', (ULAW[byte] for byte in left))
        stereo[1::2] = array.array('h', (ULAW[byte] for byte in right))
        if sys.byteorder == 'big':
            stereo.byteswap()
        self._file.write(stereo.tobytes())
        self.frames += len(left)

    def close(self):
        """Finish the file: the voice's last words, the real sizes, then its final name. Returns
        the whole seconds recorded; 0 (and no file) when nothing was."""
        if self.closed:
            return math.ceil(self.seconds)
        self.closed = True
        try:
            if self._ahead:
                self._write(SILENCE * len(self._ahead), bytes(self._ahead))
                self._ahead.clear()
            self._file.seek(0)
            self._file.write(_header(self.frames))
            self._file.flush()
            os.fsync(self._file.fileno())
        finally:
            self._file.close()
        if self.frames:
            os.replace(self._partial, self.path)
        else:
            os.unlink(self._partial)
        return math.ceil(self.seconds)
