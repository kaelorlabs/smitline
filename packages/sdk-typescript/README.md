# Colleague AI TypeScript SDK

Versioned local SDK (`@colleague-ai/sdk` 1.0.0) that lets any agent or program place phone calls and join Zoom, Teams, and Google Meet meetings through the Colleague AI runtime daemon. GPT-Live does the talking; you get a structured result when the call ends. The client interface is transport-independent; the default transport talks to the loopback daemon.

Requires Node.js 22+. This package is for local use and is not published to npm.

## Calls and meetings

Both go through `startCall` with a brief:

```ts
import { Colleague } from '@colleague-ai/sdk';

const colleague = new Colleague();

// A phone call
const call = await colleague.startCall({
  channel: 'phone',
  to: '+14155550142',
  objective: 'Book a table for two at 7pm on Friday',
  mayAgreeTo: ['6:30pm to 8pm'],
  mustNotShare: ['card number'],
});

// A meeting: `to` is the Zoom, Teams, or Google Meet invite URL
const meeting = await colleague.startCall({
  channel: 'meeting',
  to: 'https://zoom.us/j/123456789',
  objective: 'Take notes on the roadmap review and answer questions about the launch plan',
  context: { summary: 'We ship the beta on the 14th.', details: longBackground },
});

let done = await colleague.waitForCall(meeting.id, 50);
while (!['completed', 'failed', 'canceled'].includes(done.status)) {
  done = await colleague.waitForCall(meeting.id, 50);
}
console.log(done.result?.summary);
```

`startCall` returns at once with the queued call. An incomplete brief throws a `ValidationError` whose `details.missing` lists a question to ask the user for each missing field; `checkCall(brief)` reports the same without placing the call.

## Methods

| Method | Behavior |
| --- | --- |
| `checkCall(brief)` | Validate a brief and configuration without placing the call |
| `startCall(brief)` | Place a phone call or join a meeting |
| `getCall(id)` / `listCalls(limit)` | Current state of one call, or the most recent calls |
| `waitForCall(id, timeoutSeconds)` | Long-poll (at most 280 s per request) until the call ends |
| `instructCall(id, text, { silent })` | Guidance during a call; `silent` adds a background note |
| `endCall(id)` / `transferCall(id)` | Wrap up politely, or hand a phone call to the user's phone |
| `getProfile()` / `updateProfile(update)` | The user's profile that every call gets as background |
| `listVoices()` | GPT-Live voices |

## Errors

| Class | When |
| --- | --- |
| `ValidationError` | The daemon rejected the brief or request (`details` has the fields and questions) |
| `StartupError` | The daemon could not be reached or started |
| `RuntimeError` | Any other daemon failure |

## Loopback transport

The default transport:

1. Connects to `127.0.0.1:8765` (or `COLLEAGUE_DAEMON_PORT`)
2. Reads the host-only token from `.colleague/daemon.auth` and never returns it
3. Sends `Authorization: Bearer …` on every request
4. Rereads the token file after 401
5. Starts `start-runtime-daemon.sh` if the port is closed
