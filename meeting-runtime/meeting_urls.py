"""Pure URL recognition, shared by preflight and adapter registry."""
import re
from urllib.parse import urlsplit, urlunsplit

def platform_for_url(value):
    try:
        url = urlsplit(value)
        if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443):
            raise ValueError()
        host = url.hostname or ''
        if re.fullmatch(r'(?:[a-z0-9-]+\.)?zoom\.us', host) and re.fullmatch(r'/(?:j/|wc/(?:join/)?)[0-9]+/?', url.path):
            return 'zoom'
        if host in ('teams.microsoft.com', 'teams.live.com') and re.match(r'^/(?:l/meetup-join/[^/]+|meet/[^/]+)', url.path):
            return 'teams'
    except (ValueError, TypeError):
        pass
    raise ValueError('Use a supported HTTPS Zoom or Teams meeting invite.')

def normalize_url(value):
    platform = platform_for_url(value)
    url = urlsplit(value)
    path = re.sub(r'^/j/(\d+)', r'/wc/join/\1', url.path) if platform == 'zoom' else url.path
    return urlunsplit((url.scheme, url.netloc, path, url.query, ''))
