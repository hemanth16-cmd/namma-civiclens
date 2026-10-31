// Intelligence studio (staff-authenticated server mode + illustrative local fallback).
// Tabs: priority queue, hotspot outlook, relationship graph, cognitive map, scenario lab, readiness.
// In server mode every tab calls the authenticated backend API; in local/demo mode it shows labelled
// illustrative data so the UI is useful even without a running backend.

import {api, esc, categories, PRIORITY_WEIGHTS} from '../services.js';
import {workspaceRecords} from '../server-records.js';
import {icon, workspaceShell, heading, notice, panel, panelHead, pill, priorityPill, toast, fmtDate} from '../ui.js';
import {viewer} from '../state.js';
import {state} from '../state.js';
import {rerender} from '../router.js';
import {staffSession} from '../staff-session.js';
import {loadApiConfig, DEFAULT_BASE_URL} from '../api-config.js';
import {createApiClient} from '../api-client.js';

const TABS = [['priority', 'Priority queue'], ['hotspots', 'Hotspot outlook'], ['graph', 'Relationship graph'], ['fcm', 'Cognitive map'], ['scenario', 'Scenario lab'], ['readiness', 'Model readiness']];
let activeTab = sessionStorage.getItem('ncl-intel-tab') || 'priority';

const STATUS = {
  impl: ['Implemented and tested', 'check', 'green'],
  pending: ['Implemented; device/browser verification pending', 'eye', 'navy'],
  demo: ['Illustrative demo', 'flag', 'coral'],
  planned: ['Proposed; not connected or trained', 'clock', 'neutral']
};
const statusPillFor = key => pill(STATUS[key][0], STATUS[key][2], STATUS[key][1]);

// ---------- Server mode helpers ----------

/** Returns {http, token} when a staff session is active, or null. */
async function serverCtx() {
  const session = staffSession.get();
  if (!session || !session.token) return null;
  const config = await loadApiConfig();
  if (config.mode !== 'backend') return null;
  const http = createApiClient({baseUrl: config.baseUrl || DEFAULT_BASE_URL});
  return {http, token: session.token};
}

// ---------- Render entry point ----------

export async function renderIntelligence(root) {
  const records = await workspaceRecords(viewer());
  const isServer = !!(staffSession.get());
  const modeBadge = isServer
    ? pill('Live backend', 'green', 'check')
    : pill('Demo mode', 'coral', 'flag');

  const tabs = '<div class="tablist" role="tablist" aria-label="Intelligence views">' +
    TABS.map(([id, label]) =>
      '<button type="button" role="tab" id="tab-' + id + '" aria-controls="intel-panel" aria-selected="' + (activeTab === id) + '" tabindex="' + (activeTab === id ? 0 : -1) + '" data-tab="' + id + '">' + label + '</button>'
    ).join('') + '</div>';

  const serverNote = isServer
    ? notice('Connected to the CivicLens backend. Tabs load live data from authenticated API endpoints.', 'info', 'check')
    : notice('Showing illustrative demo data. Sign in as staff and set api-config.json mode to "backend" to connect to a live server.', 'warn', 'alert');

  root.innerHTML = workspaceShell({title: 'Intelligence studio', body:
    heading({eyebrow: 'Decision support · ' + (isServer ? 'live backend' : 'illustrative'), title: 'Intelligence studio', lead: 'Each technique has one defined job, a required evidence base and an honest status. Nothing here is a validated city forecast or an automated detection.'}) +
    serverNote +
    tabs +
    '<section id="intel-panel" role="tabpanel" aria-labelledby="tab-' + activeTab + '" class="intel-panel"><div class="intel-loading">' + icon('refresh') + ' Loading\u2026</div></section>'
  });

  root.querySelectorAll('[role=tab]').forEach(tab => {
    tab.onclick = () => selectTab(tab.dataset.tab);
    tab.onkeydown = e => {
      const ids = TABS.map(t => t[0]);
      const i = ids.indexOf(tab.dataset.tab);
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        const next = ids[(i + (e.key === 'ArrowRight' ? 1 : ids.length - 1)) % ids.length];
        selectTab(next);
        root.querySelector('#tab-' + next).focus();
      }
    };
  });

  await loadTab(root, activeTab, records);
}

function selectTab(id) {
  activeTab = id;
  try { sessionStorage.setItem('ncl-intel-tab', id); } catch { /* optional */ }
  rerender();
}

async function loadTab(root, tab, records) {
  const panel = root.querySelector('#intel-panel');
  if (!panel) return;
  panel.innerHTML = '<div class="intel-loading">' + icon('refresh') + ' Loading\u2026</div>';
  try {
    let html = '';
    switch (tab) {
      case 'hotspots':  html = await hotspotsTab(); break;
      case 'graph':     html = await graphTab(); break;
      case 'fcm':       html = await fcmTab(); break;
      case 'scenario':  html = await scenarioTab(); break;
      case 'readiness': html = readiness(); break;
      default:          html = await priorityTab(records); break;
    }
    panel.innerHTML = html;
    wireTab(root);
  } catch (err) {
    panel.innerHTML = '<div class="notice notice-warn" role="alert">' + icon('alert', 'notice-icon') + '<div>Failed to load tab: ' + esc(String(err.message || err)) + '</div></div>';
  }
}

