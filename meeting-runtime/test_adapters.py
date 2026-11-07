import asyncio
import unittest
from unittest import mock
from playwright.async_api import Error as PlaywrightError, async_playwright
import adapters
from adapters import create_adapter
from adapters_base import AuthenticationRequired, GuestRefused
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
        for url in ('https://kaelor.daily.co/Tq9-room_2', 'https://kaelor-labs.daily.co/abc/',
                    'https://kaelor.daily.co/abc?t=token'):
            self.assertEqual(platform_for_url(url), 'daily')
        self.assertEqual(normalize_url('https://kaelor.daily.co/abc?t=x#y'), 'https://kaelor.daily.co/abc?t=x')
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
            'http://kaelor.daily.co/abc', 'https://daily.co/abc', 'https://www.daily.co/pricing/',
            'https://kaelor.daily.co/', 'https://kaelor.daily.co/abc/def',
            'https://kaelor.daily.co.evil.org/abc', 'https://a.b.daily.co/abc',
            'https://user:pass@kaelor.daily.co/abc', 'https://kaelor.daily.co:8443/abc',
            'https://kaelor.daily.co/abc%2Fdef', 'https://-bad.daily.co/abc',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError): platform_for_url(url)

# Headless Chromium sometimes crashes while starting on CI runners (SIGSEGV before the first
# page opens, in about one run of twenty). That has nothing to do with the code under test,
# so the fixture starts it again instead of failing the run.
LAUNCH_ATTEMPTS = 3

