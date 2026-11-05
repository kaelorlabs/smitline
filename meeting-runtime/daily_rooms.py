"""Make or remove a Daily room for a meeting agent, through Daily's REST API.

    SMITLINE_DAILY_API_KEY=... python meeting-runtime/daily_rooms.py create [--minutes 60]
    SMITLINE_DAILY_API_KEY=... python meeting-runtime/daily_rooms.py delete <room name>

create prints only the room's link: anyone with it can join from a browser, with no account, until
the room expires and Daily ejects everyone. The room is public with no knocking, which suits a
quick meeting with an agent; the link is the only key, so share it only with the people invited.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = 'https://api.daily.co/v1'
MAX_MINUTES = 24 * 60


def _request(api_key, method, path, body=None, opener=urllib.request.urlopen):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f'{API}{path}', data=data, method=method,
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'})
    try:
        with opener(request, timeout=20) as response:
            return json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as error:
        detail = error.read()[:300].decode('utf-8', 'replace')
        raise RuntimeError(f'Daily answered {error.code}: {detail}') from None


def room_properties(minutes, now=None):
    """A public room that expires, ejects everyone when it does, and lets people chat."""
    if not 1 <= minutes <= MAX_MINUTES:
        raise ValueError(f'--minutes must be from 1 to {MAX_MINUTES}')
    return {
        'privacy': 'public',
        'properties': {
            'exp': int((now if now is not None else time.time()) + minutes * 60),
            'eject_at_room_exp': True,
            'enable_knocking': False,
            'enable_prejoin_ui': True,
            'enable_chat': True,
            'max_participants': 10,
        },
    }


def create_room(api_key, minutes=60, opener=urllib.request.urlopen):
    """Create a room; returns (its link, its name)."""
    room = _request(api_key, 'POST', '/rooms', room_properties(minutes), opener=opener)
    url, name = room.get('url'), room.get('name')
    if not url or not name:
        raise RuntimeError('Daily did not say where the room is')
    return url, name


def delete_room(api_key, name, opener=urllib.request.urlopen):
    if not name or '/' in name:
        raise ValueError('Give the room name, as create printed it at the end of the link')
    _request(api_key, 'DELETE', f'/rooms/{name}', opener=opener)


def main(argv=None, environ=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description='Make or remove a Daily room for a meeting agent.')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='make a room and print its link')
    create.add_argument('--minutes', type=int, default=60, help='how long the room lasts (default 60)')
    delete = commands.add_parser('delete', help='remove a room now')
    delete.add_argument('name', help='the room name: the end of its link')
    args = parser.parse_args(argv)
    api_key = (environ if environ is not None else os.environ).get('SMITLINE_DAILY_API_KEY', '').strip()
    if not api_key:
        parser.error('set SMITLINE_DAILY_API_KEY to the Daily account\'s API key')
    if args.command == 'create':
        url, _name = create_room(api_key, args.minutes)
        print(url, file=out)
    else:
        delete_room(api_key, args.name)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
