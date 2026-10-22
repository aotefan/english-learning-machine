const labels = {asr:'语音识别 ASR',english:'英语生成 LLM',tts:'英语合成 TTS'};
const money = value => value == null ? '待核对' : `¥${value.toFixed(6)}`;
function node(tag, text) { const n=document.createElement(tag); n.textContent=text; return n; }
function row(parent, values) { const tr=document.createElement('tr'); for (const v of values) tr.append(node('td',v)); parent.append(tr); }
let budgetLoaded=false, budgetDirty=false, budgetSaving=false, refreshVersion=0;
function renderBudget(runtime) {
  const fields=[document.querySelector('#budget-turns'),document.querySelector('#budget-money')];
  const enabled=runtime?.budget?.enabled===true;
  for(const field of fields)field.disabled=!enabled||budgetSaving;
  document.querySelector('#budget-save').disabled=!enabled||budgetSaving;
  if(!enabled){document.querySelector('#budget-status').textContent='当前未启用语音调用，暂不能调整额度。';return;}
  // Periodic polling must not erase unsaved edits, including focused fields.
  if(!budgetDirty&&!budgetSaving){fields[0].value=runtime.budget.turn_limit;fields[1].value=runtime.budget.limit_cny;}
  if(!budgetLoaded)document.querySelector('#budget-status').textContent='修改后点击保存，今日用量会保留。';
  budgetLoaded=true;
}
function renderDiagnostics(data) {
  const runtime=document.querySelector('#runtime');runtime.replaceChildren();
  const r=data.runtime;
  renderBudget(r);
  if (r) {
    runtime.append(node('p',r.mode==='STANDBY'?'待机模式：收费调用关闭':'运行模式：已启用有预算的模型调用'));
    runtime.append(node('p',`在线连接 ${r.active_connections} / ${r.connection_limit} · 本次运行 ${r.uptime_seconds} 秒`));
    const b=r.budget;
    runtime.append(node('p',b.enabled?`${b.utc_day}（UTC）剩余额度 ${money(b.remaining_cny)} / ${money(b.limit_cny)}，剩余 ${b.remaining_turns} / ${b.turn_limit} 轮`:'日预算尚未启用；本页不能开启收费调用。'));
    if (r.missing_configuration.length) {
      const names={DOUBAO_SPEECH_API_KEY:'语音 API 密钥',ARK_API_KEY:'文本模型密钥',ARK_MODEL_ID:'文本模型 ID',DOUBAO_TTS_SPEAKER:'TTS 音色',DAILY_TURN_LIMIT:'每日轮数',DAILY_CNY_LIMIT:'每日预算'};
      runtime.append(node('p','待填写：'+r.missing_configuration.map(k=>names[k]??k).join('、')));
    }
  } else runtime.append(node('p','运行状态暂不可用。'));
  document.querySelector('#diagnostic-counts').textContent=`当前窗口 ${data.requests_in_window} 次请求 · 完成 ${data.completed} · 成功 ${data.succeeded} · 失败 ${data.failed}`;
  const labels={audio_duration_ms:'录音时长',asr_ms:'语音识别',english_ms:'英语生成',tts_ms:'语音合成',return_ms:'音频下发（含节奏控制）',total_ms:'请求总耗时',stop_to_playback_ack_ms:'录音停止至播放回执',vad_stop_ms:'VAD 判停',network_upload_ms:'纯上传网络耗时'};
  const table=document.querySelector('#latencies');table.replaceChildren();
  const ms=n=>n==null?'未测量':`${Math.round(n)} ms`;
  for(const [key,s] of Object.entries(data.timings))row(table,[labels[key]??key,s.samples,ms(s.mean_ms),ms(s.p95_ms)]);
  const recent=document.querySelector('#diagnostic-recent');recent.replaceChildren();
  recent.append(node('h3','最近请求诊断'));
  const errors={empty_audio:'未收到音频',standby:'待机模式，模型调用已关闭',budget_exhausted:'日预算或轮数已耗尽',rate_limited:'请求过于频繁',credential_revoked:'设备凭据已撤销',asr_failed:'语音识别失败',english_failed:'英语生成失败',tts_failed:'语音合成失败',timeout:'请求超时',other_error:'其他错误，需结合服务日志排查'};
  for(const item of data.recent){
    const block=document.createElement('details');
    const state=item.success===true?'处理完成':item.success===false?'失败':'尚无完成记录';
    block.append(node('summary',`${new Date(item.time*1000).toLocaleString('zh-CN',{hour12:false})} · ${state}`),node('p',`请求编号：${item.request_id}`));
    if(item.error_code)block.append(node('p',errors[item.error_code]??'未知错误'));
    block.append(node('p',item.playback_reported?'已收到客户端播放回执':'未收到客户端播放回执'));
    recent.append(block);
  }
  if(!data.recent.length)recent.append(node('p','尚无请求记录。'));
}
function render(data) {
  document.querySelector('#total').textContent=money(data.estimated_total_cny);
  document.querySelector('#unknown').textContent=data.uncertain_calls;
  const stages=document.querySelector('#stages');stages.replaceChildren();
  for (const [name,s] of Object.entries(data.stages)) {
    const units=name==='asr' ? `${s.input_units.toFixed(2)} 秒` : name==='english' ? `输入 ${s.input_units} / 输出 ${s.output_units} Token` : `${s.input_units} 计费字符`;
    row(stages,[labels[name],s.calls,units,money(s.estimated_cny),s.uncertain_calls]);
  }
  const days=document.querySelector('#days');days.replaceChildren();
  for (const d of data.days) row(days,[d.day,d.turns,...['asr','english','tts'].map(s=>money(d.stages[s]?.estimated_cny ?? 0)+(d.stages[s]?.uncertain_calls?' + 待核对':'')),money(d.reserved_cny)]);
  if (!data.days.length) row(days,['暂无真实调用记录','—','—','—','—','—']);
  const recent=document.querySelector('#recent');recent.replaceChildren();
  for (const r of data.recent) {
    const box=document.createElement('details');
    const state={success:'链路完成',failed:'请求失败',processing:'处理中',interrupted:'服务中断'}[r.status] ?? r.status;
    box.append(node('summary',`${new Date(r.time*1000).toLocaleString('zh-CN',{hour12:false})} · ${state}`),node('p',`请求编号：${r.request_id}`));
    for (const s of r.stages) box.append(node('p',`${labels[s.stage]}：${money(s.estimated_cny)}${s.status==='unknown'||s.status==='pending'?'（用量待核对）':''}`));
    recent.append(box);
  }
  if (!data.recent.length) recent.append(node('p','尚未执行有预算的真实 API 调用。'));
  const infra=document.querySelector('#infrastructure');infra.replaceChildren();
  const bill=data.infrastructure_cost;
  if (bill) {
    infra.append(node('p',`${bill.billing_cycle} 账期 · 应付金额 ${money(bill.amount_due_cny)}`));
    for (const item of bill.items) infra.append(node('p',`${item.product} · 服务周期 ${item.service_period ?? '—'} ${item.period_unit ?? ''} · ${money(item.amount_due_cny)}`));
    infra.append(node('p',`来源：阿里云账单 API · 最近同步 ${new Date(bill.checked_at).toLocaleString('zh-CN',{hour12:false})}`));
  } else infra.append(node('p','暂未取得云服务器账单，金额未知。'));
}
async function refresh() {
  const version=++refreshVersion;
  const status=document.querySelector('#status');
  try {
    const [r,d]=await Promise.all(['/api/costs','/api/diagnostics'].map(url=>fetch(url,{cache:'no-store',signal:AbortSignal.timeout(5000)})));
    if (!r.ok || !d.ok) throw new Error('request failed');
    const [data,diagnostics]=await Promise.all([r.json(),d.json()]);
    if(version!==refreshVersion)return;
    render(data);renderDiagnostics(diagnostics);
    status.textContent=`已更新 · ${new Date().toLocaleTimeString('zh-CN',{hour12:false})} · API 费用为估算，豆包账单待核对`;
  } catch {if(version===refreshVersion)status.textContent='刷新失败，以下内容可能已过期；请检查后台连接或登录后重试。';}
}
document.querySelector('#refresh').addEventListener('click',refresh);
for(const field of document.querySelectorAll('#budget-form input'))field.addEventListener('input',()=>{budgetDirty=true;});
document.querySelector('#budget-form').addEventListener('submit',async event=>{
  event.preventDefault();if(budgetSaving)return;
  const form=event.currentTarget;if(!form.reportValidity())return;
  const turn_limit=Number(document.querySelector('#budget-turns').value);
  const limit_cny=document.querySelector('#budget-money').value;
  const status=document.querySelector('#budget-status');status.classList.remove('error');
  budgetSaving=true;refreshVersion++;document.querySelector('#budget-save').disabled=true;
  for(const field of form.querySelectorAll('input'))field.disabled=true;
  status.textContent='正在保存…';
  try{
    const response=await fetch('/api/budget',{method:'POST',headers:{'Content-Type':'application/json','X-English-Edge-Action':'budget'},body:JSON.stringify({turn_limit,limit_cny}),signal:AbortSignal.timeout(10000)});
    if(!response.ok)throw new Error(response.status===401?'login':response.status===400?'invalid':'save');
    const data=await response.json();budgetDirty=false;
    status.textContent=`已保存：每日 ${data.budget.turn_limit} 轮 · 费用保护上限 ¥${data.budget.limit_cny}。今日已用 ${data.budget.turns} 轮，剩余 ${data.budget.remaining_turns} 轮。`;
  }catch(error){
    status.classList.add('error');
    status.textContent=error.message==='login'?'登录已失效，请重新登录后保存。':error.message==='invalid'?'请输入有效轮数和最多两位小数的费用上限。':'保存未确认，输入已保留；请刷新核对当前额度后再试。';
  }finally{
    budgetSaving=false;for(const field of form.querySelectorAll('input'))field.disabled=false;
    document.querySelector('#budget-save').disabled=false;
    await refresh();
  }
});
refresh();setInterval(refresh,15000);
