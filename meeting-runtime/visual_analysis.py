"""Provider-neutral visual analysis. Codex is used only when the CLI documents local-image input."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from schema_validation import reject_secrets
from screen_share import VisualObservation, observation_payload
from startup_input import clip_tokens


IMAGE_FLAG_TOKENS = ('--image', '--images', '--input-image')
MAX_CONTEXT = 400


class VisualAnalysisUnavailable(RuntimeError):
    pass


class VisualAnalysisProvider:
    def available(self):
        return False

    async def analyze(self, png, *, meeting_id, frame_artifact_id, context='', timestamp=None,
                      observation_id=None, cancel=None):
        raise VisualAnalysisUnavailable('visual analysis is unavailable')


def detect_codex_image_flag(help_text):
    text = str(help_text or '')
    lowered = text.lower()
    for token in IMAGE_FLAG_TOKENS:
        if token in lowered:
            return token
    return None


def collect_codex_help(command=None, runner=None):
    binary = command or shutil.which('codex') or os.environ.get('CODEX_BIN')
    if not binary:
        return ''
    run = runner or subprocess.run
    chunks = []
    for args in ([binary, '--help'], [binary, 'exec', '--help']):
        try:
            result = run(args, capture_output=True, text=True, timeout=4, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        chunks.append((result.stdout or '') + '\n' + (result.stderr or ''))
    return '\n'.join(chunks)


class UnavailableVisualAnalysisProvider(VisualAnalysisProvider):
    def available(self):
        return False


class StaticVisualAnalysisProvider(VisualAnalysisProvider):
    def __init__(self, observation=None, *, available=True):
        self._observation = observation or {}
        self._available = available
        self.calls = []

    def available(self):
        return bool(self._available)

    async def analyze(self, png, *, meeting_id, frame_artifact_id, context='', timestamp=None,
                      observation_id=None, cancel=None):
        if cancel is not None and getattr(cancel, 'is_set', lambda: False)():
            raise VisualAnalysisUnavailable('cancelled')
        if not self._available:
            raise VisualAnalysisUnavailable('visual analysis is unavailable')
        self.calls.append(frame_artifact_id)
        payload = dict(self._observation)
        payload.setdefault('id', observation_id or 'obs-1')
        payload.setdefault('meetingId', meeting_id)
        payload.setdefault('timestamp', timestamp or '2026-09-17T00:00:00Z')
        payload.setdefault('summary', 'Shared content changed')
        payload.setdefault('frameArtifactId', frame_artifact_id)
        payload.setdefault('confidence', 0.7)
        return VisualObservation.from_dict(payload)


class CodexVisualAnalysisProvider(VisualAnalysisProvider):
    def __init__(self, *, command=None, runner=None, help_text=None, workspace=None):
        self.command = command or shutil.which('codex') or os.environ.get('CODEX_BIN')
        self.runner = runner
        self.workspace = workspace
        detected = detect_codex_image_flag(
            help_text if help_text is not None else collect_codex_help(self.command, self.runner))
        self.image_flag = detected

    def available(self):
        return bool(self.command and self.image_flag)

    async def analyze(self, png, *, meeting_id, frame_artifact_id, context='', timestamp=None,
                      observation_id=None, cancel=None):
        if not self.available():
            raise VisualAnalysisUnavailable('Codex CLI has no documented local-image input')
        if cancel is not None and getattr(cancel, 'is_set', lambda: False)():
            raise VisualAnalysisUnavailable('cancelled')
        if not isinstance(png, (bytes, bytearray)) or png[:8] != b'\x89PNG\r\n\x1a\n':
            raise ValueError('analyzer requires a PNG frame')
        prompt = clip_tokens(
            'Summarize the shared meeting content in one short factual sentence. '
            'Return JSON {"summary": str, "confidence": number, "visibleText": [str]}. '
            'Do not include secrets, paths, credentials, or hidden reasoning. '
            'Meeting question: ' + clip_tokens(context or 'What is currently shared?', MAX_CONTEXT),
            700,
        )
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / 'frame.png'
            frame.write_bytes(png)
            os.chmod(frame, 0o600)
            args = [
                self.command, 'exec', self.image_flag, str(frame),
                '--sandbox', 'read-only', '--skip-git-repo-check',
            ]
            if self.workspace:
                args.extend(['-C', str(self.workspace)])
            args.append(prompt)
            run = self.runner or subprocess.run
            try:
                result = run(args, capture_output=True, text=True, timeout=45, check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise VisualAnalysisUnavailable('Codex image analysis failed') from error
        text = (result.stdout or '') + '\n' + (result.stderr or '')
        parsed = _extract_observation_json(text)
        if parsed is None:
            raise VisualAnalysisUnavailable('Codex image analysis returned no observation')
        reject_secrets(parsed, 'codex observation')
        return VisualObservation.from_dict(observation_payload(
            meeting_id,
            observation_id=observation_id or 'obs-codex',
            timestamp=timestamp or '2026-09-17T00:00:00Z',
            summary=str(parsed.get('summary') or 'Shared content is visible'),
            frame_artifact_id=frame_artifact_id,
            confidence=parsed.get('confidence', 0.5),
            visible_text=parsed.get('visibleText') or (),
        ))


def _extract_observation_json(text):
    import json
    blob = str(text or '')
    start = blob.find('{')
    end = blob.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(blob[start:end + 1])
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload
