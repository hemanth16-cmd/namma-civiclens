// Intelligence studio (admin only). Every technique carries one of four honest status labels.
// Priority is an implemented, tested baseline formula fed by illustrative inputs.
import {api, esc, categories, PRIORITY_WEIGHTS} from '../services.js';
import {workspaceRecords} from '../server-records.js';
import {icon, workspaceShell, heading, notice, panel, panelHead, pill, priorityPill, toast, fmtDate} from '../ui.js';
import {viewer} from '../state.js';
import {state} from '../state.js';
import {rerender} from '../router.js';

const TABS = [['priority', 'Priority queue'], ['hotspots', 'Hotspot outlook'], ['graph', 'Relationship graph'], ['fcm', 'Cognitive map'], ['scenario', 'Scenario lab'], ['readiness', 'Model readiness']];
let activeTab = sessionStorage.getItem('ncl-intel-tab') || 'priority';

const STATUS = {
  impl: ['Implemented and tested', 'check', 'green'],
  pending: ['Implemented; device/browser verification pending', 'eye', 'navy'],
  demo: ['Illustrative demo', 'flag', 'coral'],
  planned: ['Proposed; not connected or trained', 'clock', 'neutral']
};
const statusPillFor = key => pill(STATUS[key][0], STATUS[key][2], STATUS[key][1]);

export async function renderIntelligence(root) {
  const records = await workspaceRecords(viewer());
  const tabs = `<div class="tablist" role="tablist" aria-label="Intelligence views">${TABS.map(([id, label]) => `<button type="button" role="tab" id="tab-${id}" aria-controls="intel-panel" aria-selected="${activeTab === id}" tabindex="${activeTab === id ? 0 : -1}" data-tab="${id}">${label}</button>`).join('')}</div>`;
  root.innerHTML = workspaceShell({title: 'Intelligence studio', body: `
    ${heading({eyebrow: 'Decision support · illustrative', title: 'Intelligence studio', lead: 'Each technique has one defined job, a required evidence base and an honest status. Nothing here is a validated city forecast or an automated detection.'})}
    ${notice('This studio runs on sample inputs and browser-local reports. No trained vision model, forecast model, CCTV feed or push-notification service is connected.', 'warn', 'alert')}
    ${tabs}
    <section id="intel-panel" role="tabpanel" aria-labelledby="tab-${activeTab}" class="intel-panel">${panelFor(activeTab, records)}</section>`});

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
  wireTab(root, records);
}

function selectTab(id) {
  activeTab = id;
  try { sessionStorage.setItem('ncl-intel-tab', id); } catch { /* optional */ }
  rerender();
}

function panelFor(tab, records) {
  switch (tab) {
    case 'hotspots': return hotspots();
    case 'graph': return graph();
    case 'fcm': return cognitive();
    case 'scenario': return scenario();
    case 'readiness': return readiness();
    default: return priorityQueue(records);
  }
}

function priorityQueue(records) {
  const ranked = records.filter(r => r.priority).sort((a, b) => b.priority.score - a.priority.score);
  const rows = ranked.map(r => {
    const p = r.priority;
    return `<tr>
      <td><a href="#/issue/${esc(r.id)}">${esc(r.title)}</a><small class="code-inline">${esc(r.id)}</small></td>
      <td>${p.contributions.severity}<small>sev. input ${Math.round(p.inputs.severity * 100)}%</small></td>
      <td>${p.contributions.exposure}<small>${esc(p.exposureSource)}</small></td>
      <td>${p.contributions.unresolved}<small>${p.daysOpen} days</small></td>
      <td>${p.contributions.recurrence}<small>${r.recurrence || 0} recurrence</small></td>
      <td><strong>${p.score}</strong> ${priorityPill(p.band)}</td>
      <td>${esc(p.confidence)}${p.flag ? `<small class="flag">${icon('alert', 'mini-icon')} ${esc(p.flag)}</small>` : ''}</td>
    </tr>`;
  }).join('');
  return `<div class="split">
    <section class="panel panel-flush">
      ${panelHead('Explainable priority queue', statusPillFor('impl'), 'Inputs are illustrative')}
      <div class="table-wrap"><table class="data-table"><caption class="sr-only">Priority contributions per case, highest first</caption>
        <thead><tr><th scope="col">Case</th><th scope="col">Severity pts</th><th scope="col">Exposure pts</th><th scope="col">Unresolved pts</th><th scope="col">Recurrence pts</th><th scope="col">Score</th><th scope="col">Evidence confidence</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="7">No cases yet.</td></tr>'}</tbody></table></div>
    </section>
    <section class="panel">
      <p class="eyebrow">How the score is built</p>
      <h2>Urgency you can explain</h2>
      <p>Score = 100 × Σ(weight × normalised input), using four inputs:</p>
      <ul class="contrib">
        <li><span>Severity from category (initial expert value)</span><strong>${Math.round(PRIORITY_WEIGHTS.severity * 100)}%</strong></li>
        <li><span>Exposure (assessed or default assumption)</span><strong>${Math.round(PRIORITY_WEIGHTS.exposure * 100)}%</strong></li>
        <li><span>Unresolved duration, capped at 30 days</span><strong>${Math.round(PRIORITY_WEIGHTS.unresolved * 100)}%</strong></li>
        <li><span>Recurrence records, capped at 3</span><strong>${Math.round(PRIORITY_WEIGHTS.recurrence * 100)}%</strong></li>
      </ul>
      <p class="micro">Weights are initial expert assumptions to be validated against reviewed outcomes. They are not calibrated probabilities.</p>
      <p><strong>Evidence confidence is separate from urgency.</strong> A severe category with weak evidence is flagged for verification rather than quietly ranked low.</p>
      <p class="micro">Scores support authorised decisions. They do not replace emergency procedures.</p>
    </section>
  </div>`;
}

