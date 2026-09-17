"""Rate-limited shared-content capture, host ingest, and GPT-Live observation append."""
import asyncio
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from schema_validation import reject_secrets, require_meeting_id
from screen_share import (
    DEGRADED_REASONS, VisualObservation, parse_screen_share_settings, public_status,
)
from startup_input import clip_tokens
from visual_analysis import VisualAnalysisUnavailable, VisualAnalysisProvider
from visual_hash import average_hash, change_score, sha256_hex


CONTROL_NAME = 'control.json'
INBOX = 'inbox'
OUTBOX = 'outbox'


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _iso(moment):
    if isinstance(moment, str):
        return moment
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class ScreenShareBus:
    def __init__(self, root, meeting_id):
        self.meeting_id = require_meeting_id(meeting_id)
        self.root = Path(root) / 'screen-share' / self.meeting_id
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        for name in (INBOX, OUTBOX):
            directory = self.root / name
            directory.mkdir(exist_ok=True)
            os.chmod(directory, 0o700)

    def control_path(self):
        return self.root / CONTROL_NAME

    def read_control(self):
        path = self.control_path()
        if not path.is_file():
            return {'paused': False}
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            reject_secrets(payload, 'screen-share control')
        except (OSError, ValueError):
            return {'paused': False}
        if not isinstance(payload, dict):
            return {'paused': False}
        return {'paused': bool(payload.get('paused'))}

    def write_control(self, *, paused):
        payload = {'paused': bool(paused)}
        reject_secrets(payload, 'screen-share control')
        tmp = self.control_path().with_suffix('.tmp')
        tmp.write_text(json.dumps(payload) + '\n', encoding='utf-8')
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.control_path())
        os.chmod(self.control_path(), 0o600)

    def write_inbox(self, png, meta):
        frame_id = meta.get('id') or ('frm-' + secrets.token_hex(6))
        reject_secrets(meta, 'screen-share frame meta')
        png_path = self.root / INBOX / (frame_id + '.png')
        meta_path = self.root / INBOX / (frame_id + '.json')
        png_path.write_bytes(png)
        os.chmod(png_path, 0o600)
        meta_path.write_text(json.dumps(meta) + '\n', encoding='utf-8')
        os.chmod(meta_path, 0o600)
        return frame_id

    def list_inbox(self):
        items = []
        directory = self.root / INBOX
        for path in sorted(directory.glob('*.png')):
            if path.is_symlink():
                continue
            meta_path = path.with_suffix('.json')
            meta = {}
            if meta_path.is_file() and not meta_path.is_symlink():
                try:
                    meta = json.loads(meta_path.read_text(encoding='utf-8'))
                    reject_secrets(meta, 'screen-share frame meta')
                except (OSError, ValueError):
                    meta = {}
            items.append((path, meta))
        return items

    def drop_inbox(self, png_path):
        path = Path(png_path)
        try:
            path.unlink()
        except OSError:
            pass
        try:
            path.with_suffix('.json').unlink()
        except OSError:
            pass

    def write_outbox(self, observation):
        payload = observation.to_dict() if hasattr(observation, 'to_dict') else dict(observation)
        reject_secrets(payload, 'screen-share observation')
        path = self.root / OUTBOX / (payload['id'] + '.json')
        path.write_text(json.dumps(payload) + '\n', encoding='utf-8')
        os.chmod(path, 0o600)
        return path

    def take_outbox(self):
        items = []
        directory = self.root / OUTBOX
        for path in sorted(directory.glob('*.json')):
            if path.is_symlink():
                continue
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
                reject_secrets(payload, 'screen-share observation')
                items.append(VisualObservation.from_dict(payload))
            except (OSError, ValueError):
                continue
            try:
                path.unlink()
            except OSError:
                pass
        return items


