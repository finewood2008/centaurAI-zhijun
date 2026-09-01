const CSRF = 'centaur-vdb';
const RECENT_KEY = 'centaur-mobile-recent';
const TOKEN_KEY = 'centaur-mobile-token';
const SERVER_URL_KEY = 'centaur-mobile-server-url';
const SHARE_PENDING_KEY = 'centaur-pending-share';
const NATIVE_SHARE_PENDING_KEY = 'centaur-native-pending-share';
const SHARE_DB = 'centaur-mobile-shares';
const SHARE_STORE = 'shares';
const OUTBOX_DB = 'centaur-mobile-outbox';
const OUTBOX_STORE = 'items';

let mediaRecorder = null;
let recordChunks = [];
let recordStartedAt = 0;
let recordTimer = null;
let lastContextText = '';
let serverItems = [];
let lastPackInviteText = '';
let lastPackInvite = null;
let resultPollTimer = null;
let outboxSyncing = false;
let outboxItems = [];

const $ = (id) => document.getElementById(id);

function token() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

function serverUrl() {
  return localStorage.getItem(SERVER_URL_KEY) || '';
}

function setToken(value) {
  localStorage.setItem(TOKEN_KEY, value || '');
  $('token-input').value = value || '';
}

function normalizeServerUrl(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(raw) ? raw : 'http://' + raw;
  try {
    const url = new URL(withScheme);
    let pathname = url.pathname.replace(/\/+$/, '');
    if (pathname === '/mobile' || pathname.startsWith('/mobile/')) pathname = '';
    if (pathname === '/api/mobile' || pathname.startsWith('/api/mobile/')) pathname = '';
    url.pathname = pathname;
    url.search = '';
    url.hash = '';
    return url.toString().replace(/\/$/, '');
  } catch (_) {
    throw new Error('节点地址格式不正确');
  }
}

function setServerUrl(value) {
  const normalized = normalizeServerUrl(value);
  if (normalized) localStorage.setItem(SERVER_URL_KEY, normalized);
  else localStorage.removeItem(SERVER_URL_KEY);
  $('server-url-input').value = normalized;
  return normalized;
}

function localWebOrigin() {
  return location.protocol === 'http:' || location.protocol === 'https:';
}

function apiUrl(path) {
  if (/^https?:\/\//i.test(path)) return path;
  const base = serverUrl();
  if (base) return base + path;
  if (localWebOrigin()) return path;
  throw new Error('请先填写节点地址');
}

function publicUrl(path) {
  const base = serverUrl() || (localWebOrigin() ? location.origin : '');
  return base ? base + path : path;
}

function capacitorBridge() {
  return window.Capacitor || null;
}

function nativeShareTargetPlugin() {
  const cap = capacitorBridge();
  return cap && cap.Plugins && cap.Plugins.CapacitorShareTarget
    ? cap.Plugins.CapacitorShareTarget
    : null;
}

function nativeSharePlugin() {
  const cap = capacitorBridge();
  return cap && cap.Plugins && cap.Plugins.Share ? cap.Plugins.Share : null;
}

function nativeFileFetchUrl(uri) {
  const raw = String(uri || '').trim();
  if (!raw) throw new Error('分享文件地址为空');
  if (/^(data|blob|https?):/i.test(raw)) return raw;
  const cap = capacitorBridge();
  const fileUri = raw.startsWith('/') ? 'file://' + raw : raw;
  return cap && typeof cap.convertFileSrc === 'function' ? cap.convertFileSrc(fileUri) : fileUri;
}

function consumeServerUrlParam() {
  const params = new URLSearchParams(window.location.search || '');
  const raw = (params.get('server') || params.get('node') || '').trim();
  if (!raw) return;
  try {
    setServerUrl(raw);
    toast('节点地址已保存');
  } catch (e) {
    toast(e.message || String(e));
  }
}

function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('/mobile/sw.js').catch(() => {});
}

function openShareDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(SHARE_DB, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(SHARE_STORE)) db.createObjectStore(SHARE_STORE, { keyPath: 'id' });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function getSharedPayload(id) {
  const db = await openShareDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(SHARE_STORE, 'readonly');
    const req = tx.objectStore(SHARE_STORE).get(id);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror = () => reject(req.error);
  });
}

async function deleteSharedPayload(id) {
  const db = await openShareDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(SHARE_STORE, 'readwrite');
    tx.objectStore(SHARE_STORE).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

function openOutboxDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(OUTBOX_DB, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(OUTBOX_STORE)) db.createObjectStore(OUTBOX_STORE, { keyPath: 'id' });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function listOutboxItems() {
  const db = await openOutboxDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(OUTBOX_STORE, 'readonly');
    const req = tx.objectStore(OUTBOX_STORE).getAll();
    req.onsuccess = () => {
      const items = Array.isArray(req.result) ? req.result : [];
      items.sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')));
      resolve(items);
    };
    req.onerror = () => reject(req.error);
  });
}

