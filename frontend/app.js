'use strict';

// ================================================================
// STATE
// ================================================================
const state = {
  sessionId:             null,
  userName:              null,
  emergencyContact:      null,
  location:              { lat: null, lng: null, address: null },
  distressLevel:         0,
  peakDistressLevel:     0,
  status:                'idle',   // idle | active | alert | safe
  alertSent:             false,
  conversationStartTime: null,
  ws:                    null,     // backend WebSocket
  elevenlabsWs:          null,     // ElevenLabs WebSocket
  transcript:            [],
  // internals (not exposed to backend)
  _wsReconnectAttempts:  0,
  _wsReconnectTimer:     null,
};

const API_BASE = 'http://localhost:8000';
const WS_MAX_RECONNECTS = 3;
const WS_RECONNECT_DELAY_MS = 2000;

// ================================================================
// DOM REFS  (populated inside init() after DOMContentLoaded)
// ================================================================
let el = {};

// ================================================================
// SCREEN MANAGEMENT
// ================================================================
function showScreen(id) {
  document.querySelectorAll('.screen').forEach(screen => {
    if (screen.id === id) {
      screen.removeAttribute('hidden');
    } else {
      screen.setAttribute('hidden', '');
    }
  });
}

// ================================================================
// LOCATION
// ================================================================
async function reverseGeocode(lat, lng) {
  try {
    const res = await fetch(
      `https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lng}&format=json`,
      { headers: { 'Accept-Language': 'en' } }
    );
    if (!res.ok) throw new Error('non-200');
    const data = await res.json();
    return data.display_name || `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
  } catch {
    return `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
  }
}

function requestLocation() {
  if (!navigator.geolocation) {
    el.locationStatus.textContent = 'Geolocation is not supported by this browser.';
    el.locationStatus.className = 'location-status error';
    return;
  }

  el.locationStatus.textContent = 'Getting location…';
  el.locationStatus.className = 'location-status';
  el.btnLocation.disabled = true;

  navigator.geolocation.getCurrentPosition(
    async pos => {
      const { latitude: lat, longitude: lng } = pos.coords;
      state.location.lat = lat;
      state.location.lng = lng;

      const address = await reverseGeocode(lat, lng);
      state.location.address = address;

      const display = address.length > 60 ? address.slice(0, 57) + '…' : address;
      el.locationStatus.textContent = `📍 ${display}`;
      el.locationStatus.className = 'location-status';
      el.btnLocation.textContent = 'Update Location';
      el.btnLocation.disabled = false;
      validateForm();
    },
    err => {
      const msg = {
        1: 'Location access denied — please allow it to continue.',
        2: 'Location unavailable — check device settings.',
        3: 'Location request timed out — please try again.',
      }[err.code] || 'Could not get location.';
      el.locationStatus.textContent = msg;
      el.locationStatus.className = 'location-status error';
      el.btnLocation.disabled = false;
    },
    { timeout: 12000, enableHighAccuracy: true }
  );
}

// ================================================================
// FORM VALIDATION
// ================================================================
function validateForm() {
  const name    = el.inputName.value.trim();
  const contact = el.inputContact.value.trim();
  const hasLoc  = state.location.lat !== null;
  const validEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(contact);
  const enabled = !!(name && validEmail && hasLoc);
  console.log('[validateForm] name=%o contact=%o hasLoc=%o validEmail=%o → enabled=%o',
              name, contact, hasLoc, validEmail, enabled);
  el.btnStart.disabled = !enabled;
}

