/** Generate a self-contained visual-review artifact with clearly labelled sample data. */
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { project, threads, snapshot, tasks, catalog, files, artifact } from './fixtures.ts';

const root = path.resolve(import.meta.dirname, '..');
let html = await readFile(path.join(root, 'dist/index.html'), 'utf8');
const js = html.match(/src="\.\/(assets\/[^\"]+\.js)"/)![1];
const stylesheets = [...html.matchAll(/<link rel="stylesheet"[^>]+href="\.\/([^\"]+\.css)"[^>]*>/g)];
const fixture = JSON.stringify({ project, threads, snapshot, tasks, catalog, files, artifact }).replaceAll('<', '\\u003c');
const mock = `const fixture=${fixture};
localStorage.removeItem('lc.workspace.thread.sample');
window.fetch=async(input,init={})=>{
 const url=new URL(typeof input==='string'?input:input.url,'http://preview.local');
 const p=url.pathname, body=init.body?JSON.parse(init.body):{}, work=new URL(location.href).searchParams.get('scene')==='work';
 let data={};
 if(p==='/api/bootstrap')data={token:'sample-preview',projects:[fixture.project],backend:'Sample preview — no live backend'};
 else if(p.endsWith('/threads'))data=init.method==='POST'?fixture.threads[0]:fixture.threads;
 else if(p.endsWith('/state'))data=work?fixture.snapshot:{values:{messages:[]},next:[],interrupts:[]};
 else if(p.endsWith('/catalog'))data=fixture.catalog;
 else if(p.endsWith('/mode'))data={mode:body.mode||'manual'};
 else if(p.endsWith('/runs'))data=[];
 else if(p.endsWith('/background'))data=body.operation==='inspect'?{task:{...fixture.tasks.find(t=>t.task_id===body.task_id),conversation:{text:'Sample task transcript. No live agent was run.',page:0,pages:1,limited:false,notice:'Static preview only'},activity:[]}}:{tasks:work?fixture.tasks:[],enabled:true,pending_results:[]};
 else if(p.endsWith('/files'))data=url.searchParams.has('read')?fixture.artifact:fixture.files;
 else if(p.endsWith('/run'))return new Response(JSON.stringify({detail:'This is a static preview. Start the local backend to run the agent.'}),{status:503,headers:{'content-type':'application/json'}});
 return Response.json(data);
};`;
html = html.replace(/<script type="module"[^>]+><\/script>/, '');
for (const stylesheet of stylesheets) html = html.replace(stylesheet[0], `<style>${await readFile(path.join(root, 'dist', stylesheet[1]), 'utf8')}</style>`);
html = html.replace(/<script src="\.\/theme-init.js"><\/script>/, `<script>${await readFile(path.join(root, 'dist/theme-init.js'), 'utf8')}</script>`);
html = html.replace('</body>', `<div style="position:fixed;right:12px;bottom:3px;z-index:100;font:9px system-ui;color:#798b80;background:#f2f7ef;padding:2px 6px;border-radius:3px">STATIC PREVIEW · SAMPLE DATA</div><script>${mock.replaceAll('</script', '<\\/script')}</script><script type="module">${(await readFile(path.join(root, 'dist', js), 'utf8')).replaceAll('</script', '<\\/script')}</script></body>`);
await mkdir(path.join(root, 'test-results'), { recursive: true });
await writeFile(path.join(root, 'test-results/preview.html'), html);
console.log(path.join(root, 'test-results/preview.html'));