async function putOutboxItem(item) {
  const db = await openOutboxDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(OUTBOX_STORE, 'readwrite');
    tx.objectStore(OUTBOX_STORE).put(item);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function deleteOutboxItem(id) {
  const db = await openOutboxDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(OUTBOX_STORE, 'readwrite');
    tx.objectStore(OUTBOX_STORE).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function enqueueOutboxItem(item) {
  const now = new Date().toISOString();
  await putOutboxItem(Object.assign({
    id: 'outbox-' + Date.now() + '-' + Math.random().toString(16).slice(2),
    created_at: now,
    updated_at: now,
    attempts: 0,
    last_error: '',
  }, item));
  await updateOutboxStatus();
}

function outboxTitle(item) {
  return item.title || item.name || (item.kind === 'clip' ? '待同步剪藏' : '待同步文件');
}

async function updateOutboxStatus() {
  try {
    outboxItems = await listOutboxItems();
  } catch (_) {
    outboxItems = [];
  }
  const count = outboxItems.length;
  const el = $('outbox-status');
  if (el) el.textContent = count ? `待同步 ${count}` : '待同步 0';
  const btn = $('outbox-sync');
  if (btn) btn.disabled = !count || outboxSyncing;
  if ($('tab-results') && $('tab-results').classList.contains('active')) renderRecent();
  return count;
}

function enqueueSummary(kind) {
  if (kind === 'clip') return '已保存到待同步剪藏';
  if (kind === 'recording') return '已保存到待同步录音';
  return '已保存到待同步文件';
}

function consumePairingToken() {
  const raw = window.location.hash || '';
  if (!raw || raw.length < 2) return;
  const params = new URLSearchParams(raw.slice(1));
  const pairedToken = (params.get('token') || '').trim();
  if (!pairedToken) return;
  setToken(pairedToken);
  toast('已完成配对');
  if (window.history && window.history.replaceState) {
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
  }
}

function fillSharedClip(title, text, url) {
  $('clip-title').value = title || url || '手机分享';
  $('clip-url').value = url || '';
  const safeText = text || '';
  const lines = [];
  if (safeText) lines.push(safeText);
  if (url && !safeText.includes(url)) lines.push(url);
  $('clip-text').value = lines.join('\n\n');
  switchTab('clips');
}

function isAudioFile(file, name) {
  const fileName = name || (file && file.name) || '';
  const ext = (fileName.split('.').pop() || '').toLowerCase();
  const type = (file && file.type) || '';
  return type.startsWith('audio/') || ['m4a', 'mp3', 'wav', 'aac', 'ogg', 'opus', 'flac', 'webm'].includes(ext);
}

async function postRecordingFile(file, filename, meta = {}) {
  if (!token()) throw new Error('请先保存 App Token');
  const fd = new FormData();
  fd.append('file', file, filename);
  fd.append('title', meta.title || '');
  fd.append('note', meta.note || '');
  fd.append('started_at', meta.started_at || new Date().toISOString());
  const data = await api('/api/mobile/recordings', { method: 'POST', body: fd });
  addRecent({ doc_id: data.doc_id, result_url: data.result_url, name: data.file_name || filename, kind: 'recording' });
  return data;
}

async function postGenericFile(file, filename) {
  if (!token()) throw new Error('请先保存 App Token');
  const fd = new FormData();
  fd.append('file', file, filename);
  const data = await api('/api/mobile/uploads', { method: 'POST', body: fd });
  addRecent({ doc_id: data.doc_id, name: data.file_name || filename, kind: 'file' });
  return data;
}

async function postClipPayload(payload) {
  if (!token()) throw new Error('请先保存 App Token');
  const data = await api('/api/mobile/clips', {
    method: 'POST',
    json: true,
    body: JSON.stringify(payload),
  });
  addRecent({ doc_id: data.doc_id, name: payload.title || '手机剪藏', kind: 'clip' });
  return data;
}

async function uploadQueuedItem(item) {
  if (item.kind === 'clip') {
    return postClipPayload({
      title: item.title || '手机剪藏',
      url: item.url || '',
      text: item.text || '',
      tags: item.tags || ['手机剪藏'],
    });
  }
  if (!item.file) throw new Error('待同步文件缺失');
  if (item.kind === 'recording') {
    return postRecordingFile(item.file, item.name || 'recording.webm', {
      title: item.title || '',
      note: item.note || '',
      started_at: item.started_at || item.created_at || new Date().toISOString(),
    });
  }
  return postGenericFile(item.file, item.name || 'shared-file');
}

async function syncOutbox(opts = {}) {
  if (outboxSyncing) return;
  const items = await listOutboxItems().catch(() => []);
  outboxItems = items;
  if (!items.length) {
    await updateOutboxStatus();
    return;
  }
  if (!token()) {
    if (!opts.quiet) toast('请先保存 App Token');
    await updateOutboxStatus();
    return;
  }
  outboxSyncing = true;
  await updateOutboxStatus();
  let synced = 0;
  let failed = 0;
  try {
    for (const item of items) {
      try {
        await uploadQueuedItem(item);
        await deleteOutboxItem(item.id);
        synced += 1;
      } catch (e) {
        failed += 1;
        await putOutboxItem(Object.assign({}, item, {
          attempts: Number(item.attempts || 0) + 1,
          last_error: e.message || String(e),
          updated_at: new Date().toISOString(),
        }));
        break;
      }
    }
  } finally {
    outboxSyncing = false;
    await updateOutboxStatus();
  }
  if (!opts.quiet) {
    if (synced && !failed) toast(`已同步 ${synced} 项`);
    else if (synced && failed) toast(`已同步 ${synced} 项，仍有待同步`);
    else if (failed) toast('同步失败，稍后会重试');
  }
}

async function uploadSharedFile(file, title, opts = {}) {
  const name = file.name || 'shared-file';
  const kind = isAudioFile(file, name) ? 'recording' : 'file';
  try {
    if (kind === 'recording') {
      await postRecordingFile(file, name, { title: title || name, started_at: new Date().toISOString() });
    } else {
      await postGenericFile(file, name);
    }
    return { queued: false };
  } catch (e) {
    if (opts.queueOnFail === false) throw e;
    await enqueueOutboxItem({ kind, file, name, title: title || name, last_error: e.message || String(e) });
    return { queued: true, error: e };
  }
}

async function nativeSharedFileToFile(sharedFile) {
  const url = nativeFileFetchUrl(sharedFile && sharedFile.uri);
  const res = await fetch(url);
  if (!res.ok) throw new Error('读取分享文件失败: HTTP ' + res.status);
  const blob = await res.blob();
  const name = (sharedFile && sharedFile.name) || 'shared-file';
  const type = (sharedFile && sharedFile.mimeType) || blob.type || 'application/octet-stream';
  return new File([blob], name, { type });
}

function persistNativeShareEvent(event) {
  try {
    localStorage.setItem(NATIVE_SHARE_PENDING_KEY, JSON.stringify(event || {}));
  } catch (_) {
    localStorage.removeItem(NATIVE_SHARE_PENDING_KEY);
  }
}

async function consumeNativeShareEvent(event) {
  const title = String((event && event.title) || '').trim();
  const texts = Array.isArray(event && event.texts) ? event.texts.filter(Boolean).join('\n\n').trim() : '';
  const files = Array.isArray(event && event.files) ? event.files : [];
  if (title || texts) fillSharedClip(title, texts, '');
  if (!files.length) {
    toast('已接收系统分享');
    return;
  }
  if (!token()) {
    try {
      for (const sharedFile of files) {
        const file = await nativeSharedFileToFile(sharedFile);
        await uploadSharedFile(file, title);
      }
      localStorage.removeItem(NATIVE_SHARE_PENDING_KEY);
      toast('已接收分享文件，保存到待同步队列');
      switchTab('results');
    } catch (e) {
      persistNativeShareEvent(event);
      toast('已接收分享文件，请先保存 App Token');
    }
    return;
  }
  let queued = 0;
  let uploaded = 0;
  for (const sharedFile of files) {
    const file = await nativeSharedFileToFile(sharedFile);
    const result = await uploadSharedFile(file, title);
    if (result && result.queued) queued += 1;
    else uploaded += 1;
  }
  localStorage.removeItem(NATIVE_SHARE_PENDING_KEY);
  toast(queued ? `已提交 ${uploaded} 项，${queued} 项待同步` : '分享文件已提交');
  switchTab('results');
}

async function retryPendingNativeShare() {
  const raw = localStorage.getItem(NATIVE_SHARE_PENDING_KEY);
  if (!raw || !token()) return;
  try {
    await consumeNativeShareEvent(JSON.parse(raw));
  } catch (e) {
    toast(e.message || String(e));
  }
}

async function registerNativeShareTarget() {
  const plugin = nativeShareTargetPlugin();
  if (!plugin || typeof plugin.addListener !== 'function') return;
  try {
    await plugin.addListener('shareReceived', (event) => {
      consumeNativeShareEvent(event).catch((e) => toast(e.message || String(e)));
    });
  } catch (_) {
    return;
  }
  retryPendingNativeShare();
}

async function consumeSharedPayload(id) {
  const payload = await getSharedPayload(id);
  if (!payload) return;
  const title = String(payload.title || '').trim();
  const text = String(payload.text || '').trim();
  const url = String(payload.url || '').trim();
  const files = Array.isArray(payload.files) ? payload.files : [];
  if (title || text || url) fillSharedClip(title, text, url);
  if (!files.length) {
    await deleteSharedPayload(id);
    localStorage.removeItem(SHARE_PENDING_KEY);
    toast('已从系统分享预填剪藏');
    return;
  }
  if (!token()) {
    localStorage.setItem(SHARE_PENDING_KEY, id);
    toast('已接收分享文件，请先保存 App Token');
    return;
  }
  let queued = 0;
  let uploaded = 0;
  for (const file of files) {
    const result = await uploadSharedFile(file, title);
    if (result && result.queued) queued += 1;
    else uploaded += 1;
  }
  await deleteSharedPayload(id);
  localStorage.removeItem(SHARE_PENDING_KEY);
  toast(queued ? `已提交 ${uploaded} 项，${queued} 项待同步` : '分享文件已提交');
  switchTab('results');
}

async function consumeSharedClip() {
  const params = new URLSearchParams(window.location.search || '');
  const sharedId = (params.get('shared') || '').trim();
  if (sharedId) {
    try {
      await consumeSharedPayload(sharedId);
      if (window.history && window.history.replaceState) {
        window.history.replaceState(null, '', '/mobile');
      }
    } catch (e) {
      toast(e.message || String(e));
    }
    return;
  }
  const sharedTitle = (params.get('title') || '').trim();
  const sharedText = (params.get('text') || '').trim();
  const sharedUrl = (params.get('url') || '').trim();
  if (!sharedTitle && !sharedText && !sharedUrl) return;
  fillSharedClip(sharedTitle, sharedText, sharedUrl);
  toast('已从系统分享预填剪藏');
  if (window.history && window.history.replaceState) {
    window.history.replaceState(null, '', '/mobile');
  }
}

function toast(message) {
  const el = $('toast');
  el.textContent = message;
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 2600);
}

function fmtTime(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return String(m).padStart(2, '0') + ':' + String(r).padStart(2, '0');
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (opts.mobileAuth !== false && token()) headers.Authorization = 'Bearer ' + token();
  if (opts.csrf) headers['X-Requested-By'] = CSRF;
  if (opts.json) headers['Content-Type'] = 'application/json';
  const res = await fetch(apiUrl(path), Object.assign({}, opts, { headers }));
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || 'HTTP ' + res.status);
  return data;
}