// ================================================================
// API HELPER
// ================================================================
async function apiFetch(path, { method = 'GET', body } = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${text}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

// ================================================================
// SETUP — START WALK
// ================================================================
async function startWalk(evt) {
  evt.preventDefault();
  el.btnStart.disabled = true;
  el.btnStart.textContent = 'Starting…';

  state.userName        = el.inputName.value.trim();
  state.emergencyContact = el.inputContact.value.trim();

  try {
    // 1. Create session
    const session = await apiFetch('/api/sessions/create', {
      method: 'POST',
      body: {
        user_name:         state.userName,
        emergency_contact: state.emergencyContact,
        location:          state.location,
      },
    });
    state.sessionId            = session.session_id;
    state.conversationStartTime = Date.now();
    state.status               = 'active';

    // 2. Get ElevenLabs signed URL
    const { signed_url } = await apiFetch(
      `/api/elevenlabs/signed-url?session_id=${state.sessionId}`
    );

    // 3. Switch screen, then connect
    showScreen('screen-active');
    setConnected(false);
    updateDistressMeter(0);

    connectBackendWS(state.sessionId);
    connectElevenLabs(signed_url);

  } catch (err) {
    console.error('startWalk failed:', err);
    el.btnStart.textContent = 'Start SafeWalk';
    el.btnStart.disabled = false;
    alert(`Could not start walk: ${err.message}`);
  }
}

// ================================================================
// BACKEND WEBSOCKET
// ================================================================
function connectBackendWS(sessionId) {
  const url = `${API_BASE.replace(/^http/, 'ws')}/api/ws/${sessionId}`;
  const ws = new WebSocket(url);
  state.ws = ws;

  ws.onopen = () => {
    setConnected(true);
    state._wsReconnectAttempts = 0;
  };

  ws.onmessage = evt => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }

    if (msg.type === 'distress_update') {
      // Only ratchet up — never let a backend 0 undo a keyword nudge
      updateDistressMeter(Math.max(state.distressLevel, msg.level));
      if (msg.recommend_alert && !state.alertSent) flashEmergencyButton();

    } else if (msg.type === 'emergency_triggered') {
      if (!state.alertSent) {
        state.alertSent = true;
        populateAlertScreen();
        showScreen('screen-alert');
      }

    } else if (msg.type === 'error') {
      console.warn('Backend WS error msg:', msg.detail);
    }
  };

  ws.onclose = () => {
    setConnected(false);
    if (
      state.status === 'active' &&
      state._wsReconnectAttempts < WS_MAX_RECONNECTS
    ) {
      state._wsReconnectAttempts++;
      addSystemMessage(
        `Connection lost — reconnecting (${state._wsReconnectAttempts}/${WS_MAX_RECONNECTS})…`
      );
      state._wsReconnectTimer = setTimeout(
        () => connectBackendWS(sessionId),
        WS_RECONNECT_DELAY_MS
      );
    } else if (state.status === 'active') {
      addSystemMessage('Connection lost — please check your network.');
    }
  };

  ws.onerror = () => {
    setConnected(false);
    addSystemMessage('WebSocket error — attempting to reconnect.');
  };
}

function sendTranscriptToBackend(speaker, text) {
  if (state.ws?.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: 'transcript', speaker, text }));
  }
}

function setConnected(connected) {
  el.connIndicator.classList.toggle('connected', connected);
  el.connLabel.textContent = connected ? 'Connected' : 'Connecting…';
}

// ================================================================
// ELEVENLABS WEBSOCKET
// ================================================================
function connectElevenLabs(signedUrl) {
  const ws = new WebSocket(signedUrl);
  state.elevenlabsWs = ws;

  ws.onopen = () => {
    // Initiation handshake per ElevenLabs Conversational AI protocol
    ws.send(JSON.stringify({ type: 'conversation_initiation_client_data' }));
    startMicCapture();
  };

  ws.onmessage = evt => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }

    switch (msg.type) {
      case 'conversation_initiation_metadata': {
        const meta = msg.conversation_initiation_metadata_event ?? {};
        console.log('ElevenLabs session:', meta.conversation_id, 'agent:', meta.agent_id);
        break;
      }
      case 'audio': {
        const chunk = msg.audio_event?.audio_base_64;
        if (chunk) enqueueAudio(chunk);
        break;
      }
      case 'agent_response': {
        const text = msg.agent_response_event?.agent_response;
        if (text) {
          addToTranscript('agent', text);
          sendTranscriptToBackend('agent', text);
        }
        break;
      }
      case 'user_transcript': {
        const text = msg.user_transcription_event?.user_transcript;
        if (text) {
          addToTranscript('user', text);
          sendTranscriptToBackend('user', text);
          nudgeDistressFromKeywords(text);
        }
        break;
      }
      case 'interruption':
        clearAudioQueue();
        break;
      case 'ping': {
        const eventId = msg.ping_event?.event_id;
        ws.send(JSON.stringify({ type: 'pong', event_id: eventId }));
        break;
      }
      // 'internal_tentative_agent_response' — intentionally ignored
    }
  };

  ws.onerror = () => {
    addSystemMessage('Voice connection failed — text mode active.');
    el.micLabel.textContent = 'Voice unavailable';
  };

  ws.onclose = () => {
    stopMicCapture();
    el.micContainer?.classList.remove('speaking');
    if (state.status === 'active') {
      el.micLabel.textContent = 'Voice disconnected';
    }
  };
}