class BrowserFixtures(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        for attempt in range(LAUNCH_ATTEMPTS):
            browser = None
            try:
                browser = await self.pw.chromium.launch(headless=True, executable_path=self.pw.chromium.executable_path, args=['--no-sandbox'])
                page = await browser.new_page()
            except PlaywrightError:
                if browser is not None:
                    try:
                        await browser.close()
                    except PlaywrightError:
                        pass
                if attempt == LAUNCH_ATTEMPTS - 1:
                    await self.pw.stop()
                    raise
                continue
            self.browser, self.page = browser, page
            break
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
        with self.assertRaises(AuthenticationRequired) as signin:
            await self.adapter().join('https://teams.microsoft.com/meet/123', 'Colleague')
        self.assertEqual(signin.exception.kind, 'sign_in_required')
        # An organizer who keeps anonymous people out: restricted, still an AuthenticationRequired.
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(
            body="Anonymous users can't join this meeting"))
        with self.assertRaises(AuthenticationRequired) as restricted:
            await self.adapter().join('https://teams.microsoft.com/meet/123', 'Colleague')
        self.assertEqual(restricted.exception.kind, 'restricted')
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
    async def test_zoom_reads_its_participants_panel(self):
        # Zoom's web client markup as Recall.ai's Zoom bot guide reads it (June 2026): the toolbar
        # button opens the panel, which lists display names; labels such as (Host, me) are apart.
        await self.page.set_content('''
            <button aria-label="open the participants list pane,[3] particpants" onclick="
                document.getElementById('panel').innerHTML =
                  '<div class=participants-item__item-layout><span class=participants-item__display-name>Smitline AI</span><span class=participants-item__name-label>(me)</span></div>' +
                  '<div class=participants-item__item-layout><span class=participants-item__display-name>Smith Lee</span><span class=participants-item__name-label>(Host)</span></div>' +
                  '<div class=participants-item__item-layout><span class=participants-item__display-name>Priya  Shah</span></div>'">Participants</button>
            <div id="panel"></div>''')
        a = self.adapter(False)
        # The count comes from the button's label, without opening the panel.
        self.assertEqual(await a.get_participant_count(), 3)
        self.assertEqual(await a.get_participant_names(), ['Smitline AI', 'Smith Lee', 'Priya Shah'])
        # Open already: read without clicking again (a second click would close it).
        self.assertEqual(await a.get_participant_names(), ['Smitline AI', 'Smith Lee', 'Priya Shah'])
        await self.page.set_content('<p>No participants button here</p>')
        self.assertEqual(await a.get_participant_names(), [])
        self.assertIsNone(await a.get_participant_count())

    async def test_a_failed_zoom_chat_post_says_what_zoom_showed(self):
        await self.page.set_content('''
            <button aria-label="open the chat pane">Chat</button>
            <button aria-label="Send chat to Everyone">To: Everyone</button>
            <div contenteditable="true" role="textbox" aria-label="Type message here"></div>''')
        a = self.adapter(False)
        with self.assertRaises(RuntimeError) as caught:
            await a.send_chat_message('From the agent:\nhttps://example.com/pr/75')
        self.assertIn('chat controls seen', str(caught.exception))
        self.assertIn('open the chat pane', str(caught.exception))
        self.assertIn('Send chat to Everyone', str(caught.exception))

    async def test_zoom_chat_posts_one_line(self):
        await self.page.set_content('''
            <button aria-label="close the chat panel">Chat</button>
            <button aria-label="Send chat to Everyone please select a receiver">Everyone</button>
            <div contenteditable="true" role="textbox" id="box"
                 onkeydown="if (event.key === 'Enter') { window.sent = this.innerText; this.innerText = ''; }"></div>''')
        a = self.adapter(False)
        await a.send_chat_message("From Smith's agent, request r1 for Priya:\nhttps://example.com/pr/75")
        self.assertEqual(await self.page.evaluate('window.sent'),
                         "From Smith's agent, request r1 for Priya: | https://example.com/pr/75")

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
        with self.assertRaises(AuthenticationRequired) as signin:
            await self.adapter(meet=True).join('https://meet.google.com/aaa-bbbb-ccc', 'Colleague')
        self.assertNotIsInstance(signin.exception, GuestRefused)
        self.assertEqual(signin.exception.kind, 'sign_in_required')
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(
            body='Only people invited by the host can join this call'))
        with self.assertRaises(AuthenticationRequired) as restricted:
            await self.adapter(meet=True).join('https://meet.google.com/aaa-bbbb-ccc', 'Colleague')
        self.assertEqual(restricted.exception.kind, 'restricted')
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(
            body="You can't join this video call"))
        # Google's refusal of the guest is its own error, and still an AuthenticationRequired so the
        # join retries with a connected Google account.
        with self.assertRaises(GuestRefused) as refused:
            await self.adapter(meet=True).join('https://meet.google.com/aaa-bbbb-ccc', 'Colleague')
        self.assertIsInstance(refused.exception, AuthenticationRequired)
        self.assertIn('turned away', str(refused.exception))
        self.assertEqual(refused.exception.kind, 'guest_refused')

    async def test_meet_mute_camera_chat_end_and_degraded_policy(self):
        await self.page.set_content('''
          <button onclick="this.innerText=this.innerText==='Turn on mic'?'Turn off mic':'Turn on mic'">Turn on mic</button>
          <button onclick="this.setAttribute('aria-label', this.getAttribute('aria-label')==='Turn on camera'?'Turn off camera':'Turn on camera')" aria-label="Turn on camera">cam</button>
        ''')
        a = self.adapter(meet=True)
        self.assertTrue(a.capabilities.camera)
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
        await self.page.set_content('<button id="leave">Leave call</button><button>Turn off mic</button>')
        await self.page.locator('#leave').evaluate('''(el) => el.addEventListener("click", () => {
            window.left = true;
            document.body.innerHTML = "<p>You left the meeting</p><button>Rejoin</button>";
        })''')
        await a.leave()
        self.assertTrue(await self.page.evaluate('window.left'))

    # Leaving -------------------------------------------------------------------------------

    # Zoom's web client as the 2026-11-05 test met it: the toolbar hides after a moment without
    # pointer movement, and Leave opens a confirmation (a host's also offers End Meeting for All).
    ZOOM_MEETING = '''
      <style>#toolbar, #confirm { display: none }</style>
      <div id="video">Smith Lee</div>
      <footer id="toolbar">
        <button aria-label="Mute my microphone">mic</button>
        <button id="leave" aria-label="Leave">Leave</button>
      </footer>
      <div id="confirm" class="leave-meeting-options">
        <button id="end-all">End Meeting for All</button>
        <button id="leave-meeting">Leave Meeting</button>
      </div>
      <script>
        window.clicks = [];
        let hide = null;
        const toolbar = document.querySelector('#toolbar');
        document.addEventListener('mousemove', () => {
          toolbar.style.display = 'block';
          clearTimeout(hide);
          hide = setTimeout(() => { toolbar.style.display = 'none'; }, 1500);
        });
        document.querySelector('#leave').addEventListener('click', () => {
          window.clicks.push('leave');
          setTimeout(() => { document.querySelector('#confirm').style.display = 'block'; }, 150);
        });
        document.querySelector('#end-all').addEventListener('click', () => window.clicks.push('end-all'));
        document.querySelector('#leave-meeting').addEventListener('click', () => {
          window.clicks.push('leave-meeting');
          if (window.leaveWorks !== false) {
            setTimeout(() => { document.body.innerHTML = '<p>You have left the meeting.</p>'; }, 200);
          }
        });
      </script>'''

    async def test_zoom_leaves_through_its_hidden_toolbar_and_the_confirmation(self):
        # 2026-11-05: Leave was clicked only if visible and Leave Meeting never, so the bot stayed
        # until its browser was killed and Zoom showed its frozen tile for four minutes.
        await self.page.set_content(self.ZOOM_MEETING)
        a = self.adapter(False)
        self.assertFalse(await self.page.locator('#leave').is_visible())  # the toolbar is hidden
        loop = asyncio.get_running_loop()
        began = loop.time()
        await a.leave()
        self.assertLess(loop.time() - began, 2.5)  # at the first try, not after a second
        self.assertEqual(await self.page.evaluate('window.clicks'), ['leave', 'leave-meeting'])
        self.assertIn('You have left the meeting', await self.page.locator('body').inner_text())
        # Out already: a second leave clicks nothing.
        await self.page.evaluate('window.clicks = []')
        await a.leave()
        self.assertEqual(await self.page.evaluate('window.clicks'), [])

    async def test_zoom_answers_a_confirmation_that_is_already_open(self):
        await self.page.set_content(self.ZOOM_MEETING)
        await self.page.evaluate("document.querySelector('#confirm').style.display = 'block'")
        await self.adapter(False).leave()
        self.assertEqual(await self.page.evaluate('window.clicks'), ['leave-meeting'])

    async def test_zoom_says_so_when_it_cannot_leave(self):
        await self.page.set_content(self.ZOOM_MEETING)
        await self.page.evaluate('window.leaveWorks = false')
        with mock.patch.object(adapters, 'LEAVE_CONFIRM_SECONDS', 0.5), \
                mock.patch.object(adapters, 'LEAVE_OUT_SECONDS', 0.5), \
                self.assertRaisesRegex(RuntimeError, 'Zoom still showed the meeting'):
            await self.adapter(False).leave()
        self.assertNotIn('end-all', await self.page.evaluate('window.clicks'))

    async def test_zoom_has_left_once_the_meeting_is_over(self):
        a = self.adapter(False)
        for page in ('<p>This meeting has been ended by host</p>', '<p>You have left the meeting</p>'):
            await self.page.set_content(page)
            await a.leave()  # nothing to click, nothing raised

    async def test_teams_leaves_with_one_of_several_leave_controls(self):
        # Teams draws "Leave" with its shortcut, and an organizer's "Leave options" beside it:
        # a locator for any "leave" matched both and gave up (Playwright's strict mode).
        await self.page.set_content('''
          <button aria-label="Unmute">mic</button>
          <button id="options" aria-label="Leave options">v</button>
          <button id="leave" aria-label="Leave (Ctrl+Shift+H)">Leave</button>
          <script>
            window.clicks = [];
            document.querySelector('#options').addEventListener('click', () => window.clicks.push('options'));
            document.querySelector('#leave').addEventListener('click', () => {
              window.clicks.push('leave');
              document.body.innerHTML = '<p>You left the meeting</p><button>Rejoin</button>';
            });
          </script>''')
        await self.adapter().leave()
        self.assertEqual(await self.page.evaluate('window.clicks'), ['leave'])

    async def test_teams_says_so_when_it_cannot_leave(self):
        await self.page.set_content('<button aria-label="Leave">Leave</button><button>Unmute</button>')
        with mock.patch.object(adapters, 'LEAVE_OUT_SECONDS', 0.3), \
                self.assertRaisesRegex(RuntimeError, 'Teams still showed the meeting'):
            await self.adapter().leave()

    MEET_HOST_LEAVING = '''
      <button id="leave" aria-label="Leave call">call_end</button>
      <button aria-label="Turn off mic">mic</button>
      <div role="dialog" id="host" style="display: none">
        <button id="end" data-mdc-dialog-action="end">End the call for everyone</button>
        <button id="just">Just leave the call</button>
      </div>
      <script>
        window.clicks = [];
        document.querySelector('#leave').addEventListener('click', () => {
          window.clicks.push('leave');
          document.querySelector('#host').style.display = 'block';
        });
        document.querySelector('#end').addEventListener('click', () => window.clicks.push('end'));
        document.querySelector('#just').addEventListener('click', () => {
          window.clicks.push('just-leave');
          document.body.innerHTML = '<p>You left the meeting</p><button>Rejoin</button>';
        });
      </script>'''

    async def test_meet_host_only_leaves_and_never_ends_the_call_for_everyone(self):
        await self.page.set_content(self.MEET_HOST_LEAVING)
        await self.adapter(meet=True).leave()
        self.assertEqual(await self.page.evaluate('window.clicks'), ['leave', 'just-leave'])
        # The question already open: answered, and still never "End the call for everyone".
        await self.page.set_content(self.MEET_HOST_LEAVING)
        await self.page.evaluate("document.querySelector('#host').style.display = 'block'")
        await self.adapter(meet=True).leave()
        self.assertEqual(await self.page.evaluate('window.clicks'), ['just-leave'])


class RefusalKindTests(unittest.TestCase):
    """How a platform kept the browser out, as one of a few known words (adapters_base.JOIN_REFUSALS)."""

    def test_a_refusal_kind_is_one_of_the_known_words(self):
        from adapters_base import JOIN_REFUSALS, join_refusal
        self.assertEqual(join_refusal(AuthenticationRequired()), 'sign_in_required')
        self.assertEqual(join_refusal(AuthenticationRequired('x', kind='restricted')), 'restricted')
        self.assertEqual(join_refusal(AuthenticationRequired('x', kind='made up')), 'sign_in_required')
        self.assertEqual(join_refusal(GuestRefused('x')), 'guest_refused')
        self.assertIsNone(join_refusal(RuntimeError('Zoom meeting ended or is invalid')))
        self.assertEqual(set(JOIN_REFUSALS), {'sign_in_required', 'restricted', 'guest_refused'})
