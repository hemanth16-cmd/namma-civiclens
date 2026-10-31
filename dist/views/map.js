// City map: MapLibre GL (OpenFreeMap vector style, OpenStreetMap data) with a Leaflet raster fallback.
// Marker meaning: green circle = administratively marked resolved with no open dispute or recheck;
// red circle = unresolved or disputed. Verification is shown in popups and lists, never by colour alone.
import {SAMPLE_POSITIONS, esc, categories} from '../services.js';
import {markerSVG, categoryIcon, resolutionState} from '../presentation.js';
import {toast} from '../ui.js';

export const MAP_ATTRIBUTION = '© OpenStreetMap contributors · vector tiles by OpenFreeMap';
const VECTOR_STYLES = {positron: 'https://tiles.openfreemap.org/styles/positron', bright: 'https://tiles.openfreemap.org/styles/bright'};
const CENTER = [77.595, 13.005];

/** Points for plotting: recorded coordinates when visible to the viewer, otherwise labelled sample positions. */
export function pointsFor(records) {
  const points = [];
  const hidden = [];
  for (const r of records) {
    if (r.gps && Number.isFinite(r.gps.lat) && Number.isFinite(r.gps.lng) && Math.abs(r.gps.lat) <= 90 && Math.abs(r.gps.lng) <= 180) {
      points.push({r, lat: r.gps.lat, lng: r.gps.lng, approx: r.gps.approximate === true ? 'public' : false});
    } else if (r.sample && SAMPLE_POSITIONS[r.id]) {
      const [lat, lng] = SAMPLE_POSITIONS[r.id];
      points.push({r, lat, lng, approx: 'sample'});
    } else {
      hidden.push(r);
    }
  }
  return {points, hidden};
}

function webglAvailable() {
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch { return false; }
}

function popupHTML(p) {
  const s = resolutionState(p.r);
  return `<div class="map-popup">
    <p class="eyebrow">${esc(p.r.category)}</p>
    <h3>${esc(p.r.title)}</h3>
    <p>${esc(p.r.area)}${p.approx === 'public' ? ' · approximate position (about 110 m)' : p.approx ? ' · approximate neighbourhood position (sample)' : ''}</p>
    <p class="popup-state"><span class="state-dot ${s.className}" aria-hidden="true"></span>${esc(s.label)} · ${esc(p.r.status)}</p>
    <p class="micro">Verification: ${esc(p.r.verification)}</p>
    <a class="btn btn-primary btn-sm" href="#/issue/${esc(p.r.id)}">Open case</a>
  </div>`;
}

function featureOf(p) {
  const s = resolutionState(p.r);
  return {
    type: 'Feature',
    geometry: {type: 'Point', coordinates: [p.lng, p.lat]},
    properties: {id: p.r.id, title: p.r.title, category: p.r.category, status: p.r.status, verification: p.r.verification, resolved: s.solved,
      iconKey: `cat-${categories.indexOf(p.r.category)}-${s.solved ? 'resolved' : 'unresolved'}`}
  };
}

/**
 * Mounts the map into `container` and returns a controller.
 * setVisible(ids) is the only filter path: the page passes the same ids that its list shows.
 */
export async function mountCityMap(container, {records = [], compact = false, onStatus} = {}) {
  const {points, hidden} = pointsFor(records);
  const status = message => {
    const el = document.getElementById('map-status');
    if (el) el.textContent = message;
    onStatus?.(message);
  };
  container.innerHTML = `<div class="map-canvas" role="application" aria-label="Map of civic reports. Use the list for keyboard access."></div>`;
  const canvas = container.querySelector('.map-canvas');
  const hiddenNote = hidden.length ? ` ${hidden.length} ${hidden.length === 1 ? 'report is' : 'reports are'} not plotted because its location is private.` : '';
  const attributionNote = `${MAP_ATTRIBUTION}.${points.some(p => p.approx === 'sample') ? ' Sample incidents appear at approximate neighbourhood positions.' : ''}${points.some(p => p.approx === 'public') ? ' Public reports appear at approximate positions (about 110 m) to protect reporters.' : ''}${hiddenNote}`;

  let impl = null;
  let visibleIds = null;
  const controller = {
    mode: 'none', points, hidden,
    destroy() { impl?.destroy(); impl = null; },
    setVisible(ids) { visibleIds = ids; impl?.setVisible(ids); },
    fit() { impl?.fit(); },
    locate() { impl?.locate(); },
    toggleStyle() { impl?.toggleStyle(); }
  };

  const useLeaflet = note => {
    if (window.L) {
      impl?.destroy();
      canvas.innerHTML = '';
      impl = mountLeaflet(canvas, points, {compact, status});
      controller.mode = 'leaflet';
      impl.setVisible(visibleIds);
      status(`${note ? note + ' ' : ''}Coordinate fallback: markers use real coordinates where recorded. ${attributionNote}`);
    } else {
      canvas.innerHTML = '<div class="map-unavailable"><p><strong>The map could not load on this device.</strong></p><p>Use the list beside the map to open every case. Check the connection and reload to retry.</p></div>';
      controller.mode = 'list';
      status('Map unavailable. The accessible list still works.');
    }
  };

  const useVector = () => {
    try {
      impl = mountVector(canvas, points, {
        compact, status,
        onFail: () => { useLeaflet('The vector basemap could not load.'); }
      });
      controller.mode = 'vector';
      impl.setVisible(visibleIds);
      status(attributionNote);
    } catch (err) {
      console.warn('MapLibre could not start:', err?.message);
      useLeaflet('WebGL vector map is unavailable here.');
    }
  };

  if (window.maplibregl && webglAvailable()) useVector();
  else useLeaflet('WebGL vector map is unavailable here.');
  return controller;
}

