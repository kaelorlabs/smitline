---
name: call-with-smitline
description: Place a phone call or join a video meeting for the user with Smitline, then report the result. Use when the user says "call", "phone", "ring", "book by phone", "leave a message", "join this meeting", or pastes a Zoom, Teams, or Google Meet link for someone to attend.
---

# Calls with Smitline

Smitline talks with people in real time, on the phone or in a video meeting, and returns a structured result. Use the MCP tools (`start_call`, `wait_for_call`, and the rest) when they are available; otherwise the CLI: `smitline call ... --wait`. When Smitline runs in its Docker container, as it does after a normal setup, the CLI is `docker exec smitline smitline call ... --wait`.

Phone calls and meetings work the same way. To join a Zoom, Teams, or Google Meet meeting, set `channel` to `meeting` and `to` to the invite URL (CLI: `smitline call --meeting <url> --objective "..." --wait`). Smitline joins as a participant, listens, answers when spoken to, and returns the summary, decisions, action items, and transcript when the meeting ends or you call `end_call`.

## 1. Write a complete brief

| Field | What to put |
| --- | --- |
| `channel` | `phone`, or `meeting` for a Zoom, Teams, or Google Meet link |
| `to` | E.164 number such as `+14155550142`, or the invite URL |
| `onBehalfOf` | The user's name. The call opens with "Hi, this is NAME's AI assistant." Leave it out to use the name from setup. |
| `objective` | What the call must achieve, in one or two sentences |
| `context` | What you and the user have been working on that the other side may ask about. Text, or an object: `summary` (a few sentences), `facts`, `decisions`, `openQuestions`, and `details` for long reference material. The assistant looks things up in it; it does not recite it. |
| `questions` | What to find out. The result answers each one. |
| `tone` | How to come across, such as "casual; he is a close friend". Leave it out to match the relationship. |
| `contact` | Who you are calling (`name`, `relationship`, `notes`), when the profile does not know the number |
| `mayAgreeTo` | What may be accepted without checking back, such as times or a price ceiling |
| `mustNotShare` | What must never be said, such as payment details |
| `successCriteria` | How to tell the call worked |

If something important is unknown, ask the user before calling. Never guess prices, dates, or commitments. Never put card numbers, passwords, or one-time codes in a brief.

Every phone call also gets the user's profile: who they are, the people they know, how they like to come across, and standing boundaries. Read it with `get_profile`. When the user tells you about someone ("Alex is my close friend"), save it with `update_profile` so later calls know them too.

## 2. Offer a rehearsal the first time

For a new kind of call, offer: "Want me to practice on you first?" Then start the same brief with `rehearsal: true` and without `to`: a rehearsal always rings the user's own phone, and the user plays the other side.

## 3. Start, then wait

1. `start_call` with the brief. If it returns `brief_incomplete`, ask the user the listed questions, then retry. If the call is refused, tell the user why in plain words. `outside_calling_hours`: offer to call later; set `afterHours: true` only if the user confirms the person expects a call now. `do_not_call`: the person asked not to be called again; never remove them from the list unless the user says the person has since agreed to calls. Smitline never calls emergency numbers; if someone needs help, tell the user to call themselves.
2. Tell the user in one line: "Calling Luigi's now. I'll tell you when it's done." (or "Joining the meeting now.")
3. `wait_for_call` until the status is `completed`, `failed`, or `canceled`, calling again while it is still running.

While the call runs, `send_call_instruction` passes on new guidance from the user (with `silent: true` for a fact the assistant should know without acting on it right away), `transfer_call_to_me` hands a phone call to the user's phone, and `end_call` wraps up politely.

## 4. Report the result

Lead with the outcome in plain words, then the details the user needs later (confirmation numbers, times, prices), then any open questions or action items. Offer the transcript if they want it. Do not claim anything the result does not say.

Outcomes: `achieved`, `partial`, `declined`, `not_reached` (nobody answered or the line was busy), `voicemail` (voicemail answered; the summary says whether a message was left), `failed`, `canceled`. If `disclosureVerified` is `false`, tell the user the AI disclosure was not clearly heard on that call. If `doNotCall` is `true`, tell the user the person asked not to be called again and Smitline will not call them again.