function recentItems() {
  try {
    const items = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
    return Array.isArray(items) ? items : [];
  } catch (_) {
    return [];
  }
}

function saveRecent(items) {
  localStorage.setItem(RECENT_KEY, JSON.stringify(items.slice(0, 40)));
}

function addRecent(item) {
  const items = recentItems().filter((x) => x.doc_id !== item.doc_id);
  items.unshift(Object.assign({ created_at: new Date().toISOString() }, item));
  saveRecent(items);
  renderRecent();
}

function switchTab(tab) {
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach((p) => p.classList.remove('active'));
  $('tab-' + tab).classList.add('active');
  if (tab === 'results') loadMobileItems();
  else stopResultPolling();
  if (tab === 'context') loadPacks();
}

async function loadStatus() {
  try {
    const [health, cfg] = await Promise.all([
      api('/api/health', { mobileAuth: false }),
      api('/api/mobile/config', { mobileAuth: false }),
    ]);
    const enabled = cfg.enabled ? '已启用' : '未启用';
    $('node-status').textContent = `${enabled} · 转写 ${health.capabilities && health.capabilities.transcribe ? '可用' : '不可用'}`;
    const urls = Array.isArray(cfg.urls) ? cfg.urls : [];
    const current = serverUrl();
    $('endpoint-info').textContent = current
      ? '当前节点：' + current
      : (urls.length ? '可用节点：' + urls.join('  ') : '未检测到 Tailscale/局域网地址');
  } catch (e) {
    $('node-status').textContent = '连接失败';
    $('endpoint-info').textContent = e.message || String(e);
  }
}