// ---------- Priority Queue tab ----------

async function priorityTab(records) {
  const ctx = await serverCtx();
  if (ctx) {
    // Live: load from backend
    let data;
    try {
      data = await ctx.http.getIntelligencePriorityQueue(ctx.token, {limit: 100});
    } catch (e) {
      return serverError('priority queue', e) + priorityExplanation();
    }
    const items = Array.isArray(data && data.queue) ? data.queue : (Array.isArray(data) ? data : []);
    const rows = items.map(r => {
      const score = typeof r.score === 'number' ? r.score.toFixed(1) : (r.priority_score || '—');
      const band = r.band || r.priority_band || 'medium';
      const cat = esc(r.category || '');
      const area = esc(r.area || r.neighbourhood || '');
      const id = esc(r.id || r.complaint_id || '');
      const title = esc(r.title || r.description || cat);
      const days = typeof r.days_open === 'number' ? r.days_open : '—';
      const recur = typeof r.recurrence_count === 'number' ? r.recurrence_count : '—';
      const status = esc(r.status || '');
      return '<tr>' +
        '<td><a href="#/staff/' + id + '">' + title + '</a><small class="code-inline">' + id + '</small></td>' +
        '<td>' + cat + '</td>' +
        '<td>' + area + '</td>' +
        '<td>' + days + '<small>days open</small></td>' +
        '<td>' + recur + '<small>recurrences</small></td>' +
        '<td><strong>' + score + '</strong> ' + priorityPill(band) + '</td>' +
        '<td>' + esc(status) + '</td>' +
      '</tr>';
    }).join('');
    const header = items.length > 0
      ? '<p class="micro">Showing ' + items.length + ' live complaint(s) scored by the backend priority engine, highest first.</p>'
      : '<p class="micro">No complaints in the database yet.</p>';
    return '<div class="split"><section class="panel panel-flush">' +
      panelHead('Live priority queue', statusPillFor('impl'), 'Data from backend API') +
      header +
      '<div class="table-wrap"><table class="data-table"><caption class="sr-only">Priority queue from backend</caption>' +
      '<thead><tr><th scope="col">Case</th><th scope="col">Category</th><th scope="col">Area</th><th scope="col">Age</th><th scope="col">Recurrences</th><th scope="col">Score</th><th scope="col">Status</th></tr></thead>' +
      '<tbody>' + (rows || '<tr><td colspan="7">No complaints scored yet.</td></tr>') + '</tbody>' +
      '</table></div></section>' +
      priorityExplanation() + '</div>';
  }

  // Demo fallback
  const ranked = records.filter(r => r.priority).sort((a, b) => b.priority.score - a.priority.score);
  const rows = ranked.map(r => {
    const p = r.priority;
    return '<tr>' +
      '<td><a href="#/issue/' + esc(r.id) + '">' + esc(r.title) + '</a><small class="code-inline">' + esc(r.id) + '</small></td>' +
      '<td>' + p.contributions.severity + '<small>sev. input ' + Math.round(p.inputs.severity * 100) + '%</small></td>' +
      '<td>' + p.contributions.exposure + '<small>' + esc(p.exposureSource) + '</small></td>' +
      '<td>' + p.contributions.unresolved + '<small>' + p.daysOpen + ' days</small></td>' +
      '<td>' + p.contributions.recurrence + '<small>' + (r.recurrence || 0) + ' recurrence</small></td>' +
      '<td><strong>' + p.score + '</strong> ' + priorityPill(p.band) + '</td>' +
      '<td>' + esc(p.confidence) + (p.flag ? '<small class="flag">' + icon('alert', 'mini-icon') + ' ' + esc(p.flag) + '</small>' : '') + '</td>' +
    '</tr>';
  }).join('');
  return '<div class="split">' +
    '<section class="panel panel-flush">' +
    panelHead('Explainable priority queue', statusPillFor('impl'), 'Inputs are illustrative') +
    '<div class="table-wrap"><table class="data-table"><caption class="sr-only">Priority contributions per case, highest first</caption>' +
    '<thead><tr><th scope="col">Case</th><th scope="col">Severity pts</th><th scope="col">Exposure pts</th><th scope="col">Unresolved pts</th><th scope="col">Recurrence pts</th><th scope="col">Score</th><th scope="col">Evidence confidence</th></tr></thead>' +
    '<tbody>' + (rows || '<tr><td colspan="7">No cases yet.</td></tr>') + '</tbody></table></div>' +
    '</section>' +
    priorityExplanation() + '</div>';
}

