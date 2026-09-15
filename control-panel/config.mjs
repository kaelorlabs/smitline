import fs from 'node:fs';
import path from 'node:path';

export const MODELS = ['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.5'];

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

function bool(value, fallback) {
  if (value === undefined || value === '') return fallback;
  return ['1', 'true', 'yes', 'on'].includes(String(value).toLowerCase());
}

export function publicSettings(values = {}) {
  return {
    platform: detectPlatform(values.MEETING_URL),
    meetingUrl: values.MEETING_URL || '',
    hasPasscode: Boolean(values.MEETING_PASSCODE),
    participantName: values.COLLEAGUE_PARTICIPANT_NAME || 'Colleague AI',
    model: values.COLLEAGUE_CODEX_MODEL || 'gpt-5.6-terra',
    workspace: values.COLLEAGUE_WORKSPACE || '',
    meetingInstructions: values.COLLEAGUE_MEETING_INSTRUCTIONS || '',
    tools: {
      webSearch: bool(values.COLLEAGUE_ENABLE_WEB_SEARCH, true),
      codex: bool(values.COLLEAGUE_ENABLE_CODEX, true),
      charts: bool(values.COLLEAGUE_ENABLE_CHARTS, false),
    },
  };
}

export function validateSettings(input, { isDirectory = value => fs.existsSync(value) && fs.statSync(value).isDirectory() } = {}) {
  const errors = {};
  if (!detectPlatform(input.meetingUrl)) errors.meetingUrl = 'Use a supported HTTPS Zoom or Teams meeting invite.';
  const participantName = String(input.participantName || '').trim();
  if (!participantName || participantName.length > 80 || /[\u0000-\u001f]/.test(participantName)) {
    errors.participantName = 'Use 1–80 printable characters.';
  }
  if (!MODELS.includes(input.model)) errors.model = 'Choose a supported Codex model.';
  const tools = input.tools || {};
  if (tools.charts && !tools.codex) errors.charts = 'Charts require the Codex tool.';
  const workspace = String(input.workspace || '').trim();
  if (tools.codex && workspace && (!path.isAbsolute(workspace) || !isDirectory(workspace))) {
    errors.workspace = 'Choose an existing absolute directory.';
  }
  const meetingInstructions = String(input.meetingInstructions || '').trim();
  if (meetingInstructions.length > 2000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(meetingInstructions)) {
    errors.meetingInstructions = 'Use at most 2,000 printable characters.';
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
    `COLLEAGUE_PARTICIPANT_NAME=${clean(input.participantName)}`,
    `COLLEAGUE_CODEX_MODEL=${clean(input.model)}`,
    `COLLEAGUE_ENABLE_WEB_SEARCH=${input.tools?.webSearch ? '1' : '0'}`,
    `COLLEAGUE_ENABLE_CODEX=${input.tools?.codex ? '1' : '0'}`,
    `COLLEAGUE_ENABLE_CHARTS=${input.tools?.charts ? '1' : '0'}`,
    `COLLEAGUE_WORKSPACE=${clean(input.workspace)}`,
    `COLLEAGUE_MEETING_INSTRUCTIONS=${clean(input.meetingInstructions)}`,
  ];
  return `${lines.join('\n')}\n`;
}

export function detectPlatform(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || (url.port && url.port !== '443')) return null;
    if (/^(?:[a-z0-9-]+\.)?zoom\.us$/i.test(url.hostname) && /^\/(?:j\/|wc\/(?:join\/)?)[0-9]+\/?$/.test(url.pathname)) return 'zoom';
    if (['teams.microsoft.com', 'teams.live.com'].includes(url.hostname) && /^\/(?:l\/meetup-join\/[^/]+|meet\/[^/]+)/.test(url.pathname)) return 'teams';
  } catch {}
  return null;
}