// ================================================================
// MICROPHONE  (PCM16 @ 16 kHz → ElevenLabs)
// ================================================================
let _micAudioCtx  = null;
let _micStream    = null;
let _micProcessor = null;

async function startMicCapture() {
  try {
    _micStream   = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    _micAudioCtx = new AudioContext({ sampleRate: 16000 });

    const source = _micAudioCtx.createMediaStreamSource(_micStream);
    // ScriptProcessor is deprecated but has universal browser support;
    // 4096 samples ≈ 256 ms at 16 kHz — a comfortable chunk size for ElevenLabs.
    _micProcessor = _micAudioCtx.createScriptProcessor(4096, 1, 1);

    _micProcessor.onaudioprocess = evt => {
      const float32 = evt.inputBuffer.getChannelData(0);

      // RMS-based voice-activity detection for the mic animation
      let sumSq = 0;
      for (let i = 0; i < float32.length; i++) sumSq += float32[i] * float32[i];
      const rms = Math.sqrt(sumSq / float32.length);
      if (rms > 0.01 && !_isPlaying) {
        el.micContainer?.classList.add('speaking');
      } else if (rms <= 0.01 && !_isPlaying) {
        el.micContainer?.classList.remove('speaking');
      }

      if (state.elevenlabsWs?.readyState === WebSocket.OPEN) {
        const pcm16 = float32ToPcm16(float32);
        state.elevenlabsWs.send(
          JSON.stringify({ user_audio_chunk: arrayBufferToBase64(pcm16.buffer) })
        );
      }
    };

    source.connect(_micProcessor);
    _micProcessor.connect(_micAudioCtx.destination);
    el.micLabel.textContent = 'SafeWalk is listening…';

  } catch (err) {
    console.error('Microphone error:', err);
    el.micLabel.textContent = 'Microphone unavailable — check browser permissions.';
  }
}

function stopMicCapture() {
  _micProcessor?.disconnect();
  _micProcessor = null;
  _micAudioCtx?.close();
  _micAudioCtx = null;
  _micStream?.getTracks().forEach(t => t.stop());
  _micStream = null;
}

