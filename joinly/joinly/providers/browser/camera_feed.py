"""Virtual camera feed via getUserMedia and RTCPeerConnection overrides.

Overrides ``navigator.mediaDevices.getUserMedia`` so that video
requests return a canvas-backed ``MediaStreamTrack`` instead of a
real camera, while audio requests pass through to the real device.

Also patches ``RTCPeerConnection.prototype.addTrack`` to swap any
video track with the canvas track, ensuring WebRTC negotiation
always uses our virtual feed regardless of platform behavior.

Patches ``enumerateDevices`` to include a virtual camera so
platforms that check for camera hardware still show a video toggle.

The camera canvas renders the Colleague AI mark (or a validated
local avatar) directly. Audio amplitude drives an equalizer that
reacts to speech in real time. Presence uses ``set_visual_state``.
"""

import asyncio
import json
from collections.abc import Callable
from urllib.parse import quote

import numpy as np
from playwright.async_api import Page

from joinly.core import AudioWriter

_CAM_WIDTH = 1280
_CAM_HEIGHT = 720
_BAND_THROTTLE_S = 0.05
_NUM_BANDS = 7

_COLLEAGUE_MARK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">'
    '<rect width="128" height="128" rx="28" fill="#30323b"/>'
    '<rect x="34" y="44" width="12" height="40" rx="4" fill="#fff"/>'
    '<rect x="58" y="28" width="12" height="72" rx="4" fill="#fff"/>'
    '<rect x="82" y="44" width="12" height="40" rx="4" fill="#fff"/>'
    "</svg>"
)
_LOGO_SVG = "data:image/svg+xml," + quote(_COLLEAGUE_MARK_SVG, safe="")
_VISUAL_STATES = (
    "joining",
    "listening",
    "working",
    "speaking",
    "finalizing",
    "needs_attention",
    "ended",
)
_JOINLY_EFFECT_TO_VISUAL = {
    "typing": "working",
    "reading": "working",
    "interrupted": "needs_attention",
    "sharing": "working",
    "thinking": "joining",
    "busy": "working",
    "speaking": "speaking",
    "listening": "listening",
}

# ---------------------------------------------------------------------------
# Status effect functions — each draws a small animation below the logo.
# Kept as separate JS function bodies for readability; interpolated into
# the main render loop via _CAMERA_OVERRIDE_TEMPLATE.
# ---------------------------------------------------------------------------

# Speaking: frequency spectrum bars driven by real FFT band levels
_FX_SPEAKING = """\
function fxSpeaking(ctx, cx, y, bands, alpha) {
    const N = bands.length;
    const gap = H * 0.012, barW = H * 0.006;
    const ox = cx - (N - 1) * gap / 2;
    ctx.lineCap = 'round';
    for (let i = 0; i < N; i++) {
        const v = Math.min(bands[i] * 6, 1);
        if (v < 0.01) continue;
        const h = H * 0.004 + H * 0.028 * v;
        ctx.globalAlpha = (0.25 + v * 0.4) * alpha;
        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.roundRect(ox + i * gap - barW / 2, y - h,
            barW, h * 2, barW / 2);
        ctx.fill();
    }
}"""

# Typing: three dots with crisp sequential bounce
_FX_TYPING = """\
function fxTyping(ctx, cx, y, t, alpha) {
    const N = 3, gap = H * 0.024, r = H * 0.008;
    const ox = cx - (N - 1) * gap / 2;
    for (let i = 0; i < N; i++) {
        const phase = (t * 4 - i * 0.9) % (Math.PI * 2);
        const raw = Math.sin(phase);
        const bounce = raw > 0 ? Math.pow(raw, 0.8) : 0;
        const dy = bounce * H * 0.018;
        ctx.globalAlpha = (0.3 + bounce * 0.5) * alpha;
        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.arc(ox + i * gap, y - dy, r, 0, Math.PI * 2);
        ctx.fill();
    }
}"""

# Share screen: rounded rectangles expanding from logo size outward
# (drawn behind the logo on the background)
_FX_SHARE = """\
function fxShare(ctx, cx, cy, logoW, logoH, t, alpha) {
    const endW = logoW * 2, endH = logoH * 1.8;
    for (let i = 0; i < 3; i++) {
        const p = ((t * 0.35 + i / 3) % 1);
        const ease = 1 - Math.pow(1 - p, 2.5);
        const w = logoW * 0.5 + (endW - logoW * 0.5) * ease;
        const h = logoH * 0.5 + (endH - logoH * 0.5) * ease;
        const fade = (1 - p) * alpha * 0.5;
        if (fade < 0.01) continue;
        ctx.globalAlpha = fade;
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 2 - ease;
        ctx.beginPath();
        ctx.roundRect(cx - w / 2, cy - h / 2, w, h,
            6 + ease * 4);
        ctx.stroke();
    }
}"""

