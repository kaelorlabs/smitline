"""Zoom, Teams, and Google Meet adapters. Add future platforms only through REGISTRY."""
import asyncio
import contextlib
import re
from urllib.parse import urlsplit
from adapters_base import MeetingPlatformAdapter, Capabilities, AuthenticationRequired
from meeting_urls import platform_for_url, normalize_url
from zoom_controls import microphone_is_muted
from zoom_join import join_zoom, connect_audio

async def visible(locator):
    return await locator.count() > 0 and await locator.first.is_visible()

class ZoomAdapter(MeetingPlatformAdapter):
    platform_id = 'zoom'
    capabilities = Capabilities(file_delivery=True, camera=True, shared_content=True)
    async def join(self, url, name, passcode=''):
        await join_zoom(self.page, self.normalize_url(url), passcode, self.stop, self.stage, name)
    async def connect_audio(self):
        await connect_audio(self.page, self.stop)
    async def get_microphone_state(self):
        state = await microphone_is_muted(self.page)
        return 'unknown' if state is None else ('muted' if state else 'open')
    async def _set_mute(self, muted):
        desired = 'muted' if muted else 'open'
        if await self.get_microphone_state() == desired:
            return
        label = r'^mute(?: my microphone)?(?:\s*\([^)]*\))?$' if muted else r'^unmute(?: my microphone)?(?:\s*\([^)]*\))?$'
        # Zoom hides its meeting toolbar after a short idle period. The button
        # remains in the accessibility tree, but Playwright cannot click it
        # until pointer movement reveals the controls.
        await self.page.mouse.move(80, 680)
        button = self.page.get_by_role('button', name=re.compile(label, re.I), include_hidden=True).first
        try:
            await button.click(timeout=1500)
        except Exception:
            # Zoom's documented in-meeting shortcut is more reliable when the
            # toolbar animation or an overlay prevents a pointer click.
            await self.page.keyboard.press('Alt+A')
        for _ in range(20):
            if await self.get_microphone_state() == desired:
                return
            await asyncio.sleep(.1)
        raise RuntimeError(f'Zoom microphone could not be confirmed {desired}')
    async def mute(self): await self._set_mute(True)
    async def unmute(self): await self._set_mute(False)
    async def get_camera_state(self):
        return await _camera_state(self.page, zoom=True)
    async def enable_camera(self):
        await self._set_camera(True)
    async def disable_camera(self):
        await self._set_camera(False)
    async def _set_camera(self, enabled):
        desired = 'on' if enabled else 'off'
        if await self.get_camera_state() == desired:
            return
        await self.page.mouse.move(80, 680)
        label = (
            r'^(?:start video|start my video)(?:\s*\([^)]*\))?$' if enabled
            else r'^(?:stop video|stop my video)(?:\s*\([^)]*\))?$'
        )
        button = self.page.get_by_role('button', name=re.compile(label, re.I), include_hidden=True).first
        try:
            await button.click(timeout=1500)
        except Exception:
            await self.page.keyboard.press('Alt+V')
        for _ in range(20):
            actual = await self.get_camera_state()
            if actual == desired:
                return
            if actual == 'blocked':
                raise RuntimeError('Zoom camera is blocked by meeting policy')
            await asyncio.sleep(.1)
        raise RuntimeError(f'Zoom camera could not be confirmed {desired}')
    async def chat_available(self):
        return await self.page.get_by_role('button', name=re.compile(r'^(open|close) the chat panel$', re.I)).count() > 0
    async def send_chat_message(self, text):
        if not await visible(self.page.get_by_role('button', name='close the chat panel', exact=True)):
            await self.page.get_by_role('button', name='open the chat panel', exact=True).click(timeout=3000)
        recipient = self.page.get_by_role('button', name=re.compile(r'^Send chat to (Everyone|Meeting Group Chat) please select a receiver$', re.I))
        if not await visible(recipient):
            raise RuntimeError('Cannot confirm meeting-wide chat recipient')
        box = self.page.locator('[contenteditable="true"][role="textbox"], textarea[placeholder*="message" i]').first
        await box.fill(text, timeout=3000)
        await box.press('Enter')
        return {'status': 'submitted', 'delivery_confirmed': False}
    async def has_ended(self):
        body = (await self.page.locator('body').inner_text()).lower()
        return any(x in body for x in ('meeting has been ended', 'meeting has ended', 'removed by the host', 'you have been removed'))
    async def leave(self):
        button = self.page.get_by_role('button', name=re.compile(r'^leave', re.I)).first
        if await visible(button): await button.click(timeout=2000)
    async def get_shared_content_state(self):
        from shared_content import shared_content_state
        return await shared_content_state(self.page, 'zoom')
    async def capture_shared_content(self):
        from shared_content import capture_shared_content
        return await capture_shared_content(self.page, 'zoom')