function float32ToPcm16(float32) {
  const pcm = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const clamped = Math.max(-1, Math.min(1, float32[i]));
    pcm[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
  }
  return pcm;
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  // Process in 8 kB slices to avoid call-stack overflow on large chunks
  const CHUNK = 8192;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

// ================================================================
// AUDIO PLAYBACK  (ElevenLabs agent output)
// ================================================================
let _playbackCtx = null;
let _audioQueue  = [];
let _isPlaying   = false;

function getPlaybackCtx() {
  if (!_playbackCtx || _playbackCtx.state === 'closed') {
    _playbackCtx = new AudioContext();
  }
  return _playbackCtx;
}

function enqueueAudio(base64) {
  _audioQueue.push(base64);
  if (!_isPlaying) playNextChunk();
}

function clearAudioQueue() {
  _audioQueue = [];
  _isPlaying  = false;
  el.micContainer?.classList.remove('speaking');
}

async function playNextChunk() {
  if (_audioQueue.length === 0) {
    _isPlaying = false;
    el.micContainer?.classList.remove('speaking');
    return;
  }
  _isPlaying = true;
  el.micContainer?.classList.add('speaking');

  const base64 = _audioQueue.shift();
  try {
    const ctx   = getPlaybackCtx();
    const bytes = base64ToUint8Array(base64);

    let audioBuffer;
    try {
      // Try standard decode first (MP3, WAV, OGG…)
      audioBuffer = await ctx.decodeAudioData(bytes.buffer.slice(0));
    } catch {
      // Fall back: treat as raw PCM16 mono at 16 kHz
      audioBuffer = pcm16BytesToAudioBuffer(ctx, bytes);
    }

    const source = ctx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(ctx.destination);
    source.onended = () => playNextChunk();
    source.start();
  } catch (err) {
    console.warn('Audio playback error — skipping chunk:', err);
    playNextChunk();
  }
}

function base64ToUint8Array(base64) {
  const binary = atob(base64);
  const bytes  = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function pcm16BytesToAudioBuffer(ctx, bytes) {
  const samples     = bytes.length / 2;
  const audioBuffer = ctx.createBuffer(1, samples, 16000);
  const channel     = audioBuffer.getChannelData(0);
  const view        = new DataView(bytes.buffer);
  for (let i = 0; i < samples; i++) {
    // Little-endian signed 16-bit → float32
    channel[i] = view.getInt16(i * 2, true) / 32768;
  }
  return audioBuffer;
}

// ================================================================
// DISTRESS METER
// ================================================================
const DISTRESS_LABELS = [
  'Calm / Normal',
  'Mild anxiety',
  'Nervous / Concerned',
  'Distressed',
  'High distress / Possible danger',
  'Emergency / Immediate help needed',
];

const DISTRESS_KEYWORDS = [
  'scared', 'help', 'following', 'danger', 'afraid',
  'terrified', 'attack', 'hurt', 'threat', 'emergency',
  'dark', 'nervous', 'uncomfortable', 'weird', 'strange',
  'someone', 'alone', 'night', 'worried', 'uneasy',
];

function nudgeDistressFromKeywords(text) {
  const lower = text.toLowerCase();
  const count = DISTRESS_KEYWORDS.filter(kw => lower.includes(kw)).length;
  if (count > 0) {
    updateDistressMeter(Math.min(5, state.distressLevel + count));
    console.log('distress nudge:', state.distressLevel);
  }
}

function updateDistressMeter(level) {
  level = Math.max(0, Math.min(5, Math.round(level)));
  state.distressLevel  = level;
  state.peakDistressLevel = Math.max(state.peakDistressLevel, level);

  // Bar
  el.distressFill.style.width   = `${(level / 5) * 100}%`;
  el.distressFill.dataset.level = level;
  el.distressValue.textContent  = `${level} / 5`;
  el.distressDesc.textContent   = DISTRESS_LABELS[level];
  el.distressMeter?.setAttribute('aria-valuenow', level);

  // Status chip
  const si = el.statusIndicator;
  si.className = 'status-indicator';
  if (level <= 1) {
    si.classList.add('status-safe');
    el.statusLabel.textContent = 'Safe';
  } else if (level <= 3) {
    si.classList.add('status-caution');
    el.statusLabel.textContent = 'Cautious';
  } else {
    si.classList.add('status-danger');
    el.statusLabel.textContent = 'Alert';
  }

  // Extra attention at level 4+
  if (level >= 4) flashEmergencyButton();
}

function flashEmergencyButton() {
  const btn = el.btnEmergency;
  // Restart animation by briefly clearing it
  btn.style.animation = 'none';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    btn.style.animation = '';
  }));
  btn.style.boxShadow = '0 0 0 3px var(--color-danger)';
  setTimeout(() => { btn.style.boxShadow = ''; }, 2500);
}

