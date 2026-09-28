---
name: call-with-colleague-ai
description: Place a phone call or join a video meeting for the user with Colleague AI, then report the result. Use when the user says "call", "phone", "ring", "book by phone", "leave a message", "join this meeting", or pastes a Zoom, Teams, or Google Meet link for someone to attend.
---

# Calls with Colleague AI

Colleague AI talks with people in real time and returns a structured result. Use the MCP tools (`start_call`, `wait_for_call`, and the rest) when they are available; otherwise the CLI: `colleague call ... --wait`.

## 1. Write a complete brief

| Field | What to put |
| --- | --- |
| `channel` | `phone`, or `meeting` for a Zoom, Teams, or Google Meet link |
| `to` | E.164 number such as `+14155550142`, or the invite URL |
| `onBehalfOf` | The user's name. The call opens with "Hi, I'm an AI assistant calling on behalf of NAME." Leave it out to use the name from setup. |
| `objective` | What the call must achieve, in one or two sentences |
| `context` | What the other side may ask: names, dates, reference numbers, preferences |
| `mayAgreeTo` | What may be accepted without checking back, such as times or a price ceiling |
| `mustNotShare` | What must never be said, such as payment details |
| `successCriteria` | How to tell the call worked |

If something important is unknown, ask the user before calling. Never guess prices, dates, or commitments. Never put card numbers, passwords, or one-time codes in a brief.

## 2. Offer a rehearsal the first time

For a new kind of call, offer: "Want me to practice on you first?" Then start the same brief with `rehearsal: true` and without `to`: a rehearsal always rings the user's own phone, and the user plays the other side.

## 3. Start, then wait

1. `start_call` with the brief. If it returns `brief_incomplete`, ask the user the listed questions, then retry.
2. Tell the user in one line: "Calling Luigi's now. I'll tell you when it's done."
3. `wait_for_call` until the status is `completed`, `failed`, or `canceled`, calling again while it is still running.

While the call runs, `send_call_instruction` passes on new guidance from the user, `transfer_call_to_me` hands a phone call to the user's phone, and `end_call` wraps up politely.

## 4. Report the result

Lead with the outcome in plain words, then the details the user needs later (confirmation numbers, times, prices), then any open questions or action items. Offer the transcript if they want it. Do not claim anything the result does not say.

Outcomes: `achieved`, `partial`, `declined`, `not_reached` (nobody answered or the line was busy), `voicemail` (voicemail answered; the summary says whether a message was left), `failed`, `canceled`. If `disclosureVerified` is `false`, tell the user the AI disclosure was not clearly heard on that call.
