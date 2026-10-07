# Smitline Python SDK

Versioned local SDK (`smitline` 0.2.0) that lets any agent or program place phone calls and join Zoom, Teams, and Google Meet meetings through the Smitline runtime daemon. GPT-Live does the talking; you get a structured result when the call ends. The default transport uses the standard library against the loopback daemon.

Requires Python 3.10+. This package is not published to PyPI: use it from a checkout of this repository, with `packages/sdk-python` on `PYTHONPATH` or installed with `pip install ./packages/sdk-python`. There are no third-party runtime dependencies.

## Calls and meetings

Both go through `start_call` with a brief:

```python
from smitline import Smitline

client = Smitline()

# A phone call
call = await client.start_call({
    'channel': 'phone',
    'to': '+14155550142',
    'objective': 'Book a table for two at 7pm on Friday',
    'mayAgreeTo': ['6:30pm to 8pm'],
    'mustNotShare': ['card number'],
})

# A meeting: 'to' is the Zoom, Teams, or Google Meet invite URL
meeting = await client.start_call({
    'channel': 'meeting',
    'to': 'https://zoom.us/j/123456789',
    'objective': 'Take notes on the roadmap review and answer questions about the launch plan',
    'context': {'summary': 'We ship the beta on the 14th.'},
})

done = await client.wait_for_call(meeting['id'], 50)
while done['status'] not in ('completed', 'failed', 'canceled'):
    done = await client.wait_for_call(meeting['id'], 50)
print(done['result']['summary'])
```

`start_call` returns at once with the queued call. An incomplete brief raises `ValidationError`, whose `details['missing']` lists a question to ask the user for each missing field; `check_call(brief)` reports the same without placing the call.

## Methods

`check_call`, `start_call`, `get_call`, `wait_for_call`, `list_calls`, `instruct_call` (with `silent=True` for a background note), `end_call`, `transfer_call`, `get_profile`, `update_profile`, and `list_voices`.

Typed errors: `ValidationError`, `StartupError`, `RuntimeError`.

The loopback transport reads `.smitline/daemon.auth` under `root` (default: the working directory), authenticates every request, rotates a stale token from that file, and can start `start-runtime-daemon.sh` (`autostart=False` turns this off).

When Smitline runs in the `smitline` container, the token is inside the container and changes each time it starts. Pass a `read_auth` function that fetches it, and turn off autostart:

```python
import subprocess

def read_auth():
    return subprocess.run(['docker', 'exec', '-u', 'app', 'smitline', 'cat', '/data/.smitline/daemon.auth'],
                          capture_output=True, text=True, check=True).stdout.strip()

client = Smitline(read_auth=read_auth, autostart=False)
```
