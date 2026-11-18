// Account quota UI exercises real page code without browsers or generation calls.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
let accountWrites = 0, interval;
function element(selector) {
  if (selector === 'meta[name="csrf-token"]') return {content:'test-only-csrf-token'};
  if (!elements.has(selector)) {
    let markup = '';
    elements.set(selector,{value:'',textContent:'',style:{display:'none'},listeners:{},
      get innerHTML(){return markup;},set innerHTML(value){markup=value;if(selector==='#accountRows')accountWrites++;},
      addEventListener(name,fn){this.listeners[name]=fn;}});
  }
  return elements.get(selector);
}
const name = "中文's <账号>";
const quota = {date:'2026-10-31',timezone:'Asia/Shanghai',limit:4,used:1,reserved:1,unknown:1,pending_recovery:1,remaining:1,estimated:true,blocked_reason:'历史请求尚未核对'};
let rows = [{name,enabled:true,status:'needs_login',note:'请登录 <safe>',quota:structuredClone(quota)}];
const requests = [], confirmations = [], delayedLists = [];
let delayList = false, actionGate, rejectAction = false, confirmAnswer = false;
let model = 'mini', modelLocked = false;
const accountList = () => ({model,model_locked:modelLocked,accounts:structuredClone(rows)});
const state = {rows:[],requested_script_count:1,running:false,returncode:null};
const context = vm.createContext({document:{querySelector:element,activeElement:null},FormData:class {},console,
  setInterval(fn){interval=fn;},confirm(text){confirmations.push(text);return confirmAnswer;},
  fetch:async(path,options={})=>{
    requests.push({path,options});
    if(path==='/api/accounts' && delayList) {
      const captured = accountList();
      await new Promise(resolve=>delayedLists.push(resolve));
      return {ok:true,json:async()=>captured};
    }
    if(path.startsWith('/api/accounts/') && actionGate) await actionGate;
    const failed = path.startsWith('/api/accounts/') && rejectAction;
    return {ok:!failed,status:failed?400:200,statusText:'failed',json:async()=>{
      if(path==='/api/state') return structuredClone(state);
      if(path==='/api/accounts') return accountList();
      if(path==='/api/accounts/model' && !failed) {
        model = JSON.parse(options.body).model;
        rows[0].quota={...quota,limit:2,used:0,reserved:0,unknown:0,remaining:0,blocked_reason:'今日已使用 Seedance 2.0 Mini'};
        return {model,model_locked:modelLocked};
      }
      if(path.startsWith('/api/accounts/')) return failed?{error:'仍需登录 <retry>'}:{ok:true,name,message:'账号状态已检查，余额未重置'};
      return {ok:true};
    }};
  }});