function mountVector(canvas, points, {compact, status, onFail}) {
  let styleKey = 'positron';
  let loaded = false;
  let failed = false;
  let visibleIds = null;
  const map = new maplibregl.Map({
    container: canvas,
    style: VECTOR_STYLES[styleKey],
    center: CENTER,
    zoom: compact ? 10.4 : 10.9,
    attributionControl: {compact: true}
  });
  map.addControl(new maplibregl.NavigationControl({showCompass: false}), 'top-right');
  map.addControl(new maplibregl.ScaleControl({unit: 'metric'}), 'bottom-left');
  const loadTimer = setTimeout(() => {
    if (!loaded && !failed) {
      failed = true;
      onFail?.();
    }
  }, 10000);
  let errorCount = 0;
  map.on('error', () => {
    errorCount += 1;
    if (!loaded && !failed && errorCount >= 3) {
      failed = true;
      clearTimeout(loadTimer);
      onFail?.();
    }
  });

  const features = () => ({type: 'FeatureCollection', features: points.filter(p => !visibleIds || visibleIds.has(p.r.id)).map(featureOf)});

  async function ensureImages() {
    for (const [i, name] of categories.entries()) {
      for (const solved of [true, false]) {
        const key = `cat-${i}-${solved ? 'resolved' : 'unresolved'}`;
        if (map.hasImage(key)) continue;
        const img = new Image(88, 88);
        img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(markerSVG(name, solved));
        await img.decode();
        const canvasEl = document.createElement('canvas');
        canvasEl.width = 88;
        canvasEl.height = 88;
        canvasEl.getContext('2d').drawImage(img, 0, 0, 88, 88);
        map.addImage(key, canvasEl.getContext('2d').getImageData(0, 0, 88, 88), {pixelRatio: 2});
      }
    }
  }

  let adding = false;
  async function addLayers() {
    if (!map.isStyleLoaded() || adding) return;
    adding = true;
    try {
      await ensureImages();
      // Both 'load' and 'style.load' can run this for the same style. A source that is still in use by
      // its layers cannot be removed, so an existing one is updated in place and missing layers are added.
      const source = map.getSource('reports');
      if (source) source.setData(features());
      else map.addSource('reports', {type: 'geojson', data: features(), cluster: true, clusterMaxZoom: 14, clusterRadius: 45});
      if (!map.getLayer('report-clusters')) map.addLayer({id: 'report-clusters', type: 'circle', source: 'reports', filter: ['has', 'point_count'],
        paint: {'circle-color': '#18324A', 'circle-radius': ['step', ['get', 'point_count'], 18, 5, 22, 12, 28], 'circle-stroke-color': '#FFFFFF', 'circle-stroke-width': 3}});
      if (!map.getLayer('cluster-count')) map.addLayer({id: 'cluster-count', type: 'symbol', source: 'reports', filter: ['has', 'point_count'],
        layout: {'text-field': ['get', 'point_count_abbreviated'], 'text-size': 13, 'text-font': ['Noto Sans Regular'], 'text-allow-overlap': true},
        paint: {'text-color': '#FFFFFF'}});
      if (!map.getLayer('report-points')) map.addLayer({id: 'report-points', type: 'symbol', source: 'reports', filter: ['!', ['has', 'point_count']],
        layout: {'icon-image': ['get', 'iconKey'], 'icon-size': compact ? 0.72 : 0.8, 'icon-allow-overlap': true}});
      loaded = true;
      clearTimeout(loadTimer);
    } catch (err) {
      status('Report symbols could not be drawn: ' + (err?.message || 'unknown error') + '. The list still works.');
    } finally {
      adding = false;
    }
  }

  map.on('load', addLayers);
  map.on('style.load', () => { loaded = false; map.once('idle', addLayers); });

  map.on('click', 'report-points', e => {
    const p = e.features?.[0]?.properties;
    const point = points.find(x => x.r.id === p?.id);
    if (!point) return;
    new maplibregl.Popup({offset: 16, maxWidth: '300px'}).setLngLat(e.lngLat).setHTML(popupHTML(point)).addTo(map);
  });
  map.on('click', 'report-clusters', async e => {
    const f = e.features?.[0];
    try {
      const zoom = await map.getSource('reports').getClusterExpansionZoom(f.properties.cluster_id);
      map.easeTo({center: f.geometry.coordinates, zoom});
    } catch { /* cluster no longer exists */ }
  });
  for (const layer of ['report-points', 'report-clusters']) {
    map.on('mouseenter', layer, () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', layer, () => { map.getCanvas().style.cursor = ''; });
  }

  return {
    destroy() {
      clearTimeout(loadTimer);
      try { map.remove(); } catch { /* already removed */ }
    },
    setVisible(ids) {
      visibleIds = ids ? new Set(ids) : null;
      map.getSource('reports')?.setData(features());
      const count = features().features.length;
      if (loaded && visibleIds && count === 0) status('No reports match these filters. Clear a filter to see markers again.');
    },
    fit() {
      const list = features().features;
      if (!list.length) return toast('No reports match these filters.');
      const bounds = new maplibregl.LngLatBounds();
      list.forEach(f => bounds.extend(f.geometry.coordinates));
      map.fitBounds(bounds, {padding: 60, maxZoom: 15, duration: 450});
    },
    locate() {
      if (!navigator.geolocation) return toast('Device location is not available in this browser.');
      navigator.geolocation.getCurrentPosition(p => {
        const pos = [p.coords.longitude, p.coords.latitude];
        map.flyTo({center: pos, zoom: 15.5});
        new maplibregl.Marker({color: '#18324A'}).setLngLat(pos)
          .setPopup(new maplibregl.Popup({offset: 20}).setText(`Your device location · accuracy ±${Math.round(p.coords.accuracy)} m (not saved)`))
          .addTo(map).togglePopup();
      }, () => toast('Location is blocked or unavailable. Allow location for this site and try again.'), {enableHighAccuracy: true, timeout: 15000});
    },
    toggleStyle() {
      styleKey = styleKey === 'positron' ? 'bright' : 'positron';
      map.setStyle(VECTOR_STYLES[styleKey]);
      toast(styleKey === 'bright' ? 'Map style: Bright.' : 'Map style: Light.');
    }
  };
}

function mountLeaflet(canvas, points, {compact}) {
  const L = window.L;
  const map = L.map(canvas, {scrollWheelZoom: true, attributionControl: true}).setView([13.005, 77.595], compact ? 10 : 11);
  let tileErrors = 0;
  const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'});
  tiles.on('tileerror', () => {
    tileErrors += 1;
    if (tileErrors === 4) document.getElementById('map-status') && (document.getElementById('map-status').textContent = 'The online basemap is unavailable. Markers, zoom and the list still work.');
  });
  tiles.addTo(map);
  const cluster = L.markerClusterGroup
    ? L.markerClusterGroup({showCoverageOnHover: false, maxClusterRadius: 45, iconCreateFunction: c => L.divIcon({html: `<span>${c.getChildCount()}</span>`, className: 'civic-cluster', iconSize: [40, 40]})})
    : L.layerGroup();
  cluster.addTo(map);
  const markers = points.map(p => {
    const s = resolutionState(p.r);
    const label = `${p.r.category} · ${s.label} · ${p.r.title}`;
    const icon = L.divIcon({className: `civic-marker ${s.className}`, html: `<span>${categoryIcon(p.r.category)}</span>`, iconSize: [40, 40], iconAnchor: [20, 20]});
    const marker = L.marker([p.lat, p.lng], {icon, title: label, alt: label, keyboard: true});
    marker.bindPopup(popupHTML(p));
    return {id: p.r.id, marker};
  });
  let visible = null;
  const apply = () => {
    cluster.clearLayers();
    markers.filter(m => !visible || visible.has(m.id)).forEach(m => cluster.addLayer(m.marker));
  };
  apply();
  return {
    destroy() { try { map.remove(); } catch { /* already removed */ } },
    setVisible(ids) { visible = ids ? new Set(ids) : null; apply(); },
    fit() {
      const active = markers.filter(m => !visible || visible.has(m.id));
      if (!active.length) return toast('No reports match these filters.');
      map.fitBounds(L.latLngBounds(active.map(m => m.marker.getLatLng())), {padding: [50, 50], maxZoom: 15});
    },
    locate() {
      if (!navigator.geolocation) return toast('Device location is not available in this browser.');
      navigator.geolocation.getCurrentPosition(p => {
        const pos = [p.coords.latitude, p.coords.longitude];
        map.setView(pos, 16);
        L.circle(pos, {radius: p.coords.accuracy, color: '#18324A', fillOpacity: 0.08}).addTo(map);
        L.circleMarker(pos, {radius: 6, color: '#18324A', fillColor: '#F07861', fillOpacity: 1}).addTo(map)
          .bindPopup(`Your device location · ±${Math.round(p.coords.accuracy)} m (not saved)`).openPopup();
      }, () => toast('Location is blocked or unavailable. Allow location for this site and try again.'), {enableHighAccuracy: true, timeout: 15000});
    },
    toggleStyle() {
      canvas.classList.toggle('map-dark');
      toast(canvas.classList.contains('map-dark') ? 'Dark display enabled for the fallback map.' : 'Light display restored.');
    }
  };
}
