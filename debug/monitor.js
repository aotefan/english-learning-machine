const statusBox = document.querySelector('#status');
const list = document.querySelector('#requests');
const pauseButton = document.querySelector('#pause');
const availabilityBox = document.querySelector('#availability');
let paused = false;
let lastRequests = '';
const devicePanel = document.querySelector('.device-panel');
const deviceMessage = document.querySelector('#device-message');
const volumeRange = document.querySelector('#volume-range');
let deviceState = null;
let controlPending = false;
let controlMessageUntil = 0;
let controlRevision = 0;
const teachingNames = {express:'英语表达', chat:'陪练模式'};
const listeningNames = {auto:'自动监听', manual:'按键输入'};
function controlMessage(text, error = false) {
  deviceMessage.textContent = text;
  deviceMessage.classList.toggle('error', error);
  controlMessageUntil = Date.now() + 12000;
}
function renderDevice(data) {
  deviceState = data;
  document.querySelector('#device-online').textContent = data?.online ? '设备在线' : '设备未连接';
  document.querySelector('#device-volume').textContent = data?.volume == null ? '未确认'
    : `${data.volume}%${data.status_error ? '（上次确认）' : ''}`;
  document.querySelector('#device-teaching').textContent = teachingNames[data?.teaching_mode] || '未确认';
  document.querySelector('#device-listening').textContent = listeningNames[data?.listening_mode] || '未确认';
  for (const button of devicePanel.querySelectorAll('button[data-mode]')) {
    const teaching = button.dataset.modeKind === 'teaching';
    const current = teaching ? data?.teaching_mode : data?.listening_mode;
    button.setAttribute('aria-pressed', String(button.dataset.mode === current));
    button.disabled = controlPending || !data?.online || (teaching ? !current
      : !data.controls_available || (button.dataset.mode === 'auto' && !data.automatic_available));
  }
  for (const node of devicePanel.querySelectorAll('#volume-form button, #volume-range')) {
    node.disabled = controlPending || !data?.online || !data.controls_available;
  }
  if (!controlPending && Date.now() >= controlMessageUntil) {
    deviceMessage.classList.remove('error');
    deviceMessage.textContent = !data?.online ? '请给设备通电并连接 Wi-Fi，连接后即可控制。'
      : !data.controls_available ? '当前固件需要更新才能远程调整音量、切换收音方式；交流模式可先切换。'
      : data.status_error ? '暂时未读到设备回执；这里保留上次确认的状态。'
      : '音量、收音方式以设备回执为准；交流模式由云端处理。';
  }
}
async function refreshDevice() {
  const revision = controlRevision;
  const response = await fetch('/api/device', {cache:'no-store', signal:AbortSignal.timeout(15000)});
  if (!response.ok) throw new Error('device status unavailable');
  const data = await response.json();
  if (revision === controlRevision) renderDevice(data);
}
async function sendControl(action, body) {
  if (controlPending) return;
  controlPending = true;
  controlRevision++;
  renderDevice(deviceState);
  controlMessage('正在等待确认…');
  try {
    const response = await fetch(`/api/device/${action}`, {
      method:'POST', headers:{'Content-Type':'application/json','X-English-Edge-Action':action},
      body:JSON.stringify(body), signal:AbortSignal.timeout(35000)
    });
    if (response.status === 401) throw new Error('登录已失效，请重新登录。');
    if (!response.ok) throw new Error('未确认操作结果。请确认设备在线，并等当前收音、播报结束后重试。');
    const result = await response.json();
    controlRevision++;
    if (action === 'volume') {
      if (result.confirmed !== true || result.volume !== body.volume) throw new Error('设备音量尚未确认。');
      volumeRange.value = String(result.volume);
      document.querySelector('#volume-target').textContent = `${result.volume}%`;
      deviceState = {...deviceState, volume:result.volume, status_error:false};
      controlMessage(`设备已确认音量 ${result.volume}%。`);
    } else {
      if (result.mode !== body.mode || !['cloud','device'].includes(result.confirmed_by)) throw new Error('模式尚未确认。');
      deviceState = {...deviceState, [body.kind === 'teaching' ? 'teaching_mode' : 'listening_mode']:result.mode};
      controlMessage(`${result.confirmed_by === 'cloud' ? '云端' : '设备'}已切换为${(body.kind === 'teaching' ? teachingNames : listeningNames)[body.mode]}。`);
    }
    // A read failure must not turn an acknowledged action into a failed action.
    try { await refreshDevice(); } catch { /* Next poll refreshes the snapshot. */ }
  } catch (error) {
    controlMessage(error.name === 'TimeoutError' ? '等待回执超时，操作结果尚未确认；请查看设备状态。' : error.message, true);
  } finally {
    controlPending = false;
    renderDevice(deviceState);
  }
}
volumeRange.addEventListener('input', () => {
  document.querySelector('#volume-target').textContent = `${volumeRange.value}%`;
});
document.querySelector('#volume-form').addEventListener('submit', event => {
  event.preventDefault();
  sendControl('volume', {volume:Number(volumeRange.value)});
});
document.querySelector('#volume-max').addEventListener('click', () => sendControl('volume', {volume:100}));
for (const button of devicePanel.querySelectorAll('button[data-mode]')) {
  button.addEventListener('click', () => sendControl('mode', {kind:button.dataset.modeKind, mode:button.dataset.mode}));
}
function el(tag, value, className = '') {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}
function time(seconds) { return new Date(seconds * 1000).toLocaleString('zh-CN', {hour12: false}); }
function duration(value) { return value == null ? '—' : `${Math.round(value)} ms`; }
function renderAvailability(runtime) {
  const budget = runtime?.budget;
  const exhausted = runtime?.mode === 'LIVE' && budget?.enabled &&
    (budget.remaining_turns === 0 || budget.remaining_cny === 0);
  document.querySelector('#mode').textContent = runtime?.mode === 'STANDBY' ? '待机'
    : exhausted ? '额度已用完' : runtime?.mode === 'LIVE' ? '已启用' : '未知';
  availabilityBox.classList.toggle('error', Boolean(exhausted));
  if (!runtime) {
    availabilityBox.textContent = '暂时无法确认语音服务及可用额度。';
    return;
  }
  if (!budget?.enabled) {
    availabilityBox.textContent = runtime.mode === 'STANDBY'
      ? '语音服务处于待机状态，暂不处理新的语音请求。' : '暂时无法确认可用额度。';
    return;
  }
  const turns = Number.isFinite(budget.remaining_turns) && Number.isFinite(budget.turn_limit)
    ? `今日剩余 ${budget.remaining_turns} / ${budget.turn_limit} 轮` : '今日剩余轮数未知';
  const reason = exhausted ? (budget.remaining_turns === 0 ? '今日语音轮数已用完' : '今日费用额度已用完') : '';
  const dayStart = typeof budget.utc_day === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(budget.utc_day)
    ? Date.parse(`${budget.utc_day}T00:00:00Z`) : NaN;
  const reset = Number.isFinite(dayStart)
    ? `额度于 ${time((dayStart + 86400000) / 1000)}（本地时间）按 UTC 新的一天重置`
    : '额度按 UTC 新的一天重置';
  availabilityBox.textContent = [turns, reason, reset].filter(Boolean).join(' · ');
}
function ageText(seconds) {
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}
function renderRequests(data) {
  const opened = new Set([...list.querySelectorAll('details[open]')].map(node => node.dataset.id));
  list.replaceChildren();
  document.querySelector('#count').textContent = `${data.requests.length} 条记录`;
  if (!data.requests.length) {
    list.append(el('p', '暂无请求记录。设备开始收音后会在这里显示进度。', 'empty-state'));
    return;
  }
  for (const [index, row] of data.requests.entries()) {
    const item = document.createElement('article');
    item.className = 'monitor-item';
    const top = document.createElement('div'); top.className = 'monitor-item-head';
    top.append(el('span', String(index + 1).padStart(2, '0'), 'review-index'));
    top.append(el('span', time(row.time), 'monitor-time'));
    top.append(el('span', row.mode === 'auto' ? '自动' : row.mode === 'manual' ? '按键' : '模式未知', 'monitor-mode'));
    if (row.intent) top.append(el('span', ({express:'英文表达', chat:'对话陪练', mode:'切换模式', repeat:'重说上句', slow:'慢速重说', partial_repeat:'重说片段', hint:'表达提示', explain:'解释并重听', explain_only:'只解释意思', clarify:'追问细节', correct:'修正理解', stop:'停止', no_change:'无需重复'})[row.intent] || '处理中', 'monitor-mode'));
    top.append(el('span', row.stage, row.stage === '失败' ? 'monitor-stage failed' : 'monitor-stage'));
    item.append(top);
    const text = document.createElement('div'); text.className = 'monitor-text';
    const finished = row.success != null || Boolean(row.error) || row.stage === '失败';
    for (const [label, value] of [['识别内容', row.asr], ['回复内容', row.english]]) {
      const field = document.createElement('div');
      const empty = finished ? (label === '识别内容' ? '未获得识别结果' : row.intent === 'no_change' ? '无需重复，继续交流即可。' : '未生成回复') : '等待结果…';
      field.append(el('span', label, 'monitor-label'), el('p', value || empty, value ? '' : 'muted'));
      text.append(field);
    }
    item.append(text);
    const timings = el('p', `识别 ${duration(row.timings.asr_ms)} · 回复生成 ${duration(row.timings.english_ms)} · 合成 ${duration(row.timings.tts_ms)} · 总耗时 ${duration(row.timings.total_ms)}`, 'monitor-timings');
    item.append(timings);
    if (row.error) item.append(el('p', row.error, 'monitor-error'));
    const details = document.createElement('details'); details.dataset.id = row.request_id;
    details.open = opened.has(row.request_id);
    details.append(el('summary', '请求详情'), el('p', `请求编号：${row.request_id}`),
      el('p', `录音上传 ${duration(row.timings.audio_upload_ms)} · 音频返回 ${duration(row.timings.return_ms)}`),
      el('p', row.intent === 'no_change' ? '本轮无需语音播放。' : row.playback_ack ? '已收到设备播放回执；是否实际发声需听感确认。' : '未收到设备播放回执。'));
    item.append(details);
    list.append(item);
  }
}
async function tick() {
    try {
      if (paused) return;
      const [eventsResponse, diagnosticsResponse] = await Promise.all([
        fetch('/api/requests', {cache: 'no-store', signal: AbortSignal.timeout(5000)}),
        fetch('/api/diagnostics', {cache: 'no-store', signal: AbortSignal.timeout(5000)})
      ]);
      if (!eventsResponse.ok || !diagnosticsResponse.ok) throw new Error('request failed');
      const events = await eventsResponse.json();
      const diagnostics = await diagnosticsResponse.json();
      if (paused) return;
      const runtime = diagnostics.runtime;
      document.querySelector('#latest').textContent = events.last_event_at == null ? '暂无事件' : time(events.last_event_at);
      document.querySelector('#connections').textContent = runtime ? `${runtime.active_connections} / ${runtime.connection_limit}` : '未知';
      renderAvailability(runtime);
      try { await refreshDevice(); } catch {
        if (Date.now() >= controlMessageUntil) controlMessage('设备状态暂时无法读取，稍后自动重试。', true);
      }
      const age = events.last_event_at == null ? null : Math.max(0, Math.floor(events.read_at - events.last_event_at));
      statusBox.textContent = events.log_status === 'ok'
        ? `每 2 秒更新 · ${age == null ? '暂无事件' : `最近事件 ${ageText(age)}`}${events.truncated ? ' · 较早记录已截断' : ''}`
        : events.log_status === 'missing' ? '事件记录尚未创建。' : '事件记录暂时无法读取。';
      statusBox.classList.toggle('error', events.log_status !== 'ok');
      const serialized = JSON.stringify(events.requests);
      if (serialized !== lastRequests) { renderRequests(events); lastRequests = serialized; }
    } catch {
      if (paused) return;
      statusBox.textContent = '读取失败，正在重试；上次结果仍显示在页面上。';
      statusBox.classList.add('error');
    } finally {
      setTimeout(tick, 2000);
    }
}
pauseButton.addEventListener('click', () => {
  paused = !paused;
  pauseButton.textContent = paused ? '继续刷新' : '暂停刷新';
  if (paused) statusBox.textContent = '已暂停页面刷新；云端语音服务继续运行。';
  else statusBox.textContent = '正在恢复刷新…';
});
tick();