async function uploadRecordingBlob(blob, filename) {
  const meta = {
    title: $('record-title').value.trim(),
    note: $('record-note').value.trim(),
    started_at: new Date().toISOString(),
  };
  try {
    await postRecordingFile(blob, filename, meta);
    toast('录音已提交');
  } catch (e) {
    await enqueueOutboxItem(Object.assign({ kind: 'recording', file: blob, name: filename }, meta, {
      last_error: e.message || String(e),
    }));
    toast(enqueueSummary('recording'));
  }
  switchTab('results');
}

async function startRecording() {
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    toast('当前浏览器不支持直接录音');
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const preferred = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
    const mimeType = preferred.find((x) => MediaRecorder.isTypeSupported(x)) || '';
    recordChunks = [];
    mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    mediaRecorder.ondataavailable = (event) => {
      if (event.data && event.data.size) recordChunks.push(event.data);
    };
    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach((track) => track.stop());
      clearInterval(recordTimer);
      $('record-dot').classList.remove('live');
      $('record-state').textContent = '上传中';
      const type = mediaRecorder.mimeType || 'audio/webm';
      const ext = type.includes('mp4') ? 'm4a' : 'webm';
      const blob = new Blob(recordChunks, { type });
      try {
        await uploadRecordingBlob(blob, `recording-${Date.now()}.${ext}`);
        $('record-state').textContent = '待机';
      } catch (e) {
        $('record-state').textContent = '失败';
        toast(e.message || String(e));
      }
    };
    mediaRecorder.start();
    recordStartedAt = Date.now();
    $('record-start').disabled = true;
    $('record-stop').disabled = false;
    $('record-state').textContent = '录音中';
    $('record-state').classList.add('busy');
    $('record-dot').classList.add('live');
    recordTimer = setInterval(() => {
      $('record-time').textContent = fmtTime((Date.now() - recordStartedAt) / 1000);
    }, 250);
  } catch (e) {
    toast(e.message || '无法开始录音');
  }
}

function stopRecording() {
  if (!mediaRecorder || mediaRecorder.state !== 'recording') return;
  $('record-start').disabled = false;
  $('record-stop').disabled = true;
  $('record-state').classList.remove('busy');
  mediaRecorder.stop();
}

async function uploadSelectedRecording() {
  const file = $('record-file').files[0];
  if (!file) return toast('请选择录音文件');
  try {
    await uploadRecordingBlob(file, file.name);
  } catch (e) {
    toast(e.message || String(e));
  }
}

async function uploadGenericFile() {
  const file = $('generic-file').files[0];
  if (!file) return toast('请选择文件');
  try {
    await postGenericFile(file, file.name);
    toast('文件已提交');
  } catch (e) {
    await enqueueOutboxItem({ kind: 'file', file, name: file.name, last_error: e.message || String(e) });
    toast(enqueueSummary('file'));
  }
  switchTab('results');
}

async function submitClip() {
  const text = $('clip-text').value.trim();
  if (!text) return toast('剪藏内容为空');
  const payload = {
    title: $('clip-title').value.trim(),
    url: $('clip-url').value.trim(),
    text,
    tags: ['手机剪藏'],
  };
  try {
    await postClipPayload(payload);
    toast('剪藏已提交');
  } catch (e) {
    await enqueueOutboxItem(Object.assign({ kind: 'clip', last_error: e.message || String(e) }, payload));
    toast(enqueueSummary('clip'));
  }
  $('clip-text').value = '';
  switchTab('results');
}

