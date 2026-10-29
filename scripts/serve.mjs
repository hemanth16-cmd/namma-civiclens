import http from 'node:http';
import {readFile,stat} from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
const root=path.resolve('dist');const args=process.argv.slice(2);const i=args.indexOf('--port');const port=i>=0?Number(args[i+1]):4173;
const mime={'.html':'text/html; charset=utf-8','.css':'text/css','.js':'application/javascript','.json':'application/json','.ttf':'font/ttf','.zip':'application/zip','.svg':'image/svg+xml','.png':'image/png','.jpg':'image/jpeg'};
http.createServer(async(req,res)=>{try{const pathname=decodeURIComponent(new URL(req.url,'http://localhost').pathname);const file=path.resolve(root,'.'+(pathname==='/'?'/index.html':pathname));if(!file.startsWith(root+path.sep)){res.writeHead(403);res.end('Forbidden');return}const info=await stat(file);if(!info.isFile())throw Error();res.writeHead(200,{'Content-Type':mime[path.extname(file)]||'application/octet-stream','Cache-Control':'no-cache'});res.end(await readFile(file))}catch{res.writeHead(404);res.end('Not found')}}).listen(port,'0.0.0.0',()=>{
  console.log(`Namma CivicLens running at http://localhost:${port}`);
  const lan=Object.values(os.networkInterfaces()).flat().filter(a=>a&&a.family==='IPv4'&&!a.internal).map(a=>`http://${a.address}:${port}`);
  if(lan.length)console.log(`Also reachable on your network at ${lan.join(', ')}.\nNote: browsers block camera and location on plain http:// network addresses. Use localhost on this computer, or an HTTPS address for phone testing.`);
});
