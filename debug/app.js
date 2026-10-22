const statusBox = document.querySelector('#status');
const list = document.querySelector('#requests');
const button = document.querySelector('#pause');
let paused = false;
let lastData = null;
function el(tag, text, className = '') {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  return node;
}
function ms(value) { return value == null ? '—' : `${Math.round(value)} ms`; }
function render(data) {
  const open = new Set([...list.querySelectorAll('details[open]')].map(n => n.dataset.id));
  list.replaceChildren();
  if (!data.requests.length) list.append(el('p', '暂无请求。按原方式对设备说一句中文，完成后会显示在这里。'));
  for (const row of data.requests) {
    const card = el('article', '');
    const top = el('div', '', 'top');
    top.append(el('span', `${new Date(row.time * 1000).toLocaleTimeString()} · ${row.mode === 'auto' ? '自动' : row.mode === 'manual' ? '按键' : '模式未知'}`));
    top.append(el('span', row.stage, row.stage === '失败' ? 'badge failure' : 'badge'));
    card.append(top);
    for (const [label, value] of [['中文识别', row.asr], ['英文结果', row.english]]) {
      const sentence = el('p', '', 'sentence');
      sentence.append(el('span', label, 'label'), document.createTextNode(value ?? '等待结果…'));
      card.append(sentence);
    }
    const timings = el('div', '', 'timings');
    for (const [label, key] of [['识别', 'asr_ms'], ['翻译', 'english_ms'], ['语音生成', 'tts_ms'], ['总耗时', 'total_ms']]) {
      timings.append(el('span', `${label} ${ms(row.timings[key])}`));
    }
    card.append(timings);
    if (row.error) card.append(el('p', `错误：${row.error}`, 'warning'));
    const details = el('details', '');
    details.dataset.id = row.request_id;
    details.open = open.has(row.request_id);
    details.append(el('summary', '请求编号与传输耗时'), el('p', `request_id: ${row.request_id}`),
      el('p', `录音上传 ${ms(row.timings.audio_upload_ms)} · 音频返回 ${ms(row.timings.return_ms)}`),
      el('p', row.playback_ack ? '已收到设备播放回执；实际是否发声需听感确认。' : '未收到设备播放回执。'));
    card.append(details);
    list.append(card);
  }
}
async function tick() {
  if (!paused) {
    try {
      const response = await fetch('/api/requests', {cache: 'no-store', signal: AbortSignal.timeout(4000)});
      if (!response.ok) throw new Error('HTTP failure');
      const data = await response.json();
      statusBox.className = data.log_status === 'ok' ? '' : 'error';
      const age = data.last_event_at == null ? null : Math.max(0, Math.floor(data.read_at - data.last_event_at));
      const note = data.log_status === 'missing' ? '日志尚未创建。' : data.log_status === 'unreadable' ? '日志无法读取，请检查启动时选择的文件。' : `日志读取正常 · ${age == null ? '暂无事件' : `最近事件在 ${age} 秒前`}`;
      statusBox.textContent = note + (data.truncated ? ' · 仅显示日志末尾，较早请求可能不完整。' : '') + (data.skipped_lines ? ` · 跳过 ${data.skipped_lines} 行损坏记录。` : '');
      const serialized = JSON.stringify(data.requests);
      if (lastData !== serialized) { render(data); lastData = serialized; }
    } catch {
      statusBox.className = 'error';
      statusBox.textContent = '无法连接调试页服务，保留上次结果。正在重试；这不代表语音后端停止。';
    }
  }
  setTimeout(tick, 1000);
}
button.addEventListener('click', () => {
  paused = !paused;
  button.textContent = paused ? '继续刷新' : '暂停刷新';
  if (paused) statusBox.textContent = '已暂停页面刷新，语音服务照常运行。';
});
tick();