class TeamsAdapter(MeetingPlatformAdapter):
    platform_id = 'teams'
    capabilities = Capabilities(participant_discovery=True, camera=True, shared_content=True)
    signed_in_profile = ('teams-connected', 'teams')
    def __init__(self, *args):
        super().__init__(*args)
        from joinly.providers.browser.platforms.teams import TeamsBrowserPlatformController
        self.controller = TeamsBrowserPlatformController()
    async def join(self, url, name, passcode=''):
        self.stage('opening_meeting')
        await self.page.goto(self.normalize_url(url), wait_until='domcontentloaded', timeout=60000)
        submitted = False
        deadline = asyncio.get_running_loop().time() + 600
        while not self.stop.is_set() and asyncio.get_running_loop().time() < deadline:
            body = (await self.page.locator('body').inner_text()).lower()
            if urlsplit(self.page.url).hostname in ('login.microsoftonline.com', 'login.live.com'):
                raise AuthenticationRequired('Teams requires Microsoft sign-in')
            if any(x in body for x in ('sign in to join', 'sign in to this meeting', 'anonymous users', 'only people with access', 'sign in with a different account')):
                raise AuthenticationRequired('Teams requires an authorized Microsoft account')
            if any(x in body for x in ('meeting has ended', 'you were removed', 'you have been removed', 'request to join was denied')):
                raise RuntimeError('Teams meeting ended or admission was denied')
            device_notice = self.page.get_by_role('button', name=re.compile(r'^Continue without audio or video$', re.I))
            if await visible(device_notice):
                await device_notice.click(timeout=3000)
                # Admission still requires a usable microphone in connect_audio.
                continue
            # A lobby may also have a Leave button. Check lobby text first.
            if any(x in body for x in ('someone will let you in', 'waiting for someone', 'please wait', 'waiting in the lobby', 'will let you in when')):
                self.stage('waiting_for_admission')
            elif await visible(self.page.get_by_role('button', name=re.compile(r'^leave(?:\s|$)', re.I))) and await self.get_microphone_state() != 'unknown':
                self.stage('admitted')
                return
            else:
                browser = self.page.get_by_role('button', name=re.compile(r'continue (?:on this browser|in this browser)|join on the web|continue.*web', re.I))
                if await visible(browser):
                    await browser.first.click(timeout=3000)
                field = self.page.locator('input[placeholder*="name" i], input[aria-label*="name" i]')
                if await visible(field): await field.first.fill(name)
                # Camera and microphone are off before admission when controls exist.
                for media in ('camera', 'microphone'):
                    switch = self.page.get_by_role('switch', name=re.compile(media, re.I))
                    if await visible(switch) and await switch.first.is_checked():
                        await switch.first.click(timeout=2000)
                camera = self.page.get_by_role('button', name=re.compile(r'^turn (?:camera|video) off', re.I))
                if await visible(camera): await camera.first.click(timeout=2000)
                if await self.get_microphone_state() == 'open': await self.mute()
                join = self.page.get_by_role('button', name=re.compile(r'^join(?: now| meeting)?$', re.I))
                if not submitted and await visible(join) and await join.first.is_enabled():
                    await join.first.click(timeout=3000)
                    submitted = True
                    self.stage('joining')
            await asyncio.sleep(.5)
        raise RuntimeError('Teams admission timed out or was stopped')
    async def get_microphone_state(self):
        for state, label in [('muted', r'^(?:unmute|turn (?:on )?(?:mic|microphone) on)(?:\b|$)'), ('open', r'^(?:mute|turn (?:off )?(?:mic|microphone) off)(?:\b|$)')]:
            buttons = self.page.get_by_role('button', name=re.compile(label, re.I))
            for button in await buttons.all():
                name = (await button.get_attribute('aria-label') or await button.inner_text()).lower()
                if 'all' not in name and await button.is_visible():
                    return state if await button.is_enabled() else 'blocked'
        return 'unknown'
    async def _set_mute(self, muted):
        desired = 'muted' if muted else 'open'
        if await self.get_microphone_state() == desired: return
        label = r'^mute(?: (?:mic|microphone))?(?:\s*\([^)]*\))?$' if muted else r'^unmute(?: (?:mic|microphone))?(?:\s*\([^)]*\))?$'
        button = self.page.get_by_role('button', name=re.compile(label, re.I))
        if await button.count() != 1 or not await button.is_enabled():
            raise RuntimeError('Teams microphone control is missing or blocked')
        await button.click(timeout=3000)
        for _ in range(10):
            if await self.get_microphone_state() == desired: return
            await asyncio.sleep(.1)
        raise RuntimeError('Teams microphone is unavailable or blocked by meeting policy')
    async def mute(self): await self._set_mute(True)
    async def unmute(self): await self._set_mute(False)
    async def get_camera_state(self):
        return await _camera_state(self.page, zoom=False)
    async def enable_camera(self):
        await self._set_camera(True)
    async def disable_camera(self):
        await self._set_camera(False)
    async def _set_camera(self, enabled):
        desired = 'on' if enabled else 'off'
        if await self.get_camera_state() == desired:
            return
        switch = self.page.get_by_role('switch', name=re.compile(r'camera|video', re.I))
        if await visible(switch):
            checked = await switch.first.is_checked()
            if checked != enabled:
                await switch.first.click(timeout=2000)
        else:
            label = (
                r'^turn (?:camera|video) on(?:\s*\([^)]*\))?$' if enabled
                else r'^turn (?:camera|video) off(?:\s*\([^)]*\))?$'
            )
            button = self.page.get_by_role('button', name=re.compile(label, re.I)).first
            if await visible(button):
                await button.click(timeout=2000)
            elif await button.count() and not await button.is_enabled():
                raise RuntimeError('Teams camera is blocked by meeting policy')
            else:
                raise RuntimeError('Teams camera control is missing')
        for _ in range(20):
            actual = await self.get_camera_state()
            if actual == desired:
                return
            if actual == 'blocked':
                raise RuntimeError('Teams camera is blocked by meeting policy')
            await asyncio.sleep(.1)
        raise RuntimeError(f'Teams camera could not be confirmed {desired}')
    async def connect_audio(self):
        await self.mute()
    async def chat_available(self):
        return await visible(self.page.get_by_role('button', name=re.compile(r'^chat', re.I)))
    async def send_chat_message(self, text):
        if not await self.chat_available(): raise RuntimeError('Teams meeting chat is unavailable')
        await self.controller.send_chat_message(self.page, text)
        return {'status': 'submitted', 'delivery_confirmed': False}
    async def has_ended(self):
        body = (await self.page.locator('body').inner_text()).lower()
        return any(x in body for x in ('meeting has ended', 'you left the meeting', 'you were removed', 'you have been removed', 'rejoin'))
    async def get_participant_count(self):
        # Read only the meeting toolbar, never chat text or lobby roster entries.
        buttons = self.page.get_by_role('button', name=re.compile(r'^(?:people|participants|show participants)\b', re.I))
        for button in await buttons.all():
            if not await button.is_visible():
                continue
            label = await button.get_attribute('aria-label') or await button.inner_text()
            match = re.fullmatch(r'(?:people|participants|show participants)\s*\(?\s*(\d+)\s*\)?', label.strip(), re.I)
            if match and int(match[1]) >= 1:
                return int(match[1])
        return None
    async def leave(self): await self.controller.leave(self.page)
    async def get_active_speaker(self): return self.controller.active_speaker
    async def get_shared_content_state(self):
        from shared_content import shared_content_state
        return await shared_content_state(self.page, 'teams')
    async def capture_shared_content(self):
        from shared_content import capture_shared_content
        return await capture_shared_content(self.page, 'teams')

