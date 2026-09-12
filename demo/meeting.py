"""Join a user-provided test call and demonstrate speech and screen sharing."""
import asyncio
import base64
from pathlib import Path
import sys
from fastmcp import Client


async def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python /demo/meeting.py "MEETING_URL"')
    async with Client("http://127.0.0.1:8000/mcp/") as client:
        joined = False
        try:
            print("Joining as Meeting Demo; please admit the participant.", flush=True)
            print(await client.call_tool("join_meeting", {"meeting_url": sys.argv[1]}), flush=True)
            joined = True
            snapshot = await client.call_tool("get_video_snapshot")
            for item in snapshot.content:
                if item.type == "image":
                    Path("/demo/meeting-snapshot.jpg").write_bytes(base64.b64decode(item.data))
            try:
                print(await client.call_tool("get_participants"), flush=True)
            except Exception as error:
                print(f"Participant lookup unavailable: {error}", flush=True)
            await client.call_tool("speak_text", {"text": "Hello team. This is the local Joinly demo. I have joined your meeting from Docker."})
            await client.call_tool("share_screen", {"url": "data:text/html,<html><body style='font-family:sans-serif;padding:80px;background:%23182030;color:white'><h1>Joinly local demo</h1><p>Meeting audio and screen sharing from Docker.</p><p>This is a connectivity demonstration, not an AI-generated analysis.</p></body></html>"})
            print("Demo shared. Speak a sentence; collecting transcript for 30 seconds.", flush=True)
            await asyncio.sleep(30)
            result = await client.call_tool("get_transcript")
            print(result, flush=True)
        finally:
            if joined:
                await client.call_tool("leave_meeting")


asyncio.run(main())
