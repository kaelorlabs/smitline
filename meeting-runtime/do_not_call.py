"""The do-not-call list, for the operator of an installation, on the machine it runs on.

A number someone asked on a call not to be called again is locked: the API cannot take it off the
list, and no refusal says how. Only the operator can, with this command, once the person has agreed
to calls again; it keeps the entry on record with why, who and when. It works on the list's file
directly and has no HTTP route, so nothing that holds an API token can run it.

  python3 meeting-runtime/do_not_call.py list [--removed]
  python3 meeting-runtime/do_not_call.py lift +14155550142 --reason "Agreed to calls again" --by robin

--root is the folder with the installation's .env (the checkout by default, /data in the image);
--owner picks another owner's list on an installation that serves several.
"""
import old_names
old_names.adopt_old_settings()

import argparse
import json
from pathlib import Path
import sys

from call_hooks import LOCAL_OWNER, DefaultCallHooks, valid_owner


def _list(root, owner):
    return DefaultCallHooks(environ={}, env_file=Path(root).resolve() / '.env').do_not_call_list(owner)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage the do-not-call list as the installation's operator")
    parser.add_argument('--root', default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument('--owner', default=LOCAL_OWNER, help='the owner whose list it is (default: local)')
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list', help='the numbers on the list, locked ones marked')
    listing.add_argument('--removed', action='store_true', help='also the numbers taken off, kept on record')
    lift = commands.add_parser('lift', help='take a number off the list, locked or not')
    lift.add_argument('number', help='E.164, such as +14155550142')
    lift.add_argument('--reason', required=True, help='why, such as "Agreed to calls again on 2026-11-18"')
    lift.add_argument('--by', required=True, help='who lifts it')
    args = parser.parse_args(argv)
    if not valid_owner(args.owner):
        print('An owner id is lowercase letters, digits, _ and -.', file=sys.stderr)
        return 2
    listed = _list(args.root, args.owner)
    if args.command == 'list':
        answer = {'numbers': listed.active()}
        if args.removed:
            answer['removed'] = listed.removed()
        print(json.dumps(answer, indent=2, ensure_ascii=False))
        return 0
    try:
        entry = listed.lift(args.number, reason=args.reason, by=args.by)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if entry is None:
        print(f'{args.number} is not on the do-not-call list.', file=sys.stderr)
        return 1
    print(f'Lifted {entry["number"]}: it can be called again. The entry stays on record '
          '(list --removed shows it).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