class MeetAdapter(MeetingPlatformAdapter):
    platform_id = 'meet'
    capabilities = Capabilities(participant_discovery=True, camera=True, shared_content=True)
    signed_in_profile = ('google-connected', 'google')
    def __init__(self, *args):
        super().__init__(*args)
        from joinly.providers.browser.platforms.google_meet import GoogleMeetBrowserPlatformController
        self.controller = GoogleMeetBrowserPlatformController()
    async def join(self, url, name, passcode=''):
        self.stage('opening_meeting')
        await self.page.goto(self.normalize_url(url), wait_until='domcontentloaded', timeout=60000)
        submitted = False
        deadline = asyncio.get_running_loop().time() + 600
        while not self.stop.is_set() and asyncio.get_running_loop().time() < deadline:
            host = (urlsplit(self.page.url).hostname or '').lower()
            if host in ('accounts.google.com', 'accounts.youtube.com', 'login.google.com'):
                raise AuthenticationRequired('Google Meet requires Google sign-in')
            if host in ('calendar.google.com', 'workspace.google.com', 'stream.meet.google.com'):
                raise RuntimeError('Unsupported Google Meet event or webinar variant')
            body = (await self.page.locator('body').inner_text()).lower()
            if any(x in body for x in (
                'sign in to join', 'you need to sign in', 'sign in to continue',
                'only people invited by the host', 'this meeting is restricted',
            )):
                raise AuthenticationRequired('Google Meet requires an authorized Google account')
            if "you can't join this video call" in body or 'you cannot join this video call' in body:
                if await self._signed_in_ui():
                    raise RuntimeError('Google Meet meeting ended or admission was denied')
                raise AuthenticationRequired('Google Meet requires an authorized Google account')
            if any(x in body for x in (
                'the meeting has ended', 'you were removed', 'you have been removed',
                "you've been removed", 'return to home screen',
            )) and not any(x in body for x in ('asking to be let in', 'lets you in')):
                raise RuntimeError('Google Meet meeting ended or admission was denied')
            for label in (
                r'^got it$',
                r'^continue without microphone(?: and camera)?$',
                r'^continue without camera$',
                r'^dismiss$',
            ):
                notice = self.page.get_by_role('button', name=re.compile(label, re.I))
                if await visible(notice):
                    await notice.first.click(timeout=3000)
                    break
            if any(x in body for x in (
                'asking to be let in', "you'll join when", 'someone lets you in',
                'waiting for the host', 'waiting to be let in', 'knocking',
            )):
                self.stage('waiting_for_admission')
            elif (
                await visible(self.page.get_by_role('button', name=re.compile(r'^leave(?: call)?(?:\s|$)', re.I)))
                and await self.get_microphone_state() != 'unknown'
            ):
                self.stage('admitted')
                with contextlib.suppress(Exception):
                    await self.controller._setup_active_speaker_observer(self.page)
                return
            else:
                field = self.page.get_by_placeholder(re.compile('name', re.I))
                if await visible(field):
                    await field.first.fill(name)
                if await self.get_camera_state() == 'on':
                    camera_off = self.page.get_by_role(
                        'button', name=re.compile(r'^turn off camera(?:\s*\([^)]*\))?$', re.I))
                    if await visible(camera_off):
                        with contextlib.suppress(Exception):
                            await camera_off.first.click(timeout=2000)
                if await self.get_microphone_state() == 'open':
                    mic_off = self.page.get_by_role(
                        'button', name=re.compile(r'^turn off (?:mic|microphone)\b', re.I))
                    if await visible(mic_off):
                        with contextlib.suppress(Exception):
                            await mic_off.first.click(timeout=2000)
                join = self.page.get_by_role(
                    'button', name=re.compile(r'^(?:ask to join|join now|join anyway)$', re.I))
                if not submitted and await visible(join) and await join.first.is_enabled():
                    await join.first.click(timeout=3000)
                    submitted = True
                    self.stage('joining')
            await asyncio.sleep(.5)
        raise RuntimeError('Google Meet admission timed out or was stopped')
    async def _signed_in_ui(self):
        account = self.page.locator(
            'a[aria-label*="Google Account" i], img[alt*="Google Account" i], a[href*="SignOutOptions"]')
        return await visible(account)
    async def get_microphone_state(self):
        pairs = (
            ('muted', r'^turn on (?:mic|microphone)\b'),
            ('open', r'^turn off (?:mic|microphone)\b'),
        )
        for state, label in pairs:
            buttons = self.page.get_by_role('button', name=re.compile(label, re.I))
            for button in await buttons.all():
                name = (await button.get_attribute('aria-label') or await button.inner_text()).lower()
                if 'all' not in name and await button.is_visible():
                    return state if await button.is_enabled() else 'blocked'
        return 'unknown'
    async def _set_mute(self, muted):
        desired = 'muted' if muted else 'open'
        if await self.get_microphone_state() == desired:
            return
        label = r'^turn off (?:mic|microphone)\b' if muted else r'^turn on (?:mic|microphone)\b'
        button = self.page.get_by_role('button', name=re.compile(label, re.I))
        if await button.count() != 1 or not await button.is_enabled():
            raise RuntimeError('Google Meet microphone control is missing or blocked')
        await button.click(timeout=3000)
        for _ in range(10):
            if await self.get_microphone_state() == desired:
                return
            await asyncio.sleep(.1)
        raise RuntimeError('Google Meet microphone is unavailable or blocked by meeting policy')
    async def mute(self): await self._set_mute(True)
    async def unmute(self): await self._set_mute(False)
    async def get_camera_state(self):
        pairs = (
            ('off', r'^turn on camera(?:\s*\([^)]*\))?$'),
            ('on', r'^turn off camera(?:\s*\([^)]*\))?$'),
        )
        for state, label in pairs:
            buttons = self.page.get_by_role('button', name=re.compile(label, re.I))
            for button in await buttons.all():
                if not await button.is_visible():
                    continue
                name = (await button.get_attribute('aria-label') or await button.inner_text()).lower()
                if 'all' in name:
                    continue
                if not await button.is_enabled():
                    return 'blocked'
                return state
        return 'unknown'
    async def enable_camera(self):
        await self._set_camera(True)
    async def disable_camera(self):
        await self._set_camera(False)
    async def _set_camera(self, enabled):
        desired = 'on' if enabled else 'off'
        if await self.get_camera_state() == desired:
            return
        label = (
            r'^turn on camera(?:\s*\([^)]*\))?$' if enabled
            else r'^turn off camera(?:\s*\([^)]*\))?$'
        )
        button = self.page.get_by_role('button', name=re.compile(label, re.I)).first
        if await visible(button):
            await button.click(timeout=2000)
        elif await button.count() and not await button.is_enabled():
            raise RuntimeError('Google Meet camera is blocked by meeting policy')
        else:
            raise RuntimeError('Google Meet camera control is missing')
        for _ in range(20):
            actual = await self.get_camera_state()
            if actual == desired:
                return
            if actual == 'blocked':
                raise RuntimeError('Google Meet camera is blocked by meeting policy')
            await asyncio.sleep(.1)
        raise RuntimeError(f'Google Meet camera could not be confirmed {desired}')
    async def connect_audio(self):
        await self.mute()
    async def chat_available(self):
        if await visible(self.page.locator("textarea[placeholder*='Send a message']")):
            return True
        return await visible(self.page.get_by_role('button', name=re.compile(r'^chat\b', re.I)))
    async def send_chat_message(self, text):
        if not await self.chat_available():
            raise RuntimeError('Google Meet meeting chat is unavailable')
        await self.controller.send_chat_message(self.page, text)
        return {'status': 'submitted', 'delivery_confirmed': False}
    async def has_ended(self):
        body = (await self.page.locator('body').inner_text()).lower()
        return any(x in body for x in (
            'the meeting has ended', 'meeting has ended', 'you left the meeting',
            "you've been removed", 'you were removed', 'you have been removed',
            'return to home screen', 'rejoin',
        ))
    async def get_participant_count(self):
        buttons = self.page.get_by_role(
            'button', name=re.compile(r'^(?:people|show everyone|participants)\b', re.I))
        for button in await buttons.all():
            if not await button.is_visible():
                continue
            label = await button.get_attribute('aria-label') or await button.inner_text()
            match = re.fullmatch(
                r'(?:people|show everyone|participants)\s*\(?\s*(\d+)\s*\)?', label.strip(), re.I)
            if match and int(match[1]) >= 1:
                return int(match[1])
        return None
    async def leave(self):
        with contextlib.suppress(Exception):
            await self.controller.leave(self.page)
            return
        button = self.page.get_by_role('button', name=re.compile(r'^leave', re.I)).first
        if await visible(button):
            await button.click(timeout=2000)
    async def get_active_speaker(self):
        return self.controller.active_speaker
    async def get_shared_content_state(self):
        from shared_content import shared_content_state
        return await shared_content_state(self.page, 'meet')
    async def capture_shared_content(self):
        from shared_content import capture_shared_content
        return await capture_shared_content(self.page, 'meet')

