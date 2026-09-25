const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const mqtt = require('mqtt');
const fs = require('fs');
const path = require('path');
const https = require('https');

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

// Broker address. Uses the same MQTT_BROKER / MQTT_PORT pair as the rest of the
// stack; a full mqtt://host:port URL is still accepted for the older form this
// service used to require.
const MQTT_BROKER_HOST = process.env.MQTT_BROKER || 'mqtt.home';
const MQTT_PORT = process.env.MQTT_PORT || 1883;
const MQTT_BROKER = MQTT_BROKER_HOST.includes('://')
  ? MQTT_BROKER_HOST
  : `mqtt://${MQTT_BROKER_HOST}:${MQTT_PORT}`;
const PORT = process.env.PORT || 21050;
const MAX_MESSAGES = 10000;
const MESSAGE_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
// Bridge definitions must outlive the container. Without DATA_DIR this resolves
// to /app inside the image - the ephemeral writable layer - so every configured
// ntfy bridge was silently destroyed by `docker compose up --force-recreate`.
const DATA_DIR = process.env.DATA_DIR || __dirname;
const BRIDGES_FILE = path.join(DATA_DIR, 'bridges.json');

const messages = [];
const clients = new Set();

function pruneOldMessages() {
  const cutoff = Date.now() - MESSAGE_RETENTION_MS;
  let pruned = 0;
  while (messages.length > 0 && new Date(messages[0].timestamp).getTime() < cutoff) {
    messages.shift();
    pruned++;
  }
  if (pruned > 0) console.log(`Pruned ${pruned} messages older than 7 days`);
}
let bridges = loadBridges();

function loadBridges() {
  try {
    if (fs.existsSync(BRIDGES_FILE)) {
      return JSON.parse(fs.readFileSync(BRIDGES_FILE, 'utf8'));
    }
  } catch (e) {
    console.error('Failed to load bridges:', e.message);
  }
  return [];
}

function saveBridges() {
  try {
    fs.mkdirSync(path.dirname(BRIDGES_FILE), { recursive: true });
    fs.writeFileSync(BRIDGES_FILE, JSON.stringify(bridges, null, 2));
  } catch (e) {
    console.error('Failed to save bridges:', e.message);
  }
}

function broadcast(message) {
  const data = JSON.stringify(message);
  for (const client of clients) {
    if (client.readyState === WebSocket.OPEN) {
      client.send(data);
    }
  }
}

function topicMatches(pattern, topic) {
  const patternParts = pattern.split('/');
  const topicParts = topic.split('/');
  for (let i = 0; i < patternParts.length; i++) {
    if (patternParts[i] === '#') return true;
    if (i >= topicParts.length) return false;
    if (patternParts[i] !== '+' && patternParts[i] !== topicParts[i]) return false;
  }
  return patternParts.length === topicParts.length;
}

function sendToNtfy(url, topic, title, message, priority) {
  const baseUrl = url.replace(/\/$/, '');
  const endpoint = `${baseUrl}/${encodeURIComponent(topic)}`;
  const body = JSON.stringify({ topic, title, message, priority: priority || 3 });
  const httpMod = baseUrl.startsWith('https') ? https : http;
  const req = httpMod.request(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    timeout: 5000,
  });
  req.on('error', (e) => console.error(`Ntfy error:`, e.message));
  req.write(body);
  req.end();
}

function checkBridges(topic, payload) {
  for (const bridge of bridges) {
    if (!bridge.enabled) continue;
    if (topicMatches(bridge.topic, topic)) {
      console.log(`Bridge: ${topic} → Ntfy (${bridge.name})`);
      sendToNtfy(
        bridge.ntfyUrl,
        bridge.ntfyTopic,
        bridge.titleTemplate ? bridge.titleTemplate.replace('{topic}', topic) : topic,
        bridge.messageTemplate ? bridge.messageTemplate.replace('{payload}', payload) : payload,
        bridge.priority || 0
      );
    }
  }
}

function connectMqttClient(brokerUrl, label, onMessage) {
  const client = mqtt.connect(brokerUrl, {
    clientId: `${label}-${Date.now()}`,
    clean: true,
    connectTimeout: 4000,
    reconnectPeriod: 1000,
  });

  client.on('connect', () => {
    console.log(`MQTT connected: ${label} (${brokerUrl})`);
    broadcast({ type: 'mqtt_status', connected: true });
    client.subscribe('#', (err) => {
      if (err) console.error('Failed to subscribe to #:', err);
      else console.log('MQTT subscribed to #');
    });
  });
  client.on('close', () => broadcast({ type: 'mqtt_status', connected: false }));
  client.on('error', (err) => console.error(`MQTT error (${label}):`, err.message));

  client.on('message', (topic, payload) => {
    const msg = payload.toString();
    if (onMessage) onMessage(topic, msg);
  });

  return client;
}