function priorityExplanation() {
  return '<section class="panel">' +
    '<p class="eyebrow">How the score is built</p>' +
    '<h2>Urgency you can explain</h2>' +
    '<p>Score = 100 \u00d7 \u03a3(weight \u00d7 normalised input), using four inputs:</p>' +
    '<ul class="contrib">' +
    '<li><span>Severity from category (initial expert value)</span><strong>' + Math.round(PRIORITY_WEIGHTS.severity * 100) + '%</strong></li>' +
    '<li><span>Exposure (assessed or default assumption)</span><strong>' + Math.round(PRIORITY_WEIGHTS.exposure * 100) + '%</strong></li>' +
    '<li><span>Unresolved duration, capped at 30 days</span><strong>' + Math.round(PRIORITY_WEIGHTS.unresolved * 100) + '%</strong></li>' +
    '<li><span>Recurrence records, capped at 3</span><strong>' + Math.round(PRIORITY_WEIGHTS.recurrence * 100) + '%</strong></li>' +
    '</ul>' +
    '<p class="micro">Weights are initial expert assumptions to be validated against reviewed outcomes. They are not calibrated probabilities.</p>' +
    '<p><strong>Evidence confidence is separate from urgency.</strong> A severe category with weak evidence is flagged for verification rather than quietly ranked low.</p>' +
    '<p class="micro">Scores support authorised decisions. They do not replace emergency procedures.</p>' +
  '</section>';
}

// ---------- Hotspots tab ----------

async function hotspotsTab() {
  const ctx = await serverCtx();
  if (ctx) {
    let data;
    try {
      data = await ctx.http.getIntelligenceHotspots(ctx.token, {radius: 150, topWards: 15});
    } catch (e) {
      return serverError('hotspots', e) + hotspotInfo();
    }
    const historical = Array.isArray(data && data.historical_wards) ? data.historical_wards : [];
    const live = Array.isArray(data && data.live_clusters) ? data.live_clusters : [];
    const pred = Array.isArray(data && data.predictions) ? data.predictions : [];

    const wardRows = historical.slice(0, 15).map(w =>
      '<tr><td><strong>' + esc(w.ward || '') + '</strong></td>' +
      '<td>' + esc(w.zone || '') + '</td>' +
      '<td>' + (w.total_count || 0).toLocaleString() + '</td>' +
      '<td>' + esc(w.top_category || '') + '</td>' +
      '<td>' + (w.top_category_count || 0).toLocaleString() + '</td></tr>'
    ).join('');

    const clusterRows = live.map(c =>
      '<tr><td>' + esc(c.cluster_id || '') + '</td>' +
      '<td>' + (c.complaint_count || 0) + '</td>' +
      '<td>' + esc(c.dominant_category || '') + '</td>' +
      '<td>' + (c.center ? c.center.lat.toFixed(4) + ', ' + c.center.lng.toFixed(4) : '—') + '</td></tr>'
    ).join('');

    return '<div class="stack">' +
      '<section class="panel panel-flush">' +
      panelHead('Historical ward hotspots (top 15)', statusPillFor('impl'), 'From 1.23M BBMP records') +
      '<p class="micro">Ward-level only; historical data has no GPS coordinates. Sorted by total complaint volume.</p>' +
      '<div class="table-wrap"><table class="data-table">' +
      '<thead><tr><th>Ward</th><th>Zone</th><th>Total complaints</th><th>Top category</th><th>Category count</th></tr></thead>' +
      '<tbody>' + (wardRows || '<tr><td colspan="5">No historical data loaded.</td></tr>') + '</tbody>' +
      '</table></div>' +
      (pred.length ? '<p class="micro"><strong>30-day predictions:</strong> ' +
        pred.slice(0, 5).map(p => esc(p.ward) + ' (' + esc(p.category) + '): ' + (p.predicted_rate || '—')).join(' &middot; ') +
        '</p>' : '') +
      '</section>' +
      '<section class="panel panel-flush">' +
      panelHead('Live spatial clusters', live.length > 0 ? statusPillFor('impl') : statusPillFor('pending'), 'From GPS-tagged complaints in DB') +
      '<p class="micro">Clusters are formed from complaints with GPS coordinates using ' + (data && data.radius_m ? data.radius_m : 150) + '\u00a0m radius. Empty when no GPS-tagged complaints exist in the database.</p>' +
      '<div class="table-wrap"><table class="data-table">' +
      '<thead><tr><th>Cluster</th><th>Complaints</th><th>Category</th><th>Centre (lat, lng)</th></tr></thead>' +
      '<tbody>' + (clusterRows || '<tr><td colspan="4">No live clusters (no GPS complaints in database yet).</td></tr>') + '</tbody>' +
      '</table></div>' +
      '</section></div>';
  }

  // Demo fallback
  return '<div class="split">' +
    '<section class="panel">' +
    panelHead('Hotspot outlook', statusPillFor('planned'), 'Planned prediction target') +
    '<p><strong>Defined target:</strong> whether a ward and category will show elevated complaint activity over the next 30 days, compared with its trailing historical baseline.</p>' +
    '<p><strong>Baseline first:</strong> a historical-rate baseline is the first comparison. A supervised model is tested only after chronological evaluation beats it.</p>' +
    '<p class="micro"><strong>Needed:</strong> ward boundaries, timestamped complaint history, cleanup and inspection records, and clear separation between complaint activity and verified physical recurrence.</p>' +
    hotspotInfo() +
    '</section>' +
    '<section class="panel">' +
    panelHead('Sample activity (not measured)', pill('Illustrative', 'coral')) +
    '<figure class="chart" aria-label="Sample activity over 12 days, illustrative only"><div class="chart-bars">' +
    [40, 65, 51, 84, 70, 92, 64, 78, 58, 86, 100, 73].map((b, i) =>
      '<div class="chart-bar" style="height:' + b + '%"><span>' + (i + 1) + '</span></div>'
    ).join('') +
    '</div><figcaption class="micro">Sample values for layout. Not measured complaint activity.</figcaption></figure>' +
    '</section></div>';
}

