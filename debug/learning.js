const cards = document.querySelector('#cards');
const status = document.querySelector('#status');
const candidates = document.querySelector('#candidates');
const candidateStatus = document.querySelector('#candidate-status');
const recordings = new Set();
let activeRecording = null;
let pendingRecording = null;
let recordingGeneration = 0;
const microphone = document.querySelector('#microphone');
const AudioContextClass = window.AudioContext || window.webkitAudioContext;
let reviewView = 'due';
let refreshGeneration = 0;
const viewNames = {due:'到期复习', needs_help:'最近需要帮助', all:'全部表达'};

async function updateMicrophones() {
  if (!navigator.mediaDevices?.enumerateDevices || pendingRecording || activeRecording) return;
  try {
    const selected = microphone.value;
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter(item => item.kind === 'audioinput');
    microphone.replaceChildren(new Option('系统默认麦克风', ''));
    for (const [index, device] of devices.entries()) {
      if (device.deviceId && device.deviceId !== 'default') {
        microphone.add(new Option(device.label || `麦克风 ${index + 1}`, device.deviceId));
      }
    }
    if ([...microphone.options].some(item => item.value === selected)) microphone.value = selected;
  } catch { /* Recording will show a specific permission/device error. */ }
}
function pauseAudio() {
  document.querySelectorAll('audio').forEach(audio => audio.pause());
  if ('speechSynthesis' in window) speechSynthesis.cancel();
}
function releaseInput(session) {
  clearTimeout(session.timer);
  cancelAnimationFrame(session.animation);
  session.stream?.getTracks().forEach(track => track.stop());
  if (session.meter) session.meter.hidden = true;
}
function closeAudioContext(session) {
  if (session.context?.state !== 'closed') session.context?.close().catch(() => {});
}
function replayWav(buffer) {
  if (!buffer.length || buffer.duration > 15) throw new Error('invalid_recording');
  let samples; let energy = -1; let peak = 0;
  // Preserve the strongest channel instead of cancelling opposing stereo signals.
  for (let channel = 0; channel < buffer.numberOfChannels; channel++) {
    const values = buffer.getChannelData(channel);
    let total = 0; let maximum = 0;
    for (const value of values) { total += value * value; maximum = Math.max(maximum, Math.abs(value)); }
    if (total > energy) { energy = total; peak = maximum; samples = values; }
  }
  if (!samples || peak < 0.002 || Math.sqrt(energy / samples.length) < 0.0003) {
    throw new Error('silent_recording');
  }
  const gain = Math.max(1, Math.min(4, 0.8 / peak));
  const data = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(data);
  const word = (offset, text) => [...text].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
  word(0, 'RIFF'); view.setUint32(4, data.byteLength - 8, true); word(8, 'WAVE'); word(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, buffer.sampleRate, true); view.setUint32(28, buffer.sampleRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); word(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  for (let index = 0; index < samples.length; index++) {
    const value = Math.max(-1, Math.min(1, samples[index] * gain));
    view.setInt16(44 + index * 2, Math.round(value * (value < 0 ? 32768 : 32767)), true);
  }
  return new Blob([data], {type: 'audio/wav'});
}
const reviewSupport = new Map();
let supportStorageAvailable = true;
try {
  const probe = `english-edge-review-probe:${Date.now()}`;
  sessionStorage.setItem(probe, '1');
  sessionStorage.removeItem(probe);
} catch { supportStorageAvailable = false; }

function savedSupport(card) {
  const key = `english-edge-review-support:${card.review_key || card.request_id}`;
  let saved = reviewSupport.get(key);
  if (!saved) {
    try {
      saved = JSON.parse(sessionStorage.getItem(key));
      if (!saved && card.review_key) saved = JSON.parse(sessionStorage.getItem(`english-edge-review-support:${card.request_id}`));
    }
    catch { supportStorageAvailable = false; }
  }
  return saved;
}
function readSupport(card) {
  const saved = savedSupport(card);
  // Only a newer completed review starts another attempt. Retention, rejection,
  // or a different representative record must not erase already-used help.
  if (!supportStorageAvailable) return 'answer';
  if (!saved) return 'none';
  if (!['hint', 'answer'].includes(saved.support)) return 'answer';
  if (Number.isFinite(saved.reviewedAt) && Number(card.last_reviewed_at || 0) > saved.reviewedAt) return 'none';
  return saved.support;
}
function rememberSupport(card, support) {
  const key = `english-edge-review-support:${card.review_key || card.request_id}`;
  const previous = savedSupport(card);
  const saved = {reviewedAt: Math.max(Number(card.last_reviewed_at || 0), Number(previous?.reviewedAt || 0)),
    support: readSupport(card) === 'answer' ? 'answer' : support};
  reviewSupport.set(key, saved);
  // Keep only the card identifier and help state, never the sentence or audio.
  try { sessionStorage.setItem(key, JSON.stringify(saved)); } catch { supportStorageAvailable = false; }
}
function sentenceHints(text) {
  const words = [...text.matchAll(/[\p{L}\p{N}]+(?:['’‑-][\p{L}\p{N}]+)*/gu)];
  if (words.length < 2) return [];
  const first = Math.min(2, words.length - 1);
  return [...new Set([first, Math.min(first + 2, words.length - 1)])].map(count => {
    const word = words[count - 1];
    return text.slice(0, word.index + word[0].length).trimEnd() + ' …';
  });
}

function el(tag, text, className) {
  const item = document.createElement(tag);
  item.textContent = text;
  if (className) item.className = className;
  return item;
}
function dateTime(seconds) { return new Date(seconds * 1000).toLocaleString('zh-CN', {hour12: false}); }
function clearRecordings() {
  recordingGeneration++;
  if (pendingRecording) {
    pendingRecording.cancelled = true;
    releaseInput(pendingRecording); closeAudioContext(pendingRecording);
    pendingRecording = null;
  }
  if (activeRecording) {
    activeRecording.cancelled = true;
    activeRecording.recorder.onstop = null;
    if (activeRecording.recorder.state !== 'inactive') activeRecording.recorder.stop();
    releaseInput(activeRecording); closeAudioContext(activeRecording);
    activeRecording = null;
  }
  microphone.disabled = false;
  pauseAudio();
  for (const url of recordings) URL.revokeObjectURL(url);
  recordings.clear();
}
function render(data) {
  clearRecordings();
  cards.replaceChildren();
  reviewView = data.view;
  for (const [id, key] of [['due','due_count'], ['selected','selected_count'],
    ['independent','independent_count'], ['assisted','assisted_count']]) {
    document.querySelector(`#progress-${id}`).textContent = data.summary[key];
  }
  for (const button of document.querySelectorAll('[data-review-view]')) {
    button.setAttribute('aria-pressed', String(button.dataset.reviewView === reviewView));
  }
  status.textContent = `${viewNames[reviewView]} ${data.matched_count} 句${data.truncated ? ' · 仅显示前 50 句' : ''} · 最近 7 天`;
  if (!data.cards.length) {
    const empty = !data.summary.selected_count ? '还没有挑选复习内容，可从下方最近表达中加入。'
      : reviewView === 'due' ? '当前没有到期复习，可以查看全部表达自由练习。'
      : '目前没有最近一次记录为需要帮助的句子；这不代表其他句子已经掌握。';
    cards.append(el('p', empty, 'empty-state'));
  }
  for (const [index, card] of data.cards.entries()) {
    const box = document.createElement('article'); box.className = 'review-card';
    const head = document.createElement('div'); head.className = 'review-card-head';
    head.append(el('span', `${String(index + 1).padStart(2, '0')} / ${String(data.cards.length).padStart(2, '0')}`, 'review-index'));
    head.append(el('h2', card.due ? '待复习' : '已安排下次复习', card.due ? 'due' : 'muted'));
    box.append(head, el('p', card.asr_raw, 'phrase'));
    let support = readSupport(card);
    const english = el('p', support === 'none' ? '先自己说一句，再核对；卡住时可以要一点提示。' :
      '这轮已经用过帮助，可以再试一次。', 'english muted'); box.append(english);
    english.setAttribute('aria-live', 'polite');
    const meta = el('p', '', 'review-meta');
    const updateMeta = () => {
      const reviewText = card.due ? `已复习 ${card.review_count} 次` :
        `已复习 ${card.review_count} 次 · 下次 ${dateTime(card.next_review_at)}`;
      meta.textContent = `${dateTime(card.time)} · ${reviewText} · 自评独立说出 ${card.independent_count || 0} 次 · 需要帮助 ${card.assisted_count || 0} 次 · 已跟读 ${card.practice_count || 0} 次${card.occurrence_count > 1 ? ` · 已合并 ${card.occurrence_count} 次相同表达` : ''}`;
    };
    updateMeta(); box.append(meta);
    const actions = document.createElement('div'); actions.className = 'actions';
    const attempted = el('button', '我说好了，核对');
    const hint = el('button', '给一点提示');
    const reveal = el('button', '直接看答案');
    const hints = sentenceHints(card.english);
    let hintIndex = 0;
    hint.disabled = !hints.length;
    if (!hints.length) hint.title = '这句很短，可以自己试试或直接看答案。';
    const feedbackActions = document.createElement('div'); feedbackActions.className = 'actions';
    feedbackActions.hidden = true;
    const independent = el('button', '记为自己说出'); independent.disabled = true;
    const assisted = el('button', '还需要帮助'); assisted.disabled = true;
    const feedback = el('p', '', 'practice-status'); feedback.setAttribute('role', 'status');
    const practiceActions = document.createElement('div'); practiceActions.className = 'actions';
    practiceActions.hidden = true;
    const listen = el('button', '听英文'); listen.disabled = true;
    const record = el('button', '录音跟读'); record.disabled = true;
    const replay = el('button', '回听录音'); replay.disabled = true;
    const practiced = el('button', '已跟读'); practiced.disabled = true;
    const practiceStatus = el('p', '核对后还可以听示范、录音跟读。', 'practice-status');
    practiceStatus.setAttribute('role', 'status');
    const playback = document.createElement('audio');
    playback.controls = true; playback.hidden = true;
    playback.setAttribute('aria-label', '回听自己的跟读录音');
    playback.preload = 'auto';
    const meter = document.createElement('meter'); meter.min = 0; meter.max = 1; meter.value = 0;
    meter.className = 'recording-level'; meter.hidden = true; meter.setAttribute('aria-label', '麦克风输入音量');
    let audioUrl = null;
    let proposedOutcome = 'assisted';
    let outcomeSupport = support;
    hint.addEventListener('click', () => {
      support = support === 'answer' ? 'answer' : 'hint';
      rememberSupport(card, support);
      english.textContent = hints[hintIndex++];
      english.classList.remove('muted');
      hint.disabled = hintIndex >= hints.length;
      hint.textContent = hint.disabled ? '提示到这里' : '再提示一点';
      feedback.textContent = '试着把话说完整。提示后说出也会记为需要帮助。';
    });
    function showAnswer(didAttempt) {
      proposedOutcome = didAttempt && support === 'none' ? 'independent' : 'assisted';
      outcomeSupport = didAttempt ? support : 'answer';
      rememberSupport(card, 'answer');
      if (!supportStorageAvailable) { proposedOutcome = 'assisted'; outcomeSupport = 'answer'; }
      english.textContent = card.english;
      english.classList.remove('muted');
      actions.hidden = true;
      feedbackActions.hidden = !card.due;
      practiceActions.hidden = false;
      independent.textContent = proposedOutcome === 'independent' ? '记为自己说出' :
        outcomeSupport === 'hint' ? '记为提示后说出' : '记为需要帮助';
      independent.disabled = !card.due;
      assisted.disabled = !card.due;
      assisted.hidden = !didAttempt;
      feedback.textContent = card.due ? (didAttempt ?
        '核对你刚才的表达：意思一致即可，不必逐字相同。由你确认这次结果。' :
        '可以先学会这句，再记录本次需要帮助。') : '本次可自由练习，到了下次复习时间再记录结果。';
      listen.disabled = !('speechSynthesis' in window);
      record.disabled = !(navigator.mediaDevices?.getUserMedia && window.MediaRecorder && AudioContextClass);
      practiceStatus.textContent = record.disabled ? '此浏览器不支持录音，请换用支持麦克风的浏览器。' :
        '先听英文示范，再录下自己的跟读；录音仅在本次页面保留。';
    }
    attempted.addEventListener('click', () => showAnswer(true));
    reveal.addEventListener('click', () => showAnswer(false));
    listen.addEventListener('click', () => {
      if (pendingRecording || activeRecording) { practiceStatus.textContent = '请先结束录音，再听英文。'; return; }
      pauseAudio();
      const utterance = new SpeechSynthesisUtterance(card.english);
      utterance.lang = 'en-US'; utterance.rate = 0.88;
      const voice = speechSynthesis.getVoices().find(item => item.lang.toLowerCase().startsWith('en'));
      if (voice) utterance.voice = voice;
      speechSynthesis.speak(utterance);
    });
    record.addEventListener('click', async () => {
      if (activeRecording?.cardId === card.request_id) {
        if (activeRecording.recorder.state !== 'inactive') activeRecording.recorder.stop();
        return;
      }
      if (activeRecording || pendingRecording) {
        practiceStatus.textContent = '请先结束另一张卡片的录音。';
        return;
      }
      record.disabled = true;
      practiced.disabled = true;
      replay.disabled = true;
      playback.hidden = true;
      pauseAudio();
      practiceStatus.textContent = '正在请求麦克风权限…';
      const session = {cardId: card.request_id, generation: recordingGeneration, cancelled: false, meter};
      pendingRecording = session;
      microphone.disabled = true;
      const current = () => !session.cancelled && session.generation === recordingGeneration && box.isConnected;
      try {
        session.context = new AudioContextClass();
        await session.context.resume();
        if (!current()) { closeAudioContext(session); return; }
        const audio = {channelCount: {ideal: 1}};
        if (microphone.value) audio.deviceId = {exact: microphone.value};
        session.stream = await navigator.mediaDevices.getUserMedia({audio});
        if (!current()) { releaseInput(session); closeAudioContext(session); return; }
        const source = session.context.createMediaStreamSource(session.stream);
        const analyser = session.context.createAnalyser(); analyser.fftSize = 1024; source.connect(analyser);
        const levels = new Float32Array(analyser.fftSize);
        meter.hidden = false;
        const measure = () => {
          if (!current()) return;
          analyser.getFloatTimeDomainData(levels);
          const rms = Math.sqrt(levels.reduce((sum, value) => sum + value * value, 0) / levels.length);
          meter.value = Math.min(1, rms * 8);
          session.animation = requestAnimationFrame(measure);
        };
        measure();
        const chunks = [];
        const mimeType = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus']
          .find(type => MediaRecorder.isTypeSupported(type));
        const recorder = new MediaRecorder(session.stream, mimeType ? {mimeType} : {});
        session.recorder = recorder;
        recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
        recorder.onstop = async () => {
          releaseInput(session);
          try {
            if (!current() || session.failed) return;
            practiceStatus.textContent = '正在检查录音…';
            const encoded = new Blob(chunks, {type: recorder.mimeType});
            if (!encoded.size) throw new Error('silent_recording');
            const decoded = await session.context.decodeAudioData(await encoded.arrayBuffer());
            if (!current() || session.failed) return;
            const blob = replayWav(decoded);
            if (audioUrl) { URL.revokeObjectURL(audioUrl); recordings.delete(audioUrl); }
            audioUrl = URL.createObjectURL(blob); recordings.add(audioUrl);
            playback.src = audioUrl; playback.muted = false; playback.volume = 1; playback.load();
            playback.hidden = false; replay.disabled = false;
            practiceStatus.textContent = '录音已就绪。点“回听录音”检查，听完后再记录跟读。';
          } catch (error) {
            if (current()) practiceStatus.textContent = error.message === 'silent_recording' ?
              '没有检测到足够的录音音量。请选择正确的麦克风，重录时确认音量条会动。' :
              '录音无法解码播放，请重新录音或更换浏览器。';
          } finally {
            closeAudioContext(session);
            if (activeRecording === session) activeRecording = null;
            if (current()) { record.disabled = false; record.textContent = '重新录音'; microphone.disabled = false; }
            updateMicrophones();
          }
        };
        recorder.onerror = () => {
          session.failed = true;
          if (current()) practiceStatus.textContent = '录音失败，请重新录音。';
          if (recorder.state !== 'inactive') recorder.stop();
        };
        recorder.start();
        pendingRecording = null; activeRecording = session;
        record.textContent = '停止录音'; record.disabled = false;
        practiceStatus.textContent = '正在录音，最多 12 秒；请确认音量条会动。读完点“停止录音”。';
        session.timer = setTimeout(() => { if (recorder.state !== 'inactive') recorder.stop(); }, 12000);
      } catch (error) {
        releaseInput(session); closeAudioContext(session);
        if (pendingRecording === session) pendingRecording = null;
        if (current()) {
          record.disabled = false; microphone.disabled = false;
          practiceStatus.textContent = ['NotFoundError', 'OverconstrainedError'].includes(error.name) ?
            '所选麦克风不可用，请切换麦克风后重试。' : '无法使用麦克风，请检查浏览器权限后重试。';
        }
      }
    });
    replay.addEventListener('click', async () => {
      if (pendingRecording || activeRecording) { practiceStatus.textContent = '请先结束录音，再回听。'; return; }
      pauseAudio(); playback.muted = false; playback.volume = 1; playback.currentTime = 0;
      try { await playback.play(); }
      catch { practiceStatus.textContent = '回听未能开始，请检查浏览器声音权限或重新录音。'; }
    });
    playback.addEventListener('play', () => {
      if (pendingRecording || activeRecording) { playback.pause(); return; }
      document.querySelectorAll('audio').forEach(audio => { if (audio !== playback) audio.pause(); });
      if ('speechSynthesis' in window) speechSynthesis.cancel();
    });
    playback.addEventListener('ended', () => {
      if (pendingRecording || activeRecording || playback.hidden) return;
      practiced.disabled = false;
      practiceStatus.textContent = '已回听。确认确实听到自己的声音后，可点“已跟读”。';
    });
    playback.addEventListener('error', () => {
      practiced.disabled = true;
      practiceStatus.textContent = '录音播放失败，请重新录音。';
    });
    practiced.addEventListener('click', async () => {
      practiced.disabled = true;
      try {
        const response = await fetch(`/api/learning/${encodeURIComponent(card.request_id)}/practice`, {
          method: 'POST', headers: {'X-English-Edge-Action': 'practice'}, cache: 'no-store'
        });
        if (!response.ok) throw new Error('practice failed');
        const result = await response.json();
        card.practice_count = result.practice_count;
        updateMeta();
        practiceStatus.textContent = '已记录本次跟读。你可以继续回听或重新录音。';
      } catch {
        practiceStatus.textContent = '跟读记录保存失败，请稍后重试。';
        practiced.disabled = false;
      }
    });
    async function recordOutcome(outcome) {
      independent.disabled = true;
      assisted.disabled = true;
      try {
        const response = await fetch(`/api/learning/${encodeURIComponent(card.request_id)}/review`, {
          method: 'POST', headers: {'X-English-Edge-Action': 'review', 'Content-Type': 'application/json'},
          body: JSON.stringify({outcome, support: outcomeSupport}), cache: 'no-store'
        });
        if (!response.ok) throw new Error('review failed');
        await refresh();
      } catch {
        status.textContent = '复习记录保存失败，请刷新后重试。';
        independent.disabled = !card.due;
        assisted.disabled = !card.due;
      }
    }
    independent.addEventListener('click', () => recordOutcome(proposedOutcome));
    assisted.addEventListener('click', () => recordOutcome('assisted'));
    actions.append(attempted, hint, reveal);
    feedbackActions.append(independent, assisted);
    practiceActions.append(listen, record, replay, practiced);
    box.append(actions, feedback, feedbackActions, practiceActions, meter, playback, practiceStatus); cards.append(box);
  }
  renderCandidates(data.candidates || [], data.candidates_truncated);
}
function renderCandidates(rows, truncated) {
  candidates.replaceChildren();
  candidateStatus.textContent = `可挑选 ${rows.length} 条${truncated ? '（仅显示最近 20 条）' : ''}`;
  if (!rows.length) {
    candidates.append(el('p', '暂无待挑选的表达。', 'empty-state'));
    return;
  }
  for (const row of rows) {
    const box = document.createElement('article'); box.className = 'candidate-card';
    box.append(el('p', row.asr_raw, 'candidate-original'), el('p', row.english, 'candidate-english'));
    const actions = document.createElement('div'); actions.className = 'actions';
    const select = el('button', '加入复习');
    const dismiss = el('button', '忽略这句');
    async function decide(action) {
      select.disabled = true; dismiss.disabled = true;
      try {
        const response = await fetch(`/api/learning/${encodeURIComponent(row.request_id)}/${action}`, {
          method: 'POST', headers: {'X-English-Edge-Action': action}, cache: 'no-store'
        });
        if (!response.ok) throw new Error('decision failed');
        if (action === 'select') {
          const selected = await response.json();
          rememberSupport({...row, request_id: selected.request_id || row.request_id,
            review_key: selected.review_key || row.review_key, review_count: 0, next_review_at: null}, 'answer');
        }
        await refresh();
      } catch {
        candidateStatus.textContent = '保存选择失败，请刷新后重试。';
        select.disabled = false; dismiss.disabled = false;
      }
    }
    select.addEventListener('click', () => decide('select'));
    dismiss.addEventListener('click', () => decide('dismiss'));
    actions.append(select, dismiss);
    box.append(actions); candidates.append(box);
  }
}
async function refresh(view = reviewView) {
  if (pendingRecording || activeRecording) {
    status.textContent = '请先结束录音，再刷新或切换复习列表。';
    return;
  }
  const generation = ++refreshGeneration;
  const controls = [...document.querySelectorAll('[data-review-view], #refresh')];
  controls.forEach(button => {button.disabled = true;});
  try {
    const response = await fetch(`/api/learning?view=${encodeURIComponent(view)}`, {cache: 'no-store', signal: AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error('fetch failed');
    const data = await response.json();
    if (generation === refreshGeneration) {
      if (pendingRecording || activeRecording) {
        status.textContent = '正在录音，已保留当前列表；结束录音后可再次刷新。';
      } else render(data);
    }
  } catch {
    if (generation === refreshGeneration) status.textContent = '读取失败，上次列表仍保留；请检查连接或登录后重试。';
  } finally {
    if (generation === refreshGeneration) controls.forEach(button => {button.disabled = false;});
  }
}
document.querySelector('#refresh').addEventListener('click', () => refresh());
for (const button of document.querySelectorAll('[data-review-view]')) {
  button.addEventListener('click', () => refresh(button.dataset.reviewView));
}
window.addEventListener('pagehide', clearRecordings);
navigator.mediaDevices?.addEventListener('devicechange', updateMicrophones);
updateMicrophones();
refresh();
