// City map page: legend, shared filters, map and accessible list that always agree.
import {api, categories, statuses, esc, verificationStates} from '../services.js';
import {workspaceRecords} from '../server-records.js';
import {categoryIcon, resolutionState} from '../presentation.js';
import {icon, workspaceShell, heading, pill, statusPill, verificationPill, priorityPill, toast, empty} from '../ui.js';
import {viewer, addCleanup} from '../state.js';
import {filterRecords, readFilters} from '../filters.js';
import {mountCityMap, pointsFor, MAP_ATTRIBUTION} from './map.js';

export async function renderMapPage(root) {
  const v = viewer();
  const records = await workspaceRecords(v);
  const areas = [...new Set(records.map(r => r.area))].sort();
  const base = v.role === 'official' ? '#/official' : '#/resident';
  root.innerHTML = workspaceShell({title: 'City map', body: `
    ${heading({eyebrow: 'Open map · Bengaluru', title: 'City map', lead: 'Symbols show the issue type. Colour shows administrative resolution. Verification is stated separately in every popup and list row.'})}
    <section class="map-legend" aria-label="Map legend">
      <p class="legend-item"><span class="legend-ring resolved" aria-hidden="true"></span><span><strong>Green circle</strong> · marked resolved, with no open dispute or recheck</span></p>
      <p class="legend-item"><span class="legend-ring unresolved" aria-hidden="true"></span><span><strong>Red circle</strong> · unresolved, disputed, or awaiting reinspection</span></p>
      <p class="legend-item"><span class="legend-cluster" aria-hidden="true">3</span><span><strong>Numbered navy cluster</strong> · several reports, which may mix statuses</span></p>
    </section>
    <div class="map-layout">
      <section class="map-stage" aria-label="Map">
        <div class="map-tools" role="toolbar" aria-label="Map tools">
          <button type="button" class="btn btn-secondary btn-sm" data-map="locate">${icon('locate')}<span>My location</span></button>
          <button type="button" class="btn btn-secondary btn-sm" data-map="fit">${icon('maximize')}<span>Fit reports</span></button>
          <button type="button" class="btn btn-secondary btn-sm" data-map="style">${icon('layers')}<span>Map style</span></button>
        </div>
        <div class="map-frame" id="map-frame"></div>
        <p class="map-status" id="map-status" role="status" aria-live="polite">Loading map…</p>
        <p class="map-disclosure">${esc(MAP_ATTRIBUTION)}. ${records.some(r => r.sample) ? 'Sample incidents use approximate neighbourhood positions. ' : 'Positions are the GPS coordinates recorded with each report. '}Your device location is never saved.</p>
      </section>
      <aside class="map-side" aria-label="Filters and results">
        <section class="panel map-filters" aria-label="Filter map and list">
          <form class="filter-bar map-filter" id="map-filter" role="search" aria-label="Filter map and list">
      <div class="field field-search"><label for="mf-q">Search</label><input id="mf-q" name="q" type="search" placeholder="Issue, neighbourhood or ID"></div>
      <div class="field"><label for="mf-category">Category</label><select id="mf-category" name="category"><option value="">All categories</option>${categories.map(c => `<option>${esc(c)}</option>`).join('')}</select></div>
      <div class="field"><label for="mf-status">Lifecycle</label><select id="mf-status" name="status"><option value="">All statuses</option>${statuses.map(s => `<option>${s}</option>`).join('')}</select></div>
      <div class="field"><label for="mf-resolution">Map colour</label><select id="mf-resolution" name="resolution"><option value="">All colours</option><option value="resolved">Green · marked resolved</option><option value="unresolved">Red · unresolved or disputed</option></select></div>
      <div class="field"><label for="mf-verification">Verification</label><select id="mf-verification" name="verification"><option value="">Any verification</option>${verificationStates.map(s => `<option>${s}</option>`).join('')}</select></div>
      <div class="field"><label for="mf-area">Neighbourhood</label><select id="mf-area" name="area"><option value="">All neighbourhoods</option>${areas.map(a => `<option>${esc(a)}</option>`).join('')}</select></div>
    </form>
          <details class="symbols"><summary>Category symbols (${categories.length})</summary>
            <ul class="category-legend" aria-label="Category symbols">${categories.map(c => `<li>${categoryIcon(c)}<span>${esc(c)}</span></li>`).join('')}</ul>
          </details>
        </section>
        <section class="panel panel-flush map-results" aria-labelledby="results-title">
          <div class="panel-head"><div><h2 id="results-title">In this view</h2></div><span class="pill" id="map-count" role="status" aria-live="polite"></span></div>
          <div id="map-list"></div>
        </section>
      </aside>
    </div>`});

  const frame = root.querySelector('#map-frame');
  const {points, hidden} = pointsFor(records);
  const controller = await mountCityMap(frame, {records});
  addCleanup(() => controller.destroy());
  const form = root.querySelector('#map-filter');
  const list = root.querySelector('#map-list');
  const count = root.querySelector('#map-count');

  const apply = () => {
    const filters = readFilters(form);
    const shown = filterRecords(records, filters);
    const shownIds = shown.map(r => r.id);
    const plottedIds = new Set(points.map(p => p.r.id));
    const plottedShown = shown.filter(r => plottedIds.has(r.id));
    controller.setVisible(shownIds.filter(id => plottedIds.has(id)));
    count.textContent = `${shown.length} shown · ${plottedShown.length} plotted${hidden.length ? ` · ${hidden.length} private` : ''}`;
    list.innerHTML = shown.length ? `<ul class="issue-list">${shown.map(r => {
      const s = resolutionState(r);
      const plotted = plottedIds.has(r.id);
      return `<li class="issue-row">
        <span class="issue-symbol ${s.className}" aria-hidden="true">${categoryIcon(r.category)}</span>
        <div class="issue-row-body">
          <h3 class="issue-title"><a href="#/issue/${esc(r.id)}">${esc(r.title)}</a></h3>
          <p class="issue-meta">${esc(r.area)} · ${esc(r.id)}${plotted ? '' : ' · <span class="pill">Location private</span>'}</p>
          <p class="issue-tags"><span class="state-text-inline"><span class="state-dot ${s.className}" aria-hidden="true"></span>${esc(s.label)}</span> ${statusPill(r.status)} ${verificationPill(r.verification)}</p>
        </div>
      </li>`;
    }).join('')}</ul>` : empty('No reports match these filters', 'Clear a filter to see markers and cases again.');
    if (!shown.length) toast('No reports match these filters.');
  };
  form.addEventListener('input', apply);
  form.addEventListener('change', apply);
  form.addEventListener('submit', e => e.preventDefault());
  root.querySelector('[data-map="locate"]').onclick = () => controller.locate();
  root.querySelector('[data-map="fit"]').onclick = () => controller.fit();
  root.querySelector('[data-map="style"]').onclick = () => controller.toggleStyle();
  apply();
}

export {pill, priorityPill, resolutionState};
