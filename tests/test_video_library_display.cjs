// Presentation-only harness: no network, files, approvals, or generation.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
function element(selector) {
  if (selector === 'meta[name="csrf-token"]') return {content:'fixture-csrf'};
  if (!elements.has(selector)) elements.set(selector, {
    value:'', textContent:'', innerHTML:'', style:{}, addEventListener(){},
  });
  return elements.get(selector);
}
const video = {name:'actual"filename.mp4',label:'OWALA <cup> · 脚本2 · 片段1/1',
               path:'/fixture/videos/actual"filename.mp4',size:'6.0MB'};
const batches = [{id:'r20261113_120002_abcd',label:'2026-11-13 12:00',count:1},
                 {id:'r20261113_120057_2345',label:'2026-11-13 12:00',count:2}];
let useBatches = false, confirmed = false;
const actions = [];
const context = vm.createContext({
  document:{querySelector:element,activeElement:null}, FormData:class {}, console,
  confirm(){return confirmed;}, setInterval(){},
  fetch:async(path, options)=>{
    if (path.startsWith('/api/video-batches/')) actions.push([path, JSON.parse(options.body)]);
    return {ok:true,status:200,json:async()=>path==='/api/videos'?
      {videos:[video], ...(useBatches ? {batches} : {})}:
      path==='/api/state'?{rows:[],running:false}:path==='/api/accounts'?{accounts:[]}:{message:'已完成'}};
  },
});
vm.runInContext(fs.readFileSync('dashboard_ui.js','utf8'),context);
(async()=>{
  await context.loadVideos();
  const html = element('#videoRows').innerHTML;
  assert.match(html,/OWALA &lt;cup&gt; · 脚本2 · 片段1\/1/);
  assert.match(html,/title="actual&quot;filename\.mp4"/);
  assert.ok(html.includes('/media-video?path='+encodeURIComponent(video.path)));
  assert.ok(!html.includes('OWALA <cup>'));
  useBatches = true;
  await context.loadVideos();
  const archive = element('#videoRows').innerHTML;
  assert.match(archive,/2026-11-13 12:00/);
  assert.match(archive,/打开文件夹<\/button><button[^>]+>删除文件夹/);
  assert.ok(!archive.includes('filename.mp4'));
  const button = {dataset:{id:batches[0].id}};
  await context.openVideoBatch(button);
  await context.deleteVideoBatch(button);
  assert.deepEqual(actions, [['/api/video-batches/open',{id:batches[0].id}]]);
  confirmed = true;
  await context.deleteVideoBatch(button);
  assert.deepEqual(actions.at(-1), ['/api/video-batches/delete',{id:batches[0].id}]);
  assert.ok(!element('#videoRows').innerHTML.includes(batches[0].id));
  assert.ok(element('#videoRows').innerHTML.includes(batches[1].id));
  console.log('video library labels, batch-only list, scoped open and confirmed deletion: passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
