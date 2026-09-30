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
| Participant discovery | No | Yes | Yes |
| Empty-room auto-leave | Ended/removal detection only (no participant count) | Leaves when the toolbar reports one participant | Same count-based leave when the toolbar reports one participant; ended/removal detection also applies |
| Screen sharing (outgoing or incoming) | No | No | No |

Live tenant policy can still refuse admission. Browser fixtures do not prove production compatibility.

## Voice

GPT-Live (`gpt-live-1`) is the only voice on every channel. It hands harder questions to a backend model through Responses delegation: `COLLEAGUE_PHONE_BACKEND_MODEL` for phone calls and `COLLEAGUE_MEETING_BACKEND_MODEL` for meetings, both `gpt-5.6-terra` by default. `COLLEAGUE_PHONE_WEB_SEARCH=1` and `COLLEAGUE_MEETING_WEB_SEARCH=1` add OpenAI web search to that backend.