async function runSearch() {
  if (!token()) return toast('请先保存 App Token');
  const query = $('search-query').value.trim();
  if (!query) return toast('请输入搜索内容');
  const box = $('search-output');
  box.innerHTML = '<div class="item-meta">搜索中...</div>';
  try {
    const data = await api('/api/mobile/search', {
      method: 'POST',
      json: true,
      body: JSON.stringify({
        query,
        mode: $('search-mode').value,
        n_results: 8,
      }),
    });
    renderSearchResults(data.results || []);
  } catch (e) {
    box.innerHTML = `<div class="item-meta">${escapeHtml(e.message || String(e))}</div>`;
  }
}

function resultTitle(item) {
  const meta = item.metadata || {};
  return meta.file_name || item.title || item.source_path || item.page_path || item.rel_path || '结果';
}

function renderSearchResults(results) {
  const box = $('search-output');
  if (!results.length) {
    box.innerHTML = '<div class="item-meta">没有命中结果</div>';
    return;
  }
  box.innerHTML = results.map((item) => {
    const meta = item.metadata || {};
    const type = meta.file_type || item.match_type || 'result';
    const score = item.score === undefined ? '' : Number(item.score).toFixed(3);
    const source = item.source_path || item.page_path || item.rel_path || '';
    const text = item.text || item.content || item.summary || '';
    return `<div class="recent-item">
      <div class="item-head"><span class="item-title">${escapeHtml(resultTitle(item))}</span><span class="badge">${escapeHtml(type)}</span></div>
      <div class="item-meta">${escapeHtml(score ? 'score ' + score : '')}</div>
      <div class="item-meta">${escapeHtml(source)}</div>
      <pre>${escapeHtml(text || '暂无片段')}</pre>
    </div>`;
  }).join('');
}

function mergeRecentItems() {
  const merged = [];
  const seen = new Set();
  (outboxItems || []).forEach((item) => {
    if (!item.id || seen.has(item.id)) return;
    seen.add(item.id);
    merged.push({
      doc_id: item.id,
      local_outbox: true,
      kind: item.kind || 'file',
      title: outboxTitle(item),
      created_at: item.created_at || '',
      updated_at: item.updated_at || item.created_at || '',
      state: 'local',
      summary: item.last_error ? '待同步：' + item.last_error : '待同步到个人 AI 主机',
      outbox: item,
    });
  });
  (serverItems || []).forEach((item) => {
    if (!item.doc_id || seen.has(item.doc_id)) return;
    seen.add(item.doc_id);
    merged.push(item);
  });
  recentItems().forEach((item) => {
    if (!item.doc_id || seen.has(item.doc_id)) return;
    seen.add(item.doc_id);
    merged.push(item);
  });
  return merged;
}

function itemState(item) {
  if (item.local_outbox) return 'local';
  if (item.ready) return 'ready';
  return item.state || (item.index && item.index.state) || 'pending';
}

function isResultsTabActive() {
  return $('tab-results').classList.contains('active');
}

function pendingResultCount(items) {
  return (items || []).filter((item) => {
    const state = itemState(item);
    return state === 'queued' || state === 'processing' || state === 'pending';
  }).length;
}

function stopResultPolling() {
  if (resultPollTimer) clearInterval(resultPollTimer);
  resultPollTimer = null;
}

function syncResultPolling() {
  const pending = pendingResultCount(mergeRecentItems());
  if (!pending || !isResultsTabActive() || !token()) {
    stopResultPolling();
    return;
  }
  if (resultPollTimer) return;
  resultPollTimer = setInterval(() => {
    if (!isResultsTabActive()) {
      stopResultPolling();
      return;
    }
    loadMobileItems({ quiet: true });
  }, 5000);
}

async function loadMobileItems(opts = {}) {
  if (!token()) {
    serverItems = [];
    renderRecent();
    stopResultPolling();
    return;
  }
  const list = $('recent-list');
  if (!opts.quiet) list.innerHTML = '<div class="item-meta">加载服务端记录...</div>';
  try {
    const data = await api('/api/mobile/items?limit=80');
    serverItems = data.items || [];
    renderRecent();
  } catch (e) {
    serverItems = [];
    renderRecent(e.message || String(e));
  }
}

function renderRecent(errorMessage) {
  const items = mergeRecentItems();
  const list = $('recent-list');
  if (!items.length) {
    list.innerHTML = `<div class="item-meta">${escapeHtml(errorMessage || '暂无移动端提交记录')}</div>`;
    $('result-detail').className = 'result-detail empty';
    $('result-detail').textContent = '暂无结果';
    syncResultPolling();
    return;
  }
  const pending = pendingResultCount(items);
  const pollNote = pending ? `<div class="item-meta">${pending} 项处理中，自动刷新中...</div>` : '';
  const prefix = (errorMessage ? `<div class="item-meta">${escapeHtml(errorMessage)}，显示本机记录</div>` : '') + pollNote;
  list.innerHTML = prefix + items.map((item) => {
    const state = itemState(item);
    const updated = item.updated_at || item.created_at || '';
    return `<div class="recent-item" data-doc="${escapeHtml(encodeURIComponent(item.doc_id))}">
      <div class="item-head"><span class="item-title">${escapeHtml(item.title || item.name || item.file_name || item.doc_id)}</span><span class="badge ${state === 'ready' ? 'ok' : ''}">${escapeHtml(state)}</span></div>
      <div class="item-meta">${escapeHtml(item.kind || 'doc')} · ${escapeHtml(updated)}</div>
      <div class="item-meta">${escapeHtml(item.summary || item.doc_id)}</div>
    </div>`;
  }).join('');
  list.querySelectorAll('.recent-item').forEach((el) => {
    el.addEventListener('click', () => {
      const docId = decodeURIComponent(el.dataset.doc || '');
      const item = mergeRecentItems().find((x) => x.doc_id === docId);
      if (item && item.local_outbox) renderOutboxDetail(item.outbox);
      else loadResult(docId);
    });
  });
  syncResultPolling();
}

