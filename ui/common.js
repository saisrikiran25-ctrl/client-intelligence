// Shared runner used by both agent pages. A page must define, before this file loads:
//   const AGENT = 'a' | 'b'
// and, anywhere in its own script: buildBody() -> request body, renderOut(), renderGate(gate).
const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let runId=null,next=0,timer=null,result=null,savedList=[];
const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
const setStatus=s=>{$('status').textContent=s};
const money=x=>'Rs '+Number(x||0).toLocaleString('en-IN',{maximumFractionDigits:0});
const outs=()=>result.outputs&&result.outputs.length?`<div class="data" style="margin-bottom:8px">Output files: ${result.outputs.map(n=>`<a href="/api/file/${result.run_id}/${n}" download>${esc(n)}</a>`).join(' · ')}</div>`:'';

function evHtml(e){let d='';if(e.data&&typeof e.data!=='string')d=`<div class="data">${esc(JSON.stringify(e.data)).slice(0,400)}</div>`;else if(e.data&&e.kind==='critic')d=`<div class="data">${esc(e.data).slice(0,300)}</div>`;
 return `<div class="ev k-${e.kind}"><span class="t">${e.t.toFixed(1)}s</span><span><span class="chip">${esc(e.actor)}</span></span><span>${esc(e.msg)}${d}</span></div>`}

function fillSaved(){const l=savedList.filter(x=>x.agent===AGENT);$('saved').innerHTML=l.map(x=>`<option value="${x.id}">${esc(x.title)} (${x.events} steps)</option>`).join('')||'<option value="">none yet</option>'}
function loadSaved(){fetch('/api/saved').then(r=>r.json()).then(l=>{savedList=l;fillSaved()})}

function poll(){fetch(`/api/runs/${runId}?since=${next}`).then(r=>r.json()).then(s=>{
 next=s.next;s.events.forEach(e=>$('trace').insertAdjacentHTML('beforeend',evHtml(e)));
 setStatus(s.status.replace('_',' '));const hadGate=!!document.querySelector('#gate .gate');if(!hadGate||!s.gate)renderGate(s.gate);
 // keep the newest trace line in view, but when the agent is waiting for a human, bring the approval form (top of the column) into view instead
 const col=$('trace').parentElement;if(s.gate){if(!hadGate)col.scrollTop=0}else col.scrollTop=1e9;
 if(s.status==='done'){clearInterval(timer);result=s.result;renderOut();$('go').disabled=false;loadSaved()}
 if(s.status==='error'){clearInterval(timer);$('go').disabled=false;$('out').innerHTML=`<div style="color:var(--bad)">${esc(s.error)}</div>`}})}

function startRun(){$('trace').innerHTML='';$('out').innerHTML='';$('gate').innerHTML='';result=null;next=0;$('go').disabled=true;setStatus('starting');
 const body=buildBody();body.agent=AGENT;body.auto_approve=$('auto').checked;body.cache=$('cache').checked;body.backend=$('backend').value;
 post('/api/run',body).then(r=>{if(r.error){$('go').disabled=false;$('out').innerHTML=esc(r.error);return}runId=r.id;timer=setInterval(poll,700)})}

function replay(){const id=$('saved').value;if(!id)return;fetch('/api/saved/'+id).then(r=>r.json()).then(d=>{
 $('trace').innerHTML='';$('out').innerHTML='';$('gate').innerHTML='';setStatus('replaying');let i=0;
 const step=()=>{if(i>=d.events.length){result=d.result;setStatus('done (replay)');renderOut();return}
  $('trace').insertAdjacentHTML('beforeend',evHtml(d.events[i]));$('trace').parentElement.scrollTop=1e9;i++;setTimeout(step,i<4?400:180)};step()})}

window.addEventListener('DOMContentLoaded',()=>{$('go').onclick=startRun;$('replay').onclick=replay;loadSaved()});