function hotspotInfo() {
  return '';
}

// ---------- Graph tab ----------

async function graphTab() {
  const ctx = await serverCtx();
  if (ctx) {
    let data;
    try {
      data = await ctx.http.getIntelligenceGraph(ctx.token, {limit: 50});
    } catch (e) {
      return serverError('relationship graph', e) + staticGraph();
    }
    const nodes = Array.isArray(data && data.nodes) ? data.nodes : [];
    const edges = Array.isArray(data && data.edges) ? data.edges : [];
    const counts = data && data.counts ? data.counts : {};

    if (nodes.length === 0) {
      return '<section class="panel">' +
        panelHead('Relationship graph', statusPillFor('impl'), 'Live data from DB') +
        '<p>No complaints in the database yet. The graph will appear once staff-submitted complaints exist.</p>' +
        '<p class="micro">Entity types tracked: Complaint, Ward, Zone, Category, Staff, Cluster.</p>' +
        '<p class="micro">Counts: complaints=' + (counts.complaints || 0) + ', wards=' + (counts.wards || 0) + ', zones=' + (counts.zones || 0) + ', categories=' + (counts.categories || 0) + '.</p>' +
        '</section>';
    }

    const nodeRows = nodes.slice(0, 30).map(n =>
      '<tr><td><strong>' + esc(n.id || '') + '</strong></td>' +
      '<td>' + esc(n.type || '') + '</td>' +
      '<td>' + esc(n.label || '') + '</td></tr>'
    ).join('');
    const edgeRows = edges.slice(0, 30).map(e =>
      '<tr><td>' + esc(e.source || '') + '</td>' +
      '<td>' + esc(e.relation || '') + '</td>' +
      '<td>' + esc(e.target || '') + '</td>' +
      '<td>' + (e.weight || '') + '</td></tr>'
    ).join('');

    return '<div class="stack">' +
      '<section class="panel panel-flush">' +
      panelHead('Relationship graph — live entities', statusPillFor('impl'), 'From database') +
      '<p>Nodes and edges are extracted from real complaints, wards, zones and categories in the database. Connections are for review, not automated action.</p>' +
      '<p class="micro">Totals: ' + nodes.length + ' nodes, ' + edges.length + ' edges (showing up to 30 each).</p>' +
      '</section>' +
      '<section class="panel panel-flush">' +
      '<h3>Nodes (up to 30)</h3>' +
      '<div class="table-wrap"><table class="data-table">' +
      '<thead><tr><th>ID</th><th>Type</th><th>Label</th></tr></thead>' +
      '<tbody>' + (nodeRows || '<tr><td colspan="3">None.</td></tr>') + '</tbody>' +
      '</table></div>' +
      '</section>' +
      '<section class="panel panel-flush">' +
      '<h3>Edges (up to 30)</h3>' +
      '<div class="table-wrap"><table class="data-table">' +
      '<thead><tr><th>Source</th><th>Relation</th><th>Target</th><th>Weight</th></tr></thead>' +
      '<tbody>' + (edgeRows || '<tr><td colspan="4">None.</td></tr>') + '</tbody>' +
      '</table></div>' +
      '</section></div>';
  }

  // Demo fallback: static schema diagram
  return staticGraph();
}

