"""Keys pressed in a phone menu, for briefs with phoneMenus (see docs/phone.md#phone-menus).

The voice asks its backend to press keys; phone_line sends them the way the call's audio
goes: SignalWire takes a dtmf message on the media stream, direct SIP takes
transport.dtmf.send on the session's sideband, and Twilio, whose media stream cannot send
DTMF, gets the tones themselves as audio (dtmf_frames).
"""
import base64
import math

KEYS = frozenset('0123456789*#')
PAUSE = 'w'  # half a second, as in <Play digits>
MAX_KEYS = 32
PAUSE_SECONDS = 0.5
# Between keys sent as the provider's own DTMF: it plays each one for about 100 ms.
KEY_GAP_SECONDS = 0.3
# Tones as audio: 100 ms of each key, then 100 ms of quiet (ITU-T Q.24 receivers take 40 ms).
TONE_MS = 100
GAP_MS = 100
SAMPLE_RATE = 8000
FRAME_SAMPLES = 160  # 20 ms
# A key is two tones. About -10 dBm0 for the low one and -8 dBm0 for the high one (receivers
# expect the high tone a little louder), on the 16-bit scale where a full-scale sine is +3 dBm0.
LOW_AMPLITUDE = 7200
HIGH_AMPLITUDE = 9050
ROWS = (697, 770, 852, 941)
COLUMNS = (1209, 1336, 1477, 1633)
LAYOUT = ('123A', '456B', '789C', '*0#D')
FREQUENCIES = {key: (ROWS[row], COLUMNS[column])
               for row, line in enumerate(LAYOUT) for column, key in enumerate(line)}


def menu_keys(value):
    """The keys to press, from what the backend asked for; ValueError when they are not keys.

    Spaces, dashes, dots and brackets are dropped, so "408-555 1212" is fine; W counts as w.
    """
    if not isinstance(value, str):
        raise ValueError('keys must be a string of digits, * and #')
    keys = ''.join(ch for ch in value if not ch.isspace() and ch not in '-.()').replace('W', PAUSE)
    if not keys.strip(PAUSE):
        raise ValueError('keys must hold at least one digit, * or #')
    if any(ch not in KEYS and ch != PAUSE for ch in keys):
        raise ValueError('keys may hold only digits 0-9, * and #, and w for a pause')
    if sum(ch != PAUSE for ch in keys) > MAX_KEYS:
        raise ValueError(f'at most {MAX_KEYS} keys at a time')
    return keys


def shown_keys(keys):
    """Keys as the call record shows them: a menu choice as it is, a longer entry (an account
    number, say) only as how many keys it was."""
    pressed = keys.replace(PAUSE, '')
    if len(pressed) <= 3:
        return pressed
    return f'{len(pressed)} keys'


def _ulaw(sample):
    """One 16-bit linear sample as G.711 mu-law."""
    sign = 0x80 if sample < 0 else 0
    magnitude = min(abs(int(sample)), 32635) + 0x84
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not magnitude & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def key_audio(key):
    """One key as audio: its two tones for TONE_MS, then GAP_MS of quiet (8 kHz mu-law)."""
    low, high = FREQUENCIES[key]
    tone = bytes(_ulaw(LOW_AMPLITUDE * math.sin(2 * math.pi * low * n / SAMPLE_RATE)
                       + HIGH_AMPLITUDE * math.sin(2 * math.pi * high * n / SAMPLE_RATE))
                 for n in range(SAMPLE_RATE * TONE_MS // 1000))
    return tone + b'\xff' * (SAMPLE_RATE * GAP_MS // 1000)


def key_payload(key):
    """key_audio as the base64 a media stream carries."""
    return base64.b64encode(key_audio(key)).decode('ascii')