function renderOutboxDetail(item) {
  const detail = $('result-detail');
  detail.className = 'result-detail';
  detail.innerHTML = `<div class="item-head"><strong>${escapeHtml(outboxTitle(item))}</strong><span class="badge">local</span></div>
    <p><strong>状态</strong><br>待同步到个人 AI 主机</p>
    <p><strong>类型</strong><br>${escapeHtml(item.kind || 'file')}</p>
    <p><strong>创建时间</strong><br>${escapeHtml(item.created_at || '')}</p>
    <p><strong>重试次数</strong><br>${escapeHtml(String(item.attempts || 0))}</p>
    <p><strong>最近错误</strong><br>${escapeHtml(item.last_error || '暂无')}</p>`;
}

async function loadResult(docId) {
  if (!docId) return;
  const detail = $('result-detail');
  detail.className = 'result-detail';
  detail.textContent = '加载中...';
  try {
    const data = await api('/api/mobile/results/' + encodeURIComponent(docId));
    const wiki = data.wiki_page ? `${data.wiki_page.title || data.wiki_page.path} (${data.wiki_page.path})` : '暂无';
    const transcript = (data.transcript || []).map((x) => {
      const t = x.start_time === null || x.start_time === undefined ? '' : `[${fmtTime(x.start_time)}] `;
      return t + x.text;
    }).join('\n');
    detail.innerHTML = `<div class="item-head"><strong>${escapeHtml(data.file_name || '')}</strong><span class="badge ${data.ready ? 'ok' : ''}">${data.ready ? 'ready' : (data.index && data.index.state) || 'pending'}</span></div>
      <p><strong>摘要</strong><br>${escapeHtml(data.summary || '暂无')}</p>
      <p><strong>Wiki</strong><br>${escapeHtml(wiki)}</p>
      <p><strong>转录</strong></p>
      <pre>${escapeHtml(transcript || data.text || '暂无')}</pre>`;
  } catch (e) {
    detail.className = 'result-detail empty';
    detail.textContent = e.message || String(e);
  }
}

function formatSources(sources) {
  const rows = [];
  (sources || []).forEach((group) => {
    const type = group.type || 'source';
    (group.items || []).slice(0, 8).forEach((item) => {
      const label = item.title || item.file_name || item.path || item.source_path || item.rel_path || '';
      if (label) rows.push(`${type}: ${label}`);
    });
  });
  return rows;
}

async function generateContext() {
  if (!token()) return toast('请先保存 App Token');
  const query = $('context-query').value.trim();
  if (!query) return toast('请输入要调取的 Context');
  const output = $('context-output');
  output.className = 'result-detail';
  output.textContent = '生成中...';
  lastContextText = '';
  try {
    const data = await api('/api/mobile/context/query', {
      method: 'POST',
      json: true,
      body: JSON.stringify({
        query,
        include_memory: $('context-memory').checked,
        include_wiki: $('context-wiki').checked,
        include_documents: $('context-docs').checked,
        limit_chars: 8000,
      }),
    });
    const context = data.context || '';
    const sourceLines = formatSources(data.sources);
    lastContextText = context;
    output.innerHTML = `<div class="item-head"><strong>${escapeHtml(data.query || query)}</strong><span class="badge ${context ? 'ok' : ''}">${data.total_chars || 0} chars</span></div>
      <p><strong>来源</strong><br>${escapeHtml(sourceLines.join('\n') || '暂无')}</p>
      <pre>${escapeHtml(context || '没有找到可用 Context')}</pre>`;
  } catch (e) {
    output.className = 'result-detail empty';
    output.textContent = e.message || String(e);
  }
}

async function copyContext() {
  if (!lastContextText) return toast('暂无可复制 Context');
  await copyText(lastContextText);
}

function renderContextSnapshot(payload) {
  const snapshot = payload && payload.snapshot ? payload.snapshot : payload;
  const output = $('context-output');
  const stats = (snapshot && snapshot.stats) || {};
  const vectorStats = stats.vector || {};
  const wikiStats = stats.wiki || {};
  const markdown = (snapshot && snapshot.markdown) || '';
  lastContextText = markdown;
  output.className = 'result-detail';
  output.innerHTML = `<div class="item-head"><strong>Personal Context Snapshot</strong><span class="badge ${markdown ? 'ok' : ''}">${escapeHtml((snapshot && snapshot.generated_at) || '')}</span></div>
    <p><strong>概览</strong><br>${escapeHtml(`文档 ${vectorStats.total_documents || 0} · Wiki ${wikiStats.total_pages || 0} · 链接 ${wikiStats.links || 0}`)}</p>
    <p><strong>Wiki</strong><br>${escapeHtml((snapshot && snapshot.wiki_page) || '暂无')}</p>
    <pre>${escapeHtml(markdown || '暂无快照')}</pre>`;
}

