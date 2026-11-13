// Shared email UI for both agents: a "Connect Gmail" pop-up (one-time setup) and the preview / send panel for a finished run.
// Needs common.js first (uses $, esc, post, result, AGENT).
let mailInfo={configured:false};

function renderMailChip(){const b=$('mailBtn');if(!b)return;
 b.textContent=mailInfo.configured?`✉ ${mailInfo.account||'SMTP'} → ${mailInfo.redirect_to||'real recipients'}`:'✉ Connect Gmail';
 b.title=mailInfo.configured?'Email is connected. Click to change or disconnect.':'One-time setup: connect a Gmail account to send the agents\' emails'}
async function refreshMail(){try{mailInfo=await (await fetch('/api/mail_status')).json()}catch(_){}renderMailChip();return mailInfo}

function buildMailUi(){
 const hdr=document.querySelector('header'),st=$('status');
 const btn=document.createElement('button');btn.id='mailBtn';btn.className='ghost mailbtn';btn.style.marginLeft='auto';btn.onclick=openMailDialog;hdr.insertBefore(btn,st);st.style.marginLeft='0';
 const d=document.createElement('dialog');d.id='mailDlg';d.innerHTML=`
  <h3>Connect Gmail (one-time setup)</h3>
  <div class="data">After this, the agents can email whenever you use them. Nothing is sent until you click Send on a finished run.</div>
  <ol>
   <li>Your Google account needs <b>2-Step Verification</b> turned on (Google requires it for App passwords).</li>
   <li><a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener">Open Google App passwords</a>, sign in, create one named "AI Agent Demos", and copy the 16-letter code.</li>
   <li>Paste it below. It is checked against Gmail, then stored <b>encrypted on this PC</b> and never shown again.</li>
  </ol>
  <label>Gmail address</label><input id="mlEmail" type="email" autocomplete="off" placeholder="you@gmail.com">
  <label>App password (16 letters, not your normal password)</label><input id="mlPw" type="password" autocomplete="new-password" placeholder="xxxx xxxx xxxx xxxx">
  <label>Send every email to (demo recipient)</label><input id="mlTo" type="email" autocomplete="off">
  <div class="data">While this is filled in, every message goes <b>only</b> to this address, marked [DEMO], with the intended recipient written inside it. Clear it to send to the real recipients.</div>
  <div class="row"><button id="mlSave">Save and verify</button><button class="ghost" id="mlTest">Send test email</button></div>
  <div class="row"><button class="ghost" id="mlOff">Disconnect</button><button class="ghost" id="mlClose">Close</button></div>
  <div id="mlMsg" class="data" style="margin-top:10px"></div>`;
 document.body.appendChild(d);
 $('mlClose').onclick=()=>d.close();
 $('mlSave').onclick=async()=>{const m=$('mlMsg');m.className='data';m.textContent='Checking with Gmail...';$('mlSave').disabled=true;
  const r=await post('/api/mail_settings',{email:$('mlEmail').value,app_password:$('mlPw').value,redirect_to:$('mlTo').value});$('mlSave').disabled=false;
  if(r.error){m.className='data bad';m.textContent=r.error;return}
  $('mlPw').value='';m.className='data ok';m.textContent='Connected and saved. You will not need to do this again. Use "Send test email" to confirm.';await refreshMail()};
 $('mlTest').onclick=async()=>{const m=$('mlMsg');m.className='data';m.textContent='Sending a test email...';
  const r=await post('/api/mail_test',{});m.className='data '+(r.error?'bad':'ok');m.textContent=r.error||`Test email sent to ${r.to}. Check that inbox.`};
 $('mlOff').onclick=async()=>{await post('/api/mail_settings/clear',{});$('mlMsg').className='data';$('mlMsg').textContent='Disconnected. The saved password was deleted.';await refreshMail();fillMailForm()};
 refreshMail().then(()=>{fillMailForm();if(location.hash==='#mail')openMailDialog()})}
function fillMailForm(){$('mlEmail').value=mailInfo.account||'';$('mlTo').value=mailInfo.configured?(mailInfo.redirect_to||''):'saisrikiran_ipm25@iift.edu';$('mlPw').placeholder=mailInfo.configured?'(saved: leave blank to keep)':'xxxx xxxx xxxx xxxx'}
function openMailDialog(){fillMailForm();$('mlMsg').textContent='';$('mailDlg').showModal()}

// ---- preview / send panel for a finished run (both agents)
async function mailPlan(send){const body={run_id:result.run_id,dry_run:!send,confirm:!!send};
 if(send){const pre=await post('/api/send',{run_id:result.run_id,dry_run:true});if(pre.error){$('mailOut').innerHTML=`<span class="bad">${esc(pre.error)}</span>`;return}
  const ready=(pre.results||[]).filter(x=>x.status==='would send');
  if(!ready.length){alert('Nothing is ready to send.');return}
  if(!confirm('Send '+ready.length+' real email(s)?\n\n'+ready.map(x=>'To '+x.to+': '+x.subject).join('\n')))return}
 const r=await post('/api/send',body);
 $('mailOut').innerHTML=r.error?`<span class="bad">${esc(r.error)}</span>`:`<div class="data">${r.dry_run?'Preview only, nothing sent.':'Done.'}${r.redirect_to?` Demo mode: everything goes to ${esc(r.redirect_to)}.`:''}</div>`+r.results.map(x=>`<div class="q"><b>${esc(x.status)}</b> · ${esc(x.kind)} · ${esc(x.to||'no recipient')}${x.intended?` (intended: ${esc(x.intended)})`:''} · ${esc(x.subject)}${x.reason?' <i>('+esc(x.reason)+')</i>':''}</div>`).join('')}
function mailPanel(){const what=AGENT==='a'?'the approved outreach email':'the reviewed drafts';
 return `<details style="margin-bottom:8px"><summary>Email: ${mailInfo.configured?`connected as ${esc(mailInfo.account||'SMTP')}${mailInfo.redirect_to?' · demo mode → '+esc(mailInfo.redirect_to):''}`:'not connected (preview only)'}</summary>
 <div class="row"><button class="ghost" onclick="mailPlan(false)">Preview recipients</button><button class="ghost" ${mailInfo.configured?'':'disabled'} onclick="mailPlan(true)">Send ${what}</button></div>
 ${mailInfo.configured?'':'<div class="data">Click <b>✉ Connect Gmail</b> at the top to set this up once.</div>'}<div id="mailOut"></div></details>`}

window.addEventListener('DOMContentLoaded',buildMailUi);