// ================================================================
// TRANSCRIPT
// ================================================================
function addToTranscript(speaker, text) {
  if (!text?.trim()) return;
  const entry = { speaker, text, timestamp: Date.now() };
  state.transcript.push(entry);

  const bubble  = document.createElement('div');
  bubble.className = `bubble ${speaker === 'user' ? 'user' : 'agent'}`;

  const label   = document.createElement('span');
  label.className = 'bubble-label';
  label.textContent = speaker === 'user' ? 'You' : 'SafeWalk';

  const content = document.createElement('span');
  content.className = 'bubble-text';
  content.textContent = text;

  bubble.appendChild(label);
  bubble.appendChild(content);
  el.transcript.appendChild(bubble);
  el.transcript.scrollTop = el.transcript.scrollHeight;
}

function addSystemMessage(text) {
  const div = document.createElement('div');
  div.className = 'bubble system';
  div.textContent = text;
  el.transcript.appendChild(div);
  el.transcript.scrollTop = el.transcript.scrollHeight;
}

// ================================================================
// EMERGENCY FLOW
// ================================================================
async function triggerEmergency(manual = true) {
  if (state.alertSent) return;

  // Snapshot before any async work — state may change while awaiting the API call
  const summarySnapshot = {
    peakDistress:  state.peakDistressLevel,
    alertSent:     true,
    transcriptLen: state.transcript.length,
    startTime:     state.conversationStartTime,
  };

  // Push current distress level to backend before the emergency API reads it from MongoDB
  if (state.ws?.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: 'distress_update', level: state.distressLevel }));
  }

  try {
    await apiFetch('/api/emergency/trigger', {
      method: 'POST',
      body: { session_id: state.sessionId, manual },
    });
    state.alertSent = true;
    state.status = 'alert';
    populateSafeScreen(summarySnapshot);
    populateAlertScreen();
    showScreen('screen-alert');
  } catch (err) {
    console.error('Emergency trigger failed:', err);
    alert('Could not send alert. Please call 911 directly.');
  }
}