async function loadContextSnapshot() {
  if (!token()) return toast('请先保存 App Token');
  const output = $('context-output');
  output.className = 'result-detail';
  output.textContent = '加载快照...';
  try {
    renderContextSnapshot(await api('/api/mobile/context/snapshot'));
  } catch (e) {
    output.className = 'result-detail empty';
    output.textContent = e.message || String(e);
  }
}

async function refreshContextSnapshot() {
  if (!token()) return toast('请先保存 App Token');
  const output = $('context-output');
  output.className = 'result-detail';
  output.textContent = '刷新快照...';
  try {
    renderContextSnapshot(await api('/api/mobile/context/snapshot/refresh', { method: 'POST' }));
    toast('快照已刷新');
  } catch (e) {
    output.className = 'result-detail empty';
    output.textContent = e.message || String(e);
  }
}

async function loadPacks() {
  const list = $('pack-list');
  const detail = $('pack-detail');
  lastPackInviteText = '';
  lastPackInvite = null;
  $('pack-share-last').disabled = true;
  if (detail) {
    detail.className = 'result-detail empty';
    detail.textContent = '暂无 A2A 邀请';
  }
  list.innerHTML = '<div class="item-meta">加载中...</div>';
  try {
    const data = await api('/api/mobile/context/packs');
    const packs = data.packs || [];
    if (!packs.length) {
      list.innerHTML = '<div class="item-meta">暂无 Context Pack</div>';
      return;
    }
    list.innerHTML = packs.map((p) => {
      const card = `/api/a2a/${encodeURIComponent(p.id)}/agent-card.json`;
      const msg = `/api/a2a/${encodeURIComponent(p.id)}/message:send`;
      return `<div class="pack-item">
        <div class="item-head"><span class="item-title">${escapeHtml(p.name || p.id)}</span><span class="badge">${p.enabled ? 'on' : 'off'}</span></div>
        <div class="item-meta">ID: ${escapeHtml(p.id)} · Token 尾号: ${escapeHtml(p.token_suffix || '无')}</div>
        <div class="item-meta">${escapeHtml(p.query || p.description || '')}</div>
        <div class="button-grid">
          <button class="secondary" data-copy="${escapeHtml(card)}" type="button">Agent Card</button>
          <button class="secondary" data-copy="${escapeHtml(msg)}" type="button">Message</button>
          <button class="secondary" data-invite="${escapeHtml(p.id)}" type="button">邀请</button>
          <button class="secondary" data-share-invite="${escapeHtml(p.id)}" type="button">分享</button>
          <button class="secondary" data-preview="${escapeHtml(p.id)}" type="button">预览</button>
        </div>
      </div>`;
    }).join('');
    list.querySelectorAll('button[data-copy]').forEach((btn) => {
      btn.addEventListener('click', () => copyText(publicUrl(btn.dataset.copy || '')));
    });
    list.querySelectorAll('button[data-invite]').forEach((btn) => {
      btn.addEventListener('click', () => loadPackInvite(btn.dataset.invite || ''));
    });
    list.querySelectorAll('button[data-share-invite]').forEach((btn) => {
      btn.addEventListener('click', () => sharePackInvite(btn.dataset.shareInvite || ''));
    });
    list.querySelectorAll('button[data-preview]').forEach((btn) => {
      btn.addEventListener('click', () => previewPack(btn.dataset.preview || ''));
    });
  } catch (e) {
    list.innerHTML = `<div class="item-meta">${escapeHtml(e.message || String(e))}</div>`;
  }
}

async function loadPackInvite(packId, opts = {}) {
  if (!packId) return;
  const autoCopy = opts.autoCopy !== false;
  const detail = $('pack-detail');
  detail.className = 'result-detail';
  detail.textContent = '生成邀请中...';
  try {
    const data = await api('/api/mobile/context/packs/' + encodeURIComponent(packId) + '/invite');
    lastPackInvite = data;
    lastPackInviteText = data.share_text || JSON.stringify(data, null, 2);
    $('pack-share-last').disabled = false;
    if (autoCopy) await copyText(lastPackInviteText);
    detail.innerHTML = `<div class="item-head"><strong>${escapeHtml(data.pack && data.pack.name || packId)}</strong><span class="badge ok">invite</span></div>
      <p><strong>Agent Card</strong><br>${escapeHtml(data.agent_card_url || '')}</p>
      <p><strong>Message</strong><br>${escapeHtml(data.message_url || '')}</p>
      <p><strong>Token</strong><br>${escapeHtml((data.authorization && data.authorization.token) || '')}</p>
      <pre>${escapeHtml(lastPackInviteText)}</pre>`;
  } catch (e) {
    detail.className = 'result-detail empty';
    detail.textContent = e.message || String(e);
  }
}

async function sharePackInvite(packId) {
  if (packId) await loadPackInvite(packId, { autoCopy: false });
  if (!lastPackInviteText) return toast('暂无 A2A 邀请');
  const packName = lastPackInvite && lastPackInvite.pack && lastPackInvite.pack.name;
  const title = 'A2A Context Pack: ' + (packName || 'Personal Context');
  const url = lastPackInvite && lastPackInvite.agent_card_url ? lastPackInvite.agent_card_url : '';
  try {
    const usedSystemShare = await shareText(title, lastPackInviteText, url);
    if (usedSystemShare) toast('已打开系统分享');
  } catch (e) {
    await copyText(lastPackInviteText);
    toast('系统分享不可用，已复制邀请');
  }
}

