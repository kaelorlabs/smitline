export const VISUAL_STATES = [
  'joining', 'listening', 'working', 'speaking', 'finalizing', 'needs_attention', 'ended',
];

const PRIVATE = [
  'transcript', 'prompt', 'filename', 'tool result', 'customer', 'password', 'api key', 'secret',
  'checking project', 'preparing handoff',
];

export function isPrivateVisualText(value) {
  const text = String(value || '').toLowerCase();
  return PRIVATE.some(token => text.includes(token));
}

export function drawPresencePreview(canvas, { visualState = 'listening', logo } = {}) {
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;
  const state = VISUAL_STATES.includes(visualState) ? visualState : 'listening';
  ctx.fillStyle = '#30323b';
  ctx.fillRect(0, 0, width, height);
  const cx = width / 2;
  const cy = height / 2;
  const mark = Math.min(width, height) * 0.28;
  if (state === 'ended') ctx.globalAlpha = 0.38;
  if (logo) {
    const size = mark * 2;
    ctx.drawImage(logo, cx - size / 2, cy - size / 2, size, size);
  } else {
    ctx.fillStyle = '#fff';
    const bar = mark * 0.18;
    ctx.beginPath();
    roundRect(ctx, cx - mark * 0.55, cy - mark * 0.25, bar, mark * 0.7, 3);
    ctx.fill();
    ctx.beginPath();
    roundRect(ctx, cx - bar / 2, cy - mark * 0.45, bar, mark * 1.1, 3);
    ctx.fill();
    ctx.beginPath();
    roundRect(ctx, cx + mark * 0.37, cy - mark * 0.25, bar, mark * 0.7, 3);
    ctx.fill();
  }
  ctx.globalAlpha = 1;
  ctx.strokeStyle = 'rgba(255,255,255,0.35)';
  ctx.lineWidth = 2;
  if (state === 'joining' || state === 'listening') {
    ctx.beginPath();
    ctx.arc(cx, cy, mark * 1.15, 0, Math.PI * 2);
    ctx.stroke();
  }
  if (state === 'working' || state === 'finalizing') {
    for (let i = 0; i < 3; i += 1) {
      ctx.beginPath();
      ctx.fillStyle = `rgba(255,255,255,${0.35 + i * 0.15})`;
      ctx.arc(cx - 16 + i * 16, cy + mark * 1.05, 3, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  if (state === 'speaking') {
    ctx.fillStyle = 'rgba(255,255,255,0.7)';
    for (let i = 0; i < 5; i += 1) {
      const h = 6 + (i % 3) * 8;
      ctx.fillRect(cx - 20 + i * 10, cy + mark * 0.95 - h, 4, h * 2);
    }
  }
  if (state === 'needs_attention') {
    ctx.fillStyle = '#c45c4a';
    ctx.beginPath();
    ctx.arc(cx + mark * 0.85, cy - mark * 0.85, 5, 0, Math.PI * 2);
    ctx.fill();
  }
  return state;
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.roundRect(x, y, w, h, r);
}

export async function readAvatarFile(file, { maxBytes = 80 * 1024 } = {}) {
  if (!file) return null;
  const allowed = ['image/png', 'image/jpeg', 'image/webp', 'image/svg+xml'];
  if (!allowed.includes(file.type) && !/\.(png|jpe?g|webp|svg)$/i.test(file.name || '')) {
    throw new Error('Use a PNG, JPEG, WebP, or SVG image.');
  }
  if (file.size > maxBytes) throw new Error('Choose an image smaller than 80 KB.');
  const dataUri = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('Could not read that image.'));
    reader.readAsDataURL(file);
  });
  if (isPrivateVisualText(dataUri.slice(0, 32))) throw new Error('That image cannot be used.');
  return dataUri;
}