# Interrupted: dots scatter outward from center and fade
_FX_INTERRUPTED = """\
function fxInterrupted(ctx, cx, y, t, alpha) {
    const N = 5, r = H * 0.007;
    for (let i = 0; i < N; i++) {
        const angle = (i / N) * Math.PI * 2 + t * 1.5;
        const p = (t * 2.5 + i / N) % 1;
        const spread = H * 0.01 + p * H * 0.04;
        const dx = Math.cos(angle) * spread;
        const dy = Math.sin(angle) * spread * 0.5;
        const fade = (1 - p) * alpha;
        if (fade < 0.01) continue;
        ctx.globalAlpha = fade;
        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.arc(cx + dx, y + dy, r * (1 - p * 0.5), 0, Math.PI * 2);
        ctx.fill();
    }
}"""

# Thinking: rotating arc segments with soft glow around the logo
_FX_THINKING = """\
function fxThinking(ctx, cx, cy, logoW, logoH, t, alpha) {
    const r = Math.max(logoW, logoH) * 0.62;
    const pulse = 0.5 + 0.5 * Math.sin(t * 2.0);

    // Outer glow ring — subtle breathing
    ctx.globalAlpha = (0.06 + pulse * 0.06) * alpha;
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = H * 0.012;
    ctx.beginPath();
    ctx.arc(cx, cy, r + H * 0.006, 0, Math.PI * 2);
    ctx.stroke();

    // Rotating arc segments — 3 arcs at different speeds
    for (let i = 0; i < 3; i++) {
        const speed = 1.2 + i * 0.4;
        const dir = i % 2 ? -1 : 1;
        const base = t * speed * dir + i * Math.PI * 0.667;
        const len = Math.PI * (0.3 + 0.15 * Math.sin(t * 1.5 + i));
        ctx.globalAlpha = (0.2 + (1 - i * 0.25) * 0.25) * alpha;
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 2 - i * 0.4;
        ctx.beginPath();
        ctx.arc(cx, cy, r + H * (0.002 + i * 0.006),
            base, base + len);
        ctx.stroke();
    }

    // Orbiting dots — 2 dots at different orbits
    for (let i = 0; i < 2; i++) {
        const a = t * (1.6 + i * 0.5) + i * Math.PI;
        const orbitR = r + H * (0.01 + i * 0.008);
        const dx = Math.cos(a) * orbitR;
        const dy = Math.sin(a) * orbitR;
        const dotPulse = 0.5 + 0.5 * Math.sin(t * 3 + i * 2);
        ctx.globalAlpha = (0.35 + dotPulse * 0.4) * alpha;
        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.arc(cx + dx, cy + dy,
            H * (0.005 + dotPulse * 0.002), 0, Math.PI * 2);
        ctx.fill();
    }
}"""

# Busy: radar sweep with trailing particles
_FX_BUSY = """\
function fxBusy(ctx, cx, y, t, alpha) {
    const w = H * 0.08;
    const speed = 0.6;
    const p = (t * speed) % 2;
    const dir = p <= 1 ? 1 : -1;
    const norm = p <= 1 ? p : p - 1;
    const ease = norm < 0.5
        ? 2 * norm * norm
        : 1 - 2 * (1 - norm) * (1 - norm);
    const x = dir > 0
        ? cx - w + ease * w * 2
        : cx + w - ease * w * 2;

    // Glow line
    const grad = ctx.createLinearGradient(
        x - H * 0.015, y, x + H * 0.015, y);
    grad.addColorStop(0, 'rgba(255,255,255,0)');
    grad.addColorStop(0.5, 'rgba(255,255,255,1)');
    grad.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.globalAlpha = 0.5 * alpha;
    ctx.strokeStyle = grad;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x, y - H * 0.018);
    ctx.lineTo(x, y + H * 0.018);
    ctx.stroke();

    // Centre dot
    ctx.globalAlpha = 0.6 * alpha;
    ctx.fillStyle = '#ffffff';
    ctx.beginPath();
    ctx.arc(x, y, H * 0.004, 0, Math.PI * 2);
    ctx.fill();

    // Trail particles
    for (let i = 1; i <= 5; i++) {
        const d = i * 0.04;
        const tn = p <= 1 ? Math.max(0, p - d) : Math.max(0, (p - 1) - d);
        const te = tn < 0.5
            ? 2 * tn * tn
            : 1 - 2 * (1 - tn) * (1 - tn);
        const tx = dir > 0
            ? cx - w + te * w * 2
            : cx + w - te * w * 2;
        const fade = (1 - i / 6);
        ctx.globalAlpha = fade * 0.35 * alpha;
        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.arc(tx, y, H * (0.004 - i * 0.0004), 0, Math.PI * 2);
        ctx.fill();
    }

    // Static endpoint markers
    ctx.globalAlpha = 0.12 * alpha;
    ctx.fillStyle = '#ffffff';
    for (const ex of [cx - w, cx + w]) {
        ctx.beginPath();
        ctx.arc(ex, y, H * 0.003, 0, Math.PI * 2);
        ctx.fill();
    }
}"""

