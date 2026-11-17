"""Operator-owned Microsoft login. No credential capture or scripted sign-in."""
import asyncio
import contextlib
import os
import signal
from pathlib import Path
from urllib.parse import urlsplit
from aiohttp import web
from joinly.providers.browser.browser_session import BrowserSession
from joinly.providers.browser.devices.virtual_display import VirtualDisplay
from meeting_control import health_route
from meeting_view import start_meeting_view

async def connect():
    root = Path('/meeting-runtime/profiles')
    profile = root / 'teams'
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile.chmod(0o700)
    marker = root / 'teams-connected'
    marker.unlink(missing_ok=True)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    state = {'stage': 'connecting_account', 'mode': 'teams_account', 'authenticationState': 'connecting'}
    app = web.Application()
    # The sign-in stage, for the console: like a meeting's, only to this computer's own requests.
    app.router.add_get('/health', health_route(None, lambda: state))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 8094).start()
    env = {k:v for k,v in os.environ.items() if not any(word in k for word in ('KEY', 'TOKEN', 'PASSCODE', 'MEETING_URL'))}
    env['JOINLY_BROWSER_PROFILE_DIR'] = str(profile)
    try:
        async with VirtualDisplay(env=env, use_vnc_server=True, vnc_port=5900):
            # The meeting view, for this start's token only (meeting_view.py).
            viewer = await start_meeting_view(env)
            try:
                async with BrowserSession(env=env) as browser:
                    page = await browser.get_page()
                    await page.goto('https://teams.microsoft.com/', wait_until='domcontentloaded')
                    while not stop.is_set():
                        # A Teams URL alone is not proof of login: require the account menu.
                        if urlsplit(page.url).hostname in ('teams.microsoft.com', 'teams.live.com'):
                            account = page.locator('[data-tid="me-control-avatar-trigger"], button[aria-label*="Your profile" i], button[aria-label*="Account manager" i]')
                            if await account.count() and await account.first.is_visible():
                                marker.write_text('connected\n')
                                marker.chmod(0o600)
                                state.update(stage='account_connected', authenticationState='connected')
                        await asyncio.sleep(1)
            finally:
                await viewer.stop()
    finally:
        await runner.cleanup()
