const $ = id => document.getElementById(id);
const output = $('output');
let call = null;
function setStatus(text) { $('status').textContent = text; }
function send(c, event) { if (c.events?.readyState === 'open') c.events.send(JSON.stringify(event)); }
function cleanup(c) {
  if (call !== c) return;
  call = null;
  clearTimeout(c.startTimeout); clearTimeout(c.closeTimeout); clearInterval(c.meterTimer);
  c.mic?.getTracks().forEach(t => t.stop());
  c.events?.close(); c.peer?.close(); c.context?.close();
  output.srcObject = null;
  $('join').disabled = false; $('mute').disabled = $('end').disabled = true;
  $('mute').textContent = 'Mute microphone'; $('mute').setAttribute('aria-pressed', 'false');
  $('level').style.width = '0%';
  $('stats').textContent = c.finalized ? `Call ended · ${c.seconds ?? 0} seconds` : 'Microphone off';
}
function fail(c, message) { if (call !== c) return; $('error').textContent = message; setStatus('Call disconnected'); cleanup(c); }
function caption(c, event) {
  const speaker = event.type === 'session.input_transcript.delta' ? 'You' : 'Colleague AI';
  const box = $('captions');
  const follow = box.scrollHeight - box.scrollTop - box.clientHeight < 50;
  // Independent speaker rows preserve simultaneous speech. Raw fragments are
  // retained with timing; display grouping does not drive turn-taking.
  c.fragments.push({ speaker, delta: event.delta, start_ms: event.start_ms, end_ms: event.end_ms });
  let row = c.rows[speaker];
  if (!row || event.start_ms - row.end > 2000 || event.start_ms < row.start) {
    box.querySelector('.empty')?.remove();
    const block = document.createElement('div'); block.className = 'caption';
    const label = document.createElement('b'); label.textContent = speaker;
    const text = document.createElement('span'); block.append(label, text); box.append(block);
    row = { text, start: event.start_ms, end: event.end_ms }; c.rows[speaker] = row;
  }
  row.text.append(document.createTextNode(event.delta)); row.end = event.end_ms;
  if (follow) box.scrollTop = box.scrollHeight;
}
function onEvent(c, event) {
  if (call !== c) return;
  if (event.type === 'session.started') {
    c.ready = true; clearTimeout(c.startTimeout);
    $('mute').disabled = $('end').disabled = false;
    setStatus('Connected · speak naturally'); $('stats').textContent = 'Microphone on';
    send(c, { type: 'session.instructions.append', event_id: 'welcome', delegation_id: null,
      content: 'Speak first now: briefly introduce yourself as Colleague AI and invite the caller to talk. Then listen.' });
  } else if (event.type === 'session.input_transcript.delta' || event.type === 'session.output_transcript.delta') {
    caption(c, event);
  } else if (event.type === 'session.usage.updated') {
    c.seconds = event.usage?.seconds;
    $('stats').textContent = `${c.muted ? 'Microphone muted' : 'Microphone on'} · ${Math.round(c.seconds || 0)} seconds`;
  } else if (event.type === 'session.closed') {
    c.finalized = true; c.seconds = event.usage?.seconds;
    setStatus('Call ended'); cleanup(c);
  } else if (event.type === 'error') {
    $('error').textContent = event.error?.message || 'OpenAI reported a session error.';
  }
}
async function gather(peer) {
  if (peer.iceGatheringState === 'complete') return;
  await new Promise((resolve, reject) => {
    const finish = () => { clearTimeout(timer); peer.removeEventListener('icegatheringstatechange', changed); };
    const changed = () => { if (peer.iceGatheringState === 'complete') { finish(); resolve(); } };
    const timer = setTimeout(() => { finish(); reject(new Error('ICE gathering timed out. Check your network.')); }, 10000);
    peer.addEventListener('icegatheringstatechange', changed); changed();
  });
}
$('join').onclick = async () => {
  const c = { rows: {}, fragments: [], ready: false, finalized: false, muted: false };
  call = c; $('join').disabled = true; $('error').textContent = '';
  $('captions').replaceChildren(); setStatus('Connecting…');
  c.startTimeout = setTimeout(() => fail(c, 'The call did not connect in time. Please try again.'), 65000);
  try {
    c.mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    if (call !== c) { c.mic.getTracks().forEach(t => t.stop()); return; }
    c.peer = new RTCPeerConnection();
    c.peer.ontrack = ({ track }) => {
      output.srcObject = new MediaStream([track]);
      output.play().catch(() => { $('error').textContent = 'Press Play on the audio controls to hear your colleague.'; });
    };
    c.peer.onconnectionstatechange = () => {
      if (c.peer.connectionState === 'failed') fail(c, 'The voice connection failed. Check your network and rejoin.');
    };
    c.mic.getAudioTracks().forEach(t => c.peer.addTrack(t, c.mic));
    c.events = c.peer.createDataChannel('oai-events');
    c.events.onmessage = ({ data }) => { try { onEvent(c, JSON.parse(data)); } catch { $('error').textContent = 'An unreadable call event was received.'; } };
    c.events.onclose = () => { if (call === c && !c.finalized) fail(c, 'The call disconnected before final usage was confirmed.'); };
    c.context = new AudioContext(); await c.context.resume();
    const analyser = c.context.createAnalyser(); analyser.fftSize = 256;
    c.context.createMediaStreamSource(c.mic).connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    c.meterTimer = setInterval(() => {
      analyser.getFloatTimeDomainData(samples);
      const rms = Math.sqrt(samples.reduce((sum, x) => sum + x*x, 0) / samples.length);
      $('level').style.width = `${c.muted ? 0 : Math.min(100, rms * 700)}%`;
    }, 100);
    await c.peer.setLocalDescription(await c.peer.createOffer());
    await gather(c.peer);
    const r = await fetch('/api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sdp: c.peer.localDescription.sdp }), signal: AbortSignal.timeout(50000) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `Session failed (${r.status}).`);
    if (call !== c) return;
    await c.peer.setRemoteDescription({ type: 'answer', sdp: data.transport.sdp });
  } catch (error) {
    fail(c, error.name === 'NotAllowedError' ? 'Allow microphone access in your browser, then join again.' : error.message);
  }
};
$('mute').onclick = () => {
  if (!call?.ready) return;
  call.muted = !call.muted;
  call.mic.getAudioTracks().forEach(t => t.enabled = !call.muted);
  $('mute').textContent = call.muted ? 'Unmute microphone' : 'Mute microphone';
  $('mute').setAttribute('aria-pressed', String(call.muted));
  $('stats').textContent = call.muted ? 'Microphone muted' : 'Microphone on';
};
$('end').onclick = () => {
  const c = call; if (!c?.ready) return;
  $('end').disabled = $('mute').disabled = true; setStatus('Ending call…');
  // Silence the microphone immediately, keeping the media track alive to drain events.
  c.mic.getAudioTracks().forEach(t => t.enabled = false);
  send(c, { type: 'session.close' });
  c.closeTimeout = setTimeout(() => fail(c, 'Call ended locally; final API usage was not confirmed.'), 15000);
};
window.addEventListener('pagehide', () => { if (call) { send(call, { type: 'session.close' }); cleanup(call); } });
fetch('/health').then(r => r.json()).then(h => { if (!h.key_configured) $('error').textContent = 'The server needs an OpenAI API key.'; }).catch(() => { $('error').textContent = 'The local call server is unavailable.'; });
