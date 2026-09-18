import asyncio
import unittest
from playwright.async_api import async_playwright
from adapters import create_adapter
from adapters_base import AuthenticationRequired
from meeting_urls import platform_for_url, normalize_url

class UrlTests(unittest.TestCase):
    def test_supported_invites(self):
        self.assertEqual(platform_for_url('https://us05web.zoom.us/j/123?pwd=x'), 'zoom')
        for url in ('https://teams.microsoft.com/l/meetup-join/abc?context=x', 'https://teams.live.com/meet/123?p=x'):
            self.assertEqual(platform_for_url(url), 'teams')
        self.assertEqual(platform_for_url('https://meet.google.com/aaa-bbbb-ccc'), 'meet')
        self.assertEqual(platform_for_url('https://meet.google.com/abc-defg-hij?authuser=0'), 'meet')
        self.assertEqual(normalize_url('https://zoom.us/j/123?pwd=x'), 'https://zoom.us/wc/join/123?pwd=x')
        self.assertEqual(normalize_url('https://meet.google.com/aaa-bbbb-ccc'), 'https://meet.google.com/aaa-bbbb-ccc')
    def test_rejects_unsafe_and_unsupported(self):
        for url in (
            'http://zoom.us/j/123', 'https://zoom.us.evil.com/j/123',
            'https://teams.microsoft.com.evil.org/meet/123', 'https://teams.microsoft.us/meet/123',
            'https://user:password@zoom.us/j/123', 'https://zoom.us:8443/j/123',
            'https://meet.google.com/abc', 'https://zoom.us/j/123/evil',
            'http://meet.google.com/aaa-bbbb-ccc',
            'https://meet.google.com.evil.org/aaa-bbbb-ccc',
            'https://www.meet.google.com/aaa-bbbb-ccc',
            'https://user:pass@meet.google.com/aaa-bbbb-ccc',
            'https://meet.google.com:8443/aaa-bbbb-ccc',
            'https://meet.google.com/',
            'https://meet.google.com/landing',
            'https://meet.google.com/new',
            'https://meet.google.com/lookup/nickname',
            'https://meet.google.com/aaa-bbbb-ccc/event',
            'https://workspace.google.com/meet/aaa-bbbb-ccc',
            'https://stream.meet.google.com/aaa-bbbb-ccc',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError): platform_for_url(url)

