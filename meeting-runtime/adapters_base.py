"""Meeting platform contract; browser mechanics stay outside voice orchestration."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict

class AuthenticationRequired(RuntimeError):
    pass

@dataclass(frozen=True)
class Capabilities:
    text_chat: bool = True
    file_delivery: bool = False
    screen_sharing: bool = False
    participant_discovery: bool = False
    camera: bool = False
    shared_content: bool = False
    def public(self):
        return asdict(self)

class MeetingPlatformAdapter(ABC):
    platform_id: str
    capabilities = Capabilities()
    signed_in_profile = None
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
    async def leave(self): ...
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

    async def get_camera_state(self):
        """Return on, off, blocked, or unknown without raising."""
        return 'unknown'

    async def enable_camera(self):
        """Turn the meeting camera on after admission. May raise if the control is missing."""
        return None

    async def disable_camera(self):
        """Turn the meeting camera off. May raise if the control is missing."""
        return None

    async def get_shared_content_state(self):
        """Return whether a meeting shared-content surface is confidently visible."""
        from shared_content import SharedContentState
        return SharedContentState(False, 'none', 'unavailable')

    async def capture_shared_content(self):
        """Screenshot only the shared-content surface, or (None, state) when unsure."""
        state = await self.get_shared_content_state()
        return None, state