function staticGraph() {
  const nodes = [
    ['Complaint', 'A submitted report keeps its own evidence and public reference.', 15, 130],
    ['Incident', 'Possibly related complaints need human review before any link is confirmed.', 220, 130],
    ['Location', 'Neighbourhood, device GPS or EXIF location gives spatial context.', 440, 45],
    ['Evidence', 'Original and repair evidence keep separate provenance.', 440, 130],
    ['Work order', 'Assigned team, target date and public progress attach to the case.', 440, 230],
    ['Repair', 'Administrative completion is distinct from community verification.', 635, 230],
    ['Recurrence', 'Repeated observations can indicate that a repair needs reinspection.', 635, 80]
  ];
  const edges = [[0, 1], [1, 2], [1, 3], [1, 4], [4, 5], [5, 6], [6, 1]];
  const pos = i => [nodes[i][2] + 65, nodes[i][3] + 18];
  return '<section class="panel">' +
    panelHead('Relationship graph', statusPillFor('demo'), 'Stable IDs in the planned relational model') +
    '<p>Connections are <strong>suggestions for human review</strong>. Nearby reports are never merged automatically. A PostgreSQL/PostGIS link table is the planned store; a separate graph database is optional.</p>' +
    '<div class="graph-wrap"><svg viewBox="0 0 800 320" role="img" aria-labelledby="graph-title graph-desc" class="graph">' +
    '<title id="graph-title">Civic relationship graph</title>' +
    '<desc id="graph-desc">Complaint, incident, location, evidence, work order, repair and recurrence, connected for review.</desc>' +
    edges.map(([a, b]) => '<line x1="' + pos(a)[0] + '" y1="' + pos(a)[1] + '" x2="' + pos(b)[0] + '" y2="' + pos(b)[1] + '" />').join('') +
    nodes.map(([label, , x, y], i) => '<g role="button" tabindex="0" data-node="' + i + '" aria-label="' + label + ': show what it means"><rect x="' + x + '" y="' + y + '" width="130" height="36" rx="8" /><text x="' + (x + 65) + '" y="' + (y + 22) + '" text-anchor="middle">' + label + '</text></g>').join('') +
    '</svg></div>' +
    '<p id="node-info" class="notice notice-info" role="status">Select a node to see what it represents in the workflow.</p>' +
  '</section>';
}

// ---------- FCM tab ----------

const FCM_CONCEPTS = ['Severity', 'Exposure', 'Maintenance delay', 'Repair quality', 'Recurrence', 'Urgency'];
const FCM_W = {
  'Severity>Urgency': 0.6, 'Exposure>Urgency': 0.5, 'Maintenance delay>Recurrence': 0.5,
  'Repair quality>Recurrence': -0.6, 'Recurrence>Urgency': 0.4, 'Maintenance delay>Urgency': 0.3
};
function fcmRun(init) {
  const n = FCM_CONCEPTS.length;
  const w = Array.from({length: n}, () => Array(n).fill(0));
  for (const [k, val] of Object.entries(FCM_W)) {
    const [from, to] = k.split('>');
    w[FCM_CONCEPTS.indexOf(from)][FCM_CONCEPTS.indexOf(to)] = val;
  }
  let a = init.slice();
  const f = x => 1 / (1 + Math.exp(-5 * (x - 0.5)));
  for (let t = 0; t < 15; t++) {
    a = a.map((aj, j) => {
      let s = aj;
      for (let i = 0; i < n; i++) s += w[i][j] * a[i];
      return f(s);
    });
  }
  return a;
}

async function fcmTab() {
  const ctx = await serverCtx();
  let liveSection = '';
  if (ctx) {
    try {
      const data = await ctx.http.getIntelligenceFcm(ctx.token);
      const activations = data && data.activations ? data.activations : {};
      const meta = data && data.meta ? data.meta : {};
      const conceptRows = Object.entries(activations).map(([concept, val]) =>
        '<li><span>' + esc(concept) + '</span>' +
        '<span class="fcm-bar" aria-hidden="true"><i style="width:' + Math.round((typeof val === 'number' ? val : 0) * 100) + '%"></i></span>' +
        '<strong>' + (typeof val === 'number' ? val.toFixed(2) : '—') + '</strong></li>'
      ).join('');
      liveSection = '<section class="panel">' +
        '<h2>Live activations from backend ' + pill('Live', 'green', 'check') + '</h2>' +
        '<p class="micro">Derived from real complaint data in the database. Complaint count: ' + (meta.complaint_count || 0) + '. Computed at: ' + esc(meta.computed_at || '—') + '</p>' +
        '<ul class="fcm-list">' + (conceptRows || '<li>No data.</li>') + '</ul>' +
        '</section>';
    } catch (e) {
      liveSection = serverError('FCM live activations', e);
    }
  }

  return '<div class="split">' +
    '<section class="panel form">' +
    panelHead('Cognitive map (Fuzzy Cognitive Mapping)', statusPillFor('impl')) +
    '<p class="micro">FCM here means expert-informed influence mapping. It is not Firebase Cloud Messaging, and it does not establish causality or produce calibrated probabilities.</p>' +
    '<div class="stack" id="fcm-controls">' +
    FCM_CONCEPTS.slice(0, 4).map((c, i) =>
      '<div class="field"><label for="fcm-' + i + '">' + esc(c) + ' <span class="optional" id="fcm-v-' + i + '">0.50</span></label>' +
      '<input id="fcm-' + i + '" type="range" min="0" max="1" step="0.05" value="0.5" data-fcm="' + i + '"></div>'
    ).join('') +
    '</div>' +
    '<p class="micro">Starting activations for the four inputs; recurrence and urgency are computed. Sliders are illustrative.</p>' +
    '<button type="button" class="btn btn-secondary mt" id="fcm-scenario-btn">' + icon('refresh') + '<span>Run live scenario</span></button>' +
    '</section>' +
    '<section class="panel">' +
    (liveSection || '') +
    '<h2>Browser simulation (15 steps)</h2>' +
    '<div id="fcm-out" class="fcm-out" aria-live="polite"></div>' +
    '<details class="explain"><summary>Assumed influence weights and their origin</summary>' +
    '<ul class="contrib">' + Object.entries(FCM_W).map(([k, v]) =>
      '<li><span>' + esc(k.replace('>', ' \u2192 ')) + '</span><strong>' + (v > 0 ? '+' : '') + v + '</strong></li>'
    ).join('') + '</ul>' +
    '<p class="micro">Origin: illustrative expert assumptions for discussion. To be elicited from domain staff and tested against reviewed history before use.</p>' +
    '</details>' +
    '</section></div>';
}