async function previewPack(packId) {
  if (!packId) return;
  const detail = $('pack-detail');
  detail.className = 'result-detail';
  detail.textContent = '预览中...';
  try {
    const data = await api('/api/mobile/context/packs/' + encodeURIComponent(packId) + '/preview');
    const sourceLines = formatSources(data.sources);
    detail.innerHTML = `<div class="item-head"><strong>${escapeHtml(data.name || packId)}</strong><span class="badge ${data.context ? 'ok' : ''}">${data.total_chars || 0} chars</span></div>
      <p><strong>来源</strong><br>${escapeHtml(sourceLines.join('\n') || '暂无')}</p>
      <pre>${escapeHtml(data.context || '没有找到可用 Context')}</pre>`;
  } catch (e) {
    detail.className = 'result-detail empty';
    detail.textContent = e.message || String(e);
  }
}

async function createPack() {
  if (!token()) return toast('请先保存 App Token');
  const name = $('pack-name').value.trim();
  if (!name) return toast('请填写端点名称');
  try {
    const data = await api('/api/mobile/context/packs', {
      method: 'POST',
      json: true,
      body: JSON.stringify({
        name,
        query: $('pack-query').value.trim(),
        description: $('pack-desc').value.trim(),
        include_memory: $('pack-memory').checked,
        include_wiki: $('pack-wiki').checked,
        include_documents: $('pack-docs').checked,
        enabled: true,
        generate: true,
      }),
    });
    if (data.pack && data.pack.token) await copyText(data.pack.token);
    $('pack-name').value = '';
    $('pack-query').value = '';
    $('pack-desc').value = '';
    toast('Context Pack 已创建，Token 已复制');
    loadPacks();
  } catch (e) {
    toast(e.message || String(e));
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制');
  } catch (_) {
    toast(text);
  }
}

async function shareText(title, text, url) {
  const payload = {
    title: title || '半人马个人 AI 节点',
    text: text || '',
    url: url || '',
    dialogTitle: title || '分享',
  };
  const plugin = nativeSharePlugin();
  if (plugin && typeof plugin.share === 'function') {
    try {
      await plugin.share(payload);
      return true;
    } catch (e) {
      if (!String(e && e.message || e).toLowerCase().includes('cancel')) throw e;
      return true;
    }
  }
  if (navigator.share) {
    try {
      await navigator.share(payload);
      return true;
    } catch (e) {
      if (!String(e && e.message || e).toLowerCase().includes('abort')) throw e;
      return true;
    }
  }
  await copyText([payload.text, payload.url].filter(Boolean).join('\n'));
  return false;
}

function escapeHtml(value) {
  return String(value || '').replace(/[&<>"']/g, (m) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[m]));
}

function bindEvents() {
  document.querySelectorAll('.tab').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.tab));
  });
  $('save-token').addEventListener('click', () => {
    setToken($('token-input').value.trim());
    toast('Token 已保存到本机');
    const pendingShare = localStorage.getItem(SHARE_PENDING_KEY);
    if (pendingShare) consumeSharedPayload(pendingShare).catch((e) => toast(e.message || String(e)));
    retryPendingNativeShare();
    syncOutbox({ quiet: true });
  });
  $('save-server-url').addEventListener('click', () => {
    try {
      setServerUrl($('server-url-input').value);
      toast(serverUrl() ? '节点地址已保存' : '已使用当前页面节点');
      loadStatus();
      if ($('tab-results').classList.contains('active')) loadMobileItems();
      syncOutbox({ quiet: true });
    } catch (e) {
      toast(e.message || String(e));
    }
  });
  $('refresh-btn').addEventListener('click', () => {
    loadStatus();
    if ($('tab-results').classList.contains('active')) loadMobileItems();
    else renderRecent();
  });
  $('record-start').addEventListener('click', startRecording);
  $('record-stop').addEventListener('click', stopRecording);
  $('record-file-upload').addEventListener('click', uploadSelectedRecording);
  $('generic-upload').addEventListener('click', uploadGenericFile);
  $('clip-submit').addEventListener('click', submitClip);
  $('search-run').addEventListener('click', runSearch);
  $('context-run').addEventListener('click', generateContext);
  $('context-copy').addEventListener('click', copyContext);
  $('context-snapshot').addEventListener('click', loadContextSnapshot);
  $('context-refresh').addEventListener('click', refreshContextSnapshot);
  $('clear-results').addEventListener('click', () => {
    saveRecent([]);
    renderRecent();
  });
  $('pack-create').addEventListener('click', createPack);
  $('pack-share-last').addEventListener('click', () => sharePackInvite(''));
  $('outbox-sync').addEventListener('click', () => syncOutbox());
  window.addEventListener('online', () => syncOutbox({ quiet: true }));
}

registerServiceWorker();
try {
  setServerUrl(serverUrl());
} catch (_) {
  localStorage.removeItem(SERVER_URL_KEY);
  $('server-url-input').value = '';
}
setToken(token());
bindEvents();
consumeServerUrlParam();
consumePairingToken();
consumeSharedClip();
registerNativeShareTarget();
loadStatus();
updateOutboxStatus().then(() => syncOutbox({ quiet: true }));
setInterval(() => syncOutbox({ quiet: true }), 30000);
renderRecent();
