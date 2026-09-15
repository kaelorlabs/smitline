"""Zoom and Teams adapters. Add future platforms only through REGISTRY."""
import asyncio
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
    capabilities = Capabilities(file_delivery=True)
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

class TeamsAdapter(MeetingPlatformAdapter):
    platform_id = 'teams'
    capabilities = Capabilities(participant_discovery=True)
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

REGISTRY = {'zoom': ZoomAdapter, 'teams': TeamsAdapter}

def create_adapter(url, page, stop, stage):
    return REGISTRY[platform_for_url(url)](page, stop, stage)
