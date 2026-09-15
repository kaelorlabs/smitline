"""Guest-first browser ownership with a single, explicit account retry."""
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from adapters import create_adapter
from adapters_base import AuthenticationRequired
from joinly.providers.browser.browser_session import BrowserSession

@asynccontextmanager
async def joined_meeting(url, name, passcode, env, stop, stage, state,
                         profile_root=Path('/meeting-runtime/profiles'),
                         browser_factory=BrowserSession, adapter_factory=create_adapter):
    async with AsyncExitStack() as stack:
        guest_env = {k: v for k, v in env.items() if k != 'JOINLY_BROWSER_PROFILE_DIR'}
        browser = await stack.enter_async_context(browser_factory(env=guest_env))
        page = await browser.get_page()
        adapter = adapter_factory(url, page, stop, stage)
        state['capabilities'] = adapter.capabilities.public()
        state['authenticationState'] = 'guest'
        try:
            await adapter.join(url, name, passcode)
        except AuthenticationRequired:
            if adapter.platform_id != 'teams' or not (profile_root / 'teams-connected').is_file():
                state['authenticationState'] = 'required'
                raise
            # Close the guest completely before using the account profile.
            await stack.aclose()
            browser = await stack.enter_async_context(browser_factory(env={**guest_env, 'JOINLY_BROWSER_PROFILE_DIR': str(profile_root / 'teams')}))
            page = await browser.get_page()
            adapter = adapter_factory(url, page, stop, stage)
            state['authenticationState'] = 'signed_in'
            try:
                await adapter.join(url, name, passcode)
            except AuthenticationRequired:
                state['authenticationState'] = 'required'
                raise
        try:
            yield page, adapter
        finally:
            # Closing the browser also leaves if the explicit leave button fails.
            import contextlib
            with contextlib.suppress(Exception):
                await adapter.leave()
