// The Studio's practice page: a song's parts as notes and tablature
// (alphaTab), played with the song itself -- whole, without the guitars and
// keys, or those alone -- or with the notes on a synthesizer; slower, and a
// stretch on repeat.
//
// alphaTab keeps the score's clock; the song is an <audio> element alphaTab
// drives through its external-media handler, told where the song is on every
// frame. The score is written at a whole-number tempo from `start` seconds
// into the song, so the two clocks are mapped by the ratio of the song's real
// tempo to the written one -- without it the cursor drifts a beat or so over
// a song (studio/score.py in the Studio has why).
import * as alphaTab from '/static/vendor/alphatab/alphaTab.mjs';

const CFG = JSON.parse(document.getElementById('practice-cfg').textContent);
const S = CFG.strings;
const meta = CFG.meta;
const $ = (q) => document.querySelector(q);

const ratio = meta.score_tempo / meta.tempo;           // song seconds per score second
const toSong = (ms) => meta.start + (ms / 1000) * ratio;
const toScore = (sec) => Math.max(0, ((sec - meta.start) / ratio) * 1000);

const audio = new Audio();
audio.preload = 'auto';
audio.preservesPitch = true;
// A seek asked for before the song has loaded is applied once it has: a
// currentTime set on an empty element is dropped.
let pendingSeek = null;
audio.addEventListener('loadedmetadata', () => {
  if (pendingSeek !== null) { audio.currentTime = pendingSeek; pendingSeek = null; }
});

const handler = {
  get backingTrackDuration() { return isFinite(audio.duration) ? toScore(audio.duration) : 0; },
  get playbackRate() { return audio.playbackRate; },
  set playbackRate(v) { audio.playbackRate = v; },
  get masterVolume() { return audio.volume; },
  set masterVolume(v) { audio.volume = Math.max(0, Math.min(1, v)); },
  seekTo(ms) {
    const t = Math.max(0, toSong(ms));
    if (audio.readyState >= 1) audio.currentTime = t; else pendingSeek = t;
  },
  play() { audio.play().catch(() => {}); },
  pause() { audio.pause(); },
};

let api = null, score = null, source = 'song', trackIndex = 0, view = 'both';

function settings(mode) {
  return {
    core: { fontDirectory: '/static/vendor/alphatab/font/', engine: 'svg', logLevel: 'warning' },
    display: { layoutMode: 'page', scale: window.innerWidth < 700 ? 0.75 : 1.0 },
    player: {
      playerMode: mode,
      soundFont: '/static/vendor/alphatab/soundfont/sonivox.sf3',
      scrollElement: document.scrollingElement,
      scrollOffsetY: -80,
      enableCursor: true, enableAnimatedBeatCursor: true, enableUserInteraction: true,
    },
  };
}

function mount(mode) {
  const keep = api ? { speed: api.playbackSpeed, loop: api.isLooping, range: api.playbackRange, tick: api.tickPosition } : null;
  if (api) { api.destroy(); $('#score').innerHTML = ''; }
  $('#loading').hidden = false;
  api = new alphaTab.AlphaTabApi($('#score'), settings(mode));
  window.practiceApi = api;
  api.error.on((e) => showError(e && e.message ? e.message : String(e)));
  api.scoreLoaded.on((s) => { score = s; applyView(); });
  api.renderFinished.on(() => { $('#loading').hidden = true; window.practiceRendered = true; });
  api.playerStateChanged.on((e) => { $('#play').textContent = e.state === 1 ? '⏸' : '▶'; });
  api.playerPositionChanged.on((e) => {
    const now = source === 'synth' ? e.currentTime / 1000 : toSong(e.currentTime);
    const end = source === 'synth' ? e.endTime / 1000 : (isFinite(audio.duration) ? audio.duration : toSong(e.endTime));
    $('#clock').textContent = clock(now) + ' / ' + clock(end);
  });
  api.playerReady.on(() => {
    window.practiceReady = true;
    if (mode === alphaTab.PlayerMode.EnabledExternalMedia) api.player.output.handler = handler;
    if (keep) {
      api.playbackSpeed = keep.speed; api.isLooping = keep.loop;
      if (keep.range) api.playbackRange = keep.range;
      api.tickPosition = keep.tick;
    }
    api.countInVolume = $('#countin').checked ? 1 : 0;
    api.metronomeVolume = $('#metronome').checked ? 1 : 0;
    soloShown();
  });
  fetch(CFG.score).then((r) => {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.arrayBuffer();
  }).then((b) => api.load(new Uint8Array(b))).catch((e) => showError(e.message));
}