vm.runInContext(fs.readFileSync('dashboard_ui.js','utf8'),context);
const button = {dataset:{name}};
function html(){return element('#accountRows').innerHTML;}
function posts(){return requests.filter(r=>r.options.method==='POST');}
(async()=>{
  await context.loadAccounts();
  assert.match(html(),/配置已保存 · 待登录/,'quota cannot override login identity');
  assert.match(html(),/今日已使用 1\/4/);
  assert.match(html(),/今日剩余 1 次 · 预留 1 · 结果未知 1/);
  assert.match(html(),/历史未决 1/);
  assert.match(html(),/历史请求尚未核对/);
  assert.match(html(),/本地估算，平台余额待核验/);
  assert.match(html(),/按北京时间每日重置/);
  assert.match(html(),/结果未知的次数暂占余额/);
  assert.match(html(),/data-name="中文&#39;s &lt;账号&gt;"/);
  assert.match(html(),/onclick="confirmAccount\(this\)">检查登录状态/);
  assert.doesNotMatch(html(),/onclick="\w+Account\('/);
  assert.doesNotMatch(html(),/请登录 <safe>/);
  const writes = accountWrites;await context.loadAccounts();
  assert.equal(accountWrites,writes,'unchanged polling preserves account DOM');

  rows[0].quota.remaining=0;await context.loadAccounts();
  assert.match(html(),/配置已保存 · 待登录/);
  assert.match(html(),/剩余为0，后续任务自动跳过/);
  rows[0].status='exhausted';rows[0].quota.estimated=false;
  rows[0].quota.blocked_reason='平台提示今日额度已耗尽';
  await context.loadAccounts();
  assert.match(html(),/配置已保存 · 今日额度耗尽/);
  assert.match(html(),/已按平台显示校准（非实时）/);
  assert.doesNotMatch(html(),/已登录，可运行/);

  // The visible account page polls again across Beijing midnight.  A fresh
  // server day changes the used count to 0/4 and restores its free balance.
  rows[0].status='available';
  rows[0].quota={date:'2026-11-01',limit:4,used:0,reserved:0,unknown:0,remaining:4,estimated:true,blocked_reason:''};
  element('#accounts').style.display='';interval();await new Promise(resolve=>setImmediate(resolve));
  assert.match(html(),/今日已使用 0\/4/);
  assert.match(html(),/今日剩余 4 次/);
  assert.match(html(),/2026-11-01/);
  assert.doesNotMatch(html(),/剩余为0，后续任务自动跳过/);
  rows[0].status='exhausted';rows[0].quota={...quota,remaining:0,estimated:false,blocked_reason:'平台提示今日额度已耗尽'};

  rows[0].login_window_open=true;rows[0].login_status='opening';rows[0].login_note='等待验证 <login>';
  await context.loadAccounts();
  assert.match(html(),/配置已保存 · 今日额度耗尽/,'login window is separate from quota state');
  assert.match(html(),/登录窗口：等待验证 &lt;login&gt;/);
  assert.match(html(),/onclick="openAccount\(this\)" disabled/);

  // An older GET must not replace the result of a newer account action.
  delayList=true;const stale=context.loadAccounts();
  delayList=false;rows[0].note='较新的状态';
  let finishAction;actionGate=new Promise(resolve=>{finishAction=resolve;});
  const pending=context.confirmAccount(button);
  await context.confirmAccount(button);
  assert.equal(posts().length,1,'rapid repeated click sends one action');
  assert.match(html(),/正在检查登录状态/);
  finishAction();await pending;actionGate=null;
  delayedLists.shift()();await stale;
  assert.match(html(),/较新的状态/);
  assert.match(html(),/账号状态已检查，余额未重置/);
  assert.equal(JSON.parse(posts()[0].options.body).name,name,'JSON preserves arbitrary account names');
  assert.deepEqual(JSON.parse(posts()[0].options.body),{name},'checking status sends no quota resets');
  assert.equal(rows[0].quota.remaining,0);

  rejectAction=true;await context.confirmAccount(button);rejectAction=false;
  assert.match(html(),/仍需登录 &lt;retry&gt;/);
  await context.loadAccounts();assert.match(html(),/仍需登录 &lt;retry&gt;/,'polling preserves actionable errors');
  const count=posts().length;await context.deleteAccount(button);
  assert.equal(posts().length,count,'cancelled account deletion sends no request');
  assert.ok(confirmations[0].includes(name));
  const modelSelect=element('#seedanceModel');
  assert.equal(modelSelect.value,'mini');
  assert.equal(modelSelect.disabled,false);
  delayList=true;const staleModelList=context.loadAccounts();delayList=false;
  let finishModel;actionGate=new Promise(resolve=>{finishModel=resolve;});
  modelSelect.value='fast';
  const pendingModel=context.changeSeedanceModel(modelSelect);
  assert.equal(modelSelect.disabled,true,'selector is disabled while saving');
  await context.changeSeedanceModel(modelSelect);
  await context.run();
  assert.equal(posts().filter(r=>r.path==='/api/accounts/model').length,1,'one model change is written');
  assert.match(element('#log').textContent,/正在保存视频生成模型/);
  finishModel();await pendingModel;actionGate=null;
  delayedLists.shift()();await staleModelList;
  assert.equal(modelSelect.value,'fast','stale Mini poll cannot restore the old selection');
  assert.equal(modelSelect.disabled,false);
  assert.match(html(),/今日已使用 0\/2/);
  assert.match(html(),/今日剩余 0 次/);
  assert.match(html(),/今日已使用 Seedance 2\.0 Mini/);
  assert.deepEqual(JSON.parse(posts().at(-1).options.body),{model:'fast'});
  rejectAction=true;modelSelect.value='mini';await context.changeSeedanceModel(modelSelect);rejectAction=false;
  assert.equal(modelSelect.value,'fast','saving failure restores the server-selected model');
  assert.match(element('#log').textContent,/切换模型失败/);
  modelLocked=true;await context.loadAccounts();
  assert.equal(modelSelect.disabled,true);
  modelSelect.value='mini';await context.changeSeedanceModel(modelSelect);
  assert.equal(modelSelect.value,'fast');
  assert.equal(posts().filter(r=>r.path==='/api/accounts/model').length,2,'locked model cannot submit');
  rows=[{name:'legacy',enabled:false,status:'needs_login'}];await context.loadAccounts();
  assert.match(html(),/每日次数统计暂不可用/);assert.doesNotMatch(html(),/今日剩余 4/);
  element('#accounts').style.display='';interval();await new Promise(resolve=>setImmediate(resolve));
  assert.ok(requests.filter(r=>r.path==='/api/accounts').length>5);
  assert.ok(!posts().some(r=>r.path==='/api/run'),'tests never generate videos');
  console.log('Account UI: quota categories, stale polls, action gating, and model selection passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
