/* Plane Tracker front end: click an airport, get its aircraft boxed on Sentinel imagery. */
const $ = (id) => document.getElementById(id);

const map = L.map('map', { zoomControl: true }).setView([38, 60], 3);
const basemap = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 19, attribution: 'Basemap © Esri, Maxar, Earthstar Geographics · Imagery: Copernicus Sentinel · Airfields: © OpenStreetMap' }).addTo(map);
const labels = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 19 });

const airportLayer = L.layerGroup().addTo(map);
const imageLayer = L.layerGroup().addTo(map);
const boxLayer = L.layerGroup().addTo(map);
const outlineLayer = L.geoJSON(null, { style: { color: '#39ff5a', weight: 1.5, dashArray: '4 4', fillOpacity: 0.04 },
  interactive: false }).addTo(map);
L.control.layers({ 'Satellite basemap': basemap }, {
  'Place names': labels, 'Sentinel image': imageLayer, 'Aircraft boxes': boxLayer,
  'Airfield outline (search area)': outlineLayer,
}).addTo(map);

const COLOURS = { 'very large': '#ff3b30', 'large': '#ff9500', 'medium': '#ffd60a', 'small': '#4cc9f0' };
const legend = L.control({ position: 'bottomright' });
legend.onAdd = () => {
  const d = L.DomUtil.create('div', 'legend');
  d.innerHTML = [['very large', '≥ 45 m · bomber, tanker, heavy transport'], ['large', '32–45 m'],
    ['medium', '20–32 m · fighter-bomber, bizjet'], ['small', '< 20 m · fighter, helicopter']]
    .map(([k, t]) => `<i style="border-color:${COLOURS[k]}"></i>${t}`).join('<br>');
  return d;
};
legend.addTo(map);

const NAMES = { S2: 'Sentinel-2', S1: 'Sentinel-1' };
const fmtDate = (iso) => iso.replace('T', ' ').slice(0, 16) + ' UTC';
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}

let airports = [];
let current = null;       // selected airport detail
let currentScene = null;  // selected pass key
let features = [];
let polling = null;
let lastLogLen = -1;

// ---------------------------------------------------------------- airport list

function summaryText(s) {
  const parts = [];
  for (const k of ['S2', 'S1']) {
    if (s[k]) parts.push(`${k} ${s[k].datetime.slice(0, 10)}: ${s[k].n_planes}`);
  }
  return parts.length ? parts.join(' · ') : 'not analysed yet';
}

function latestCount(s) {
  const c = [s.S2, s.S1].filter(Boolean).sort((a, b) => b.datetime.localeCompare(a.datetime));
  return c.length ? c[0].n_planes : '–';
}

function busy(job) { return job && (job.state === 'queued' || job.state === 'running'); }

function renderAirports() {
  airportLayer.clearLayers();
  const ul = $('airports');
  ul.innerHTML = '';
  for (const a of airports) {
    const [w, s, e, n] = a.bounds;
    const rect = L.rectangle([[s, w], [n, e]], { color: '#fff', weight: 1, dashArray: '3 5', fill: true, fillOpacity: 0.02 });
    rect.on('click', () => selectAirport(a.id, true));
    airportLayer.addLayer(rect);
    const icon = L.divIcon({
      className: '', iconSize: null, iconAnchor: [14, 12],
      html: `<span class="plane-icon ${busy(a.job) ? 'busy' : ''}">✈ ${esc(a.name)}</span>`,
    });
    const m = L.marker([a.lat, a.lon], { icon, title: `Analyse ${a.name}` });
    m.on('click', () => selectAirport(a.id, true));
    airportLayer.addLayer(m);

    const li = document.createElement('li');
    li.className = 'airport' + (current && current.id === a.id ? ' sel' : '');
    li.innerHTML = `<div><b>${esc(a.name)}</b><div class="meta">${esc(a.country)}</div>
      <div class="meta">${busy(a.job) ? `<span class="tag">${a.job.state}…</span>` : esc(summaryText(a.summary))}</div></div>
      <div class="count">${latestCount(a.summary)}<span>aircraft, latest pass</span></div>`;
    li.onclick = () => selectAirport(a.id, true);
    ul.appendChild(li);
  }
}

async function loadAirports() {
  airports = await getJSON('/api/airports');
  renderAirports();
}

// ---------------------------------------------------------------- one airport

function showView(which) {
  $('view-list').classList.toggle('hidden', which !== 'list');
  $('view-airport').classList.toggle('hidden', which !== 'airport');
}