function shownTrack() { return score ? score.tracks[Math.min(trackIndex, score.tracks.length - 1)] : null; }

function applyView() {
  if (!score) return;
  score.tracks.forEach((tr) => tr.staves.forEach((st) => {
    if (!st.isStringed) return;
    st.showTablature = view !== 'notes';
    st.showStandardNotation = view !== 'tab';
  }));
  const tr = shownTrack();
  api.renderTracks([tr]);
  $('#view').disabled = !tr.staves.some((st) => st.isStringed);
  soloShown();
}

// On the synthesizer, the part on the page is the part heard.
function soloShown() {
  if (!api || !score || source !== 'synth') return;
  const tr = shownTrack();
  api.changeTrackSolo(score.tracks, false);
  api.changeTrackSolo([tr], true);
}

function follow() {
  if (api && source !== 'synth' && !audio.paused && api.player && api.player.output && api.player.output.updatePosition) {
    api.player.output.updatePosition(toScore(audio.currentTime));
  }
  requestAnimationFrame(follow);
}

function useSource(next) {
  const wasSynth = source === 'synth';
  source = next;
  $('#synth-only').hidden = next !== 'synth';
  if (next === 'synth') {
    audio.pause();
    mount(alphaTab.PlayerMode.EnabledSynthesizer);
    return;
  }
  const at = audio.currentTime || 0, playing = !audio.paused;
  audio.src = CFG.audio[next];
  pendingSeek = at;
  if (playing) audio.play().catch(() => {});
  if (wasSynth || !api) mount(alphaTab.PlayerMode.EnabledExternalMedia);
}

function clock(s) {
  s = Math.max(0, s || 0);
  return Math.floor(s / 60) + ':' + String(Math.floor(s % 60)).padStart(2, '0');
}
function showError(msg) {
  const el = $('#error');
  el.hidden = false;
  el.textContent = S.practice_error.replace('{e}', msg);
  $('#loading').hidden = true;
}

// -- the controls ------------------------------------------------------------
meta.tracks.forEach((t, i) => {
  const o = document.createElement('option');
  o.value = i;
  o.textContent = S['track_' + t.id] || t.name;
  $('#track').appendChild(o);
});
$('#track').onchange = (e) => { trackIndex = Number(e.target.value); applyView(); };
$('#view').onchange = (e) => { view = e.target.value; applyView(); };
$('#source').onchange = (e) => useSource(e.target.value);
$('#play').onclick = () => api && api.playPause();
$('#stop').onclick = () => api && api.stop();
$('#speed').oninput = (e) => {
  const v = Number(e.target.value) / 100;
  $('#speed-v').textContent = Math.round(v * 100) + '%';
  if (api) api.playbackSpeed = v;
};
$('#loop').onchange = (e) => { if (api) api.isLooping = e.target.checked; };
$('#countin').onchange = (e) => { if (api) api.countInVolume = e.target.checked ? 1 : 0; };
$('#metronome').onchange = (e) => { if (api) api.metronomeVolume = e.target.checked ? 1 : 0; };
$('#clear').onclick = () => { if (api) { api.playbackRange = null; api.clearPlaybackRangeHighlight(); } };
audio.addEventListener('ended', () => { if (api && !api.isLooping) api.stop(); });

useSource('song');
requestAnimationFrame(follow);
