"""Validate Joinly MCP and round-trip local speech without API credentials."""
import asyncio
import json
from pathlib import Path
import wave

import numpy as np
from fastmcp import Client
from faster_whisper import WhisperModel
from joinly.services.tts.kokoro import KokoroTTS


async def main():
    report = {}
    async with Client("http://127.0.0.1:8000/mcp/") as client:
        await client.ping()
        names = sorted(tool.name for tool in await client.list_tools())
        assert {"join_meeting", "speak_text", "share_screen", "get_transcript"} <= set(names)
        report["mcp_tools"] = names
        report["mcp_ping"] = "passed"
    phrase = "Hello team. Joinly is running locally. I can help you review last week's sales."
    async with KokoroTTS() as tts:
        chunks = [chunk async for chunk in tts.stream(phrase)]
    samples = np.frombuffer(b"".join(chunks), dtype=np.float32)
    assert len(samples) > 24000 and np.max(np.abs(samples)) > 0.01
    audio_path = Path("/demo/joinly-voice.wav")
    with wave.open(str(audio_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    model = WhisperModel("base", device="cpu", compute_type="int8", local_files_only=True)
    segments, _ = model.transcribe(str(audio_path), language="en")
    transcript = " ".join(segment.text.strip() for segment in segments)
    assert "sales" in transcript.lower(), transcript
    report.update(speech_seconds=round(len(samples) / 24000, 2), transcript=transcript,
                  speech_round_trip="passed", live_meeting="not tested")
    Path("/demo/verification.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


asyncio.run(main())