async function selectAirport(id, autoAnalyse) {
  const a = airports.find((x) => x.id === id);
  if (!a) return;
  showView('airport');
  $('a-name').textContent = a.name;
  $('a-country').textContent = `${a.country} · ${a.lat.toFixed(4)}, ${a.lon.toFixed(4)}`;
  const [w, s, e, n] = a.bounds;
  map.flyToBounds([[s, w], [n, e]], { duration: 0.8 });
  currentScene = null;
  features = [];
  imageLayer.clearLayers();
  boxLayer.clearLayers();
  $('planes').innerHTML = '';
  $('scenes').innerHTML = '';
  $('status').textContent = '';
  lastLogLen = -1;
  await refreshAirport(id);
  loadOutline(id);
  // Pressing an airport analyses it, unless that already happened in the last 12 hours.
  const last = current.summary.last_processed;
  const fresh = last && (Date.now() / 1000 - last) < 12 * 3600;
  if (autoAnalyse && !busy(current.job) && !fresh) startAnalysis();
  else if (busy(current.job)) startPolling();
}

async function loadOutline(id) {
  outlineLayer.clearLayers();
  try {
    const f = await getJSON(`/api/airports/${id}/outline`);
    if (f.geometry) outlineLayer.addData(f);
    $('outline-src').textContent = `Search area: ${f.properties.source}`;
  } catch (e) { console.error(e); }
}

async function refreshAirport(id) {
  current = await getJSON(`/api/airports/${id}`);
  renderScenes();
  renderJob(current.job);
  const ok = current.scenes.filter((s) => s.status === 'done');
  if (!currentScene && ok.length) {
    const pick = ok.find((s) => s.sensor === 'S2') || ok[0];
    selectScene(pick.key);
  }
}

function renderScenes() {
  const ul = $('scenes');
  ul.innerHTML = '';
  if (!current.scenes.length) {
    ul.innerHTML = '<li class="off"><span class="meta">No passes analysed yet.</span></li>';
    return;
  }
  for (const s of current.scenes) {
    const li = document.createElement('li');
    const usable = s.status === 'done';
    li.className = (s.key === currentScene ? 'sel ' : '') + (usable ? '' : 'off');
    const extra = s.status === 'done' ? '' : `<span class="tag">${esc(s.status)}</span>`;
    li.innerHTML = `<span class="badge ${s.sensor}">${s.sensor}</span>
      <div><div>${esc(fmtDate(s.datetime))} ${extra}</div>
      <div class="meta">${esc(s.note || '')}${s.sensor === 'S2' && s.clear_fraction != null ? ` · airfield ${(s.clear_fraction * 100).toFixed(0)}% clear` : ''}</div></div>
      <div class="count">${usable ? s.n_planes : ''}<span>${usable ? 'aircraft' : ''}</span></div>`;
    if (s.overlay_url) li.onclick = () => selectScene(s.key);
    ul.appendChild(li);
  }
}

function popupHtml(p) {
  const s2 = p.sensor === 'S2';
  const contrast = s2
    ? `${p.contrast > 0 ? 'brighter' : 'darker'} than pavement (${Math.abs(p.contrast).toFixed(3)} reflectance)`
    : `${p.contrast.toFixed(1)} dB above the tarmac`;
  return `<div class="popup">
    ${p.chip_url ? `<img src="${esc(p.chip_url)}" alt="aircraft close-up">` : ''}
    <table>
      <tr><td>#</td><td><b>${p.num}</b> · ${esc(NAMES[p.sensor])}</td></tr>
      <tr><td>Size</td><td><b>~${p.size_m.toFixed(0)} m</b> (${esc(p.size_class)})${s2 ? '' : ' – radar size is rough'}</td></tr>
      <tr><td>Contrast</td><td>${esc(contrast)}</td></tr>
      <tr><td>Confidence</td><td>${(p.confidence * 100).toFixed(0)}% (SNR ${p.snr})</td></tr>
      <tr><td>Other satellite</td><td>${p.corroborated ? `also seen by ${p.sensor === 'S2' ? 'Sentinel-1' : 'Sentinel-2'} within 3 days` : 'not confirmed'}</td></tr>
      <tr><td>Same spot</td><td>${p.persistent ? `occupied on ${p.persistent} other pass(es)` : 'not on other passes'}</td></tr>
      <tr><td>Seen</td><td>${esc(fmtDate(p.datetime))}</td></tr>
      <tr><td>Position</td><td>${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}</td></tr>
    </table></div>`;
}

async function selectScene(key) {
  currentScene = key;
  renderScenes();
  const s = current.scenes.find((x) => x.key === key);
  imageLayer.clearLayers();
  if (s && s.overlay_url && s.bounds) {
    const [w, so, e, n] = s.bounds;
    imageLayer.addLayer(L.imageOverlay(s.overlay_url, [[so, w], [n, e]], { opacity: 1 }));
  }
  const fc = await getJSON(`/api/airports/${current.id}/detections?scene=${encodeURIComponent(key)}`);
  features = fc.features;
  renderPlanes(s);
}