REGISTRY = {'zoom': ZoomAdapter, 'teams': TeamsAdapter, 'meet': MeetAdapter}

def create_adapter(url, page, stop, stage):
    return REGISTRY[platform_for_url(url)](page, stop, stage)


async def _camera_state(page, *, zoom):
    if zoom:
        pairs = (
            ('off', r'^(?:start video|start my video)(?:\s*\([^)]*\))?$'),
            ('on', r'^(?:stop video|stop my video)(?:\s*\([^)]*\))?$'),
        )
    else:
        pairs = (
            ('off', r'^turn (?:camera|video) on(?:\s*\([^)]*\))?$'),
            ('on', r'^turn (?:camera|video) off(?:\s*\([^)]*\))?$'),
        )
        switch = page.get_by_role('switch', name=re.compile(r'camera|video', re.I))
        if await visible(switch):
            try:
                checked = await switch.first.is_checked()
            except Exception:
                return 'unknown'
            enabled = await switch.first.is_enabled()
            if not enabled:
                return 'blocked'
            return 'on' if checked else 'off'
    for state, label in pairs:
        buttons = page.get_by_role('button', name=re.compile(label, re.I), include_hidden=zoom)
        for button in await buttons.all():
            try:
                if zoom:
                    name = (await button.get_attribute('aria-label') or await button.inner_text()).lower()
                    if 'all' in name:
                        continue
                else:
                    if not await button.is_visible():
                        continue
                    name = (await button.get_attribute('aria-label') or await button.inner_text()).lower()
                    if 'all' in name:
                        continue
                if not await button.is_enabled():
                    return 'blocked'
                return state
            except Exception:
                continue
    return 'unknown'
