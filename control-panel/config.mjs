export function parseEnv(text = '') {
  const values = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const split = line.indexOf('=');
    if (split < 1) continue;
    const key = line.slice(0, split).trim();
    let value = line.slice(split + 1).trim();
    if (value.length > 1 && value[0] === value.at(-1) && ['"', "'"].includes(value[0])) {
      value = value.slice(1, -1);
    }
    values[key] = value;
  }
  return values;
}

export function publicSettings(values = {}) {
  return {
    platform: detectPlatform(values.MEETING_URL),
    meetingUrl: values.MEETING_URL || '',
    hasPasscode: Boolean(values.MEETING_PASSCODE),
    participantName: values.SMITLINE_PARTICIPANT_NAME || 'Smitline',
    meetingInstructions: values.SMITLINE_MEETING_INSTRUCTIONS || '',
  };
}

// The person Smitline acts for in meetings, from SMITLINE_OWNER_NAME. Empty when unset.
export function ownerName(values = {}) {
  const name = String(values.SMITLINE_OWNER_NAME || '').replace(/\s+/g, ' ').trim();
  return name && name.length <= 80 && !/[\u0000-\u001f]/.test(name) ? name : '';
}

export function validateSettings(input) {
  const errors = {};
  if (!detectPlatform(input.meetingUrl)) errors.meetingUrl = 'Use a supported HTTPS Zoom, Teams, or Google Meet meeting invite.';
  // The calls API brief needs a goal; the result says whether the meeting reached it.
  const objective = String(input.objective || '').trim();
  if (!objective) errors.objective = 'Say what this meeting should achieve.';
  else if (objective.length > 1000) errors.objective = 'Use at most 1,000 characters.';
  const participantName = String(input.participantName || '').trim();
  if (!participantName || participantName.length > 80 || /[\u0000-\u001f]/.test(participantName)) {
    errors.participantName = 'Use 1–80 printable characters.';
  }
  const meetingInstructions = String(input.meetingInstructions || '').trim();
  if (meetingInstructions.length > 2000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(meetingInstructions)) {
    errors.meetingInstructions = 'Use at most 2,000 printable characters.';
  }
  const camera = input.camera;
  if (camera && camera.avatarDataUri) {
    const uri = String(camera.avatarDataUri);
    if (!uri.startsWith('data:image/')) errors.camera = 'Use a PNG, JPEG, WebP, or SVG image.';
    else if (uri.length > 120000) errors.camera = 'Choose an image smaller than 80 KB.';
  }
  return { valid: Object.keys(errors).length === 0, errors };
}

function clean(value) {
  const result = String(value ?? '').replace(/[\r\n]+/g, ' ').trim();
  if (result.includes('\0')) throw new Error('Settings cannot contain null bytes.');
  return result;
}

export function serializeSettings(input, previous = {}) {
  const lines = [
    `MEETING_URL=${clean(input.meetingUrl)}`,
    `MEETING_PASSCODE=${clean(input.passcode || (input.keepPasscode ? previous.MEETING_PASSCODE : ''))}`,
    `SMITLINE_PARTICIPANT_NAME=${clean(input.participantName)}`,
    `SMITLINE_MEETING_INSTRUCTIONS=${clean(input.meetingInstructions)}`,
  ];
  return `${lines.join('\n')}\n`;
}

export function detectPlatform(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || (url.port && url.port !== '443')) return null;
    if (/^(?:[a-z0-9-]+\.)?zoom\.us$/i.test(url.hostname) && /^\/(?:j\/|wc\/(?:join\/)?)[0-9]+\/?$/.test(url.pathname)) return 'zoom';
    if (['teams.microsoft.com', 'teams.live.com'].includes(url.hostname) && /^\/(?:l\/meetup-join\/[^/]+|meet\/[^/]+)/.test(url.pathname)) return 'teams';
    if (url.hostname.toLowerCase() === 'meet.google.com' && /^\/[a-z]{3}-[a-z]{4}-[a-z]{3}\/?$/i.test(url.pathname)) return 'meet';
  } catch {}
  return null;
}