# Reading: dot sweeping back and forth with a trail
_FX_READING = """\
function fxReading(ctx, cx, y, t, alpha) {
    const w = H * 0.035;
    const r = H * 0.008;
    const p = (t * 1.8 % 2);
    for (let i = 0; i < 3; i++) {
        const d = i * 0.1;
        const tp = p <= 1
            ? cx - w + Math.max(0, p - d) * w * 2
            : cx + w - Math.max(0, (p - 1) - d) * w * 2;
        ctx.globalAlpha = (0.15 + (1 - i / 3) * 0.35) * alpha;
        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.arc(tp, y, r * (1 - i * 0.12), 0, Math.PI * 2);
        ctx.fill();
    }
}"""

# Listening: restrained breathing ring around the mark
_FX_LISTENING = """\
function fxListening(ctx, cx, cy, logoW, logoH, t, alpha) {
    const r = Math.max(logoW, logoH) * 0.62;
    const pulse = 0.5 + 0.5 * Math.sin(t * 1.15);
    ctx.globalAlpha = (0.05 + pulse * 0.08) * alpha;
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = H * 0.008;
    ctx.beginPath();
    ctx.arc(cx, cy, r + H * 0.004 * pulse, 0, Math.PI * 2);
    ctx.stroke();
}"""

# The init script only patches API methods (no DOM access).
# All canvas/Image/rAF work is deferred to _initCanvas which
# runs on the first getUserMedia call (DOM is guaranteed ready).
_CAMERA_OVERRIDE_TEMPLATE = """\
(() => {{
    const W = {w}, H = {h};
    const LOGO_SRC = __LOGO_SRC__;
    const VISUAL_STATES = new Set(__VISUAL_STATES__);

    if (window.__camOrigGUM) return;

    let camTrack = null;
    window.__visualState = 'joining';

    {fx_speaking}
    {fx_typing}
    {fx_share}
    {fx_reading}
    {fx_interrupted}
    {fx_thinking}
    {fx_busy}
    {fx_listening}

    const FX = {{
        typing: fxTyping,
        reading: fxReading,
        interrupted: fxInterrupted,
        busy: fxBusy,
    }};

    const FX_BG = {{
        thinking: fxThinking,
        sharing: fxShare,
        listening: fxListening,
    }};

    const VISUAL_TO_STATUS = {{
        joining: 'thinking',
        listening: 'listening',
        working: 'typing',
        speaking: '',
        finalizing: 'reading',
        needs_attention: 'interrupted',
        ended: '',
    }};

    function _initCanvas() {{
        if (camTrack) return camTrack;

        const c = document.createElement('canvas');
        c.width = W; c.height = H;
        const ctx = c.getContext('2d');

        let logoImg = null;
        const bands = new Float32Array({n_bands});
        const smoothBands = new Float32Array({n_bands});
        let t = 0;
        let status = '';
        let statusAlpha = 0;
        let statusT = 0;
        let statusSetAt = 0;
        const STATUS_MIN_MS = 1500;

        const img = new Image();
        img.onload = () => {{ logoImg = img; }};
        img.src = LOGO_SRC;

        window.__setBands = (b) => {{
            for (let i = 0; i < bands.length; i++)
                bands[i] = b[i] || 0;
        }};
        window.__setStatus = (s) => {{
            if (s) {{
                status = s;
                statusT = 0;
                statusSetAt = performance.now();
            }} else {{
                const elapsed = performance.now() - statusSetAt;
                if (elapsed >= STATUS_MIN_MS) {{
                    status = '';
                }} else {{
                    setTimeout(() => {{ status = ''; }},
                        STATUS_MIN_MS - elapsed);
                }}
            }}
        }};
        window.__setVisualState = (name) => {{
            if (!VISUAL_STATES.has(name)) return;
            window.__visualState = name;
            const mapped = VISUAL_TO_STATUS[name] || '';
            window.__setStatus(mapped);
        }};

        function draw() {{
            t += 0.02;

            ctx.fillStyle = '#30323b';
            ctx.fillRect(0, 0, W, H);

            if (logoImg) {{
                const logoH = H * 0.35;
                const logoW = logoH;
                const cx = W / 2;
                const cy = H / 2;
                const logoBot = cy + logoH / 2;
                const ended = window.__visualState === 'ended';
                const markAlpha = ended ? 0.38 : 1;

                // Action status (compute alpha for all effects)
                const wantAlpha = status ? 1 : 0;
                statusAlpha += (wantAlpha - statusAlpha) * 0.12;
                if (status) statusT += 0.02;

                // Background effects — behind the logo
                if (statusAlpha > 0.02) {{
                    const bgFn = FX_BG[status];
                    if (bgFn) {{
                        ctx.save();
                        bgFn(ctx, cx, cy, logoW, logoH,
                            statusT, statusAlpha);
                        ctx.restore();
                    }}
                }}

                // Speaking — behind the logo
                let anyBand = false;
                for (let i = 0; i < bands.length; i++) {{
                    smoothBands[i] += (bands[i] - smoothBands[i]) * 0.3;
                    if (smoothBands[i] < 0.005) smoothBands[i] = 0;
                    if (smoothBands[i] > 0) anyBand = true;
                    bands[i] *= 0.75;
                }}
                if (anyBand) {{
                    ctx.save();
                    fxSpeaking(ctx, cx, logoBot + H * 0.04,
                        smoothBands, 1);
                    ctx.restore();
                }}

                ctx.globalAlpha = markAlpha;
                ctx.drawImage(
                    logoImg,
                    cx - logoW / 2, cy - logoH / 2,
                    logoW, logoH
                );
                ctx.globalAlpha = 1;

                // Foreground effects — below the logo
                if (statusAlpha > 0.02) {{
                    const fn = FX[status];
                    if (fn) {{
                        ctx.save();
                        fn(ctx, cx, logoBot + H * 0.08,
                            statusT, statusAlpha);
                        ctx.restore();
                    }}
                }}
            }}
            requestAnimationFrame(draw);
        }}
        requestAnimationFrame(draw);

        camTrack = c.captureStream(30).getVideoTracks()[0];
        return camTrack;
    }}

    const md = navigator.mediaDevices;

    window.__camOrigGUM = md.getUserMedia.bind(md);
    md.getUserMedia = async (constraints) => {{
        const wantsVideo = !!constraints?.video;
        const wantsAudio = !!constraints?.audio;

        if (wantsAudio) {{
            const real = await window.__camOrigGUM({{
                audio: constraints.audio,
                video: false,
            }});
            if (wantsVideo) real.addTrack(_initCanvas().clone());
            return real;
        }}
        if (wantsVideo) {{
            return new MediaStream([_initCanvas().clone()]);
        }}
        return window.__camOrigGUM(constraints);
    }};

    const origAddTrack = RTCPeerConnection.prototype.addTrack;
    RTCPeerConnection.prototype.addTrack = function(track, ...streams) {{
        if (track.kind === 'video') {{
            return origAddTrack.call(
                this, _initCanvas().clone(), ...streams
            );
        }}
        return origAddTrack.call(this, track, ...streams);
    }};

    const origEnum = md.enumerateDevices.bind(md);
    md.enumerateDevices = async () => {{
        const devices = await origEnum();
        const hasCamera = devices.some(d => d.kind === 'videoinput');
        if (!hasCamera) {{
            devices.push({{
                deviceId: 'virtual-camera',
                groupId: 'virtual',
                kind: 'videoinput',
                label: 'Colleague AI',
                toJSON() {{ return this; }},
            }});
        }}
        return devices;
    }};
}})();"""


