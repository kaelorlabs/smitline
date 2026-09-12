"""Live local voice room: WebRTC peers, Whisper input, Codex replies, Kokoro output."""
import asyncio
import io
import json
from pathlib import Path
import time
import uuid
import wave

from aiohttp import web
from faster_whisper import WhisperModel
import numpy as np
from joinly.services.tts.kokoro import KokoroTTS

ROOT = Path('/live')
clients = {}
history = []
turn_lock = asyncio.Lock()
audio_cache = {}
state = 'Ready'


async def broadcast(data):
    for ws in list(clients.values()):
        if not ws.closed:
            await ws.send_json(data)


async def status(text):
    global state
    state = text
    await broadcast({'type': 'status', 'text': text})


async def socket(request):
    origin = request.headers.get('Origin', '')
    if origin and origin != f'http://{request.host}':
        raise web.HTTPForbidden()
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    ident = uuid.uuid4().hex
    others = list(clients)
    clients[ident] = ws
    await ws.send_json({'type': 'welcome', 'id': ident, 'peers': others, 'history': history[-16:], 'status': state})
    await broadcast({'type': 'presence', 'count': len(clients)})
    try:
        async for message in ws:
            if message.type == web.WSMsgType.TEXT:
                data = json.loads(message.data)
                target = clients.get(data.get('to'))
                if data.get('type') == 'signal' and target:
                    await target.send_json({'type': 'signal', 'from': ident, 'signal': data['signal']})
    finally:
        clients.pop(ident, None)
        await broadcast({'type': 'left', 'id': ident})
        await broadcast({'type': 'presence', 'count': len(clients)})
    return ws


async def utterance(request):
    if request.headers.get('Content-Type', '').split(';')[0] not in ('audio/webm', 'audio/mp4', 'audio/wav', 'application/octet-stream'):
        raise web.HTTPUnsupportedMediaType()
    if turn_lock.locked():
        return web.json_response({'error': 'The agent is still responding. Try again when ready.'}, status=409)
    data = await request.read()
    if len(data) < 100:
        raise web.HTTPBadRequest(text='Audio was empty')
    async with turn_lock:
        try:
            await status('Transcribing')
            def transcribe():
                segments, _ = request.app['whisper'].transcribe(io.BytesIO(data), language='en', vad_filter=True)
                return ' '.join(segment.text.strip() for segment in segments)
            text = await asyncio.to_thread(transcribe)
            if not text:
                return web.json_response({'error': 'No speech detected. Try speaking closer to the microphone.'}, status=422)
            user = {'role': 'user', 'text': text}
            history.append(user)
            await broadcast({'type': 'message', **user})
            await status('Thinking')
            ident = uuid.uuid4().hex
            job = ROOT / 'jobs' / f'{ident}.request.json'
            response = ROOT / 'jobs' / f'{ident}.response.json'
            temp = job.with_suffix('.tmp')
            temp.write_text(json.dumps({'history': history[-16:]}))
            temp.replace(job)
            try:
                for _ in range(240):
                    if response.exists():
                        break
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeError('Local agent worker did not respond.')
                answer = json.loads(response.read_text())
                if 'error' in answer:
                    raise RuntimeError(answer['error'])
            finally:
                job.unlink(missing_ok=True)
                response.unlink(missing_ok=True)
            await status('Preparing voice')
            chunks = [chunk async for chunk in request.app['tts'].stream(answer['text'])]
            samples = np.frombuffer(b''.join(chunks), dtype=np.float32)
            buf = io.BytesIO()
            with wave.open(buf, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
            audio_cache[ident] = buf.getvalue()
            while len(audio_cache) > 10:
                audio_cache.pop(next(iter(audio_cache)))
            assistant = {'role': 'assistant', 'text': answer['text']}
            history.append(assistant)
            del history[:-32]
            await broadcast({'type': 'message', **assistant, 'audio': f'/audio/{ident}'})
            return web.json_response({'transcript': text, 'answer': answer['text'], 'audio': f'/audio/{ident}'})
        except Exception as exc:
            await broadcast({'type': 'error', 'text': str(exc)})
            return web.json_response({'error': str(exc)}, status=500)
        finally:
            await status('Ready')


async def health(request):
    hb = ROOT / 'jobs' / 'heartbeat'
    return web.json_response({'status': 'ready', 'participants': len(clients), 'agent_status': state,
                              'worker_connected': hb.exists() and time.time() - hb.stat().st_mtime < 130})


async def audio(request):
    data = audio_cache.get(request.match_info['id'])
    if not data:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type='audio/wav')


async def index(request):
    return web.FileResponse(ROOT / 'index.html')


async def startup(app):
    app['whisper'] = await asyncio.to_thread(WhisperModel, 'base', device='cpu', compute_type='int8', local_files_only=True)
    app['tts'] = await KokoroTTS().__aenter__()


async def cleanup(app):
    for ws in list(clients.values()):
        await ws.close()
    await app['tts'].__aexit__()


app = web.Application(client_max_size=8 * 1024 * 1024)
app.on_startup.append(startup)
app.on_cleanup.append(cleanup)
app.add_routes([web.get('/', index), web.get('/ws', socket), web.post('/utterance', utterance),
                web.get('/health', health), web.get('/audio/{id}', audio)])
web.run_app(app, host='0.0.0.0', port=8092)
