# Capability matrix

Values below match the code. Smitline does not claim platform features it has not built or tested.

## Meeting platforms

From `MeetingPlatformAdapter.capabilities` in `meeting-runtime/adapters.py` and the adapters' join paths.

| Capability | Zoom | Teams | Google Meet |
| --- | --- | --- | --- |
| Official HTTPS invite only | `*.zoom.us` `/j/` or `/wc/join/` numeric ids | `teams.microsoft.com` / `teams.live.com` meetup or meet paths | `meet.google.com` `/xxx-yyyy-zzz` (3-4-3 letters) |
| Government / lookalike hosts | Rejected | Government Teams not enabled; lookalikes rejected | `www.`, workspace/stream hosts, `/landing`, `/new` rejected |
| Guest join first | Yes | Yes | Yes |
| Signed-in profile fallback | No | Microsoft (`teams-connected` / `teams`) | Google (`google-connected` / `google`) |
| Browser controls | Smitline's own Zoom controls | Joinly Teams controller | Joinly Google Meet controller |
| Virtual camera | Yes | Yes | Yes |
| Participant discovery | Yes (the participants button and panel) | Yes | Yes |
| Empty-room auto-leave | Leaves when the participant count shows it alone for 15 s; ended/removal detection also applies | Leaves when the toolbar reports one participant | Same count-based leave when the toolbar reports one participant; ended/removal detection also applies |
| Screen sharing (outgoing or incoming) | No | No | No |
| Join by phone (`dialIn`, [joining by phone](meetings.md#joining-by-phone)) | Meeting ID, then the numeric phone passcode after `*`: `,,ID#,,,,*PASSCODE#` | Phone conference ID: `,,ID#` (organizers with audio conferencing) | PIN: `,,PIN#` (Workspace meetings) |
| Read from a pasted invitation | One-tap lines, dial-in numbers, "Meeting ID", the numeric "Passcode" | One-tap lines, "Phone conference ID" (never the app's "Meeting ID" or passcode) | "Or dial" numbers, "PIN", with Google's bidi marks |
| Dial-in fallback | When the browser join is refused (`authentication_required`) or no meeting seat is free | Same | Same |

Live tenant policy can still refuse admission. Browser fixtures do not prove production compatibility. Joining by phone is tested against a simulated bridge (`test_dial_in_sim.py`); real Zoom, Teams and Google Meet bridges are not yet verified.

## Voice

GPT-Live (`gpt-live-1`) is the only voice on every channel. It hands harder questions to a backend model through Responses delegation: `SMITLINE_PHONE_BACKEND_MODEL` for phone calls and `SMITLINE_MEETING_BACKEND_MODEL` for meetings, both `gpt-5.6-terra` by default. `SMITLINE_PHONE_WEB_SEARCH=1` and `SMITLINE_MEETING_WEB_SEARCH=1` add OpenAI web search to that backend.