def build_camera_override_script(*, logo_src: str | None = None) -> str:
    """Build the getUserMedia override script without interpolating untrusted braces."""
    script = _CAMERA_OVERRIDE_TEMPLATE.format(
        w=_CAM_WIDTH,
        h=_CAM_HEIGHT,
        n_bands=_NUM_BANDS,
        fx_speaking=_FX_SPEAKING,
        fx_typing=_FX_TYPING,
        fx_share=_FX_SHARE,
        fx_reading=_FX_READING,
        fx_interrupted=_FX_INTERRUPTED,
        fx_thinking=_FX_THINKING,
        fx_busy=_FX_BUSY,
        fx_listening=_FX_LISTENING,
    )
    return (
        script.replace("__LOGO_SRC__", json.dumps(logo_src or _LOGO_SVG))
        .replace("__VISUAL_STATES__", json.dumps(list(_VISUAL_STATES)))
    )


class CameraFeed:
    """Manages the virtual camera canvas and amplitude-driven glow.

    Draws the Colleague AI mark (or a validated avatar data URI) on the
    camera canvas. Wraps an ``AudioWriter`` to extract amplitude and
    push it to the canvas render loop. Presence states use
    ``set_visual_state``.
    """

    def __init__(
        self,
        writer: AudioWriter,
        *,
        enabled: bool = True,
        logo_src: str | None = None,
    ) -> None:
        """Initialize with the underlying audio writer."""
        self.enabled = enabled
        self._logo_src = logo_src or _LOGO_SVG
        self._meeting_page: Page | None = None
        self._last_band_time: float = 0
        if enabled:
            self.audio_writer = _AmplitudeAudioWriter(writer, self._on_bands)
        else:
            self.audio_writer = writer

    async def install(self, meeting_page: Page) -> None:
        """Install the getUserMedia override on the meeting page."""
        self._meeting_page = meeting_page
        if not self.enabled:
            return
        await meeting_page.add_init_script(
            build_camera_override_script(logo_src=self._logo_src)
        )

    def set_visual_state(self, name: str | None) -> None:
        """Set a Colleague presence state, or listening when cleared."""
        if not self.enabled:
            return
        visual = name if name in _VISUAL_STATES else "listening"
        page = self._meeting_page
        if page and not page.is_closed():
            payload = json.dumps(visual)
            task = asyncio.ensure_future(
                page.evaluate(f"window.__setVisualState?.({payload})")
            )
            task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )

    def set_effect(self, name: str | None) -> None:
        """Map a Joinly action animation onto Colleague visual presence."""
        if not name:
            self.set_visual_state("listening")
            return
        self.set_visual_state(_JOINLY_EFFECT_TO_VISUAL.get(name, "working"))

    async def stop(self) -> None:
        """Clean up references."""
        self._meeting_page = None

    def _on_bands(self, bands: list[float]) -> None:
        now = asyncio.get_event_loop().time()
        if now - self._last_band_time < _BAND_THROTTLE_S:
            return
        self._last_band_time = now
        page = self._meeting_page
        if page and not page.is_closed():
            arr = "[" + ",".join(f"{v:.4f}" for v in bands) + "]"
            task = asyncio.ensure_future(page.evaluate(f"window.__setBands?.({arr})"))
            task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )


