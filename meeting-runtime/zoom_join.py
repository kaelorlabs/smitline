import asyncio
import re
from adapters_base import AuthenticationRequired

async def join_zoom(page, url, passcode, stop, stage, participant_name='Smitline'):
    url = re.sub(r'/j/(\d+)', r'/wc/join/\1', url)
    await page.goto(url, wait_until='domcontentloaded', timeout=60000)
    submitted = False
    deadline = asyncio.get_running_loop().time() + 600
    while not stop.is_set() and asyncio.get_running_loop().time() < deadline:
        text = (await page.locator('body').inner_text()).lower()
        if any(s in text for s in ['verify you are human', 'i am not a robot', 'i\'m not a robot']):
            stage('human_verification_required')
        elif await page.get_by_role('button', name=re.compile(r'^i agree$', re.I)).is_visible():
            stage('terms_acceptance_required')
        elif await page.get_by_role('button', name=re.compile(r'leave', re.I)).first.is_visible():
            stage('admitted')
            return
        elif 'waiting for the host' in text or "let them know you're here" in text or 'host will let you' in text:
            stage('waiting_for_admission')
        elif any(s in text for s in ['meeting has ended', 'meeting id is not valid', 'invalid meeting id', 'meeting does not exist', 'meeting is not available', 'meeting link is invalid']):
            raise RuntimeError('Zoom meeting ended or is invalid')
        elif 'sign in to join' in text:
            raise AuthenticationRequired('Zoom requires sign-in; open meeting view')
        elif not submitted:
            name = page.locator('#input-for-name, #inputname')
            if await name.count() and await name.first.is_visible():
                await name.first.fill(participant_name)
                password = page.locator('input[type="password"]')
                if await password.count() and await password.first.is_visible():
                    await password.first.fill(passcode)
                button = page.get_by_role('button', name=re.compile(r'^join$', re.I)).first
                if await button.is_enabled():
                    await button.click()
                    submitted = True
                    stage('joining')
            else:
                stage('opening_meeting')
        await asyncio.sleep(1)
    raise RuntimeError('Zoom admission timed out or was stopped')


async def connect_audio(page, stop):
    for _ in range(90):
        if stop.is_set():
            raise RuntimeError('Stopped before audio connected')
        join = page.get_by_role('button', name=re.compile(r'join audio by computer|join with computer audio', re.I)).first
        if await join.is_visible():
            await join.click()
        unmute = page.get_by_role('button', name=re.compile(r'^unmute( my microphone)?', re.I)).first
        if await unmute.is_visible():
            return
        mute = page.get_by_role('button', name=re.compile(r'^mute( my microphone)?', re.I)).first
        if await mute.is_visible():
            await mute.click()
            if await unmute.is_visible():
                return
        await asyncio.sleep(1)
    raise RuntimeError('Zoom computer audio could not be enabled; inspect browser viewer')
