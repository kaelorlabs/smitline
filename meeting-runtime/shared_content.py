"""Platform-specific shared-content location and element capture."""
from dataclasses import dataclass

MIN_WIDTH = 160
MIN_HEIGHT = 90
MAX_MATCHES = 1

ZOOM_SELECTORS = (
    '#sharee-container',
    '[data-testid="sharee-container"]',
    '.sharee-container',
    '[aria-label="You are viewing someone\'s screen"]',
    '.pwa-webclient-sharing-container',
)
TEAMS_SELECTORS = (
    '[data-tid="calling-screen-sharing-stage"]',
    '[data-tid="screen-sharing-stage"]',
    '[data-tid="calling-screen-share-viewer"]',
    '[data-tid="screen-share-video"]',
)
ZOOM_EXCLUDE = (
    '#wc-container-left',
    '.gallery-video-container',
    '.participants-section-container',
    '[aria-label*="chat panel" i]',
    '[aria-label*="open the chat" i]',
)
TEAMS_EXCLUDE = (
    '[data-tid="calling-participant-stream"]',
    '[data-tid="chat-pane"]',
    '[data-tid="roster-list"]',
    '[id*="gallery"]',
)
SELECTORS = {'zoom': ZOOM_SELECTORS, 'teams': TEAMS_SELECTORS}
EXCLUDE = {'zoom': ZOOM_EXCLUDE, 'teams': TEAMS_EXCLUDE}


@dataclass(frozen=True)
class SharedContentState:
    available: bool
    confidence: str
    reason: str = None
    width: int = None
    height: int = None

    def public(self):
        payload = {
            'available': self.available,
            'confidence': self.confidence,
        }
        if self.reason:
            payload['reason'] = self.reason
        return payload


def _selectors(platform):
    if platform not in SELECTORS:
        return (), ()
    return SELECTORS[platform], EXCLUDE[platform]


async def _visible_boxes(page, selectors):
    matches = []
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = await locator.count()
        except Exception:
            continue
        for index in range(count):
            element = locator.nth(index)
            try:
                if not await element.is_visible():
                    continue
                box = await element.bounding_box()
            except Exception:
                continue
            if not box or box.get('width', 0) < MIN_WIDTH or box.get('height', 0) < MIN_HEIGHT:
                continue
            matches.append((element, box, selector))
    return matches


async def _excluded(element, selectors):
    if not selectors:
        return False
    check = '''(el, selectors) => {
      for (const selector of selectors) {
        try {
          if (el.matches(selector)) return true;
          const closest = el.closest(selector);
          if (closest && closest !== el && !el.querySelector('[data-share-content], canvas, video')) {
            return true;
          }
        } catch (error) {}
      }
      return false;
    }'''
    try:
        return bool(await element.evaluate(check, list(selectors)))
    except Exception:
        return True


def _dedupe(matches):
    unique = []
    for element, box, selector in matches:
        overlap = False
        for _, other, _ in unique:
            if _boxes_overlap(box, other, 0.8):
                overlap = True
                break
        if not overlap:
            unique.append((element, box, selector))
    return unique


def _boxes_overlap(left, right, threshold):
    ax2, ay2 = left['x'] + left['width'], left['y'] + left['height']
    bx2, by2 = right['x'] + right['width'], right['y'] + right['height']
    overlap_w = max(0, min(ax2, bx2) - max(left['x'], right['x']))
    overlap_h = max(0, min(ay2, by2) - max(left['y'], right['y']))
    inter = overlap_w * overlap_h
    area = min(left['width'] * left['height'], right['width'] * right['height'])
    return area > 0 and (inter / area) >= threshold


async def locate_shared_content(page, platform):
    selectors, exclude = _selectors(platform)
    if not selectors:
        return None, SharedContentState(False, 'none', 'unavailable')
    matches = []
    for element, box, selector in await _visible_boxes(page, selectors):
        if await _excluded(element, exclude):
            continue
        try:
            tag = (await element.evaluate('el => el.tagName && el.tagName.toLowerCase()')) or ''
        except Exception:
            continue
        if tag in ('body', 'html'):
            continue
        matches.append((element, box, selector))
    unique = _dedupe(matches)
    if not unique:
        return None, SharedContentState(False, 'none', 'unavailable')
    if len(unique) > MAX_MATCHES:
        return None, SharedContentState(False, 'low', 'selector_ambiguous')
    element, box, _selector = unique[0]
    return element, SharedContentState(
        True, 'high', width=int(box['width']), height=int(box['height']))


async def shared_content_state(page, platform):
    _element, state = await locate_shared_content(page, platform)
    return state


async def capture_shared_content(page, platform):
    element, state = await locate_shared_content(page, platform)
    if element is None or not state.available:
        return None, state
    try:
        png = await element.screenshot(type='png')
    except Exception:
        return None, SharedContentState(False, 'low', 'unavailable')
    if not png:
        return None, SharedContentState(False, 'low', 'unavailable')
    return bytes(png), state