// ---------- Scenario / Monte Carlo tab ----------

async function scenarioTab() {
  const ctx = await serverCtx();
  let defaults = {base_rate: 100, capacity: null, n_runs: 2000, seed: 42};
  let defaultsNote = '';
  if (ctx) {
    try {
      const d = await ctx.http.getSimulationDefaults(ctx.token);
      defaults = Object.assign(defaults, d);
      defaultsNote = '<p class="micro">Defaults loaded from backend (base rate derived from historical BBMP data and live DB).</p>';
    } catch (e) {
      defaultsNote = '<p class="micro">Could not load backend defaults: ' + esc(String(e.message || e)) + '. Using built-in defaults.</p>';
    }
  }

  const baseRate = typeof defaults.base_rate === 'number' ? Math.round(defaults.base_rate) : 100;

  return '<div class="split">' +
    '<section class="panel form" aria-labelledby="sc-title">' +
    panelHead('What if maintenance became proactive?', statusPillFor('impl'), 'Monte Carlo simulation') +
    defaultsNote +
    '<div class="field"><label for="sc-base">Baseline recurrences: <strong id="sc-base-v">' + baseRate + '</strong></label>' +
    '<input id="sc-base" type="range" min="10" max="500" value="' + baseRate + '"></div>' +
    '<div class="field"><label for="sc-budget">Preventive share of effort: <strong id="sc-budget-v">50</strong>%</label>' +
    '<input id="sc-budget" type="range" min="0" max="100" value="50"></div>' +
    '<div class="field"><label for="sc-delay">Average response delay: <strong id="sc-delay-v">14</strong> days</label>' +
    '<input id="sc-delay" type="range" min="1" max="45" value="14"></div>' +
    '<button type="button" class="btn btn-primary" id="sc-run">' + icon('refresh') + '<span>Run simulation</span></button>' +
    '<div class="notice notice-info mt" role="note">' + icon('alert', 'notice-icon') + '<div><strong>Labelled assumptions:</strong> preventive effort reduces recurrence by up to 45%; each delay day adds 0.6 recurrences; outcomes vary by \u00b120%. Seeded pseudo-random draws. Exploratory only: input distributions have not been estimated from reliable history.</div></div>' +
    '</section>' +
    '<section class="panel" aria-live="polite"><div id="sc-out" class="sc-out"><h2>Explore the trade-off</h2><p>Adjust the assumptions and run the simulation to see the spread of outcomes.</p></div></section>' +
  '</div>';
}

// ---------- Readiness tab ----------

