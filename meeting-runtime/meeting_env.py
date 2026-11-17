"""The environment of a meeting started by hand (start-meeting-agent.sh): its OpenAI key and what
the meeting reads (meeting_line.single_meeting_environment), with the meeting's URL and passcode
from .env.meeting, as the daemon gives the one meeting container. It writes them to a new private
file (0600) that compose reads instead of .env, and nothing else of .env reaches the container.

    python3 meeting_env.py DATA_ROOT ENV_FILE
"""
import old_names
old_names.adopt_old_settings()

import os
from pathlib import Path
import sys

from call_hooks import LOCAL_OWNER, DefaultCallHooks, MissingCredentials
from meeting_line import single_meeting_environment
from meeting_supervisor import owner_env_file


def write_meeting_env(root, target, environ=None):
    root = Path(root)
    hooks = DefaultCallHooks(environ=os.environ if environ is None else environ,
                             env_file=root / '.env')
    values = single_meeting_environment(hooks, LOCAL_OWNER, root / '.env.meeting', by_hand=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(owner_env_file(values))
    return target


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print('usage: meeting_env.py DATA_ROOT ENV_FILE', file=sys.stderr)
        return 2
    try:
        write_meeting_env(args[0], args[1])
    except MissingCredentials as error:
        print(f'{error}. Set OPENAI_API_KEY first: smitline setup secrets', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