const mqttClient = connectMqttClient(MQTT_BROKER, 'main', (topic, payload) => {
  const message = {
    id: Date.now() + Math.random(),
    timestamp: new Date().toISOString(),
    topic,
    payload,
  };
  messages.push(message);
  if (messages.length > MAX_MESSAGES) messages.shift();
  pruneOldMessages();
  broadcast({ type: 'new', message });
  checkBridges(topic, payload);
});

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));
// The shared translation catalogues, staged into this build context by
// the deployer. Served from here rather than fetched from another box:
// a cross-origin fetch would make this page depend on the hub being up
// just to be legible.
app.use('/i18n', express.static(path.join(__dirname, 'i18n')));

function refreshSubscriptions() {
  if (mqttClient.connected) {
    mqttClient.unsubscribe('#', () => {});
    mqttClient.subscribe('#', (err) => {
      if (err) console.error('Failed to subscribe to #:', err);
      else console.log('MQTT subscribed to #');
    });
  }
}

// Liveness for the deployer. Reports whether the broker connection is actually
// up, not merely whether express is listening: a dashboard that cannot reach
// the broker shows an empty message list, which is indistinguishable from a
// quiet house.
app.get('/health', (req, res) => {
  const connected = Boolean(mqttClient && mqttClient.connected);
  res.status(connected ? 200 : 503).json({
    status: connected ? 'ok' : 'degraded',
    broker: MQTT_BROKER,
  });
});

app.get('/api/messages', (req, res) => {
  const { limit = 500, topic, subtopic, dateFrom, dateTo } = req.query;
  let filtered = messages;

  if (topic) filtered = filtered.filter((m) => m.topic.includes(topic));
  if (subtopic) filtered = filtered.filter((m) => m.topic.split('/').includes(subtopic));
  if (dateFrom) { const from = new Date(dateFrom); filtered = filtered.filter((m) => new Date(m.timestamp) >= from); }
  if (dateTo) { const to = new Date(dateTo); filtered = filtered.filter((m) => new Date(m.timestamp) <= to); }

  res.json(filtered.slice(-Math.min(limit, MAX_MESSAGES)));
});

app.get('/api/subtopics', (req, res) => {
  const subtopicSet = new Set();
  for (const msg of messages) {
    msg.topic.split('/').forEach((p) => { if (p) subtopicSet.add(p); });
  }
  res.json(Array.from(subtopicSet).sort());
});

app.get('/api/topics', (req, res) => {
  const topicSet = new Set();
  for (const msg of messages) {
    const parts = msg.topic.split('/');
    for (let i = 1; i <= parts.length; i++) topicSet.add(parts.slice(0, i).join('/'));
  }
  res.json(Array.from(topicSet).sort());
});

app.get('/api/bridges', (req, res) => {
  res.json(bridges);
});

app.post('/api/bridges', (req, res) => {
  const { name, topic, ntfyUrl, ntfyTopic, titleTemplate, messageTemplate, priority } = req.body;
  if (!name || !topic || !ntfyUrl || !ntfyTopic) {
    return res.status(400).json({ error: 'name, topic, ntfyUrl, and ntfyTopic are required' });
  }
  const bridge = {
    id: Date.now().toString(),
    name,
    topic,
    ntfyUrl,
    ntfyTopic,
    titleTemplate: titleTemplate || '',
    messageTemplate: messageTemplate || '',
    priority: priority || 0,
    enabled: true,
    createdAt: new Date().toISOString(),
  };
  bridges.push(bridge);
  saveBridges();
  res.status(201).json(bridge);
});

app.put('/api/bridges/:id', (req, res) => {
  const idx = bridges.findIndex((b) => b.id === req.params.id);
  if (idx === -1) return res.status(404).json({ error: 'Bridge not found' });
  bridges[idx] = { ...bridges[idx], ...req.body };
  saveBridges();
  res.json(bridges[idx]);
});

app.delete('/api/bridges/:id', (req, res) => {
  const idx = bridges.findIndex((b) => b.id === req.params.id);
  if (idx === -1) return res.status(404).json({ error: 'Bridge not found' });
  bridges.splice(idx, 1);
  saveBridges();
  res.json({ success: true });
});

app.post('/api/bridges/:id/test', (req, res) => {
  const bridge = bridges.find((b) => b.id === req.params.id);
  if (!bridge) return res.status(404).json({ error: 'Bridge not found' });
  sendToNtfy(bridge.ntfyUrl, bridge.ntfyTopic, 'Test Notification', `Test from bridge: ${bridge.name}`, 0);
  res.json({ success: true, message: 'Test notification sent' });
});

wss.on('connection', (ws) => {
  clients.add(ws);
  ws.on('close', () => clients.delete(ws));
});

server.listen(PORT, () => {
  console.log(`Dashboard running on http://localhost:${PORT}`);
  console.log(`Broker: ${MQTT_BROKER}`);
  console.log(`Bridges file: ${BRIDGES_FILE} (set DATA_DIR to override)`);
  pruneOldMessages();
  setInterval(pruneOldMessages, 60 * 60 * 1000);
  refreshSubscriptions();
});
