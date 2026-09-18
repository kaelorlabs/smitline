"""Respond only to Zoom's host-unmute dialog, never the toolbar or chat."""
import re

HOST_REQUEST = re.compile(r'(?:the\s+)?host\s+(?:would like you to|asks? you to|is asking you to|has asked you to)\s+unmute', re.I)


async def accept_host_unmute(page):
    dialogs = page.locator('[role="dialog"], [role="alertdialog"]')
    for dialog in await dialogs.all():
        if not await dialog.is_visible():
            continue
        if not HOST_REQUEST.search(await dialog.inner_text()):
            continue
        button = dialog.get_by_role('button', name=re.compile(r'^unmute$', re.I))
        if await button.count() == 1 and await button.is_visible() and await button.is_enabled():
            await button.click(timeout=1000)
            return True
    return False


async def microphone_is_muted(page):
    # Zoom auto-hides its toolbar. Its accessible labels still track the
    # microphone state. Prefer a visible control because Zoom can leave stale
    # hidden toolbar copies in the DOM during responsive layout changes.
    for muted, label in [(True, r'^unmute(?: my microphone)?(?:\s*\([^)]*\))?$'),
                         (False, r'^mute(?: my microphone)?(?:\s*\([^)]*\))?$')]:
        buttons = page.get_by_role('button', name=re.compile(label, re.I), include_hidden=True)
        for button in await buttons.all():
            if await button.is_visible():
                return muted
    for muted, label in [(True, r'^unmute(?: my microphone)?(?:\s*\([^)]*\))?$'),
                         (False, r'^mute(?: my microphone)?(?:\s*\([^)]*\))?$')]:
        if await page.get_by_role(
                'button', name=re.compile(label, re.I), include_hidden=True).count():
            return muted
    return None
