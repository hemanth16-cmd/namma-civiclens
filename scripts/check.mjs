// Syntax-checks every shipped and test module with Node. Works on macOS, Linux and Windows.
import {execFileSync} from 'node:child_process';
import {readdirSync, statSync} from 'node:fs';
import path from 'node:path';

const roots = ['dist', 'scripts', 'tests'];
const files = [];
const walk = dir => {
  for (const name of readdirSync(dir)) {
    const p = path.join(dir, name);
    if (statSync(p).isDirectory()) walk(p);
    else if (/\.(m?js)$/.test(name) && !p.includes(`${path.sep}fonts${path.sep}`) && !/(maplibre-gl|leaflet|markercluster)\.js$/.test(name)) files.push(p);
  }
};
roots.forEach(walk);
for (const f of files) execFileSync(process.execPath, ['--check', f], {stdio: 'inherit'});
console.log(`Syntax check passed for ${files.length} files.`);