class BrowserFixtures(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True, executable_path=self.pw.chromium.executable_path, args=['--no-sandbox'])
        self.page = await self.browser.new_page()
        self.stop = asyncio.Event()
        self.stages = []
    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
    def adapter(self, teams=True, meet=False):
        if meet:
            url = 'https://meet.google.com/aaa-bbbb-ccc'
        elif teams:
            url = 'https://teams.microsoft.com/meet/123'
        else:
            url = 'https://zoom.us/j/123'
        return create_adapter(url, self.page, self.stop, self.stages.append)
    async def test_teams_lobby_is_not_admission(self):
        await self.page.route('**/*', lambda route: route.fulfill(body='<p>Please wait, someone will let you in</p><button>Leave</button><button>Unmute</button>', content_type='text/html'))
        a = self.adapter()
        task = asyncio.create_task(a.join('https://teams.microsoft.com/meet/123', 'Colleague'))
        try:
            for _ in range(30):
                if 'waiting_for_admission' in self.stages: break
                await asyncio.sleep(.05)
            self.assertFalse(task.done())
            await self.page.locator('p').evaluate('(p) => p.remove()')
            await asyncio.wait_for(task, 2)
            self.assertIn('admitted', self.stages)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async def test_teams_auth_failure(self):
        await self.page.route('**/*', lambda route: route.fulfill(body='Sign in to join this meeting'))
        with self.assertRaises(AuthenticationRequired):
            await self.adapter().join('https://teams.microsoft.com/meet/123', 'Colleague')
    async def test_mute_controls_and_chat_capabilities(self):
        await self.page.set_content('''<button onclick="this.innerText=this.innerText==='Unmute'?'Mute':'Unmute'">Unmute</button><button>Mute all</button>''')
        a = self.adapter()
        self.assertEqual(await a.get_microphone_state(), 'muted')
        await a.unmute()
        self.assertEqual(await a.get_microphone_state(), 'open')
        await a.mute()
        self.assertEqual(await a.get_microphone_state(), 'muted')
        self.assertFalse(await a.chat_available())
        await self.page.set_content('<p>You were removed from the meeting</p>')
        self.assertTrue(await a.has_ended())
    async def test_zoom_controls_and_end(self):
        await self.page.set_content('''<button aria-label="Unmute my microphone" onclick="this.setAttribute('aria-label',this.getAttribute('aria-label').startsWith('Unmute')?'Mute my microphone':'Unmute my microphone')">mic</button>''')
        a = self.adapter(False)
        await a.unmute()
        self.assertEqual(await a.get_microphone_state(), 'open')
        await a.mute()
        self.assertEqual(await a.get_microphone_state(), 'muted')
        await self.page.set_content('<p>The meeting has been ended</p>')
        self.assertTrue(await a.has_ended())

    async def test_zoom_and_teams_camera_controls(self):
        await self.page.set_content(
            '''<button aria-label="Start video" onclick="this.setAttribute('aria-label',this.getAttribute('aria-label').startsWith('Start')?'Stop video':'Start video')">cam</button>'''
        )
        zoom = self.adapter(False)
        self.assertTrue(zoom.capabilities.camera)
        self.assertEqual(await zoom.get_camera_state(), 'off')
        await zoom.enable_camera()
        self.assertEqual(await zoom.get_camera_state(), 'on')
        await zoom.disable_camera()
        self.assertEqual(await zoom.get_camera_state(), 'off')
        await self.page.set_content(
            '''<button aria-label="Turn camera on" onclick="this.setAttribute('aria-label',this.getAttribute('aria-label').includes('on')?'Turn camera off':'Turn camera on')">cam</button>'''
        )
        teams = self.adapter()
        self.assertTrue(teams.capabilities.camera)
        self.assertEqual(await teams.get_camera_state(), 'off')
        await teams.enable_camera()
        self.assertEqual(await teams.get_camera_state(), 'on')

    async def test_blocked_camera_is_reported_without_join(self):
        await self.page.set_content('<button aria-label="Turn camera on" disabled>cam</button>')
        teams = self.adapter()
        self.assertEqual(await teams.get_camera_state(), 'blocked')

    async def test_zoom_reveals_hidden_toolbar_before_remuting(self):
        await self.page.set_content('''
          <style>#mic { display: none }</style>
          <button id="mic" aria-label="Mute my microphone"
            onclick="this.setAttribute('aria-label','Unmute my microphone')">mic</button>
          <script>
            document.addEventListener('mousemove', () => {
              document.querySelector('#mic').style.display = 'block';
            });
          </script>
        ''')
        a = self.adapter(False)
        self.assertEqual(await a.get_microphone_state(), 'open')
        await a.mute()
        self.assertEqual(await a.get_microphone_state(), 'muted')

    async def test_zoom_ignores_stale_hidden_microphone_control(self):
        await self.page.set_content('''
          <button hidden aria-label="Unmute my microphone">stale</button>
          <button id="mic" aria-label="Mute my microphone"
            onclick="this.setAttribute('aria-label','Unmute my microphone')">mic</button>
        ''')
        a = self.adapter(False)
        self.assertEqual(await a.get_microphone_state(), 'open')
        await a.mute()
        self.assertEqual(await a.get_microphone_state(), 'muted')

    async def test_zoom_uses_shortcut_when_visible_click_does_not_change_state(self):
        await self.page.set_content('''
          <button aria-label="Unmute my microphone">mic</button>
          <script>
            document.addEventListener('keydown', event => {
              if (event.altKey && event.key.toLowerCase() === 'a') {
                document.querySelector('button').setAttribute('aria-label','Mute my microphone');
              }
            });
          </script>
        ''')
        a = self.adapter(False)
        await a.unmute()
        self.assertEqual(await a.get_microphone_state(), 'open')

    async def test_teams_participant_count_is_explicit(self):
        a = self.adapter()
        for label in ('People (1)', 'Participants 1', 'Show participants (1)'):
            await self.page.set_content(f'<button aria-label="{label}">People</button>')
            self.assertEqual(await a.get_participant_count(), 1)
        await self.page.set_content('<button>People (2)</button><p>Participants 1</p>')
        self.assertEqual(await a.get_participant_count(), 2)
        for html in ('<button>People</button>', '<p>People (1)</p>', '<button hidden>People (1)</button>', '<button>People (0)</button>'):
            await self.page.set_content(html)
            self.assertIsNone(await a.get_participant_count())

    async def test_meet_lobby_is_not_admission(self):
        await self.page.route('**/*', lambda route: route.fulfill(
            body='<p>Asking to be let in</p><button>Leave call</button><button>Turn on mic</button>',
            content_type='text/html'))
        a = self.adapter(meet=True)
        task = asyncio.create_task(a.join('https://meet.google.com/aaa-bbbb-ccc', 'Colleague'))
        try:
            for _ in range(30):
                if 'waiting_for_admission' in self.stages: break
                await asyncio.sleep(.05)
            self.assertFalse(task.done())
            await self.page.locator('p').evaluate('(p) => p.remove()')
            await asyncio.wait_for(task, 2)
            self.assertIn('admitted', self.stages)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_meet_guest_prejoin_then_admission(self):
        html = (
            '<input placeholder="Your name">'
            '<button>Ask to join</button>'
            '<button>Turn off mic</button>'
            '<button>Turn off camera</button>'
        )
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        a = self.adapter(meet=True)
        task = asyncio.create_task(a.join('https://meet.google.com/aaa-bbbb-ccc', 'Colleague'))
        try:
            for _ in range(40):
                if 'joining' in self.stages: break
                await asyncio.sleep(.05)
            self.assertIn('joining', self.stages)
            self.assertFalse(task.done())
            await self.page.evaluate("""() => {
              document.body.innerHTML = '<button>Leave call</button><button>Turn on mic</button><button>Turn off camera</button>';
            }""")
            await asyncio.wait_for(task, 2)
            self.assertIn('admitted', self.stages)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_meet_auth_failure_and_guest_denial(self):
        await self.page.route('**/*', lambda route: route.fulfill(body='Sign in to join this video call'))
        with self.assertRaises(AuthenticationRequired):
            await self.adapter(meet=True).join('https://meet.google.com/aaa-bbbb-ccc', 'Colleague')
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(
            body="You can't join this video call"))
        with self.assertRaises(AuthenticationRequired):
            await self.adapter(meet=True).join('https://meet.google.com/aaa-bbbb-ccc', 'Colleague')

    async def test_meet_mute_camera_chat_end_and_degraded_policy(self):
        await self.page.set_content('''
          <button onclick="this.innerText=this.innerText==='Turn on mic'?'Turn off mic':'Turn on mic'">Turn on mic</button>
          <button onclick="this.setAttribute('aria-label', this.getAttribute('aria-label')==='Turn on camera'?'Turn off camera':'Turn on camera')" aria-label="Turn on camera">cam</button>
        ''')
        a = self.adapter(meet=True)
        self.assertTrue(a.capabilities.camera)
        self.assertTrue(a.capabilities.shared_content)
        self.assertEqual(await a.get_microphone_state(), 'muted')
        await a.unmute()
        self.assertEqual(await a.get_microphone_state(), 'open')
        await a.mute()
        self.assertEqual(await a.get_microphone_state(), 'muted')
        await a.connect_audio()
        self.assertEqual(await a.get_microphone_state(), 'muted')
        self.assertEqual(await a.get_camera_state(), 'off')
        await a.enable_camera()
        self.assertEqual(await a.get_camera_state(), 'on')
        await a.disable_camera()
        self.assertEqual(await a.get_camera_state(), 'off')
        self.assertFalse(await a.chat_available())
        with self.assertRaises(RuntimeError):
            await a.send_chat_message('hello')
        await self.page.set_content('<button aria-label="Turn on camera" disabled>cam</button>')
        self.assertEqual(await a.get_camera_state(), 'blocked')
        await self.page.set_content('<p>You\'ve been removed from the meeting</p>')
        self.assertTrue(await a.has_ended())
        await self.page.set_content('<p>You left the meeting</p>')
        self.assertTrue(await a.has_ended())

    async def test_meet_participant_count_and_cleanup_leave(self):
        a = self.adapter(meet=True)
        await self.page.set_content('<button aria-label="People (2)">People</button>')
        self.assertEqual(await a.get_participant_count(), 2)
        await self.page.set_content('<button>People</button>')
        self.assertIsNone(await a.get_participant_count())
        await self.page.set_content('<button id="leave">Leave call</button>')
        await self.page.locator('#leave').evaluate(
            '(el) => el.addEventListener("click", () => { el.dataset.left = "1" })')
        await a.leave()
        self.assertEqual(await self.page.locator('#leave').get_attribute('data-left'), '1')