function readiness() {
  const rows = [
    ['Road-damage vision (YOLO11n proposed)', 'Detect potholes and cracks in images', 'Labelled road-damage data and evaluation on held-out images', 'planned', 'classificationService returns unavailable'],
    ['Waste-specific vision', 'Detect visible waste accumulation (not road-only)', 'Waste-specific examples and classes', 'planned', 'None; reviewers classify manually'],
    ['Before/after repair assessment', 'Apparent change with uncertainty; never proof of repair', 'Matched locations, usable images and reviewed outcomes', 'pending', 'Side-by-side evidence and manual verification states are implemented'],
    ['Incident linking', 'Suggest duplicates or recurrence for review', 'Spatial, temporal, category and visual evidence', 'planned', 'Recurrence records only; no automatic merging'],
    ['Knowledge graph', 'Connect complaints, incidents, evidence, repairs', 'Stable entity IDs and explicit relationships', 'impl', 'API endpoint /intelligence/graph — live DB entities'],
    ['Priority engine', 'Rank inspection needs with explained contributions', 'Severity, exposure, age and recurrence inputs', 'impl', 'Formula and tests implemented; live queue at /intelligence/priority/queue'],
    ['Hotspot detection', 'Ward-level historical + live GPS spatial clustering', 'Historical BBMP data (1.23M rows) + GPS complaints', 'impl', 'API endpoint /intelligence/hotspots; 484 wards indexed'],
    ['FCM / Cognitive mapping', 'Expert-informed influence propagation', 'Expert elicitation + live complaint metrics', 'impl', 'API endpoints /fcm/live and /fcm/scenario'],
    ['Prediction (30-day ward/category activity)', 'Estimate elevated complaint activity', 'Time-stamped history and a measurable target', 'impl', 'Baseline model at /intelligence/predictions using BBMP history'],
    ['Monte Carlo scenarios', 'Compare uncertain intervention scenarios', 'Justified distributions or labelled assumptions', 'impl', 'Backend at /intelligence/simulations/run (Poisson/NB)'],
    ['Video dumping-action detection', 'Not planned for this release', 'Validated model, consent and retention rules', 'planned', 'videoAnalysisAdapter reports unavailable'],
    ['Push notifications (FCM)', 'Deliver alerts to devices', 'Configured project, consent and delivery policy', 'planned', 'notificationAdapter reports not configured']
  ];
  return '<section class="panel panel-flush">' +
    panelHead('Model and integration readiness', '', 'Status for every technique') +
    '<div class="table-wrap"><table class="data-table"><caption class="sr-only">Readiness of each intelligence technique</caption>' +
    '<thead><tr><th scope="col">Technique</th><th scope="col">One defined job</th><th scope="col">Evidence needed</th><th scope="col">Status</th><th scope="col">Adapter or current behaviour</th></tr></thead>' +
    '<tbody>' + rows.map(([a, b, c, s, d]) =>
      '<tr><td><strong>' + esc(a) + '</strong></td><td>' + esc(b) + '</td><td>' + esc(c) + '</td><td>' + statusPillFor(s) + '</td><td>' + esc(d) + '</td></tr>'
    ).join('') + '</tbody></table></div>' +
    '<p class="micro">Status vocabulary: Implemented and tested \u00b7 Implemented; device/browser verification pending \u00b7 Illustrative demo \u00b7 Proposed; not connected or trained.</p>' +
  '</section>';
}

// ---------- Error helper ----------

function serverError(name, err) {
  const msg = err && err.message ? err.message : String(err);
  return '<div class="notice notice-warn" role="alert">' + icon('alert', 'notice-icon') +
    '<div>Could not load ' + esc(name) + ' from backend: ' + esc(msg) + ' \u2014 showing demo data below.</div></div>';
}

// ---------- Tab interaction wiring ----------