function hotspots() {
  const bars = [40, 65, 51, 84, 70, 92, 64, 78, 58, 86, 100, 73];
  return `<div class="split">
    <section class="panel">
      ${panelHead('Hotspot outlook', statusPillFor('planned'), 'Planned prediction target')}
      <p><strong>Defined target:</strong> whether a ward and category will show elevated complaint activity over the next 30 days, compared with its trailing historical baseline.</p>
      <p><strong>Baseline first:</strong> a historical-rate baseline is the first comparison. A supervised model is tested only after chronological evaluation beats it.</p>
      <p class="micro"><strong>Needed:</strong> ward boundaries, timestamped complaint history, cleanup and inspection records, and clear separation between complaint activity and verified physical recurrence.</p>
    </section>
    <section class="panel">
      ${panelHead('Sample activity (not measured)', pill('Illustrative', 'coral'))}
      <figure class="chart" aria-label="Sample activity over 12 days, illustrative only"><div class="chart-bars">${bars.map((b, i) => `<div class="chart-bar" style="height:${b}%"><span>${i + 1}</span></div>`).join('')}</div><figcaption class="micro">Sample values for layout. Not measured complaint activity.</figcaption></figure>
    </section>
  </div>`;
}

function graph() {
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
  return `<section class="panel">
    ${panelHead('Relationship graph', statusPillFor('demo'), 'Stable IDs in the planned relational model')}
    <p>Connections are <strong>suggestions for human review</strong>. Nearby reports are never merged automatically. A PostgreSQL/PostGIS link table is the planned store; a separate graph database is optional.</p>
    <div class="graph-wrap"><svg viewBox="0 0 800 320" role="img" aria-labelledby="graph-title graph-desc" class="graph">
      <title id="graph-title">Civic relationship graph</title>
      <desc id="graph-desc">Complaint, incident, location, evidence, work order, repair and recurrence, connected for review.</desc>
      ${edges.map(([a, b]) => `<line x1="${pos(a)[0]}" y1="${pos(a)[1]}" x2="${pos(b)[0]}" y2="${pos(b)[1]}" />`).join('')}
      ${nodes.map(([label, , x, y], i) => `<g role="button" tabindex="0" data-node="${i}" aria-label="${label}: show what it means"><rect x="${x}" y="${y}" width="130" height="36" rx="8" /><text x="${x + 65}" y="${y + 22}" text-anchor="middle">${label}</text></g>`).join('')}
    </svg></div>
    <p id="node-info" class="notice notice-info" role="status">Select a node to see what it represents in the workflow.</p>
  </section>`;
}

const FCM_CONCEPTS = ['Severity', 'Exposure', 'Maintenance delay', 'Repair quality', 'Recurrence', 'Urgency'];
// Expert-informed signed influences (from → to). Origin: illustrative assumptions for discussion, not elicited or calibrated.
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

