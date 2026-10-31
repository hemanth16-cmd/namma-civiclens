// One filter function for every list, table and map so results always agree.
import {wardForRecord} from './ward-catalog.js';
import {resolutionState} from './presentation.js';

export function filterRecords(records, {q = '', status = '', category = '', verification = '', area = '', resolution = '', ward = '', zone = ''} = {}) {
  const needle = q.trim().toLowerCase();
  return records.filter(r => {
    const geo = wardForRecord(r);
    if (ward && (ward === 'unassigned' ? !!geo : String(geo?.wardNumber) !== ward)) return false;
    if (zone && (zone === 'unassigned' ? !!geo : String(geo?.zoneNumber) !== zone)) return false;
    if (needle && !`${r.title} ${r.area} ${r.id} ${r.category}`.toLowerCase().includes(needle)) return false;
    if (status && r.status !== status) return false;
    if (category && r.category !== category) return false;
    if (verification && r.verification !== verification) return false;
    if (area && r.area !== area) return false;
    if (resolution && resolutionState(r).className !== resolution) return false;
    return true;
  });
}

export function sortRecords(records, mode = 'newest') {
  const list = [...records];
  if (mode === 'priority') return list.sort((a, b) => (b.priority?.score ?? -1) - (a.priority?.score ?? -1));
  if (mode === 'oldest') return list.sort((a, b) => (a.created > b.created ? 1 : -1));
  return list.sort((a, b) => (b.created > a.created ? 1 : -1));
}

export function readFilters(form) {
  const data = new FormData(form);
  return Object.fromEntries([...data.entries()].map(([k, v]) => [k, String(v)]));
}