function wireTab(root) {
  if (activeTab === 'graph') {
    root.querySelectorAll('[data-node]').forEach(n => {
      const text = {
        0: 'A submitted report keeps its own evidence and public reference.',
        1: 'Possibly related complaints need human review before any link is confirmed.',
        2: 'Neighbourhood, device GPS or EXIF location gives spatial context.',
        3: 'Original and repair evidence keep separate provenance.',
        4: 'Assigned team, target date and public progress attach to the case.',
        5: 'Administrative completion is distinct from community verification.',
        6: 'Repeated observations can indicate that a repair needs reinspection.'
      };
      const labels = ['Complaint', 'Incident', 'Location', 'Evidence', 'Work order', 'Repair', 'Recurrence'];
      const show = () => {
        const info = root.querySelector('#node-info');
        if (info) info.textContent = labels[+n.dataset.node] + ': ' + text[+n.dataset.node];
      };
      n.onclick = show;
      n.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); show(); } };
    });
  }

  if (activeTab === 'fcm') {
    const paint = () => {
      const vals = [0, 1, 2, 3].map(i => Number(document.getElementById('fcm-' + i) && document.getElementById('fcm-' + i).value || 0.5));
      vals.forEach((v, i) => { const el = document.getElementById('fcm-v-' + i); if (el) el.textContent = v.toFixed(2); });
      const settled = fcmRun([...vals, 0, 0]);
      const out = document.getElementById('fcm-out');
      if (out) out.innerHTML = '<ul class="fcm-list">' +
        FCM_CONCEPTS.map((c, i) =>
          '<li><span>' + esc(c) + '</span>' +
          '<span class="fcm-bar" aria-hidden="true"><i style="width:' + Math.round(settled[i] * 100) + '%"></i></span>' +
          '<strong>' + settled[i].toFixed(2) + '</strong></li>'
        ).join('') +
        '</ul><p class="micro">Values are relative activations between 0 and 1. They describe the assumed model, not measured probabilities.</p>';
    };
    root.querySelectorAll('[data-fcm]').forEach(i => i.oninput = paint);
    paint();

    // Live scenario button
    const scenBtn = root.querySelector('#fcm-scenario-btn');
    if (scenBtn) {
      scenBtn.onclick = async () => {
        const ctx = await serverCtx();
        if (!ctx) { toast('Sign in as staff to run a live scenario.'); return; }
        const vals = [0, 1, 2, 3].map(i => Number(document.getElementById('fcm-' + i) && document.getElementById('fcm-' + i).value || 0.5));
        const base = {};
        FCM_CONCEPTS.slice(0, 4).forEach((c, i) => { base[c] = vals[i]; });
        try {
          scenBtn.disabled = true;
          const data = await ctx.http.runFcmScenario(ctx.token, {base, deltas: {}});
          const activations = data && data.settled_activations ? data.settled_activations : {};
          const out = document.getElementById('fcm-out');
          if (out) out.innerHTML = '<p class="micro">' + pill('Live scenario result', 'green', 'check') + ' Computed by backend, ' + (data.iterations || 15) + ' iterations.</p>' +
            '<ul class="fcm-list">' + Object.entries(activations).map(([c, v]) =>
              '<li><span>' + esc(c) + '</span>' +
              '<span class="fcm-bar" aria-hidden="true"><i style="width:' + Math.round((typeof v === 'number' ? v : 0) * 100) + '%"></i></span>' +
              '<strong>' + (typeof v === 'number' ? v.toFixed(2) : '—') + '</strong></li>'
            ).join('') + '</ul>';
          toast('Live FCM scenario computed.');
        } catch (e) {
          toast('FCM scenario error: ' + (e.message || String(e)));
        } finally {
          scenBtn.disabled = false;
        }
      };
    }
  }

  if (activeTab === 'scenario') {
    const bud = root.querySelector('#sc-budget');
    const del = root.querySelector('#sc-delay');
    const base = root.querySelector('#sc-base');
    if (bud) bud.oninput = () => { const el = root.querySelector('#sc-budget-v'); if (el) el.textContent = bud.value; };
    if (del) del.oninput = () => { const el = root.querySelector('#sc-delay-v'); if (el) el.textContent = del.value; };
    if (base) base.oninput = () => { const el = root.querySelector('#sc-base-v'); if (el) el.textContent = base.value; };

    const runBtn = root.querySelector('#sc-run');
    if (runBtn) {
      runBtn.onclick = async () => {
        const b = bud ? +bud.value : 50;
        const d = del ? +del.value : 14;
        const br = base ? +base.value : 100;
        const ctx = await serverCtx();

        if (ctx) {
          // Try backend Monte Carlo
          try {
            runBtn.disabled = true;
            const result = await ctx.http.runSimulation(ctx.token, {baseRate: br, capacity: null, nRuns: 2000, seed: 42});
            const out = root.querySelector('#sc-out');
            if (out && result) {
              const p50 = typeof result.p50 === 'number' ? Math.round(result.p50) : '—';
              const p05 = typeof result.p05 === 'number' ? Math.round(result.p05) : '—';
              const p95 = typeof result.p95 === 'number' ? Math.round(result.p95) : '—';
              const mean = typeof result.mean === 'number' ? Math.round(result.mean) : '—';
              const nRuns = result.n_runs || 2000;
              out.innerHTML = '<h2>Backend simulation result ' + pill('Live', 'green', 'check') + '</h2>' +
                '<p class="score"><strong>' + p50 + '</strong> <span>expected recurrences (median P50)</span></p>' +
                '<p>90% of ' + nRuns + ' draws fall between <strong>' + p05 + '</strong> and <strong>' + p95 + '</strong>.</p>' +
                '<p>Mean: <strong>' + mean + '</strong>.</p>' +
                '<p class="micro">Backend Monte Carlo (Poisson/Negative Binomial) \u00b7 base rate from BBMP history \u00b7 not a government forecast.</p>';
              toast('Backend simulation complete.');
            }
            return;
          } catch (e) {
            toast('Backend simulation failed, running in browser: ' + (e.message || String(e)));
          } finally {
            runBtn.disabled = false;
          }
        }

        // Browser fallback
        const mean = br * (1 - 0.45 * b / 100) + d * 0.6;
        let s = 42;
        const draws = Array.from({length: 1000}, () => {
          s = (s * 1664525 + 1013904223) >>> 0;
          return mean * (0.8 + (s / 4294967296) * 0.4);
        }).sort((x, y) => x - y);
        const avg = Math.round(draws.reduce((x, y) => x + y, 0) / draws.length);
        const out = root.querySelector('#sc-out');
        if (out) out.innerHTML = '<h2>Browser simulation result</h2>' +
          '<p class="score"><strong>' + avg + '</strong> <span>expected recurrences (mean)</span></p>' +
          '<p>90% of draws fall between <strong>' + Math.round(draws[50]) + '</strong> and <strong>' + Math.round(draws[949]) + '</strong>.</p>' +
          '<p class="micro">Illustrative assumptions \u00b7 1,000 seeded draws \u00b7 not a government forecast.</p>';
        toast('Scenario simulation complete.');
      };
    }
  }
}

export {state, categories, fmtDate, panel};
