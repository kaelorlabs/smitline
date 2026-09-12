"""Expose Joinly's browser locally so the user can sign into Google directly."""
import asyncio
import signal
from joinly.providers.browser.meeting_provider import BrowserMeetingProvider


async def main():
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, done.set)
    async with BrowserMeetingProvider(vnc_server=True, vnc_server_port=5900) as provider:
        page = await provider._browser_session.get_page()
        await page.goto("https://accounts.google.com/", wait_until="domcontentloaded")
        viewer = await asyncio.create_subprocess_exec(
            "/usr/bin/websockify", "--web=/usr/share/novnc", "6080", "127.0.0.1:5900")
        print("Google sign-in ready at http://127.0.0.1:6081/vnc.html?autoconnect=true", flush=True)
        try:
            await done.wait()
        finally:
            viewer.terminate()
            await viewer.wait()


asyncio.run(main())