class _AmplitudeAudioWriter(AudioWriter):
    """Audio writer that computes frequency bands per chunk."""

    def __init__(
        self,
        writer: AudioWriter,
        on_bands: Callable[[list[float]], None],
    ) -> None:
        self._writer = writer
        self._on_bands = on_bands
        self.audio_format = writer.audio_format
        self.chunk_size = writer.chunk_size

    async def write(self, data: bytes) -> None:
        """Write audio and forward frequency band levels."""
        n_samples = len(data) // 2
        if n_samples < _NUM_BANDS:
            await self._writer.write(data)
            return
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        fft = np.abs(np.fft.rfft(samples))
        # Normalize: FFT magnitudes scale with n_samples and sample range
        fft /= n_samples * 32768
        # Log-spaced band edges so lower frequencies get finer resolution
        n_bins = len(fft)
        edges = np.logspace(np.log10(1), np.log10(n_bins), _NUM_BANDS + 1).astype(int)
        edges = np.clip(edges, 0, n_bins)
        bands = [
            float(np.mean(fft[edges[i] : max(edges[i + 1], edges[i] + 1)]))
            for i in range(_NUM_BANDS)
        ]
        self._on_bands(bands)
        await self._writer.write(data)

    async def drain(self) -> None:
        """Wait for the wrapped output device to finish queued playback."""
        drain = getattr(self._writer, "drain", None)
        if drain is not None:
            await drain()

    def discard_pending(self) -> None:
        """Discard queued playback in the wrapped output device."""
        discard = getattr(self._writer, "discard_pending", None)
        if discard is not None:
            discard()