function cognitive() {
  return `<div class="split">
    <section class="panel form">
      ${panelHead('Cognitive map (Fuzzy Cognitive Mapping)', statusPillFor('demo'))}
      <p class="micro">FCM here means expert-informed influence mapping. It is not Firebase Cloud Messaging, and it does not establish causality or produce calibrated probabilities.</p>
      <div class="stack" id="fcm-controls">
        ${FCM_CONCEPTS.slice(0, 4).map((c, i) => `<div class="field"><label for="fcm-${i}">${esc(c)} <span class="optional" id="fcm-v-${i}">0.50</span></label><input id="fcm-${i}" type="range" min="0" max="1" step="0.05" value="0.5" data-fcm="${i}"></div>`).join('')}
      </div>
      <p class="micro">Starting activations for the four inputs; recurrence and urgency are computed. Sliders are illustrative.</p>
    </section>
    <section class="panel">
      <h2>Settled activation (15 steps)</h2>
      <div id="fcm-out" class="fcm-out" aria-live="polite"></div>
      <details class="explain"><summary>Assumed influence weights and their origin</summary>
        <ul class="contrib">${Object.entries(FCM_W).map(([k, v]) => `<li><span>${esc(k.replace('>', ' → '))}</span><strong>${v > 0 ? '+' : ''}${v}</strong></li>`).join('')}</ul>
        <p class="micro">Origin: illustrative expert assumptions for discussion. To be elicited from domain staff and tested against reviewed history before use.</p>
      </details>
    </section>
  </div>`;
}

function scenario() {
  return `<div class="split">
    <section class="panel form" aria-labelledby="sc-title">
      ${panelHead('What if maintenance became proactive?', statusPillFor('demo'), 'Exploratory Monte Carlo')}
      <div class="field"><label for="sc-budget">Preventive share of effort: <strong id="sc-budget-v">50</strong>%</label><input id="sc-budget" type="range" min="0" max="100" value="50"></div>
      <div class="field"><label for="sc-delay">Average response delay: <strong id="sc-delay-v">14</strong> days</label><input id="sc-delay" type="range" min="1" max="45" value="14"></div>
      <button type="button" class="btn btn-primary" id="sc-run">${icon('refresh')}<span>Run 1,000 draws</span></button>
      <div class="notice notice-info mt" role="note">${icon('alert', 'notice-icon')}<div><strong>Labelled assumptions:</strong> 100 baseline recurrences; preventive effort reduces recurrence by up to 45%; each delay day adds 0.6 recurrences; outcomes vary by ±20%. Seeded pseudo-random draws. Exploratory only: input distributions have not been estimated from reliable history.</div></div>
    </section>
    <section class="panel" aria-live="polite"><div id="sc-out" class="sc-out"><h2>Explore the trade-off</h2><p>Adjust the assumptions and run the simulation to see the spread of outcomes.</p></div></section>
  </div>`;
}

function readiness() {
  const rows = [
    ['Road-damage vision (YOLO11n proposed)', 'Detect potholes and cracks in images', 'Labelled road-damage data and evaluation on held-out images', 'planned', 'classificationService returns unavailable'],
    ['Waste-specific vision', 'Detect visible waste accumulation (not road-only)', 'Waste-specific examples and classes', 'planned', 'None; reviewers classify manually'],
    ['Before/after repair assessment', 'Apparent change with uncertainty; never proof of repair', 'Matched locations, usable images and reviewed outcomes', 'pending', 'Side-by-side evidence and manual verification states are implemented'],
    ['Incident linking', 'Suggest duplicates or recurrence for review', 'Spatial, temporal, category and visual evidence', 'planned', 'Recurrence records only; no automatic merging'],
    ['Knowledge graph', 'Connect complaints, incidents, evidence, repairs', 'Stable entity IDs and explicit relationships', 'demo', 'Illustrative graph view'],
    ['Priority engine', 'Rank inspection needs with explained contributions', 'Severity, exposure, age and recurrence inputs', 'impl', 'Formula and tests implemented; inputs illustrative'],
    ['Prediction (30-day ward/category activity)', 'Estimate elevated complaint activity', 'Time-stamped history and a measurable target', 'planned', 'Target defined only; no model trained'],
    ['Monte Carlo scenarios', 'Compare uncertain intervention scenarios', 'Justified distributions or labelled assumptions', 'demo', 'Labelled exploratory simulation'],
    ['Video dumping-action detection', 'Not planned for this release', 'Validated model, consent and retention rules', 'planned', 'videoAnalysisAdapter reports unavailable'],
    ['Push notifications (Firebase Cloud Messaging)', 'Deliver alerts to devices', 'Configured project, consent and delivery policy', 'planned', 'notificationAdapter reports not configured']
  ];
  return `<section class="panel panel-flush">
    ${panelHead('Model and integration readiness', '', 'Status for every technique')}
    <div class="table-wrap"><table class="data-table"><caption class="sr-only">Readiness of each intelligence technique</caption>
      <thead><tr><th scope="col">Technique</th><th scope="col">One defined job</th><th scope="col">Evidence needed</th><th scope="col">Status</th><th scope="col">Adapter or current behaviour</th></tr></thead>
      <tbody>${rows.map(([a, b, c, s, d]) => `<tr><td><strong>${esc(a)}</strong></td><td>${esc(b)}</td><td>${esc(c)}</td><td>${statusPillFor(s)}</td><td>${esc(d)}</td></tr>`).join('')}</tbody></table></div>
    <p class="micro">Status vocabulary: Implemented and tested · Implemented; device/browser verification pending · Illustrative demo · Proposed; not connected or trained.</p>
  </section>`;
}