function renderPlanes(scene) {
  boxLayer.clearLayers();
  const minConf = Number($('o-conf').value || 0);
  const shown = features.filter((f) => f.properties.confidence >= minConf);
  const ul = $('planes');
  ul.innerHTML = '';
  for (const f of shown) {
    const p = f.properties;
    const ring = f.geometry.coordinates[0];
    const bounds = [[ring[0][1], ring[0][0]], [ring[2][1], ring[2][0]]];
    const col = COLOURS[p.size_class] || '#fff';
    const rect = L.rectangle(bounds, { color: col, weight: 2, fill: true, fillOpacity: 0.05, opacity: 0.95,
      dashArray: p.confidence < 0.6 ? '4 3' : null });
    rect.bindTooltip(String(p.num), { permanent: true, direction: 'right', className: 'box-num', offset: [4, 0] });
    rect.bindPopup(popupHtml(p), { maxWidth: 280 });
    f._rect = rect;
    boxLayer.addLayer(rect);

    const li = document.createElement('li');
    li.innerHTML = `${p.chip_url ? `<img loading="lazy" src="${esc(p.chip_url)}" alt="">` : '<img alt="">'}
      <div><div><b>#${p.num}</b> ~${p.size_m.toFixed(0)} m <span style="color:${col}">■</span> ${esc(p.size_class)}
        ${p.corroborated ? `<span class="tag ok">${p.sensor === 'S2' ? 'S1' : 'S2'} ✓</span>` : ''}
        ${p.persistent ? '<span class="tag">same spot before</span>' : ''}</div>
      <div class="meta">confidence ${(p.confidence * 100).toFixed(0)}% · ${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}</div></div>`;
    li.onclick = () => {
      map.setView(rect.getBounds().getCenter(), 17);
      rect.openPopup();
    };
    ul.appendChild(li);
  }
  if (scene) {
    $('scene-info').innerHTML = `${esc(NAMES[scene.sensor])} · ${esc(fmtDate(scene.datetime))} · <b>${shown.length}</b> aircraft`
      + (shown.length !== features.length ? ` (${features.length - shown.length} below min confidence)` : '')
      + (scene.annotated_url ? ` · <a href="${esc(scene.annotated_url)}" target="_blank">annotated image</a>` : '');
  }
}

// ---------------------------------------------------------------- analysis jobs

function renderJob(job) {
  const running = busy(job);
  $('go').disabled = running;
  const r = job && job.report;
  if (!job) { $('job-state').textContent = ''; return; }
  $('job-state').textContent = running
    ? `${job.state}… ${r ? `${r.processed + r.cloudy + r.nodata + r.failed + r.already_done} passes done, ${r.planes} aircraft` : ''}`
    : (job.error ? `error: ${job.error}` : (job.state === 'done' ? 'finished' : ''));
  if (r && r.log.length !== lastLogLen) {
    lastLogLen = r.log.length;
    $('status').textContent = r.log.slice(-12).join('\n');
    $('status').scrollTop = 1e9;
  }
}

async function startAnalysis() {
  if (!current) return;
  const sensors = [];
  if ($('o-s2').checked) sensors.push('S2');
  if ($('o-s1').checked) sensors.push('S1');
  try {
    const job = await getJSON(`/api/airports/${current.id}/analyse`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start: $('o-start').value, end: $('o-end').value,
        max_scenes: Number($('o-max').value || 4), max_cloud: Number($('o-cloud').value || 60),
        sensors, reprocess: $('o-re').checked,
      }),
    });
    current.job = job;
    renderJob(job);
  } catch (e) { $('job-state').textContent = 'error: ' + e.message; return; }
  startPolling();
  loadAirports();
}

function startPolling() {
  if (polling) return;
  let seen = -1;
  polling = setInterval(async () => {
    if (!current) return;
    try {
      const job = await getJSON(`/api/airports/${current.id}/job`);
      renderJob(job);
      const r = job && job.report;
      const done = r ? r.processed + r.cloudy + r.nodata + r.failed : 0;
      if (done !== seen) { seen = done; await refreshAirport(current.id); loadOutline(current.id); }
      if (!busy(job)) {
        clearInterval(polling); polling = null;
        await refreshAirport(current.id);
        if (currentScene) selectScene(currentScene);
        loadAirports();
      }
    } catch (e) { console.error(e); }
  }, 2500);
}

$('go').onclick = startAnalysis;
$('o-conf').onchange = () => renderPlanes(current && current.scenes.find((s) => s.key === currentScene));
$('back').onclick = () => {
  current = null; currentScene = null;
  imageLayer.clearLayers(); boxLayer.clearLayers(); outlineLayer.clearLayers();
  if (polling) { clearInterval(polling); polling = null; }
  showView('list');
  loadAirports().then(() => {
    const b = L.latLngBounds(airports.map((a) => [a.lat, a.lon]));
    if (b.isValid()) map.flyToBounds(b.pad(0.2), { duration: 0.8 });
  });
};

(function init() {
  const today = new Date();
  $('o-end').value = today.toISOString().slice(0, 10);
  $('o-start').value = new Date(today - 30 * 864e5).toISOString().slice(0, 10);
  loadAirports().then(() => {
    const b = L.latLngBounds(airports.map((a) => [a.lat, a.lon]));
    if (b.isValid()) map.fitBounds(b.pad(0.2));
  }).catch(console.error);
  setInterval(() => { if (!current) loadAirports().catch(console.error); }, 10000);
})();
