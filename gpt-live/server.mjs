import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const port = Number(process.env.PORT || 8093);
const key = process.env.OPENAI_API_KEY;
const origin = `http://127.0.0.1:${port}`;
const allowedHosts = new Set([`127.0.0.1:${port}`, `localhost:${port}`]);
export const sessionConfig = {
  model: 'gpt-live-1',
  store: false,
  instructions: `You are Colleague AI, a friendly voice teammate. Speak naturally and keep routine replies short.
Backchannel policy: Acknowledge moderately without competing with the user's speech.
Interruption policy: Stop your answer and listen when interrupted.
Delegation policy:
Backend tools: A reasoning assistant can help with explanations and calculations. It has no external tools.
Delegate to the backend when: A request requires careful reasoning or calculation.
Do not delegate to the backend when: Greeting, clarifying, or replying from the current conversation.
Do not claim to browse, modify files, or join other meetings.`,
  delegation: { type: 'responses', responses: {
    model: 'gpt-5.6-terra',
    instructions: 'Help with the spoken question. Return a concise answer suitable to read aloud. No tools are available. Do not claim to have checked current facts or performed external actions.',
  } },
};

function reply(res, status, data, type = 'application/json') {
  res.writeHead(status, { 'Content-Type': type, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
  res.end(type === 'application/json' ? JSON.stringify(data) : data);
}

export function createServer(apiKey = key, upstream = fetch) {
  return http.createServer(async (req, res) => {
    try {
      if (!allowedHosts.has(req.headers.host)) return reply(res, 403, { error: 'Unexpected host' });
      if (req.method === 'GET' && req.url === '/health') return reply(res, 200, { status: 'ready', model: sessionConfig.model, key_configured: Boolean(apiKey) });
      const files = { '/': ['index.html', 'text/html; charset=utf-8'], '/client.js': ['client.js', 'text/javascript; charset=utf-8'] };
      if (req.method === 'GET' && files[req.url]) {
        const [file, type] = files[req.url];
        return reply(res, 200, await readFile(new URL(file, import.meta.url)), type);
      }
      if (req.method !== 'POST' || req.url !== '/api/session') return reply(res, 404, { error: 'Not found' });
      if (req.headers.origin !== `http://${req.headers.host}`) return reply(res, 403, { error: 'Unexpected origin' });
      if (req.headers['content-type']?.split(';')[0] !== 'application/json') return reply(res, 415, { error: 'JSON required' });
      if (!apiKey) return reply(res, 503, { error: 'Set OPENAI_API_KEY in the server environment.' });
      let size = 0; const chunks = [];
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 65536) return reply(res, 413, { error: 'Request too large' });
        chunks.push(chunk);
      }
      let body;
      try { body = JSON.parse(Buffer.concat(chunks).toString()); } catch { return reply(res, 400, { error: 'Invalid JSON' }); }
      if (typeof body.sdp !== 'string' || !body.sdp.startsWith('v=0')) return reply(res, 400, { error: 'A valid SDP offer is required' });
      const result = await upstream('https://api.openai.com/v1/live/sessions', {
        method: 'POST', headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: sessionConfig, transport: { type: 'webrtc', sdp: body.sdp } }),
        signal: AbortSignal.timeout(45000),
      });
      const data = await result.json();
      if (!result.ok) {
        const code = String(data.error?.code || data.error?.type || 'api_error').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 80);
        console.error('Live creation rejected:', result.status, code);
        return reply(res, result.status, { error: `OpenAI rejected session creation (${result.status}, ${code}). Check model access, billing, or API key permissions.` });
      }
      if (!data.session?.id || !data.transport?.sdp) return reply(res, 502, { error: 'OpenAI returned an incomplete session answer.' });
      reply(res, 201, { session: { id: data.session.id }, transport: { type: 'webrtc', sdp: data.transport.sdp } });
    } catch (error) {
      console.error('Live gateway failed:', error.name);
      if (!res.headersSent) reply(res, 502, { error: 'Unable to connect to OpenAI. Check the network and try again.' });
      else res.end();
    }
  });
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  if (!key) { console.error('Set OPENAI_API_KEY before starting.'); process.exit(1); }
  createServer().listen(port, '127.0.0.1', () => console.log(`Colleague AI · GPT-Live 1: ${origin}`));
}