function wireTab(root) {
  if (activeTab === 'graph') {
    root.querySelectorAll('[data-node]').forEach(n => {
      const show = () => {
        const labels = ['Complaint', 'Incident', 'Location', 'Evidence', 'Work order', 'Repair', 'Recurrence'];
        const text = {
          0: 'A submitted report keeps its own evidence and public reference.',
          1: 'Possibly related complaints need human review before any link is confirmed.',
          2: 'Neighbourhood, device GPS or EXIF location gives spatial context.',
          3: 'Original and repair evidence keep separate provenance.',
          4: 'Assigned team, target date and public progress attach to the case.',
          5: 'Administrative completion is distinct from community verification.',
          6: 'Repeated observations can indicate that a repair needs reinspection.'
        };
        root.querySelector('#node-info').textContent = `${labels[+n.dataset.node]}: ${text[+n.dataset.node]}`;
      };
      n.onclick = show;
      n.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); show(); } };
    });
  }
  if (activeTab === 'fcm') {
    const init = [0, 1, 2, 3].map(i => Number(document.getElementById('fcm-' + i)?.value ?? 0.5));
    const paint = () => {
      const vals = [0, 1, 2, 3].map(i => Number(document.getElementById('fcm-' + i).value));
      vals.forEach((v, i) => { document.getElementById('fcm-v-' + i).textContent = v.toFixed(2); });
      const settled = fcmRun([...vals, 0, 0]);
      document.getElementById('fcm-out').innerHTML = `<ul class="fcm-list">${FCM_CONCEPTS.map((c, i) => `<li><span>${esc(c)}</span><span class="fcm-bar" aria-hidden="true"><i style="width:${Math.round(settled[i] * 100)}%"></i></span><strong>${settled[i].toFixed(2)}</strong></li>`).join('')}</ul><p class="micro">Values are relative activations between 0 and 1. They describe the assumed model, not measured probabilities.</p>`;
    };
    root.querySelectorAll('[data-fcm]').forEach(i => i.oninput = paint);
    paint();
    void init;
  }
  if (activeTab === 'scenario') {
    const bud = root.querySelector('#sc-budget'), del = root.querySelector('#sc-delay');
    bud.oninput = () => root.querySelector('#sc-budget-v').textContent = bud.value;
    del.oninput = () => root.querySelector('#sc-delay-v').textContent = del.value;
    root.querySelector('#sc-run').onclick = () => {
      const b = +bud.value, d = +del.value;
      const mean = 100 * (1 - 0.45 * b / 100) + d * 0.6;
      let s = 42;
      const draws = Array.from({length: 1000}, () => {
        s = (s * 1664525 + 1013904223) >>> 0;
        return mean * (0.8 + (s / 4294967296) * 0.4);
      }).sort((x, y) => x - y);
      const avg = Math.round(draws.reduce((x, y) => x + y, 0) / draws.length);
      root.querySelector('#sc-out').innerHTML = `<h2>Simulated outcome</h2><p class="score"><strong>${avg}</strong> <span>expected recurrences (mean)</span></p><p>90% of draws fall between <strong>${Math.round(draws[50])}</strong> and <strong>${Math.round(draws[949])}</strong>.</p><p class="micro">Illustrative assumptions · 1,000 seeded draws · not a government forecast.</p>`;
      toast('Scenario simulation complete.');
    };
  }
}

export {state, categories, fmtDate, panel};
