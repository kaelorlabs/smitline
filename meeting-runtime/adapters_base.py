"""Meeting platform contract; browser mechanics stay outside voice orchestration."""
from abc import ABC, abstractmethod
import asyncio
import contextlib
from dataclasses import dataclass, asdict

# Seconds a meeting platform gets to let the agent out (its Leave button, then a confirmation)
# before the rest of the meeting's teardown goes on without it.
LEAVE_TIMEOUT = 8.0


async def leave_meeting(adapter, state, *, record=None, page=None, timeout=None):
    """Leave the meeting on its platform and say how it went; True once the agent is out.

    Once per meeting: a later call waits for the first and gets its answer, so the platform is
    never asked twice. state['leaveState'] goes 'leaving', then 'left' or 'leave_failed' (the reason
    in state['leaveError']): /health carries it, and the daemon waits for it before it stops the
    meeting, as a Zoom bot killed before it left kept its tile there for four minutes (2026-11-05).
    record (the archive's event log) gets `left`, or `leave_failed` with the reason. The platform
    gets `timeout` seconds (LEAVE_TIMEOUT). After a leave that failed, the browser's meeting page is
    unloaded (about:blank), so the platform sees the page go rather than a connection that froze.
    Never raises."""
    task = getattr(adapter, '_leaving', None)
    if task is None:
        seconds = LEAVE_TIMEOUT if timeout is None else timeout
        task = asyncio.ensure_future(_leave(adapter, state, record, page, seconds))
        adapter._leaving = task
    return await asyncio.shield(task)


async def _leave(adapter, state, record, page, timeout):
    state['leaveState'] = 'leaving'
    try:
        await asyncio.wait_for(adapter.leave(), timeout)
    except Exception as error:
        if isinstance(error, TimeoutError):
            reason = f'the platform did not let Smitline out within {timeout:g} seconds'
        else:
            reason = f'{type(error).__name__}: {(str(error).splitlines() or [""])[0][:200]}'
        state.update(leaveState='leave_failed', leaveError=reason)
        if record is not None:
            record('leave_failed', reason=reason)
        print(f'Leaving the meeting could not be confirmed ({reason}); unloading its page.', flush=True)
        if page is not None:
            with contextlib.suppress(Exception):
                await page.goto('about:blank', timeout=5000)
        return False
    adapter.left_meeting = True
    state['leaveState'] = 'left'
    if record is not None:
        record('left')
    return True


class AuthenticationRequired(RuntimeError):
    pass

class GuestRefused(AuthenticationRequired):
    """The platform turned the guest browser away (not a sign-in page). A connected account may still
    get in, so the join retries with it like any other AuthenticationRequired."""

@dataclass(frozen=True)
class Capabilities:
    text_chat: bool = True
    participant_discovery: bool = False
    camera: bool = False
    def public(self):
        return asdict(self)

class MeetingPlatformAdapter(ABC):
    platform_id: str
    capabilities = Capabilities()
    signed_in_profile = None
    # True once leave_meeting() saw the platform let the agent out: nothing is left to mute.
    left_meeting = False
    def __init__(self, page, stop, stage):
        self.page, self.stop, self.stage = page, stop, stage
    def validate_url(self, url):
        from meeting_urls import platform_for_url
        if platform_for_url(url) != self.platform_id:
            raise ValueError('Meeting URL belongs to a different adapter')
    def normalize_url(self, url):
        from meeting_urls import normalize_url
        self.validate_url(url)
        return normalize_url(url)
    @abstractmethod
    async def join(self, url, name, passcode=''): ...
    @abstractmethod
    async def get_microphone_state(self): ...
    @abstractmethod
    async def mute(self): ...
    @abstractmethod
    async def unmute(self): ...
    @abstractmethod
    async def connect_audio(self): ...
    @abstractmethod
    async def send_chat_message(self, text): ...
    @abstractmethod
    async def has_ended(self): ...
    @abstractmethod
    async def leave(self):
        """Leave the meeting, and raise unless the platform shows the agent is out. Safe to call
        again: once out, it returns at once. The runtime calls it through leave_meeting()."""
    async def get_active_speaker(self):
        return None
    async def chat_available(self):
        return False

    async def accept_unmute_request(self):
        """Accept a visible host request to unmute. Return True only when one was accepted."""
        return False

    async def get_participant_count(self):
        """Confirmed in-call count including self, or None when unavailable."""
        return None

    async def get_participant_names(self):
        """The display names of everyone in the meeting, the bot included; [] when unknown."""
        return []

    async def get_camera_state(self):
        """Return on, off, blocked, or unknown without raising."""
        return 'unknown'

    async def enable_camera(self):
        """Turn the meeting camera on after admission. May raise if the control is missing."""
        return None

    async def disable_camera(self):
        """Turn the meeting camera off. May raise if the control is missing."""
        return None