function populateAlertScreen() {
  const loc = state.location;
  el.alertContact.textContent  = state.emergencyContact || '—';
  el.alertLocation.textContent =
    loc.address || (loc.lat ? `${loc.lat.toFixed(5)}, ${loc.lng.toFixed(5)}` : '—');
  el.alertTime.textContent = new Date().toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

async function cancelAlert() {
  try {
    await apiFetch('/api/emergency/cancel', {
      method: 'POST',
      body: { session_id: state.sessionId },
    });
  } catch (err) {
    console.error('Cancel alert failed:', err);
  }
  await endSession({ fromCancelledAlert: true });
}

// ================================================================
// END SESSION
// ================================================================
async function endSession({ fromCancelledAlert = false } = {}) {
  const savedPeak         = state.peakDistressLevel;
  const savedAlertSent    = state.alertSent;
  const savedTranscriptLen = state.transcript.length;
  const savedStartTime    = state.conversationStartTime;

  clearTimeout(state._wsReconnectTimer);
  state.status = 'safe';

  // Prevent reconnect loops on intentional close
  if (state.ws) {
    state.ws.onclose = null;
    state.ws.close();
    state.ws = null;
  }
  if (state.elevenlabsWs) {
    state.elevenlabsWs.onclose = null;
    state.elevenlabsWs.close();
    state.elevenlabsWs = null;
  }

  stopMicCapture();
  clearAudioQueue();

  if (!fromCancelledAlert) {
    try {
      await apiFetch(`/api/sessions/${state.sessionId}/end`, { method: 'DELETE' });
    } catch (err) {
      console.error('End session failed:', err);
    }
  }

  if (!fromCancelledAlert) {
    populateSafeScreen({
      peakDistress:  savedPeak,
      alertSent:     savedAlertSent,
      transcriptLen: savedTranscriptLen,
      startTime:     savedStartTime,
    });
  }
  showScreen('screen-safe');
}

function populateSafeScreen({ peakDistress, alertSent, transcriptLen, startTime }) {
  const duration = startTime ? Date.now() - startTime : 0;
  el.summaryDuration.textContent   = formatDuration(duration);
  el.summaryAlerts.textContent     = alertSent ? '1 — emergency alert sent' : 'None';
  el.summaryPeak.textContent       = `${peakDistress} / 5 — ${DISTRESS_LABELS[peakDistress]}`;
  el.summaryTranscript.textContent = `${transcriptLen} turns`;
}

// ================================================================
// RESET
// ================================================================
function resetState() {
  Object.assign(state, {
    sessionId:             null,
    userName:              null,
    emergencyContact:      null,
    location:              { lat: null, lng: null, address: null },
    distressLevel:         0,
    peakDistressLevel:     0,
    status:                'idle',
    alertSent:             false,
    conversationStartTime: null,
    ws:                    null,
    elevenlabsWs:          null,
    transcript:            [],
    _wsReconnectAttempts:  0,
    _wsReconnectTimer:     null,
  });
}

// ================================================================
// UTILITY
// ================================================================
function formatDuration(ms) {
  const totalSec = Math.floor(ms / 1000);
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  if (min === 0) return `${sec}s`;
  return `${min}m ${sec}s`;
}

// ================================================================
// EVENT WIRING
// ================================================================
function init() {
  el = {
    // setup
    setupForm:        document.getElementById('setup-form'),
    inputName:        document.getElementById('input-name'),
    inputContact:     document.getElementById('input-contact'),
    btnLocation:      document.getElementById('btn-location'),
    locationStatus:   document.getElementById('location-status'),
    btnStart:         document.getElementById('btn-start'),
    // active
    statusIndicator:  document.getElementById('status-indicator'),
    statusLabel:      document.getElementById('status-label'),
    connIndicator:    document.getElementById('conn-indicator'),
    connLabel:        document.getElementById('conn-label'),
    micContainer:     document.querySelector('.mic-container'),
    micLabel:         document.getElementById('mic-label'),
    distressFill:     document.getElementById('distress-fill'),
    distressValue:    document.getElementById('distress-value'),
    distressDesc:     document.getElementById('distress-desc'),
    distressMeter:    document.querySelector('.distress-meter'),
    transcript:       document.getElementById('transcript'),
    btnSafe:          document.getElementById('btn-safe'),
    btnEmergency:     document.getElementById('btn-emergency'),
    // alert
    alertContact:     document.getElementById('alert-contact'),
    alertLocation:    document.getElementById('alert-location'),
    alertTime:        document.getElementById('alert-time'),
    btnCancelAlert:   document.getElementById('btn-cancel-alert'),
    // safe
    summaryDuration:  document.getElementById('summary-duration'),
    summaryAlerts:    document.getElementById('summary-alerts'),
    summaryPeak:      document.getElementById('summary-peak'),
    summaryTranscript:document.getElementById('summary-transcript'),
    btnNewWalk:       document.getElementById('btn-new-walk'),
  };

  // Setup
  el.btnLocation.addEventListener('click', requestLocation);
  el.inputName.addEventListener('input', validateForm);
  el.inputContact.addEventListener('input', validateForm);
  el.btnStart.addEventListener('click', startWalk);

  // Active screen
  el.btnSafe.addEventListener('click', () => endSession());
  el.btnEmergency.addEventListener('click', () => triggerEmergency(true));

  // Alert screen
  el.btnCancelAlert.addEventListener('click', cancelAlert);

  // Safe screen — start over
  el.btnNewWalk.addEventListener('click', () => {
    resetState();
    el.inputName.value       = '';
    el.inputContact.value    = '';
    el.locationStatus.textContent = '';
    el.locationStatus.className   = 'location-status';
    el.btnLocation.textContent    = 'Share My Location';
    el.btnStart.disabled          = true;
    el.transcript.innerHTML       = '';
    updateDistressMeter(0);
    showScreen('screen-setup');
  });
}

document.addEventListener('DOMContentLoaded', init);