class ScreenShareCaptureLoop:
    def __init__(self, *, adapter, settings, bus, state, stop=None, now=None, sleep=None,
                 send=None):
        self.adapter = adapter
        self.settings = parse_screen_share_settings(settings)
        self.bus = bus
        self.state = state
        self.stop = stop
        self._now = now or _now
        self._sleep = sleep
        self.send = send
        self.last_hash = None
        self.last_digest = None
        self.inflight = False
        self._publish_status()

    def _stopped(self):
        if self.stop is None:
            return False
        if isinstance(self.stop, asyncio.Event):
            return self.stop.is_set()
        return bool(getattr(self.stop, 'is_set', lambda: False)())

    def _publish_status(self, **fields):
        current = dict(self.state.get('screenShare') or {})
        current.update({
            'enabled': self.settings['enabled'],
            'captureIntervalMs': self.settings['captureIntervalMs'],
            'retention': {
                'maxFrames': self.settings['maxFrames'],
                'maxBytes': self.settings['maxBytes'],
                'retentionSeconds': self.settings['retentionSeconds'],
            },
        })
        current.update(fields)
        self.state['screenShare'] = public_status(current)

    async def run(self):
        interval = self.settings['captureIntervalMs'] / 1000.0
        while not self._stopped():
            await self.tick()
            if self._sleep is not None:
                await self._sleep(interval)
            else:
                await asyncio.sleep(interval)

    async def tick(self):
        if not self.settings['enabled']:
            self._publish_status(
                available=False, active=False, capturing=False, paused=False,
                degradedReason='disabled')
            return self.state['screenShare']
        control = self.bus.read_control()
        if control.get('paused'):
            self._publish_status(
                available=self.state.get('screenShare', {}).get('available'),
                active=False, capturing=False, paused=True, degradedReason='paused')
            return self.state['screenShare']
        capability = getattr(getattr(self.adapter, 'capabilities', None), 'shared_content', False)
        if not capability:
            self._publish_status(
                available=False, active=False, capturing=False, paused=False,
                degradedReason='unavailable')
            return self.state['screenShare']
        try:
            png, located = await self.adapter.capture_shared_content()
        except Exception:
            png, located = None, None
        reason = getattr(located, 'reason', None) or 'unavailable'
        if png is None:
            mapped = reason if reason in DEGRADED_REASONS else 'unavailable'
            was_active = bool((self.state.get('screenShare') or {}).get('active'))
            self._publish_status(
                available=False, active=False, capturing=False, paused=False,
                degradedReason=mapped)
            return self.state['screenShare']
        if self.bus.list_inbox():
            self.inflight = True
            self._publish_status(
                available=True, active=True, capturing=True, paused=False,
                degradedReason='backpressure')
            return self.state['screenShare']
        digest = sha256_hex(png)
        hashed = average_hash(png)
        if self.last_digest == digest:
            self._publish_status(available=True, active=True, capturing=True, paused=False,
                                 degradedReason=None)
            return self.state['screenShare']
        if self.last_hash is not None and change_score(self.last_hash, hashed) < self.settings['minChange']:
            self._publish_status(available=True, active=True, capturing=True, paused=False,
                                 degradedReason=None)
            return self.state['screenShare']
        if len(png) > self.settings['maxBytes']:
            self._publish_status(
                available=True, active=True, capturing=False, paused=False,
                degradedReason='oversized')
            return self.state['screenShare']
        self.last_digest = digest
        self.last_hash = hashed
        frame_id = 'frm-' + secrets.token_hex(6)
        self.bus.write_inbox(png, {
            'id': frame_id,
            'meetingId': self.bus.meeting_id,
            'sha256': digest,
            'capturedAt': self._now() if not callable(self._now) else self._now(),
            'bytes': len(png),
        })
        self._publish_status(
            available=True, active=True, capturing=True, paused=False, degradedReason=None)
        return self.state['screenShare']

    async def drain_observations(self):
        observations = self.bus.take_outbox()
        for observation in observations:
            self.inflight = False
            self._publish_status(
                lastObservationAt=observation.timestamp, available=True, active=True,
                capturing=True, paused=False, degradedReason=None)
            await self._append(observation)
        return observations

    async def _append(self, observation):
        if self.send is None:
            return
        if self.state.get('floorState') == 'speaking':
            return
        text = clip_tokens('Shared content: ' + observation.summary, 240)
        if not text:
            return
        payload = {
            'type': 'session.thinking.append',
            'event_id': 'screen-' + observation.id,
            'content': text,
        }
        result = self.send(payload)
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            await result


class ScreenShareHost:
    def __init__(self, *, artifacts, analyzer=None, now=None):
        self.artifacts = artifacts
        self.analyzer = analyzer or VisualAnalysisProvider()
        self._now = now or _now
        self._inflight = set()
        self._last_hash = {}
        self._last_digest = {}

    def analyzer_available(self):
        return bool(getattr(self.analyzer, 'available', lambda: False)())

    async def ingest_png(self, meeting_id, png, *, settings, emit, store_observation,
                         captured_at=None, context='', cancel=None):
        settings = parse_screen_share_settings(settings)
        if meeting_id in self._inflight:
            return {'skipped': 'backpressure'}
        digest = sha256_hex(png)
        hashed = average_hash(png)
        if self._last_digest.get(meeting_id) == digest:
            return {'skipped': 'duplicate'}
        previous = self._last_hash.get(meeting_id)
        if previous is not None and change_score(previous, hashed) < settings['minChange']:
            return {'skipped': 'unchanged'}
        if len(png) > min(settings['maxBytes'], 2_000_000):
            emit('screen_share.failed', reason='oversized')
            return {'skipped': 'oversized'}
        self._last_digest[meeting_id] = digest
        self._last_hash[meeting_id] = hashed
        timestamp = captured_at or (self._now() if not callable(self._now) else self._now())
        meta = self.artifacts.put(
            meeting_id, kind='screenshot', body=bytes(png), media_type='image/png',
            description='Selected shared-content frame')
        emit('artifact.created', artifact={
            'id': meta['id'], 'kind': 'screenshot', 'path': meta['path'],
            'createdAt': timestamp, 'mediaType': 'image/png',
            'description': 'Selected shared-content frame',
        })
        emit('screen_share.frame_selected', artifact={
            'id': meta['id'], 'kind': 'screenshot', 'path': meta['path'],
            'createdAt': timestamp, 'mediaType': 'image/png',
            'description': 'Selected shared-content frame',
        })
        if not self.analyzer_available():
            emit('screen_share.failed', reason='analyzer_unavailable')
            return {'artifact': meta, 'observation': None, 'reason': 'analyzer_unavailable'}
        self._inflight.add(meeting_id)
        try:
            observation = await self.analyzer.analyze(
                png, meeting_id=meeting_id, frame_artifact_id=meta['id'], context=context,
                timestamp=timestamp, observation_id='obs-' + secrets.token_hex(5), cancel=cancel)
            payload = observation.to_dict()
            stored = self.artifacts.put(
                meeting_id, kind='observation', body=payload,
                description=payload.get('summary'))
            emit('artifact.created', artifact={
                'id': stored['id'], 'kind': 'observation', 'path': stored['path'],
                'createdAt': timestamp, 'mediaType': 'application/json',
                'description': payload.get('summary'),
            })
            emit('screen_share.observation', observation=payload)
            store_observation(payload)
            return {'artifact': meta, 'observation': payload, 'observationArtifact': stored}
        except VisualAnalysisUnavailable:
            emit('screen_share.failed', reason='analyzer_unavailable')
            return {'artifact': meta, 'observation': None, 'reason': 'analyzer_unavailable'}
        finally:
            self._inflight.discard(meeting_id)
