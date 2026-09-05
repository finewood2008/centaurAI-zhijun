// 半人马AI 私有记忆库 — 前端逻辑
let connected = false;
let currentMode = 'text';
let retryCount = 0;
const MAX_RETRIES = 40;
let latestLanConfig = null;
let latestRagConfig = null;
let latestMobileConfig = null;
let latestMcpRemote = null;

// ===== 标注状态 =====
// 全量标注缓存 {source_path: {tags,importance,pinned,note,caption}}，随文档列表刷新
let annotationsCache = {};
// 标签筛选当前选中（AND）
let activeTagFilter = new Set();
// 标注编辑器当前目标 + 草稿
let annotEditTarget = null;
let annotDraft = null;

// ===== 文件中心状态 =====
let currentView = 'search';          // 'search' | 'files' | 'wiki' | 'memory' | 'settings'
let groupsCache = [];                // [{name,count}]
let fcDocs = [];                     // listDocuments 缓存
let fcFilter = { kind: 'all', value: '' };  // 当前分类/分组过滤
let fcSortKey = 'mtime';
let fcSortDir = 'desc';
let fcSearchText = '';
let fcBatchMode = false;            // 批量管理模式
let fcSelected = new Set();         // 选中的文件 ID
let fcViewMode = localStorage.getItem('centaur-fc-view') || 'grid';
let fcTotal = 0;
let fcFacets = { types: {}, statuses: {}, groups: {}, tags: {}, total: 0 };
let fcOffset = 0;
const FC_PAGE_SIZE = 60;
let fcLastSelectedId = '';
let fcSearchTimer = null;
let fcUndoCallback = null;
let fcUndoTimer = null;
let fcSavedViews = (() => {
  try { return JSON.parse(localStorage.getItem('centaur-fc-saved-views') || '[]'); }
  catch (_) { return []; }
})();

// ===== Wiki 状态 =====
let wikiState = {
  currentPath: '',
  currentPage: null,
  pages: [],
  folder: '',
  query: '',
};
let wikiEngineStatus = null;
const SETTINGS_SECTIONS = {
  general: {
    kicker: '基础设置',
    title: '设备与服务',
    description: '管理文件目录、维护任务和后端运行状态。',
  },
  intelligence: {
    kicker: 'AI 与索引',
    title: '本地智能能力',
    description: '配置检索模型、RAG 策略、Wiki 索引与自动整理。',
  },
  access: {
    kicker: '连接与访问',
    title: '让 Agent 与设备安全接入',
    description: '管理局域网访问，并通过普通或高级模式连接标准 MCP。',
  },
  integrations: {
    kicker: '手机端与 A2A',
    title: '采集与 Agent 上下文共享',
    description: '手机 App 采集和 A2A Context Pack 暂未对外开通。',
    unavailable: true,
  },
};
let settingsSection = SETTINGS_SECTIONS[localStorage.getItem('centaur-settings-section')]
  ? localStorage.getItem('centaur-settings-section')
  : 'general';


async function loadAnnotations() {
  try {
    const d = await window.api.getAnnotations();
    annotationsCache = (d && d.annotations) || {};
  } catch (e) {
    annotationsCache = {};
  }
}

function annOf(sourcePath) {
  return annotationsCache[sourcePath] || null;
}

// 全部标注里出现过的标签去重排序（标签筛选条用）
function allKnownTags() {
  const s = new Set();
  for (const k in annotationsCache) (annotationsCache[k].tags || []).forEach((t) => s.add(t));
  return [...s].sort((a, b) => a.localeCompare(b, 'zh'));
}

// 标注徽标（⭐重要 / 📌置顶 / 🏷标签数）——文档列表与搜索结果共用
function annotBadges(ann) {
  if (!ann) return '';
  const parts = [];
  if (ann.importance > 0) parts.push(`<span class="ann-badge imp" title="重要度 ${ann.importance}">⭐${ann.importance}</span>`);
  if (ann.pinned) parts.push('<span class="ann-badge pin" title="置顶必回">📌</span>');
  if (ann.tags && ann.tags.length) {
    const shown = ann.tags.slice(0, 3).map((t) => `<span class="ann-tag">${escapeHtml(t)}</span>`).join('');
    const more = ann.tags.length > 3 ? `<span class="ann-tag more">+${ann.tags.length - 3}</span>` : '';
    parts.push(`<span class="ann-tags">${shown}${more}</span>`);
  }
  return parts.length ? `<span class="ann-badges">${parts.join('')}</span>` : '';
}

async function init() {
  await waitForBackend();
  document.getElementById('loading-overlay').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');

  await checkHealth();
  await loadStats();
  await loadAnnotations();
  await loadDocuments();
  renderTagFilterBar();

  // 启动索引监控（本地上传 + LAN 导入都会触发）
  startIndexWatcher();

  setInterval(checkHealth, 30000);
  setInterval(loadStats, 60000);
  setInterval(async () => { await loadAnnotations(); await loadDocuments(); renderTagFilterBar(); }, 60000);
  localStorage.removeItem('centaur-wiki-section');
  setInterval(() => { if (currentView === 'wiki') refreshWikiJobs(); }, 5000);
}

async function waitForBackend() {
  const msgEl = document.getElementById('loading-msg');
  const barEl = document.getElementById('loading-bar-fill');
  const steps = [
    '正在连接后端服务…',
    '正在加载文本嵌入模型…',
    '正在加载重排 / 视觉模型…',
    '正在构建检索索引…',
    '即将就绪…',
  ];
  while (retryCount < MAX_RETRIES) {
    try {
      const data = await window.api.health();
      if (data.status === 'ok') {
        barEl.style.width = '100%';
        msgEl.textContent = '✅ 服务就绪';
        await sleep(300);
        return;
      }
    } catch (e) {}
    retryCount++;
    const step = Math.min(Math.floor(retryCount / 4), steps.length - 1);
    msgEl.textContent = steps[step];
    barEl.style.width = Math.min(95, (retryCount / MAX_RETRIES) * 100) + '%';
    await sleep(1200);
  }
  msgEl.textContent = '⚠️ 服务连接超时，请检查后端是否启动';
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function checkHealth() {
  const el = document.getElementById('status');
  try {
    const data = await window.api.health();
    connected = data.status === 'ok';
    el.textContent = connected ? '● 已连接' : '● 连接异常';
    el.className = connected ? 'connected' : 'disconnected';
    document.getElementById('watch-folder').textContent = '监控：' + (data.watch_folder || '--');
    renderModelChips(data.capabilities || {});
  } catch (e) {
    connected = false;
    el.textContent = '● 未连接';
    el.className = 'disconnected';
  }
}

function renderModelChips(cap) {
  const chips = [
    { on: true, label: cap.text_model || 'bge' },
    { on: cap.reranker, label: '重排' },
    { on: cap.hybrid_bm25, label: 'BM25' },
    { on: cap.ocr, label: 'OCR' },
    { on: cap.visual, label: '视觉' },
    { on: cap.video, label: '视频' },
    { on: cap.transcribe, label: '转写' },
  ];
  document.getElementById('model-chips').innerHTML = chips
    .map((c) => `<span class="chip ${c.on ? 'on' : 'off'}">${c.on ? '●' : '○'} ${escapeHtml(c.label)}</span>`)
    .join('');
}

async function loadStats() {
  try {
    const d = await window.api.stats();
    document.getElementById('stat-total').textContent = d.total_documents ?? '--';
    document.getElementById('stat-chunks').textContent = d.total_chunks ?? '--';
    document.getElementById('stat-image').textContent = d.visual_indexed_images ?? d.image_documents ?? '--';
    const vEl = document.getElementById('stat-video');
    if (vEl) vEl.textContent = d.video_documents ?? '--';
  } catch (e) {}
}

const VIDEO_EXTS = ['mp4', 'mov', 'mkv', 'webm', 'avi', 'm4v'];

function fileIcon(name, fileType) {
  if (fileType === 'image') return '🖼️';
  if (fileType === 'video') return '🎬';
  const ext = (name.split('.').pop() || '').toLowerCase();
  if (VIDEO_EXTS.includes(ext)) return '🎬';
  if (ext === 'pdf') return '📕';
  if (ext === 'docx') return '📘';
  if (ext === 'md') return '📝';
  return '📄';
}

// 缩略图内容：底层 emoji 兜底 + 覆盖其上的 img（图片→自身、视频→海报帧、其它→无 img）。
// img 加载失败 onerror 自移除，露出 emoji。容器靠 .doc-thumb/.annot-thumb 样式分层。
function thumbInner(sourcePath, fileType, poster, name) {
  const base = window.api.base;
  let url = '';
  if (fileType === 'image') url = `${base}/api/image?path=${encodeURIComponent(sourcePath)}`;
  else if (fileType === 'video' && poster) url = `${base}/api/frame?path=${encodeURIComponent(poster)}`;
  const icon = fileIcon(name || (sourcePath || '').split('/').pop(), fileType);
  return `<span class="thumb-fallback">${icon}</span>` +
    (url ? `<img src="${escapeHtml(url)}" loading="lazy" onerror="this.remove()">` : '');
}

function thumbHtml(sourcePath, fileType, poster, name, cls) {
  return `<span class="${cls}">${thumbInner(sourcePath, fileType, poster, name)}</span>`;
}

async function loadDocuments() {
  try {
    const data = await window.api.listDocuments({ limit: 500, status: 'indexed' });
    document.getElementById('doc-count').textContent = data.total;
    const list = document.getElementById('doc-list');
    if (!data.items.length) {
      list.innerHTML = '<p style="font-size:12px;color:var(--text-faint);padding:8px 2px">暂无文档，拖入文件开始</p>';
      return;
    }
    list.innerHTML = data.items
      .map((item) => {
        const name = item.metadata.file_name || item.id.split('/').pop();
        const ft = item.metadata.file_type;
        const chunks = item.chunk_count || 1;
        const ann = annOf(item.id);
        const badges = annotBadges(ann);
        const sub = ft === 'image' ? '图片' : ft === 'video' ? '视频 · ' + chunks + ' 段' : chunks + ' 块';
        const posterAttr = item.poster ? encodeURIComponent(item.poster) : '';
        return `<div class="doc-item">
          ${thumbHtml(item.id, ft, item.poster, name, 'doc-thumb')}
          <span class="doc-meta">
            <div class="doc-name" title="${escapeHtml(item.id)}">${escapeHtml(name)}</div>
            <div class="doc-sub">${sub}${badges}</div>
          </span>
          <button class="annot-btn" data-id="${encodeURIComponent(item.id)}" data-name="${escapeHtml(name)}" data-ft="${escapeHtml(ft || '')}" data-poster="${posterAttr}" title="标注（重要/置顶/标签/说明）">🏷</button>
          <button class="delete-btn" data-id="${encodeURIComponent(item.id)}" title="删除">🗑</button>
        </div>`;
      })
      .join('');
    list.querySelectorAll('.delete-btn').forEach((b) =>
      b.addEventListener('click', () => deleteDoc(decodeURIComponent(b.dataset.id)))
    );
    list.querySelectorAll('.annot-btn').forEach((b) =>
      b.addEventListener('click', () => openAnnotEditor(
        decodeURIComponent(b.dataset.id), b.dataset.name, b.dataset.ft,
        b.dataset.poster ? decodeURIComponent(b.dataset.poster) : ''))
    );
  } catch (e) {}
}

async function deleteDoc(docId) {
  if (!confirm('将此文件移至回收站？可随时恢复。')) return;
  try {
    const response = await window.api.deleteDocument(docId);
    toast('已移至回收站');
    if (response.trash_id) showFcUndo('文件已移至回收站', async () => {
      await window.api.restoreTrash(response.trash_id);
      await loadFileCenter();
      loadDocuments(); loadStats();
    });
    fcSelected.delete(docId);
    loadDocuments();
    if (currentView === 'files') loadFileCenter();
    loadStats();
  } catch (e) {
    toast('删除失败');
  }
}

function scorePct(score, type) {
  // 文本重排分 0~1 直接用；视觉 CLIP 余弦偏低，放大以便可视化
  const pct = type === 'visual' ? score * 175 : score * 100;
  return Math.max(4, Math.min(100, pct));
}

async function doSearch() {
  const query = document.getElementById('search-input').value.trim();
  const results = document.getElementById('search-results');
  const metaEl = document.getElementById('result-meta');
  if (!query) return;

  results.innerHTML = '<div class="state-msg"><div class="spinner-sm"></div>检索中…</div>';
  metaEl.classList.add('hidden');

  try {
    const tags = activeTagFilter.size ? [...activeTagFilter] : null;
    const data = await window.api.search(query, { mode: currentMode, nResults: 8, tags });
    if (!data.results || data.results.length === 0) {
      results.innerHTML = '<div class="state-msg"><div class="empty-icon">🫥</div><p>没有足够相关的结果</p><p class="empty-sub">已按相关度阈值过滤，避免返回噪声</p></div>';
      metaEl.classList.add('hidden');
      return;
    }
    metaEl.classList.remove('hidden');
    const modeLabel = { text: '文本', visual: '以文搜图', hybrid: '混合' }[data.mode] || data.mode;
    metaEl.innerHTML = `共 ${data.total} 条 · 模式：${modeLabel}` +
      (data.reranked ? ' · <span class="badge-reranked">已重排精选</span>' : '');

    results.innerHTML = data.results
      .map((r) => {
        const type = r.match_type || 'text';
        const meta = r.metadata || {};
        const name = meta.file_name || '未知';
        const score = typeof r.score === 'number' ? r.score : 1 - (r.distance || 0);
        const modality = r.modality || meta.modality;
        const isVideoFrame = modality === 'frame';
        const isImg = isVideoFrame || type === 'visual' || meta.file_type === 'image';
        const src = r.source_path || meta.source_path;
        const framePath = r.frame_path || meta.frame_path;

        // 记忆命中 → 特殊卡片
        if (type === 'memory') {
          const memTypeLabel = { long_term: '长期记忆', user_profile: '用户画像', agents_rules: '行为规则', journal: '日记', custom: '自定义' }[meta.memory_type] || meta.memory_type || '记忆';
          const memRelPath = r.rel_path || src || '';
          return '<div class="result-item memory-hit" data-mem="' + escapeHtml(memRelPath) + '">' +
            '<div class="result-body">' +
            '<div class="result-row">' +
            '<span class="match-badge memory">🧠 记忆 · ' + escapeHtml(memTypeLabel) + '</span>' +
            '<span class="result-name" title="' + escapeHtml(memRelPath) + '">' + escapeHtml(name) + '</span>' +
            '<span class="score-wrap">' +
            '<span class="score-bar"><span class="score-fill memory" style="width:' + scorePct(score, 'text') + '%"></span></span>' +
            '<span class="score-val">' + score.toFixed(2) + '</span>' +
            '</span></div>' +
            '<div class="result-text">' + escapeHtml(r.text || '') + '</div>' +
            '<div class="result-file">' + escapeHtml(memRelPath) + '</div>' +
            '</div></div>';
        }

        if (type === 'wiki') {
          const pagePath = r.page_path || meta.page_path || src || '';
          const wikiType = { source: '资料页', concept: '概念页', note: '笔记', home: '首页' }[meta.wiki_type || r.wiki_type] || 'Wiki';
          return '<div class="result-item wiki-hit" data-wiki="' + escapeHtml(pagePath) + '">' +
            '<div class="result-body">' +
            '<div class="result-row">' +
            '<span class="match-badge wiki">🕸 Wiki · ' + escapeHtml(wikiType) + '</span>' +
            '<span class="result-name" title="' + escapeHtml(pagePath) + '">' + escapeHtml(r.title || name) + '</span>' +
            '<span class="score-wrap">' +
            '<span class="score-bar"><span class="score-fill wiki" style="width:' + scorePct(score, 'text') + '%"></span></span>' +
            '<span class="score-val">' + score.toFixed(2) + '</span>' +
            '</span></div>' +
            '<div class="result-text">' + escapeHtml(r.text || '') + '</div>' +
            '<div class="result-file">' + escapeHtml(pagePath) + '</div>' +
            '</div></div>';
        }

        // 视频帧走 /api/frame（帧图在 video_frames/，不在监控目录）；其余图片走 /api/image
        const thumbUrl = isVideoFrame && framePath
          ? `${window.api.base}/api/frame?path=${encodeURIComponent(framePath)}`
          : (isImg && src ? `${window.api.base}/api/image?path=${encodeURIComponent(src)}` : '');
        const thumb = thumbUrl
          ? `<img class="result-thumb" src="${thumbUrl}" onerror="this.style.display='none'">`
          : '';
        // 时间戳（视频块）：start_time 顶层或 metadata，秒 → [mm:ss]，可点跳播
        const stRaw = r.start_time != null ? r.start_time : meta.start_time;
        const sec = parseFloat(stRaw);
        const isVideoHit = meta.file_type === 'video' || ['transcript', 'ocr', 'frame'].includes(modality);
        const vsrc = isVideoHit && src ? src : '';
        const mmss = Number.isFinite(sec)
          ? `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(Math.floor(sec % 60)).padStart(2, '0')}`
          : '';
        const ts = Number.isFinite(sec)
          ? (vsrc
              ? `<button class="ts-badge playable" data-vsrc="${escapeHtml(vsrc)}" data-vstart="${sec}" title="跳到此刻播放">⏱ ${mmss} ▶</button>`
              : `<span class="ts-badge">⏱ ${mmss}</span>`)
          : '';
        const modLabel = { transcript: '🎙 转写', ocr: '🔤 字幕', frame: '🎬 画面' }[modality] || '';
        const badgeHtml = modLabel
          ? `<span class="match-badge video">${modLabel}</span>`
          : (type === 'visual'
              ? '<span class="match-badge visual">🖼️ 视觉</span>'
              : '<span class="match-badge text">📝 文本</span>');
        // 视觉命中(以文搜图/视频帧)的 r.text 实为内部文件名/帧路径(非正文)，不展示——
        // 只有文本/转写/OCR 命中才有真正的正文。
        const hasRealText = r.text && r.text.trim() && type !== 'visual';
        // caption 命中：把它当正文展示但加「说明」标识（modality==='caption'）
        const isCaption = modality === 'caption';
        const text = hasRealText
          ? `<div class="result-text${isCaption ? ' caption-hit' : ''}">${isCaption ? '✏️ ' : ''}${escapeHtml(r.text)}</div>`
          : `<div class="result-text empty">（${isVideoFrame ? '视频画面' : '图片'}，无文字内容）</div>`;
        // 标注：后端已叠加 r.annotation；缺则按 source_path 兜底查缓存
        const ann = r.annotation || annOf(src);
        const annBadges = annotBadges(ann);
        const pinFlag = r.pinned_bypass ? '<span class="ann-badge pin-bypass" title="置顶·绕过阈值返回">📌必回</span>' : '';
        const noteHtml = ann && ann.note
          ? `<div class="result-note" title="备注">🗒 ${escapeHtml(ann.note)}</div>`
          : '';
        return `<div class="result-item">
          ${thumb}
          <div class="result-body">
            <div class="result-row">
              ${badgeHtml}
              ${ts}
              <span class="result-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
              ${pinFlag}${annBadges}
              <button class="annot-btn sm" data-id="${encodeURIComponent(src || '')}" data-name="${escapeHtml(name)}" data-ft="${escapeHtml((r.metadata && r.metadata.file_type) || '')}" data-poster="${framePath ? encodeURIComponent(framePath) : ''}" title="标注">🏷</button>
              <span class="score-wrap">
                <span class="score-bar"><span class="score-fill ${type}" style="width:${scorePct(score, type)}%"></span></span>
                <span class="score-val">${score.toFixed(2)}</span>
              </span>
            </div>
            ${text}
            ${noteHtml}
          </div>
        </div>`;
      })
      .join('');
    // 视频命中的时间戳徽标可点 → 跳到该时刻播放
    results.querySelectorAll('.ts-badge.playable').forEach((b) =>
      b.addEventListener('click', () => openVideoAt(b.dataset.vsrc, parseFloat(b.dataset.vstart) || 0)));
    // 结果项标注入口
    results.querySelectorAll('.annot-btn').forEach((b) =>
      b.addEventListener('click', () => {
        const id = decodeURIComponent(b.dataset.id);
        if (id) openAnnotEditor(id, b.dataset.name, b.dataset.ft, b.dataset.poster ? decodeURIComponent(b.dataset.poster) : '');
      }));
    // 记忆命中 → 切换到记忆面板并打开对应文件
    results.querySelectorAll('.memory-hit').forEach((item) =>
      item.addEventListener('click', () => {
        const memPath = item.dataset.mem;
        if (memPath) { switchView('memory'); selectMemoryFile(memPath); }
      }));
    results.querySelectorAll('.wiki-hit').forEach((item) =>
      item.addEventListener('click', () => {
        const pagePath = item.dataset.wiki;
        if (pagePath) { switchView('wiki'); selectWikiPage(pagePath); }
      }));
  } catch (e) {
    results.innerHTML = '<div class="state-msg"><p style="color:var(--danger)">搜索失败，请检查后端</p></div>';
  }
}

// ===== 视频跳转播放 =====
function openVideoAt(videoPath, start) {
  const modal = document.getElementById('video-modal');
  const v = document.getElementById('video-player');
  const title = document.getElementById('video-title');
  const s = Math.max(0, start || 0);
  title.textContent = `${(videoPath || '').split('/').pop()} · ${String(Math.floor(s / 60)).padStart(2, '0')}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  // 媒体片段 #t= 让浏览器直接定位；并在元数据就绪后兜底 seek
  v.src = `${window.api.base}/api/video?path=${encodeURIComponent(videoPath)}#t=${s}`;
  v.onloadedmetadata = () => { try { v.currentTime = s; } catch (e) {} };
  modal.classList.remove('hidden');
  v.play().catch(() => {});
}

function closeVideoModal() {
  const modal = document.getElementById('video-modal');
  const v = document.getElementById('video-player');
  try { v.pause(); } catch (e) {}
  v.removeAttribute('src');
  v.load();
  modal.classList.add('hidden');
}

async function uploadFiles(files) {
  const prog = document.getElementById('upload-progress');
  prog.classList.remove('hidden');
  let done = 0;
  const pending = [];   // 视频后台任务
  for (const file of files) {
    prog.textContent = `上传中 ${++done}/${files.length}：${file.name}`;
    try {
      const res = await window.api.upload(file);
      if (res && res.queued && res.doc_id) pending.push({ id: res.doc_id, name: file.name });
    } catch (e) {
      var errMsg = e.message || e;
      if (errMsg.includes('413') || errMsg.includes('过大')) errMsg = '文件过大（上限4GB）';
      console.error('上传失败:', file.name, errMsg);
      toast(`上传失败：${file.name}（${errMsg}）`);
    }
  }
  loadDocuments();
  loadStats();

  if (pending.length === 0) {
    prog.textContent = `✅ 已处理 ${files.length} 个文件`;
    setTimeout(() => prog.classList.add('hidden'), 2500);
    return;
  }
  // 视频在后台转写/抽帧，轮询任务状态直到全部结束（前端不再误以为失败）
  await pollVideoJobs(pending, prog);
  prog.textContent = '✅ 视频处理完成';
  setTimeout(() => prog.classList.add('hidden'), 2500);
  loadDocuments();
  loadStats();
}

async function pollVideoJobs(jobs, prog) {
  const deadline = Date.now() + 30 * 60 * 1000;   // 最多等 30 分钟
  const remaining = new Map(jobs.map((j) => [j.id, j.name]));
  const unknownStreak = new Map();                // 连续 unknown 次数（后端重启会丢 _JOBS）
  const total = jobs.length;
  while (remaining.size > 0 && Date.now() < deadline) {
    prog.textContent = `🎬 视频处理中（转写/抽帧）…（${total - remaining.size}/${total}）`;
    await sleep(2000);
    for (const [id, name] of [...remaining]) {
      let st;
      try { st = await window.api.jobStatus(id); } catch (e) { continue; }
      const state = st && st.state;
      if (state === 'done' || state === 'failed') {
        remaining.delete(id);
        unknownStreak.delete(id);
        if (state === 'failed') toast(`视频处理失败：${name}`);
      } else if (state === 'unknown') {
        // 后端重启会清空内存任务表 → 持续 unknown；视为已结束(索引会在重启扫描时续上)
        const n = (unknownStreak.get(id) || 0) + 1;
        unknownStreak.set(id, n);
        if (n >= 3) remaining.delete(id);
      } else {
        unknownStreak.delete(id);   // queued/processing：重置
      }
    }
    loadStats();
  }
}

// ===== 设置页面 =====
function setSettingsSection(section, remember = true) {
  if (!SETTINGS_SECTIONS[section]) section = 'general';
  settingsSection = section;
  if (remember) localStorage.setItem('centaur-settings-section', section);
  var meta = SETTINGS_SECTIONS[section];
  document.querySelectorAll('[data-settings-section]').forEach(function(button) {
    button.classList.toggle('active', button.dataset.settingsSection === section);
  });
  document.querySelectorAll('[data-settings-group]').forEach(function(card) {
    card.classList.toggle('hidden', card.dataset.settingsGroup !== section);
  });
  document.getElementById('settings-content-kicker').textContent = meta.kicker;
  document.getElementById('settings-content-title').textContent = meta.title;
  document.getElementById('settings-content-desc').textContent = meta.description;
  var badge = document.getElementById('settings-scope-badge');
  if (badge) {
    badge.textContent = meta.unavailable ? '未开通' : 'LOCAL FIRST';
    badge.classList.toggle('unavailable', !!meta.unavailable);
  }
  var content = document.querySelector('.settings-content');
  if (content) content.scrollTop = 0;
}

async function loadSettingsPage() {
  setSettingsSection(settingsSection, false);
  try {
    const [h, cfg] = await Promise.all([window.api.health(), window.api.config()]);
    document.getElementById('setting-watch-folder').value = h.watch_folder || '--';
    document.getElementById('setting-api').textContent = (cfg.api && cfg.api.base_url) || window.api.base;
    document.getElementById('setting-status').textContent = h.status === 'ok' ? '✅ 正常运行' : '❌ 异常';
    const cap = h.capabilities || {};
    const rows = [
      ['文本嵌入', cap.text_model || 'bge-small-zh', true],
      ['交叉重排', 'bge-reranker', cap.reranker],
      ['词面召回', 'BM25 (jieba)', cap.hybrid_bm25],
      ['图片 OCR', 'rapidocr', cap.ocr],
      ['视觉检索', 'Chinese-CLIP', cap.visual],
      ['视频解析', 'ffmpeg', cap.video],
      ['语音转写', 'faster-whisper', cap.transcribe],
    ];
    document.getElementById('model-info').innerHTML = rows
      .map(
        ([label, val, on]) =>
          `<div class="model-row"><span class="label">${label}</span><span class="val ${on ? 'on' : 'off'}">${on ? '● ' : '○ '}${escapeHtml(val)}</span></div>`
      )
      .join('');
    renderMcpInfo(cfg.mcp || null);

    // 加载 LAN 配置
    try {
      var lanCfg = await window.api.fetch('/api/lan/config');
      renderLanConfig(lanCfg);
    } catch(e) {}
    try {
      var ragCfg = await window.api.fetch('/api/rag/config');
      renderRagConfig(ragCfg);
    } catch(e) {
      document.getElementById('rag-strategy-detail').innerHTML = '<span class="setting-hint">策略配置加载失败</span>';
    }
    await loadWikiEngineStatus();
    await loadWikiOrganizerStatus();
    try {
      var mobileCfg = await window.api.fetch('/api/mobile/config');
      renderMobileConfig(mobileCfg);
    } catch(e) {}
    try {
      await loadContextPacks();
    } catch(e) {}
    try {
      await loadMcpRemote();
    } catch(e) {
      document.getElementById('mcp-remote-info').innerHTML = '<p class="setting-hint">无法读取远程 MCP 状态</p>';
    }
  } catch (e) {
    document.getElementById('setting-status').textContent = '❌ 未连接';
    document.getElementById('mcp-info').innerHTML = '<p class="setting-hint">无法读取 MCP 配置</p>';
  }
}
function renderLanConfig(lanCfg) {
  latestLanConfig = lanCfg || {};
  var enabled = !!latestLanConfig.enabled;
  var passwordSet = !!latestLanConfig.password_set;
  var urls = Array.isArray(latestLanConfig.urls) ? latestLanConfig.urls : [];
  if (!urls.length && latestLanConfig.url) urls = [latestLanConfig.url];
  var url = urls[0] || '';

  document.getElementById('setting-lan-enabled').checked = enabled;
  document.getElementById('setting-lan-password').placeholder = passwordSet ? '访问密码（已设置，留空不变）' : '访问密码';
  document.getElementById('setting-lan-url').value = url || '未检测到局域网地址';
  document.getElementById('setting-lan-copy').disabled = !url;
  var altEl = document.getElementById('setting-lan-alt-urls');
  altEl.innerHTML = urls.slice(1).map(function(u) {
    return '<button class="lan-alt-url" type="button" data-url="' + escapeHtml(u) + '" title="复制备用地址">' + escapeHtml(u) + '</button>';
  }).join('');
  altEl.querySelectorAll('.lan-alt-url').forEach(function(btn) {
    btn.addEventListener('click', function() {
      copyText(btn.dataset.url || '');
    });
  });

  var hint = '';
  if (!url) {
    hint = '未检测到非 127.0.0.1 的 IPv4 地址';
  } else if (latestLanConfig.active) {
    hint = '已启用 HTTP 直连，同一局域网设备可打开此地址';
  } else if (enabled && passwordSet) {
    hint = '已保存，重启后端后局域网设备可访问';
  } else if (enabled) {
    hint = '设置密码并保存后，重启后端生效';
  } else {
    hint = '启用并设置密码后，重启后端生效';
  }
  if (url && latestLanConfig.scheme === 'http') hint += '；仅建议在可信局域网使用';
  if (urls.length > 1) hint += '；检测到 ' + urls.length + ' 个地址';
  document.getElementById('setting-lan-url-hint').textContent = hint;
}

function renderRagConfig(payload) {
  latestRagConfig = payload || {};
  var strategies = latestRagConfig.strategies || [];
  var cfg = latestRagConfig.config || {};
  var typeCfg = cfg.file_type_strategies || {};
  var selectIds = ['setting-rag-default', 'setting-rag-text', 'setting-rag-image', 'setting-rag-video'];
  selectIds.forEach(function(id) {
    var select = document.getElementById(id);
    if (!select) return;
    select.innerHTML = strategies.map(function(s) {
      return '<option value="' + escapeHtml(s.id) + '">' + escapeHtml(s.label || s.id) + '</option>';
    }).join('');
  });
  document.getElementById('setting-rag-default').value = cfg.default_strategy || 'balanced';
  document.getElementById('setting-rag-text').value = typeCfg.text || 'balanced';
  document.getElementById('setting-rag-image').value = typeCfg.image || 'visual_ocr';
  document.getElementById('setting-rag-video').value = typeCfg.video || 'video_hybrid';
  renderRagStrategyDetail();
}

function getSelectedRagStrategy() {
  var strategies = (latestRagConfig && latestRagConfig.strategies) || [];
  var selectedId = document.getElementById('setting-rag-default').value || 'balanced';
  return strategies.find(function(s) { return s.id === selectedId; }) || strategies[0] || null;
}

function renderRagStrategyDetail() {
  var el = document.getElementById('rag-strategy-detail');
  var strategy = getSelectedRagStrategy();
  if (!strategy) {
    el.innerHTML = '<span class="setting-hint">未加载策略</span>';
    return;
  }
  el.innerHTML =
    '<div class="rag-strategy-title">' + escapeHtml(strategy.label || strategy.id) + '</div>' +
    '<div>' + escapeHtml(strategy.description || '') + '</div>' +
    '<div class="rag-strategy-metrics">' +
      '<span>分块 ' + escapeHtml(strategy.chunk_size) + '</span>' +
      '<span>重叠 ' + escapeHtml(strategy.chunk_overlap) + '</span>' +
      '<span>召回 ×' + escapeHtml(strategy.recall_multiplier) + '</span>' +
      '<span>最少 ' + escapeHtml(strategy.recall_min) + '</span>' +
      '<span>重排 ' + escapeHtml(strategy.rerank_max) + '</span>' +
    '</div>';
}

function renderWikiOrganizerStatus(status, error) {
  status = status || {};
  var ready = !!status.ready;
  var failed = !!error || !ready;
  var runtime = document.getElementById('setting-wiki-organizer-runtime');
  setWikiEngineRuntime(runtime, ready, failed);
  document.getElementById('setting-wiki-organizer-status').textContent = ready
    ? '本地模型已就绪'
    : (error || status.error || '本地模型不可用');
  document.getElementById('setting-wiki-organizer-model').textContent = status.model || 'qwen3:1.7b';
  document.getElementById('setting-wiki-organizer-provider').textContent = 'Ollama（本机）';
  var memoryPolicy = status.memory_policy || {};
  document.getElementById('setting-wiki-organizer-memory').textContent = memoryPolicy.keep_alive_seconds === 0
    ? '按需加载 · 用后卸载'
    : '空闲后自动卸载';
  document.getElementById('setting-wiki-organizer-concurrency').textContent =
    '最多 ' + Number(memoryPolicy.max_loaded_models || 1) + ' 个模型 · ' +
    Number(memoryPolicy.max_parallel || 1) + ' 个任务';
  document.getElementById('setting-wiki-organizer-msg').textContent = ready
    ? '摘要、标签和概念由本机模型生成；任务完成即释放模型内存，资料不会发送到云端。'
    : (error || status.error || '本地模型不可用') + '；当前将使用本地规则整理。';
}

async function loadWikiOrganizerStatus() {
  try {
    var status = await window.api.fetch('/api/wiki/organizer/status');
    renderWikiOrganizerStatus(status, '');
    return status;
  } catch (error) {
    renderWikiOrganizerStatus(null, error.message || '无法检测本地模型');
    return null;
  }
}

function renderMobileConfig(cfg) {
  latestMobileConfig = cfg || {};
  var urls = Array.isArray(latestMobileConfig.urls) ? latestMobileConfig.urls : [];
  if (!urls.length && latestMobileConfig.url) urls = [latestMobileConfig.url];
  var url = urls[0] || '';
  document.getElementById('setting-mobile-enabled').checked = !!latestMobileConfig.enabled;
  document.getElementById('setting-mobile-url').value = url || '未检测到 Tailscale/局域网地址';
  document.getElementById('setting-mobile-copy').disabled = !url;
  document.getElementById('setting-mobile-token').value = '';
  document.getElementById('setting-mobile-token').placeholder = latestMobileConfig.has_token
    ? 'App Token（已设置，尾号 ' + (latestMobileConfig.token_suffix || '******') + '；留空保留）'
    : 'App Token（可点击生成）';
  document.getElementById('setting-mobile-alt-urls').innerHTML = urls.slice(1).map(function(u) {
    return '<button class="lan-alt-url" type="button" data-url="' + escapeHtml(u) + '">' + escapeHtml(u) + '</button>';
  }).join('');
  document.querySelectorAll('#setting-mobile-alt-urls .lan-alt-url').forEach(function(btn) {
    btn.addEventListener('click', function() { copyText(btn.dataset.url || ''); });
  });
  document.getElementById('setting-mobile-msg').textContent = latestMobileConfig.has_token
    ? '手机打开上方 /mobile 页面并保存 App Token；若刚启用，需重启后端让 Tailscale 地址生效'
    : '生成 Token 后，手机端保存一次即可；启用后需重启后端开放 Tailscale 访问';
  if (!latestMobileConfig.has_token) renderMobilePairing(null);
}

function renderMobilePairing(data) {
  var card = document.getElementById('setting-mobile-pair-card');
  if (!card) return;
  data = data || {};
  var url = data.url || '';
  var urls = Array.isArray(data.urls) ? data.urls : [];
  var qr = data.qr_data_url || '';
  if (!url && urls.length) url = urls[0];
  if (!url) {
    card.classList.add('hidden');
    return;
  }
  card.classList.remove('hidden');
  document.getElementById('setting-mobile-pair-url').value = url;
  var img = document.getElementById('setting-mobile-qr');
  if (qr) {
    img.src = qr;
    img.style.display = '';
  } else {
    img.removeAttribute('src');
    img.style.display = 'none';
  }
  var alt = document.getElementById('setting-mobile-pair-alt');
  alt.innerHTML = urls.slice(1).map(function(u) {
    return '<button class="lan-alt-url" type="button" data-url="' + escapeHtml(u) + '">' + escapeHtml(u) + '</button>';
  }).join('');
  alt.querySelectorAll('.lan-alt-url').forEach(function(btn) {
    btn.addEventListener('click', function() {
      document.getElementById('setting-mobile-pair-url').value = btn.dataset.url || '';
      copyText(btn.dataset.url || '');
    });
  });
}

async function loadContextPacks() {
  var data = await window.api.fetch('/api/context/packs');
  renderContextPacks((data && data.packs) || []);
}

function renderContextPacks(packs) {
  var box = document.getElementById('setting-context-list');
  if (!box) return;
  if (!packs.length) {
    box.innerHTML = '<p class="setting-hint">暂无 Context Pack</p>';
    return;
  }
  box.innerHTML = packs.map(function(p) {
    var cardPath = '/api/a2a/' + encodeURIComponent(p.id) + '/agent-card.json';
    var msgPath = '/api/a2a/' + encodeURIComponent(p.id) + '/message:send';
    return '<div class="context-pack-item">' +
      '<div class="context-pack-head">' +
        '<span class="context-pack-name">' + escapeHtml(p.name || p.id) + '</span>' +
        '<span class="pill">' + (p.enabled ? '启用' : '停用') + '</span>' +
      '</div>' +
      '<div class="context-pack-meta">ID: ' + escapeHtml(p.id) + ' · Token 尾号: ' + escapeHtml(p.token_suffix || '无') + '</div>' +
      '<div class="context-pack-meta">' + escapeHtml(p.query || p.description || '') + '</div>' +
      '<div class="context-pack-actions">' +
        '<button class="secondary-btn sm" data-copy="' + escapeHtml(cardPath) + '">Agent Card</button>' +
        '<button class="secondary-btn sm" data-copy="' + escapeHtml(msgPath) + '">Message</button>' +
      '</div>' +
    '</div>';
  }).join('');
  box.querySelectorAll('button[data-copy]').forEach(function(btn) {
    btn.addEventListener('click', function() { copyText(btn.dataset.copy || ''); });
  });
}

function renderMcpInfo(mcp) {
  const el = document.getElementById('mcp-info');
  if (!mcp) {
    el.innerHTML = '<p class="setting-hint">MCP 配置不可用</p>';
    return;
  }
  const configJson = JSON.stringify(mcp.config_json || {}, null, 2);
  const tools = (mcp.tools || []).map(function(tool) {
    return '<div class="mcp-tool-row">' +
      '<span class="mcp-tool-name">' + escapeHtml(tool.name) + '</span>' +
      '<span class="mcp-tool-desc">' + escapeHtml(tool.description || '') + '</span>' +
      '</div>';
  }).join('');
  el.innerHTML =
    '<div class="model-row"><span class="label">服务名</span><span class="val on">' + escapeHtml(mcp.name || 'local-vector-db') + '</span></div>' +
    '<div class="model-row"><span class="label">传输</span><span class="val on">' + escapeHtml(mcp.transport || 'stdio') + '</span></div>' +
    '<div class="mcp-command-row">' +
      '<input type="text" id="mcp-command" readonly value="' + escapeHtml(mcp.command || '') + '">' +
      '<button class="copy-btn" data-copy-target="mcp-command">复制</button>' +
    '</div>' +
    '<p class="setting-hint">后端需保持运行：' + escapeHtml(mcp.backend_url || window.api.base) + '</p>' +
    '<div class="mcp-code-head"><span>MCP JSON 配置</span><button class="copy-btn sm" data-copy-target="mcp-config-json">复制</button></div>' +
    '<pre class="mcp-code" id="mcp-config-json">' + escapeHtml(configJson) + '</pre>' +
    '<div class="mcp-tools-head">只读工具</div>' +
    '<div class="mcp-tools">' + tools + '</div>';
  el.querySelectorAll('.copy-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var text = btn.dataset.copyText;
      var targetId = btn.dataset.copyTarget;
      if (!text && targetId) {
        var target = document.getElementById(targetId);
        text = target ? target.value || target.textContent : '';
      }
      copyText(text || '');
    });
  });
}

function formatMcpTime(value) {
  if (!value) return '尚未调用';
  try { return new Date(Number(value) * 1000).toLocaleString('zh-CN'); }
  catch (_) { return String(value); }
}

async function loadMcpRemote() {
  latestMcpRemote = await window.api.fetch('/api/mcp/remote');
  renderMcpRemote(latestMcpRemote);
  return latestMcpRemote;
}

function setMcpModePanel(mode) {
  var basic = mode !== 'advanced';
  document.getElementById('setting-mcp-basic-panel').classList.toggle('hidden', !basic);
  document.getElementById('setting-mcp-advanced-panel').classList.toggle('hidden', basic);
  var badge = document.getElementById('setting-mcp-mode-badge');
  badge.textContent = basic ? '普通模式' : '高级模式';
  badge.classList.toggle('mcp-basic', basic);
  badge.classList.toggle('mcp-advanced', !basic);
}

function bindMcpCopyButtons(container) {
  if (!container) return;
  container.querySelectorAll('[data-copy-text]').forEach(function(btn) {
    btn.addEventListener('click', function() { copyText(btn.dataset.copyText || ''); });
  });
}

async function saveMcpCertificate() {
  try {
    var result = await window.api.saveMcpCA();
    if (result && result.saved) toast('连接证书已保存');
  } catch (e) { toast('保存连接证书失败'); }
}

function renderMcpRemote(data) {
  data = data || {};
  latestMcpRemote = data;
  var urls = data.urls || {};
  var mode = data.mode === 'advanced' ? 'advanced' : 'basic';
  var active = !!data.enabled && !!data.mcp_service_reachable && !!data.https_service_reachable;
  document.getElementById('setting-mcp-enabled').checked = !!data.enabled;
  var modeRadio = document.querySelector('input[name="setting-mcp-mode"][value="' + mode + '"]');
  if (modeRadio) modeRadio.checked = true;
  setMcpModePanel(mode);
  document.getElementById('setting-mcp-admin-password').placeholder = data.admin_password_set
    ? 'OAuth 管理员密码（已设置，留空保留）'
    : 'OAuth 管理员密码（至少 10 位）';

  var info = document.getElementById('mcp-remote-info');
  info.innerHTML = '<div class="mcp-status-grid">' +
    '<div class="model-row"><span class="label">远程访问</span><span class="val ' + (active ? 'on' : 'off') + '">' +
      (!data.enabled ? '○ 已停用' : active ? '● 已就绪' : '○ 服务未就绪') + '</span></div>' +
    '<div class="model-row"><span class="label">当前模式</span><span class="val ' + (data.enabled ? 'on' : 'off') + '">' +
      (mode === 'basic' ? '普通模式' : '高级模式') + '</span></div></div>';

  var basicKey = data.basic_key || {};
  var basicInfo = document.getElementById('mcp-basic-info');
  basicInfo.innerHTML =
    '<div class="mcp-url-block"><div class="mcp-code-head"><span>MCP 地址</span><button class="copy-btn sm" data-copy-text="' + escapeHtml(urls.basic || '') + '">复制地址</button></div>' +
      '<input type="text" readonly value="' + escapeHtml(urls.basic || '') + '"></div>' +
    '<div class="mcp-basic-key-state"><span>连接密钥：</span><strong>' +
      (basicKey.exists ? '已生成 · 尾号 ' + escapeHtml(basicKey.token_suffix || '') : '尚未生成') + '</strong><span>· 最后调用 ' + escapeHtml(formatMcpTime(basicKey.last_used_at)) + '</span></div>' +
    '<p class="setting-hint">远程设备首次连接需要信任连接证书；密钥只在生成时显示一次。</p>';
  bindMcpCopyButtons(basicInfo);
  var basicTokenBtn = document.getElementById('setting-mcp-basic-token');
  basicTokenBtn.textContent = basicKey.exists ? '重新生成连接密钥' : '生成连接密钥';
  document.getElementById('setting-mcp-basic-ca-save').disabled = !data.ca_installed;
  var basicTools = data.tools && Array.isArray(data.tools.basic) ? data.tools.basic : [];
  var basicToolLabels = {
    kb_search: '知识库搜索', kb_get_stats: '知识库统计', kb_list_documents: '文档列表',
    kb_health: '服务状态', memory_search: '记忆搜索', memory_get_context: 'Agent 上下文'
  };
  document.getElementById('setting-mcp-basic-tools').innerHTML = basicTools.map(function(name) {
    return '<span title="' + escapeHtml(name) + '">' + escapeHtml(basicToolLabels[name] || name) + '</span>';
  }).join('');

  var advancedInfo = document.getElementById('mcp-advanced-info');
  advancedInfo.innerHTML =
    '<div class="mcp-status-grid">' +
      '<div class="model-row"><span class="label">MCP 服务</span><span class="val ' + (data.mcp_service_reachable ? 'on' : 'off') + '">' + (data.mcp_service_reachable ? '● 运行中' : '○ 未运行') + '</span></div>' +
      '<div class="model-row"><span class="label">HTTPS 入口</span><span class="val ' + (data.https_service_reachable ? 'on' : 'off') + '">' + (data.https_service_reachable ? '● 运行中' : '○ 未运行') + '</span></div>' +
    '</div>' +
    '<div class="mcp-url-block"><div class="mcp-code-head"><span>知识库级</span><button class="copy-btn sm" data-copy-text="' + escapeHtml(urls.kb || '') + '">复制</button></div>' +
      '<input type="text" readonly value="' + escapeHtml(urls.kb || '') + '"></div>' +
    '<div class="mcp-url-block"><div class="mcp-code-head"><span>完整记忆级</span><button class="copy-btn sm" data-copy-text="' + escapeHtml(urls.full || '') + '">复制</button></div>' +
      '<input type="text" readonly value="' + escapeHtml(urls.full || '') + '"></div>' +
    '<div class="mcp-ca-row"><input type="text" readonly title="SHA-256" value="' + escapeHtml(data.ca_fingerprint || '证书未生成') + '">' +
      '<button class="copy-btn sm" id="setting-mcp-ca-save" ' + (data.ca_installed ? '' : 'disabled') + '>保存 CA</button>' +
      '<button class="copy-btn sm" data-copy-text="' + escapeHtml(urls.ca || '') + '">复制 CA 地址</button></div>' +
    '<p class="setting-hint">MCP ' + escapeHtml(data.protocol || '') + ' · Streamable HTTP · OAuth 2.1 + Bearer 兼容</p>';
  bindMcpCopyButtons(advancedInfo);
  var caBtn = document.getElementById('setting-mcp-ca-save');
  if (caBtn) caBtn.addEventListener('click', saveMcpCertificate);

  var clients = Array.isArray(data.clients) ? data.clients : [];
  var list = document.getElementById('setting-mcp-clients');
  if (!clients.length) {
    list.innerHTML = '<p class="setting-hint">暂无已授权客户端；支持 OAuth 的 Agent 会在首次连接时出现于这里。</p>';
  } else {
    list.innerHTML = clients.map(function(client) {
      var kind = client.kind === 'oauth' ? 'OAuth' : 'Bearer';
      var tier = client.tier === 'full' ? '完整记忆' : '知识库';
      return '<div class="mcp-client-item"><div><div class="mcp-client-title">' +
        '<span>' + escapeHtml(client.label || client.client_id) + '</span><span class="pill">' + kind + '</span><span class="pill">' + tier + '</span></div>' +
        '<div class="mcp-client-meta">' + escapeHtml(client.client_id || '') + (client.token_suffix ? ' · Token 尾号 ' + escapeHtml(client.token_suffix) : '') + ' · 最后调用 ' + escapeHtml(formatMcpTime(client.last_used_at)) + '</div></div>' +
        '<div class="mcp-client-actions">' + (client.kind === 'compat' ? '<button data-mcp-rotate="' + escapeHtml(client.client_id) + '">轮换</button>' : '') +
        '<button data-mcp-revoke="' + escapeHtml(client.client_id) + '">撤销</button></div></div>';
    }).join('');
    list.querySelectorAll('[data-mcp-rotate]').forEach(function(btn) {
      btn.addEventListener('click', function() { rotateMcpClient(btn.dataset.mcpRotate); });
    });
    list.querySelectorAll('[data-mcp-revoke]').forEach(function(btn) {
      btn.addEventListener('click', function() { revokeMcpClient(btn.dataset.mcpRevoke); });
    });
  }
  if (mode === 'advanced') loadMcpAudit();
}

async function loadMcpAudit() {
  var list = document.getElementById('setting-mcp-audit');
  if (!list) return;
  try {
    var data = await window.api.fetch('/api/mcp/audit?limit=30');
    var items = Array.isArray(data.items) ? data.items : [];
    list.innerHTML = items.length ? items.map(function(item) {
      return '<div class="mcp-audit-item"><strong>' + escapeHtml(item.label || item.client_id || '未知客户端') +
        ' · ' + escapeHtml(item.tool_name || '') + ' · ' + (item.success ? '成功' : '失败') + '</strong>' +
        '<p>' + escapeHtml(formatMcpTime(item.created_at)) + (item.source_ip ? ' · ' + escapeHtml(item.source_ip) : '') +
        (item.detail ? ' · ' + escapeHtml(item.detail) : '') + '</p></div>';
    }).join('') : '<p class="setting-hint">暂无调用记录。</p>';
  } catch (e) {
    list.innerHTML = '<p class="setting-hint">审计记录加载失败。</p>';
  }
}

function showMcpTokenOnce(data, targetId) {
  var box = document.getElementById(targetId || 'setting-mcp-token-once');
  var config = JSON.stringify({
    url: data.endpoint || '',
    headers: { Authorization: 'Bearer ' + (data.token || '') },
  }, null, 2);
  box.classList.remove('hidden');
  box.innerHTML = '<div class="mcp-token-warning">该连接密钥只显示这一次，请立即保存到 Agent 配置中。</div>' +
    '<div class="mcp-token-value"><input type="text" readonly value="' + escapeHtml(data.token || '') + '">' +
      '<button class="copy-btn sm" data-copy-token>复制密钥</button><button class="copy-btn sm" data-copy-config>复制完整配置</button></div>' +
    '<p class="setting-hint">MCP 地址：' + escapeHtml(data.endpoint || '') + '</p>';
  box.querySelector('[data-copy-token]').addEventListener('click', function() { copyText(data.token || ''); });
  box.querySelector('[data-copy-config]').addEventListener('click', function() { copyText(config); });
}

async function rotateMcpClient(clientId) {
  if (!confirm('轮换后旧 Token 立即失效，继续？')) return;
  try {
    var data = await window.api.fetch('/api/mcp/clients/' + encodeURIComponent(clientId) + '/rotate', { method: 'POST' });
    showMcpTokenOnce(data);
    await loadMcpRemote();
  } catch (e) { toast('轮换失败：' + (e.message || e)); }
}

async function revokeMcpClient(clientId) {
  if (!confirm('撤销后该客户端的所有 Token 和 OAuth 刷新会话都会立即失效。')) return;
  try {
    await window.api.fetch('/api/mcp/clients/' + encodeURIComponent(clientId), { method: 'DELETE' });
    await loadMcpRemote();
    toast('已撤销 MCP 客户端');
  } catch (e) { toast('撤销失败：' + (e.message || e)); }
}

async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    toast('已复制');
  } catch (e) {
    toast('复制失败');
  }
}

async function doReindex(btn) {
  if (!confirm('将清空并重建全部索引，确认？')) return;
  const spin = document.getElementById('reindex-btn');
  spin.classList.add('spinning');
  if (btn) { btn.disabled = true; btn.textContent = '重建中…'; }
  try {
    const r = await window.api.reindex();
    toast(`重建完成：${r.total_documents} 文档 / ${r.total_chunks} 块`);
    loadStats();
    loadDocuments();
  } catch (e) {
    toast('重建失败');
  } finally {
    spin.classList.remove('spinning');
    if (btn) { btn.disabled = false; btn.textContent = '⟳ 重建全部索引'; }
  }
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), 2600);
}

function escapeHtml(str) {
  // 同时转义引号——textContent→innerHTML 只转义 & < >，不转义 " '，
  // 用于双引号 HTML 属性(如 data-vsrc/title)时会被含引号的文件名突破造成 XSS。
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatSize(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(1) + ' GB';
}

// ===== 标注编辑器 =====
function openAnnotEditor(sourcePath, displayName, fileType, poster) {
  annotEditTarget = sourcePath;
  // 缩略图预览（确认在给正确的文件打标签）；保留 #annot-thumb 容器，只换内容
  const thumbBox = document.getElementById('annot-thumb');
  if (thumbBox) thumbBox.innerHTML = thumbInner(sourcePath, fileType, poster, displayName);
  const existing = annOf(sourcePath) || { tags: [], importance: 0, pinned: false, note: '', caption: '', group: '' };
  // 深拷贝成草稿（取消时不污染缓存）
  annotDraft = {
    tags: [...(existing.tags || [])],
    importance: existing.importance || 0,
    pinned: !!existing.pinned,
    note: existing.note || '',
    caption: existing.caption || '',
    group: existing.group || '',
  };
  renderAnnotGroupSelect(annotDraft.group);
  const tgt = document.getElementById('annot-target');
  tgt.textContent = displayName || sourcePath.split('/').pop();
  tgt.title = sourcePath;
  document.getElementById('annot-pinned').checked = annotDraft.pinned;
  document.getElementById('annot-caption').value = annotDraft.caption;
  document.getElementById('annot-note').value = annotDraft.note;
  document.getElementById('annot-tag-input').value = '';
  document.getElementById('annot-hint').textContent = '';
  renderAnnotStars();
  renderAnnotTagChips();
  document.getElementById('annot-modal').classList.remove('hidden');
  document.getElementById('annot-caption').focus();
}

function closeAnnotEditor() {
  document.getElementById('annot-modal').classList.add('hidden');
  annotEditTarget = null;
  annotDraft = null;
}

function renderAnnotStars() {
  const row = document.getElementById('annot-stars');
  let html = '';
  for (let i = 1; i <= 5; i++) {
    html += `<span class="star ${i <= annotDraft.importance ? 'on' : ''}" data-v="${i}">★</span>`;
  }
  html += `<span class="star-clear" data-v="0" title="清除">✕</span>`;
  row.innerHTML = html;
  row.querySelectorAll('[data-v]').forEach((s) =>
    s.addEventListener('click', () => {
      const v = parseInt(s.dataset.v, 10);
      // 再次点击当前星级 → 归零（便捷清除）
      annotDraft.importance = v === annotDraft.importance ? 0 : v;
      renderAnnotStars();
    })
  );
}

function renderAnnotTagChips() {
  const box = document.getElementById('annot-tag-chips');
  box.innerHTML = annotDraft.tags
    .map((t, i) => `<span class="tag-chip">${escapeHtml(t)}<button class="tag-x" data-i="${i}" title="移除">✕</button></span>`)
    .join('');
  box.querySelectorAll('.tag-x').forEach((b) =>
    b.addEventListener('click', () => {
      annotDraft.tags.splice(parseInt(b.dataset.i, 10), 1);
      renderAnnotTagChips();
    })
  );
}

function addAnnotTagFromInput() {
  const inp = document.getElementById('annot-tag-input');
  const raw = inp.value.trim().replace(/[,，]$/, '').trim();
  if (!raw) { inp.value = ''; return; }
  // 支持一次粘贴多个（逗号分隔）
  raw.split(/[,，]/).map((t) => t.trim()).filter(Boolean).forEach((t) => {
    if (!annotDraft.tags.includes(t) && annotDraft.tags.length < 32) annotDraft.tags.push(t);
  });
  inp.value = '';
  renderAnnotTagChips();
}

async function saveAnnotEditor() {
  if (!annotEditTarget) return;
  addAnnotTagFromInput(); // 容错：未回车的残留输入也收进去
  const saveBtn = document.getElementById('annot-save');
  const hint = document.getElementById('annot-hint');
  const patch = {
    tags: annotDraft.tags,
    importance: annotDraft.importance,
    pinned: document.getElementById('annot-pinned').checked,
    caption: document.getElementById('annot-caption').value.trim(),
    note: document.getElementById('annot-note').value,
    group: document.getElementById('annot-group').value || '',
  };
  saveBtn.disabled = true;
  saveBtn.textContent = '保存中…';
  try {
    const res = await window.api.setAnnotation(annotEditTarget, patch);
    annotationsCache[annotEditTarget] = res.annotation;
    // 全默认（清空了所有字段）→ 后端删了该 key，前端缓存同步移除
    const a = res.annotation;
    if (a && !a.tags.length && !a.importance && !a.pinned && !a.note && !a.caption && !a.group) {
      delete annotationsCache[annotEditTarget];
    }
    if (res.reindex_queued) {
      hint.textContent = '✅ 已保存。说明已变更，正在后台重新索引（稍后即可被搜到）…';
      // caption 改了：后台重索引，稍等再刷新文档/统计
      setTimeout(() => { loadAnnotations(); loadStats(); }, 4000);
    }
    toast('标注已保存');
    closeAnnotEditor();
    loadDocuments();
    renderTagFilterBar();
    await loadGroups();
    if (currentView === 'files') renderFileCenter();
  } catch (e) {
    hint.textContent = '保存失败：' + (e.message || e);
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = '保存';
  }
}

async function deleteAnnotFromEditor() {
  if (!annotEditTarget) return;
  if (!confirm('清除此文件的全部标注（标签/重要度/置顶/说明/备注）？')) return;
  try {
    const res = await window.api.deleteAnnotation(annotEditTarget);
    delete annotationsCache[annotEditTarget];
    if (res.reindex_queued) setTimeout(() => { loadAnnotations(); loadStats(); }, 4000);
    toast('标注已清除');
    closeAnnotEditor();
    loadDocuments();
    renderTagFilterBar();
    await loadGroups();
    if (currentView === 'files') renderFileCenter();
  } catch (e) {
    toast('清除失败');
  }
}

// ===== 通用输入弹窗（替代 Electron 不支持的 window.prompt）=====
let _promptResolve = null;
function promptModal(title, value = '') {
  return new Promise((resolve) => {
    _promptResolve = resolve;
    document.getElementById('prompt-title').textContent = title;
    const inp = document.getElementById('prompt-input');
    inp.value = value;
    document.getElementById('prompt-modal').classList.remove('hidden');
    setTimeout(() => { inp.focus(); inp.select(); }, 0);
  });
}
function _resolvePrompt(val) {
  if (document.getElementById('prompt-modal').classList.contains('hidden')) return;
  document.getElementById('prompt-modal').classList.add('hidden');
  const r = _promptResolve; _promptResolve = null;
  if (r) r(val);
}

// ===== 分组缓存 + 编辑器分组下拉 =====
async function loadGroups() {
  try {
    const d = await window.api.getGroups();
    groupsCache = (d && d.groups) || [];
  } catch (e) { groupsCache = []; }
}

function renderAnnotGroupSelect(current) {
  const sel = document.getElementById('annot-group');
  if (!sel) return;
  const names = groupsCache.map((g) => g.name);
  if (current && !names.includes(current)) names.unshift(current);
  sel.innerHTML = `<option value="">（未分组）</option>` +
    names.map((n) => `<option value="${escapeHtml(n)}"${n === current ? ' selected' : ''}>${escapeHtml(n)}</option>`).join('');
}

async function promptNewGroupForEditor() {
  const name = ((await promptModal('新建分组名称')) || '').trim();
  if (!name) return;
  await window.api.createGroup(name);
  await loadGroups();
  if (annotDraft) annotDraft.group = name;
  renderAnnotGroupSelect(name);
}

// ===== 视图切换 =====
function switchView(view) {
  currentView = view;
  document.querySelectorAll('#view-tabs .view-tab').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === view));
  const isFiles = view === 'files';
  const isWiki = view === 'wiki';
  const isMemory = view === 'memory';
  const isAgentMemory = view === 'agent-memory';
  const isSettings = view === 'settings';
  const isFullPage = isFiles || isWiki || isMemory || isAgentMemory || isSettings;
  document.querySelector('main > aside').classList.toggle('hidden', isFullPage);
  document.getElementById('search-area').classList.toggle('hidden', isFullPage);
  document.getElementById('file-center').classList.toggle('hidden', !isFiles);
  document.getElementById('wiki-panel').classList.toggle('hidden', !isWiki);
  document.getElementById('memory-panel').classList.toggle('hidden', !isMemory);
  document.getElementById('agent-memory-panel').classList.toggle('hidden', !isAgentMemory);
  document.getElementById('settings-panel').classList.toggle('hidden', !isSettings);
  if (isFiles) loadFileCenter();
  if (isWiki) loadWikiPanel();
  if (isMemory) loadMemoryPanel();
  if (isAgentMemory) loadAgentMemoryPanel();
  if (isSettings) loadSettingsPage();
}

// ===== Wiki 面板 =====
async function loadWikiPanel() {
  await Promise.allSettled([loadWikiPages(), refreshWikiJobs(), loadWikiEngineStatus()]);
  if (wikiState.currentPath) {
    await selectWikiPage(wikiState.currentPath);
  } else if (wikiState.pages.length) {
    await selectWikiPage(wikiState.pages[0].path);
  }
}

async function loadWikiPages() {
  var params = new URLSearchParams({ limit: '300', offset: '0' });
  if (wikiState.folder) params.set('folder', wikiState.folder);
  if (wikiState.query) params.set('q', wikiState.query);
  try {
    var data = await window.api.fetch('/api/wiki/pages?' + params.toString());
    wikiState.pages = (data && data.items) || [];
  } catch (e) {
    wikiState.pages = [];
  }
  renderWikiPageList();
}

function renderWikiPageList() {
  var list = document.getElementById('wiki-page-list');
  if (!wikiState.pages.length) {
    list.innerHTML = '<div class="wiki-empty">暂无 Wiki 页面</div>';
    return;
  }
  list.innerHTML = wikiState.pages.map(function(p) {
    var tags = (p.tags || []).slice(0, 3).map(function(t) { return '#' + t; }).join(' ');
    return '<div class="wiki-page-item' + (p.path === wikiState.currentPath ? ' active' : '') + '" data-path="' + escapeHtml(p.path) + '">' +
      '<div class="wiki-page-title">' + escapeHtml(p.title || p.path) + '</div>' +
      '<div class="wiki-page-sub"><span>' + escapeHtml(p.folder || 'Root') + '</span><span>' + escapeHtml(p.maturity || '') + '</span><span>' + escapeHtml(tags) + '</span></div>' +
      '</div>';
  }).join('');
  list.querySelectorAll('.wiki-page-item').forEach(function(item) {
    item.addEventListener('click', function() { selectWikiPage(item.dataset.path); });
  });
}

async function selectWikiPage(path) {
  if (!path) return;
  try {
    var page = await window.api.fetch('/api/wiki/pages/' + path.split('/').map(encodeURIComponent).join('/'));
    wikiState.currentPath = page.path;
    wikiState.currentPage = page;
    document.getElementById('wiki-title').textContent = page.title || page.path;
    document.getElementById('wiki-meta').textContent = (page.folder || 'Root') + ' · ' + (page.maturity || 'seedling') + ' · ' + formatSize(page.size || 0);
    document.getElementById('wiki-editor-text').value = page.content || '';
    renderWikiPageList();
    renderWikiLinks(page);
    await renderWikiGraph(page.path);
  } catch (e) {
    toast('Wiki 页面加载失败');
  }
}

function wikiPageUrl(path) {
  return '/api/wiki/pages/' + path.split('/').map(encodeURIComponent).join('/');
}

function renderWikiLinks(page) {
  var inbound = page.inbound || [];
  var outbound = page.outbound || [];
  var back = document.getElementById('wiki-backlinks');
  var out = document.getElementById('wiki-outlinks');
  back.innerHTML = inbound.length
    ? inbound.map(function(p) { return '<div class="wiki-link-item" data-path="' + escapeHtml(p) + '">' + escapeHtml(p) + '</div>'; }).join('')
    : '<div class="wiki-empty">暂无反链</div>';
  out.innerHTML = outbound.length
    ? outbound.map(function(l) {
        var target = l.target_path || '';
        return '<div class="wiki-link-item" data-path="' + escapeHtml(target) + '">' + escapeHtml(l.target_title || target || '未创建页面') + '</div>';
      }).join('')
    : '<div class="wiki-empty">暂无出链</div>';
  document.querySelectorAll('#wiki-backlinks .wiki-link-item[data-path], #wiki-outlinks .wiki-link-item[data-path]').forEach(function(el) {
    el.addEventListener('click', function() {
      if (el.dataset.path) selectWikiPage(el.dataset.path);
    });
  });
}

async function renderWikiGraph(path) {
  var box = document.getElementById('wiki-graph');
  try {
    var data = await window.api.fetch('/api/wiki/graph?path=' + encodeURIComponent(path || ''));
    var nodes = data.nodes || [];
    var edges = data.edges || [];
    if (!nodes.length) {
      box.innerHTML = '<div class="wiki-empty">暂无图谱</div>';
      return;
    }
    var cx = 150, cy = 90, r = 62;
    var pos = {};
    nodes.forEach(function(n, i) {
      var angle = nodes.length === 1 ? 0 : (Math.PI * 2 * i) / nodes.length - Math.PI / 2;
      pos[n.id] = {
        x: nodes.length === 1 ? cx : cx + Math.cos(angle) * r,
        y: nodes.length === 1 ? cy : cy + Math.sin(angle) * r,
      };
    });
    var edgeSvg = edges.map(function(e) {
      var a = pos[e.source], b = pos[e.target];
      if (!a || !b) return '';
      return '<line class="wiki-edge" x1="' + a.x + '" y1="' + a.y + '" x2="' + b.x + '" y2="' + b.y + '"/>';
    }).join('');
    var nodeSvg = nodes.map(function(n) {
      var p = pos[n.id];
      var label = (n.title || n.id).slice(0, 10);
      return '<g data-path="' + escapeHtml(n.id) + '">' +
        '<circle class="wiki-node ' + escapeHtml(n.type || '') + '" cx="' + p.x + '" cy="' + p.y + '" r="' + (n.id === path ? 11 : 8) + '"/>' +
        '<text class="wiki-node-label" x="' + p.x + '" y="' + (p.y + 24) + '">' + escapeHtml(label) + '</text>' +
        '</g>';
    }).join('');
    box.innerHTML = '<svg viewBox="0 0 300 180" role="img">' + edgeSvg + nodeSvg + '</svg>';
    box.querySelectorAll('g[data-path]').forEach(function(g) {
      g.addEventListener('click', function() { selectWikiPage(g.dataset.path); });
    });
  } catch (e) {
    box.innerHTML = '<div class="wiki-empty">图谱加载失败</div>';
  }
}

async function searchWiki() {
  var q = document.getElementById('wiki-search-input').value.trim();
  wikiState.query = q;
  if (!q) {
    await loadWikiPages();
    return;
  }
  try {
    var data = await window.api.fetch('/api/wiki/search', {
      method: 'POST',
      body: { query: q, n_results: 30 },
    });
    wikiState.pages = ((data && data.results) || []).map(function(r) {
      return {
        path: r.page_path,
        title: r.title,
        folder: r.folder,
        type: r.wiki_type,
        maturity: '相关度 ' + (r.score || 0).toFixed(2),
        tags: [],
      };
    });
    renderWikiPageList();
  } catch (e) {
    toast('Wiki 搜索失败');
  }
}

async function saveWikiPage() {
  if (!wikiState.currentPath) return;
  var btn = document.getElementById('wiki-save');
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    var content = document.getElementById('wiki-editor-text').value;
    var data = await window.api.fetch(wikiPageUrl(wikiState.currentPath), {
      method: 'PUT',
      body: { content: content, source_agent: 'vector-db-ui' },
    });
    wikiState.currentPage = data.page;
    toast(data.page && data.page.gbrain_sync && data.page.gbrain_sync.success
      ? 'Wiki 已保存，智能索引已更新'
      : 'Wiki 已保存，智能索引将自动重试');
    await loadWikiPages();
    await selectWikiPage(wikiState.currentPath);
  } catch (e) {
    toast('Wiki 保存失败');
  }
  btn.disabled = false;
  btn.textContent = '保存';
}

async function createWikiPage() {
  var title = ((await promptModal('新建 Wiki 页面标题')) || '').trim();
  if (!title) return;
  var folder = wikiState.folder && wikiState.folder !== 'Sources' ? wikiState.folder : 'Resources';
  try {
    var data = await window.api.fetch('/api/wiki/pages', {
      method: 'POST',
      body: { title: title, folder: folder, content: '# ' + title + '\n\n', tags: [] },
    });
    await loadWikiPages();
    await selectWikiPage(data.page.path);
  } catch (e) {
    toast('创建 Wiki 页面失败');
  }
}

async function maintainWiki() {
  var btn = document.getElementById('wiki-maintain');
  btn.disabled = true;
  btn.textContent = '维护中…';
  try {
    await window.api.fetch('/api/wiki/maintenance', { method: 'POST' });
    toast('Wiki 维护完成');
    await loadWikiPanel();
  } catch (e) {
    toast('Wiki 维护失败');
  }
  btn.disabled = false;
  btn.textContent = '维护';
}

async function organizeSelectedWikiSource() {
  var page = wikiState.currentPage;
  if (!page || !page.source_path) {
    toast('当前页面没有关联原始资料');
    return;
  }
  try {
    await window.api.fetch('/api/wiki/organize', {
      method: 'POST',
      body: { source_path: page.source_path, force: true },
    });
    toast('已提交 Wiki 重新整理');
    refreshWikiJobs();
  } catch (e) {
    toast('提交整理失败');
  }
}

async function refreshWikiJobs() {
  try {
    var data = await window.api.fetch('/api/wiki/jobs');
    var jobs = (data && data.jobs) || [];
    var box = document.getElementById('wiki-jobs');
    if (!box) return;
    if (!jobs.length) {
      box.innerHTML = '<div class="wiki-empty">暂无任务</div>';
      return;
    }
    box.innerHTML = jobs.slice(0, 8).map(function(j) {
      return '<div class="wiki-job"><span title="' + escapeHtml(j.source_path || '') + '">' + escapeHtml(j.name || j.kind || '') + '</span><span class="state">' + escapeHtml(j.state || '') + '</span></div>';
    }).join('');
  } catch (e) {}
}

// ===== Wiki 本地智能索引（GBrain 派生能力） =====
function setWikiEngineRuntime(element, ready, failed) {
  if (!element) return;
  element.classList.toggle('ready', !!ready);
  element.classList.toggle('error', !!failed);
}

function renderWikiEngineStatus(status, error) {
  var ready = !!(status && status.ready);
  var failed = !!error || !!(status && (!status.available || !status.ready));
  var stats = (status && status.stats) || {};
  var model = status && status.embedding_model
    ? String(status.embedding_model).replace(/^ollama:/, '')
    : '—';
  var dimensions = status && status.embedding_dimensions
    ? ' · ' + status.embedding_dimensions + ' 维'
    : '';
  var engine = status && status.engine === 'pglite'
    ? 'PGLite（本地）'
    : ((status && status.engine) || '—');
  var runtimeText = ready ? '本地智能检索已就绪' : (error || (status && status.error) || '智能检索不可用');

  var wikiRuntime = document.getElementById('wiki-engine-runtime');
  setWikiEngineRuntime(wikiRuntime, ready, failed);
  document.getElementById('wiki-engine-status-text').textContent = runtimeText;

  var settingRuntime = document.getElementById('setting-wiki-engine-runtime');
  setWikiEngineRuntime(settingRuntime, ready, failed);
  document.getElementById('setting-wiki-index-status').textContent = ready ? '运行正常' : runtimeText;
  document.getElementById('setting-wiki-index-model').textContent = model + dimensions;
  document.getElementById('setting-wiki-index-engine').textContent = engine;
  document.getElementById('setting-wiki-index-pages').textContent = Number(stats.page_count || 0).toLocaleString('zh-CN');
  document.getElementById('setting-wiki-index-vectors').textContent =
    Number(stats.embedded_count || 0).toLocaleString('zh-CN') + ' / ' +
    Number(stats.chunk_count || 0).toLocaleString('zh-CN');
  document.getElementById('setting-wiki-index-msg').textContent = ready
    ? 'Wiki 是唯一内容源；GBrain 只维护可重建的本地向量与关系索引。'
    : runtimeText;
}

async function loadWikiEngineStatus() {
  try {
    var status = await window.api.fetch('/api/gbrain/status');
    wikiEngineStatus = status;
    renderWikiEngineStatus(status, '');
    return status;
  } catch (error) {
    wikiEngineStatus = null;
    renderWikiEngineStatus(null, error.message || '本地智能索引不可用');
    return null;
  }
}

async function reconcileWikiIndex() {
  var button = document.getElementById('setting-wiki-index-reconcile');
  var message = document.getElementById('setting-wiki-index-msg');
  button.disabled = true;
  button.textContent = '正在对账与向量化…';
  message.textContent = '正在根据 Wiki 文件校验并重建派生索引…';
  try {
    var data = await window.api.fetch('/api/gbrain/sync-wiki', { method: 'POST' });
    var result = (((data || {}).sync || {}).gbrain || {}).result || {};
    var summary = 'Wiki 智能索引对账完成：' + Number(data.pages_indexed || 0) +
      ' 页，更新 ' + Number(result.imported || 0) + ' 页';
    message.textContent = summary;
    toast(summary);
    await Promise.allSettled([loadWikiEngineStatus(), loadWikiPages()]);
  } catch (error) {
    message.textContent = '对账失败：' + (error.message || error);
    toast('Wiki 智能索引对账失败');
  } finally {
    button.disabled = false;
    button.textContent = '对账并重建 Wiki 索引';
  }
}

// ===== 文件中心 =====
const FC_CATEGORIES = [
  { kind: 'all', label: '📁 全部' },
  { kind: 'type', value: 'image', label: '🖼️ 图片' },
  { kind: 'type', value: 'video', label: '🎬 视频' },
  { kind: 'type', value: 'audio', label: '🎙 音频' },
  { kind: 'type', value: 'text', label: '📄 文档' },
  { kind: 'special', value: 'important', label: '⭐ 重要' },
  { kind: 'special', value: 'pinned', label: '📌 置顶' },
  { kind: 'special', value: 'ungrouped', label: '🗂 未分组' },
  { kind: 'duplicates', label: '⧉ 重复文件' },
  { kind: 'status', value: 'unindexed', label: '◌ 未索引' },
  { kind: 'status', value: 'failed', label: '⚠ 索引失败' },
  { kind: 'status', value: 'missing', label: '？磁盘丢失' },
];

function fcQueryOptions(withPage = true) {
  const options = {
    q: fcSearchText,
    sort_by: fcSortKey === 'mtime' ? 'modified' : fcSortKey,
    sort_dir: fcSortDir,
  };
  if (withPage) Object.assign(options, { limit: FC_PAGE_SIZE, offset: fcOffset });
  if (fcFilter.kind === 'type') options.file_type = fcFilter.value;
  else if (fcFilter.kind === 'status') options.status = fcFilter.value;
  else if (fcFilter.kind === 'group') options.group = fcFilter.value;
  else if (fcFilter.kind === 'tag') options.tag = fcFilter.value;
  else if (fcFilter.kind === 'duplicates') options.duplicates = true;
  else if (fcFilter.kind === 'special') options.special = fcFilter.value;
  return options;
}

async function loadFileCenter({ reset = false } = {}) {
  if (reset) fcOffset = 0;
  const grid = document.getElementById('fc-grid');
  if (grid) grid.classList.add('loading');
  try {
    const [data] = await Promise.all([window.api.listDocuments(fcQueryOptions()), loadGroups()]);
    fcDocs = (data && data.items) || [];
    fcTotal = Number(data && data.total) || 0;
    fcFacets = (data && data.facets) || fcFacets;
    fcDocs.forEach((item) => {
      if (item.annotation && !annotationIsEmpty(item.annotation)) annotationsCache[item.id] = item.annotation;
      else delete annotationsCache[item.id];
    });
  } catch (e) {
    fcDocs = [];
    fcTotal = 0;
    toast('文件中心加载失败');
  }
  if (grid) grid.classList.remove('loading');
  renderFileCenter();
}

function fcCategoryCount(category) {
  if (category.kind === 'all') return fcFacets.total || 0;
  if (category.kind === 'type') return (fcFacets.types || {})[category.value] || 0;
  if (category.kind === 'status') return (fcFacets.statuses || {})[category.value] || 0;
  if (category.kind === 'duplicates') return fcFacets.duplicates || 0;
  if (category.kind === 'special') return fcFacets[category.value] || 0;
  return 0;
}

function fcFilterLabel() {
  if (fcFilter.kind === 'group') return `分组 · ${fcFilter.value}`;
  if (fcFilter.kind === 'tag') return `标签 · ${fcFilter.value}`;
  const category = FC_CATEGORIES.find((item) => item.kind === fcFilter.kind && (item.value || '') === (fcFilter.value || ''));
  return category ? category.label.replace(/^\S+\s*/, '') : '全部文件';
}

function renderFcNav() {
  const nav = document.getElementById('fc-nav');
  const categoryHtml = FC_CATEGORIES.map((category) => {
    const active = fcFilter.kind === category.kind && (fcFilter.value || '') === (category.value || '');
    return `<button class="fc-nav-item${active ? ' active' : ''}" data-kind="${category.kind}" data-value="${escapeHtml(category.value || '')}">
      <span>${category.label}</span><span class="fc-nav-count">${fcCategoryCount(category)}</span></button>`;
  }).join('');
  const savedHtml = fcSavedViews.map((view, index) =>
    `<button class="fc-nav-item fc-saved-view" data-saved-index="${index}"><span>⌁ ${escapeHtml(view.name)}</span><span class="fc-saved-del" data-saved-del="${index}" title="删除">✕</span></button>`
  ).join('');
  const tagHtml = Object.entries(fcFacets.tags || {}).sort((a, b) => b[1] - a[1]).slice(0, 20).map(([tag, count]) => {
    const active = fcFilter.kind === 'tag' && fcFilter.value === tag;
    return `<button class="fc-nav-item${active ? ' active' : ''}" data-kind="tag" data-value="${escapeHtml(tag)}"><span>🏷 ${escapeHtml(tag)}</span><span class="fc-nav-count">${count}</span></button>`;
  }).join('');
  const groupHtml = groupsCache.map((group) => {
    const active = fcFilter.kind === 'group' && fcFilter.value === group.name;
    return `<button class="fc-nav-item fc-group-drop${active ? ' active' : ''}" data-kind="group" data-value="${escapeHtml(group.name)}">
      <span>🗂 ${escapeHtml(group.name)}</span><span class="fc-nav-count">${(fcFacets.groups || {})[group.name] || group.count || 0}</span>
      <span class="fc-group-actions"><span class="fc-group-rename" data-rename="${escapeHtml(group.name)}" title="重命名">✎</span><span class="fc-group-del" data-del="${escapeHtml(group.name)}" title="删除">✕</span></span></button>`;
  }).join('');
  nav.innerHTML = `<div class="fc-nav-sec">分类</div>${categoryHtml}` +
    `<div class="fc-nav-sec">标签</div>${tagHtml || '<div class="fc-nav-empty">暂无标签</div>'}` +
    `<div class="fc-nav-sec">智能视图</div>${savedHtml || '<div class="fc-nav-empty">可保存常用筛选</div>'}` +
    `<div class="fc-nav-sec">我的分组 <button id="fc-new-group" class="fc-new-group" title="新建分组">＋</button></div>` +
    (groupHtml || '<div class="fc-nav-empty">暂无分组</div>');

  nav.querySelectorAll('.fc-nav-item[data-kind]').forEach((button) => button.addEventListener('click', (event) => {
    if (event.target.closest('.fc-group-actions')) return;
    fcFilter = { kind: button.dataset.kind, value: button.dataset.value };
    loadFileCenter({ reset: true });
  }));
  nav.querySelectorAll('[data-saved-index]').forEach((button) => button.addEventListener('click', (event) => {
    if (event.target.closest('[data-saved-del]')) return;
    const view = fcSavedViews[Number(button.dataset.savedIndex)];
    if (!view) return;
    fcFilter = view.filter || { kind: 'all', value: '' };
    fcSearchText = view.query || '';
    fcSortKey = view.sortKey || 'mtime';
    fcSortDir = view.sortDir || 'desc';
    document.getElementById('fc-search').value = fcSearchText;
    document.getElementById('fc-sort-key').value = fcSortKey;
    document.getElementById('fc-sort-dir').textContent = fcSortDir === 'asc' ? '↑' : '↓';
    loadFileCenter({ reset: true });
  }));
  nav.querySelectorAll('[data-saved-del]').forEach((button) => button.addEventListener('click', (event) => {
    event.stopPropagation();
    fcSavedViews.splice(Number(button.dataset.savedDel), 1);
    localStorage.setItem('centaur-fc-saved-views', JSON.stringify(fcSavedViews));
    renderFcNav();
  }));
  nav.querySelectorAll('.fc-group-rename').forEach((button) => button.addEventListener('click', (event) => { event.stopPropagation(); fcRenameGroup(button.dataset.rename); }));
  nav.querySelectorAll('.fc-group-del').forEach((button) => button.addEventListener('click', (event) => { event.stopPropagation(); fcDeleteGroup(button.dataset.del); }));
  nav.querySelectorAll('.fc-group-drop').forEach((button) => {
    button.addEventListener('dragover', (event) => { event.preventDefault(); button.classList.add('drag-over'); });
    button.addEventListener('dragleave', () => button.classList.remove('drag-over'));
    button.addEventListener('drop', async (event) => {
      event.preventDefault(); button.classList.remove('drag-over');
      const id = event.dataTransfer.getData('text/plain');
      const ids = fcSelected.has(id) ? [...fcSelected] : [id];
      if (!id) return;
      const response = await window.api.setAnnotationsBatch(ids, { group: button.dataset.value });
      toast(`已移入「${button.dataset.value}」`);
      if (response.audit_id) showFcUndo('分组已更新', async () => {
        await window.api.undoAudit(response.audit_id);
        await loadFileCenter();
      });
      loadFileCenter();
    });
  });
  document.getElementById('fc-new-group')?.addEventListener('click', fcCreateGroup);
}

function fcDisplayName(it) {
  return (it.metadata && it.metadata.file_name) || it.id.split('/').pop();
}

function fcTypeLabel(it) {
  const name = fcDisplayName(it);
  const fileType = it.metadata && it.metadata.file_type;
  if (fileType === 'image') return '图片';
  if (fileType === 'video') return '视频';
  const ext = (name.split('.').pop() || '').toUpperCase();
  return ext && ext !== name.toUpperCase() ? ext : '文档';
}

function formatFileDate(value) {
  const timestamp = Number(value);
  if (!timestamp) return '时间未知';
  const date = new Date(timestamp * 1000);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

function getVisibleFcDocs() {
  return fcDocs;
}

function renderFcViewSwitch() {
  if (!['grid', 'list', 'masonry'].includes(fcViewMode)) fcViewMode = 'grid';
  document.querySelectorAll('[data-fc-view]').forEach((button) => {
    const active = button.dataset.fcView === fcViewMode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}

function setFcViewMode(mode) {
  if (!['grid', 'list', 'masonry'].includes(mode)) return;
  fcViewMode = mode;
  localStorage.setItem('centaur-fc-view', mode);
  renderFcViewSwitch();
  renderFcGrid();
}

function renderFcGrid() {
  const grid = document.getElementById('fc-grid');
  const items = getVisibleFcDocs();
  grid.dataset.view = fcViewMode;
  grid.classList.toggle('batch-mode', fcBatchMode);
  document.getElementById('fc-count').textContent = fcTotal;
  document.getElementById('fc-title-context').textContent = fcFilterLabel();
  if (!items.length) {
    grid.innerHTML = `<div class="fc-empty"><strong>${fcSearchText ? '没有匹配的文件' : '该分类下暂无文件'}</strong><span>${fcSearchText ? '可搜索名称、标签、备注或说明' : '可从左侧切换分类、状态或分组'}</span></div>`;
    renderFcPagination();
    updateBatchBar();
    return;
  }
  grid.innerHTML = items.map((it) => {
    const metadata = it.metadata || {};
    const name = fcDisplayName(it);
    const ft = metadata.file_type;
    const ann = annOf(it.id);
    const grp = ann && ann.group ? `<span class="fc-card-group">🗂 ${escapeHtml(ann.group)}</span>` : '';
    const checked = fcSelected.has(it.id) ? ' checked' : '';
    const selected = fcSelected.has(it.id) ? ' selected' : '';
    const hasPreview = ft === 'image' || (ft === 'video' && it.poster);
    const cb = fcBatchMode ? `<input type="checkbox" class="fc-cb" data-id="${encodeURIComponent(it.id)}" aria-label="选择 ${escapeHtml(name)}"${checked}>` : '';
    const chunkLabel = ft === 'video' ? '段' : '块';
    const statusLabels = { indexed: '已索引', unindexed: '未索引', queued: '排队中', processing: '索引中', failed: '索引失败', missing: '磁盘丢失' };
    const jobError = it.job && it.job.error ? ` title="${escapeHtml(it.job.error)}"` : '';
    const status = `<span class="fc-status status-${escapeHtml(it.status || 'indexed')}"${jobError}>${statusLabels[it.status] || it.status || '已索引'}</span>`;
    const duplicate = it.duplicate_count > 1 ? `<span class="fc-duplicate">⧉ ${it.duplicate_count} 份</span>` : '';
    const details = [
      fcTypeLabel(it),
      formatSize(metadata.file_size),
      `${it.chunk_count || metadata.chunk_count || 1} ${chunkLabel}`,
      formatFileDate(metadata.modified_time),
    ];
    return `<article class="fc-card${selected}${hasPreview ? ' has-preview' : ''}" data-id="${encodeURIComponent(it.id)}" data-name="${escapeHtml(name)}" data-ft="${escapeHtml(ft || '')}" data-poster="${it.poster ? encodeURIComponent(it.poster) : ''}" title="${escapeHtml(name)}" role="button" tabindex="0" draggable="true">
      ${cb}
      ${thumbHtml(it.id, ft, it.poster, name, 'fc-thumb')}
      <div class="fc-card-body">
        <div class="fc-card-name">${escapeHtml(name)}</div>
        <div class="fc-card-details">${details.map((detail) => `<span>${escapeHtml(detail)}</span>`).join('')}</div>
        <div class="fc-card-meta">${status}${duplicate}${annotBadges(ann)}${grp}</div>
      </div>
      <div class="fc-card-actions">
        <button class="fc-card-tag" type="button" title="标注和分组" aria-label="标注和分组">🏷</button>
        <button class="fc-card-del" type="button" title="删除文件" aria-label="删除文件">🗑</button>
      </div>
    </article>`;
  }).join('');
  grid.querySelectorAll('.fc-card').forEach((card) => {
    const id = decodeURIComponent(card.dataset.id);
    const activate = (event = {}) => {
      if (fcBatchMode) {
        toggleFcSelect(id, Boolean(event.shiftKey));
        renderFcGrid();
        return;
      }
      openAnnotEditor(id, card.dataset.name, card.dataset.ft,
        card.dataset.poster ? decodeURIComponent(card.dataset.poster) : '');
    };
    card.addEventListener('click', (e) => {
      if (fcBatchMode) {
        if (e.target.classList.contains('fc-cb')) return; // handled below
        activate(e);
        return;
      }
      if (e.target.closest('.fc-card-actions')) return;
      activate(e);
    });
    card.addEventListener('dragstart', (event) => {
      event.dataTransfer.setData('text/plain', id);
      event.dataTransfer.effectAllowed = 'move';
    });
    card.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      showFcContextMenu(event.clientX, event.clientY, fcDocs.find((item) => item.id === id));
    });
    card.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      if (e.target !== card) return;
      e.preventDefault();
      activate();
    });
  });
  // Checkbox events
  grid.querySelectorAll('.fc-cb').forEach((cb) => {
    cb.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleFcSelect(decodeURIComponent(cb.dataset.id), e.shiftKey);
      renderFcGrid();
    });
  });
  grid.querySelectorAll('.fc-card-tag').forEach((button) => {
    button.addEventListener('click', (e) => {
      e.stopPropagation();
      const card = button.closest('.fc-card');
      openAnnotEditor(decodeURIComponent(card.dataset.id), card.dataset.name, card.dataset.ft,
        card.dataset.poster ? decodeURIComponent(card.dataset.poster) : '');
    });
  });
  grid.querySelectorAll('.fc-card-del').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteDoc(decodeURIComponent(btn.closest('.fc-card').dataset.id));
    });
  });
  updateBatchBar();
  renderFcPagination();
}

function renderFileCenter() {
  renderFcNav();
  renderFcViewSwitch();
  renderFcGrid();
}

// ===== 批量管理 =====
function toggleFcSelect(id, range = false) {
  if (range && fcLastSelectedId) {
    const ids = fcDocs.map((item) => item.id);
    const from = ids.indexOf(fcLastSelectedId), to = ids.indexOf(id);
    if (from >= 0 && to >= 0) {
      ids.slice(Math.min(from, to), Math.max(from, to) + 1).forEach((value) => fcSelected.add(value));
    }
  } else if (fcSelected.has(id)) fcSelected.delete(id);
  else fcSelected.add(id);
  fcLastSelectedId = id;
}

function renderFcPagination() {
  const page = Math.floor(fcOffset / FC_PAGE_SIZE) + 1;
  const pages = Math.max(1, Math.ceil(fcTotal / FC_PAGE_SIZE));
  document.getElementById('fc-page-info').textContent = `第 ${page} / ${pages} 页 · 共 ${fcTotal} 个`;
  document.getElementById('fc-prev').disabled = fcOffset <= 0;
  document.getElementById('fc-next').disabled = fcOffset + FC_PAGE_SIZE >= fcTotal;
}

function updateBatchBar() {
  const bar = document.getElementById('fc-batch-bar');
  const count = document.getElementById('fc-selected-count');
  const allBtn = document.getElementById('fc-select-all');
  const clearBtn = document.getElementById('fc-clear-selection');
  const editBtn = document.getElementById('fc-batch-edit');
  const reindexBtn = document.getElementById('fc-batch-reindex');
  const deleteBtn = document.getElementById('fc-batch-delete');
  bar.classList.toggle('hidden', !fcBatchMode);
  if (!fcBatchMode) return;
  const visible = getVisibleFcDocs();
  const selectedVisible = visible.filter((it) => fcSelected.has(it.id)).length;
  const allVisibleSelected = visible.length > 0 && selectedVisible === visible.length;
  count.textContent = `已选 ${fcSelected.size} 个${fcSelected.size > selectedVisible ? `（本页 ${selectedVisible} 个）` : ''}`;
  allBtn.textContent = allVisibleSelected ? '取消本页全选' : '选择本页';
  allBtn.disabled = !visible.length;
  clearBtn.disabled = !fcSelected.size;
  editBtn.disabled = !fcSelected.size;
  reindexBtn.disabled = !fcSelected.size;
  deleteBtn.disabled = !fcSelected.size;
}

function toggleBatchMode() {
  fcBatchMode = !fcBatchMode;
  if (!fcBatchMode) fcSelected.clear();
  const btn = document.getElementById('fc-batch-btn');
  btn.textContent = fcBatchMode ? '✓ 完成' : '☐ 批量管理';
  btn.classList.toggle('active', fcBatchMode);
  renderFileCenter();
}

function fcSelectAll() {
  const visible = getVisibleFcDocs();
  const allVisibleSelected = visible.length > 0 && visible.every((it) => fcSelected.has(it.id));
  visible.forEach((it) => {
    if (allVisibleSelected) fcSelected.delete(it.id);
    else fcSelected.add(it.id);
  });
  renderFileCenter();
}

function fcClearSelection() {
  fcSelected.clear();
  renderFcGrid();
}

async function fcBatchDelete() {
  if (!fcSelected.size) return;
  if (!confirm('将选中的 ' + fcSelected.size + ' 个文件移至回收站？可从回收站恢复。')) return;
  const ids = [...fcSelected];
  const response = await window.api.deleteDocumentsBatch(ids);
  const trashIds = (response.items || []).map((item) => item.trash_id).filter(Boolean);
  toast('已移至回收站 ' + (response.trashed || 0) + ' 个文件');
  if (trashIds.length) showFcUndo('文件已移至回收站', async () => {
    await Promise.all(trashIds.map((trashId) => window.api.restoreTrash(trashId)));
    await loadFileCenter();
  });
  fcSelected.clear();
  fcBatchMode = false;
  document.getElementById('fc-batch-btn').textContent = '☐ 批量管理';
  document.getElementById('fc-batch-btn').classList.remove('active');
  await loadFileCenter();
  loadDocuments();
  loadStats();
}

async function fcCreateGroup() {
  const name = ((await promptModal('新建分组名称')) || '').trim();
  if (!name) return;
  await window.api.createGroup(name);
  await loadGroups();
  renderFcNav();
}

async function fcRenameGroup(name) {
  const nextName = ((await promptModal('重命名分组', name)) || '').trim();
  if (!nextName || nextName === name) return;
  try {
    const response = await window.api.renameGroup(name, nextName);
    if (!response || !response.success) throw new Error('rename failed');
    if (fcFilter.kind === 'group' && fcFilter.value === name) fcFilter.value = nextName;
    await loadFileCenter();
    toast('分组已重命名');
  } catch (e) {
    toast('分组重命名失败');
  }
}

async function fcDeleteGroup(name) {
  if (!confirm(`删除分组「${name}」？（组内文件不会被删除，仅移出该分组）`)) return;
  await window.api.deleteGroup(name);
  if (fcFilter.kind === 'group' && fcFilter.value === name) fcFilter = { kind: 'all', value: '' };
  await loadFileCenter({ reset: true });
}

function annotationIsEmpty(annotation) {
  return annotation && !(annotation.tags || []).length && !annotation.importance && !annotation.pinned &&
    !annotation.note && !annotation.caption && !annotation.group;
}

function renderBatchGroupSelect(current = '') {
  const select = document.getElementById('fc-batch-group');
  const names = groupsCache.map((group) => group.name);
  if (current && !names.includes(current)) names.unshift(current);
  select.innerHTML = '<option value="">移出分组</option>' + names
    .map((name) => `<option value="${escapeHtml(name)}"${name === current ? ' selected' : ''}>${escapeHtml(name)}</option>`)
    .join('');
}

function setBatchFieldEnabled(field, enabled) {
  document.querySelectorAll(`[data-batch-control="${field}"]`).forEach((control) => {
    control.disabled = !enabled;
  });
  if (field === 'note') syncBatchNoteInput();
}

function syncBatchNoteInput() {
  const checked = document.querySelector('[data-batch-field="note"]').checked;
  const clear = document.getElementById('fc-batch-note-mode').value === 'clear';
  document.getElementById('fc-batch-note').disabled = !checked || clear;
}

function openBatchEditor() {
  if (!fcSelected.size) return;
  document.getElementById('fc-batch-summary').textContent = `将修改 ${fcSelected.size} 个文件`;
  document.getElementById('fc-batch-hint').textContent = '';
  document.querySelectorAll('[data-batch-field]').forEach((checkbox) => {
    checkbox.checked = false;
    setBatchFieldEnabled(checkbox.dataset.batchField, false);
  });
  renderBatchGroupSelect();
  document.getElementById('fc-batch-importance').value = '0';
  document.getElementById('fc-batch-pinned').value = 'true';
  document.getElementById('fc-batch-tags-mode').value = 'add';
  document.getElementById('fc-batch-tags').value = '';
  document.getElementById('fc-batch-note-mode').value = 'append';
  document.getElementById('fc-batch-note').value = '';
  document.getElementById('fc-batch-modal').classList.remove('hidden');
}

function closeBatchEditor() {
  document.getElementById('fc-batch-modal').classList.add('hidden');
}

function parseBatchTags() {
  const values = document.getElementById('fc-batch-tags').value
    .split(/[,，]/).map((tag) => tag.trim()).filter(Boolean);
  return [...new Set(values)];
}

function batchFieldSelected(field) {
  return document.querySelector(`[data-batch-field="${field}"]`).checked;
}

async function promptNewGroupForBatch() {
  const name = ((await promptModal('新建分组名称')) || '').trim();
  if (!name) return;
  await window.api.createGroup(name);
  await loadGroups();
  renderBatchGroupSelect(name);
}

async function applyBatchSettings() {
  const fields = ['group', 'importance', 'pinned', 'tags', 'note'].filter(batchFieldSelected);
  const hint = document.getElementById('fc-batch-hint');
  const applyButton = document.getElementById('fc-batch-apply');
  if (!fields.length) {
    hint.textContent = '请选择至少一个要修改的字段';
    return;
  }
  const tags = parseBatchTags();
  const tagsMode = document.getElementById('fc-batch-tags-mode').value;
  if (fields.includes('tags') && !tags.length && tagsMode !== 'replace') {
    hint.textContent = '请输入要追加或移除的标签';
    return;
  }

  const noteMode = document.getElementById('fc-batch-note-mode').value;
  const note = document.getElementById('fc-batch-note').value.trim();
  if (fields.includes('note') && noteMode !== 'clear' && !note) {
    hint.textContent = '请输入备注内容，或选择“清空备注”';
    return;
  }

  const values = {
    group: document.getElementById('fc-batch-group').value || '',
    importance: Number(document.getElementById('fc-batch-importance').value) || 0,
    pinned: document.getElementById('fc-batch-pinned').value === 'true',
    tags,
    tagsMode,
    note,
    noteMode,
  };

  const ids = [...fcSelected];
  applyButton.disabled = true;
  applyButton.textContent = '应用中…';

  try {
    const patch = {};
    if (fields.includes('group')) patch.group = values.group;
    if (fields.includes('importance')) patch.importance = values.importance;
    if (fields.includes('pinned')) patch.pinned = values.pinned;
    if (fields.includes('tags')) patch.tags = values.tags;
    if (fields.includes('note')) patch.note = values.note;
    hint.textContent = `正在事务处理 ${ids.length} 个文件…`;
    const response = await window.api.setAnnotationsBatch(ids, patch, {
      tags_mode: values.tagsMode, note_mode: values.noteMode,
    });
    await loadFileCenter();
    loadDocuments();
    renderTagFilterBar();
    closeBatchEditor();
    toast(`已更新 ${response.updated || ids.length} 个文件`);
    if (response.audit_id) showFcUndo('批量设置已应用', async () => {
      await window.api.undoAudit(response.audit_id);
      await loadFileCenter();
    });
  } finally {
    applyButton.disabled = false;
    applyButton.textContent = '应用到所选文件';
  }
}

async function fcSelectResults() {
  const response = await window.api.listDocumentIds(fcQueryOptions(false));
  (response.ids || []).forEach((id) => fcSelected.add(id));
  renderFcGrid();
  toast(`已选择全部 ${response.total || 0} 个结果`);
}

async function saveFcView() {
  const name = ((await promptModal('智能视图名称', fcFilterLabel())) || '').trim();
  if (!name) return;
  fcSavedViews.push({ name, filter: { ...fcFilter }, query: fcSearchText, sortKey: fcSortKey, sortDir: fcSortDir });
  fcSavedViews = fcSavedViews.slice(-20);
  localStorage.setItem('centaur-fc-saved-views', JSON.stringify(fcSavedViews));
  renderFcNav();
  toast('智能视图已保存');
}

function showFcContextMenu(x, y, item) {
  if (!item) return;
  const menu = document.getElementById('fc-context-menu');
  menu.innerHTML = `<button data-action="annotate">🏷 标注和分组</button><button data-action="reindex">↻ 重建索引</button><button data-action="trash" class="danger-action">🗑 移至回收站</button>`;
  menu.style.left = `${Math.min(x, window.innerWidth - 190)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - 140)}px`;
  menu.classList.remove('hidden');
  menu.querySelector('[data-action="annotate"]').addEventListener('click', () => {
    menu.classList.add('hidden');
    openAnnotEditor(item.id, fcDisplayName(item), item.metadata.file_type, item.poster || '');
  });
  menu.querySelector('[data-action="reindex"]').addEventListener('click', () => { menu.classList.add('hidden'); openBatchReindex([item.id]); });
  menu.querySelector('[data-action="trash"]').addEventListener('click', () => { menu.classList.add('hidden'); deleteDoc(item.id); });
}

let fcReindexTargets = [];
async function openBatchReindex(targets = null) {
  fcReindexTargets = targets || [...fcSelected];
  if (!fcReindexTargets.length) return;
  const data = await window.api.fetch('/api/rag/strategies');
  const select = document.getElementById('fc-reindex-strategy');
  select.innerHTML = '<option value="">沿用当前文件策略 / 全局默认</option>' + ((data && data.strategies) || []).map((strategy) =>
    `<option value="${escapeHtml(strategy.id)}">${escapeHtml(strategy.label)} · ${escapeHtml(strategy.description)}</option>`
  ).join('');
  document.getElementById('fc-reindex-summary').textContent = `将重建 ${fcReindexTargets.length} 个文件`;
  document.getElementById('fc-reindex-modal').classList.remove('hidden');
}

function closeBatchReindex() { document.getElementById('fc-reindex-modal').classList.add('hidden'); }

async function applyBatchReindex() {
  const button = document.getElementById('fc-reindex-apply');
  button.disabled = true; button.textContent = '提交中…';
  try {
    const response = await window.api.reindexDocuments(fcReindexTargets, document.getElementById('fc-reindex-strategy').value || null);
    closeBatchReindex();
    toast(`已提交 ${response.queued || 0} 个索引任务`);
    setTimeout(() => loadFileCenter(), 1000);
  } catch (error) { toast(`提交失败：${error.message}`); }
  finally { button.disabled = false; button.textContent = '提交重建'; }
}

function closeFcMaintenance() { document.getElementById('fc-maint-modal').classList.add('hidden'); }

async function openTrash() {
  const data = await window.api.listTrash();
  document.getElementById('fc-maint-title').textContent = '回收站';
  document.getElementById('fc-maint-subtitle').textContent = `${data.total || 0} 个可恢复文件`;
  const list = document.getElementById('fc-maint-list');
  list.innerHTML = (data.items || []).map((item) => `<div class="fc-maint-item"><div><strong>${escapeHtml(item.file_name)}</strong><small>${formatSize(item.size)} · ${escapeHtml(new Date(item.deleted_at).toLocaleString('zh-CN'))}</small></div><div><button class="secondary-btn sm" data-restore="${item.id}">恢复</button><button class="secondary-btn sm danger-action" data-purge="${item.id}">永久删除</button></div></div>`).join('') || '<div class="fc-empty"><strong>回收站为空</strong></div>';
  document.getElementById('fc-maint-modal').classList.remove('hidden');
  list.querySelectorAll('[data-restore]').forEach((button) => button.addEventListener('click', async () => { await window.api.restoreTrash(button.dataset.restore); toast('文件已恢复并重新索引'); await openTrash(); loadFileCenter(); }));
  list.querySelectorAll('[data-purge]').forEach((button) => button.addEventListener('click', async () => {
    if (!confirm('永久删除后无法恢复，确定继续？')) return;
    await window.api.purgeTrash(button.dataset.purge); await openTrash();
  }));
}

async function openAudit() {
  const data = await window.api.listAudit();
  const labels = { batch_annotation: '批量标注', trash: '移至回收站', restore: '恢复文件', purge_trash: '永久删除', batch_reindex: '批量重建', undo: '撤销操作', purge_missing: '清理孤立索引' };
  document.getElementById('fc-maint-title').textContent = '操作记录';
  document.getElementById('fc-maint-subtitle').textContent = '文件中心的关键改动均保存在本地 SQLite';
  document.getElementById('fc-maint-list').innerHTML = (data.items || []).map((item) => `<div class="fc-maint-item"><div><strong>${labels[item.action] || escapeHtml(item.action)}</strong><small>${escapeHtml(new Date(item.created_at).toLocaleString('zh-CN'))} · ${item.targets.length} 个对象 · ${item.status === 'undone' ? '已撤销' : '已记录'}</small></div>${item.action === 'batch_annotation' && item.status === 'active' ? `<button class="secondary-btn sm" data-audit-undo="${item.id}">撤销</button>` : ''}</div>`).join('') || '<div class="fc-empty"><strong>暂无操作记录</strong></div>';
  document.getElementById('fc-maint-modal').classList.remove('hidden');
  document.querySelectorAll('[data-audit-undo]').forEach((button) => button.addEventListener('click', async () => { await window.api.undoAudit(Number(button.dataset.auditUndo)); toast('操作已撤销'); await openAudit(); loadFileCenter(); }));
}

function showFcUndo(label, callback) {
  clearTimeout(fcUndoTimer);
  fcUndoCallback = callback;
  document.getElementById('fc-undo-label').textContent = label;
  document.getElementById('fc-undo-bar').classList.remove('hidden');
  fcUndoTimer = setTimeout(closeFcUndo, 12000);
}

function closeFcUndo() {
  clearTimeout(fcUndoTimer);
  fcUndoCallback = null;
  document.getElementById('fc-undo-bar').classList.add('hidden');
}

// ===== 标签筛选条 =====
function renderTagFilterBar() {
  const bar = document.getElementById('tag-filter-bar');
  const box = document.getElementById('tag-filter-chips');
  const tags = allKnownTags();
  // 清掉已不存在的选中标签
  for (const t of [...activeTagFilter]) if (!tags.includes(t)) activeTagFilter.delete(t);
  if (!tags.length) {
    bar.classList.add('hidden');
    box.innerHTML = '';
    return;
  }
  bar.classList.remove('hidden');
  box.innerHTML = tags
    .map((t) => `<button class="tag-filter-chip ${activeTagFilter.has(t) ? 'active' : ''}" data-t="${escapeHtml(t)}">${escapeHtml(t)}</button>`)
    .join('');
  box.querySelectorAll('.tag-filter-chip').forEach((b) =>
    b.addEventListener('click', () => {
      const t = b.dataset.t;
      if (activeTagFilter.has(t)) activeTagFilter.delete(t);
      else activeTagFilter.add(t);
      renderTagFilterBar();
      if (document.getElementById('search-input').value.trim()) doSearch();
    })
  );
}

// ===== 事件绑定 =====
document.getElementById('annot-close').addEventListener('click', closeAnnotEditor);
document.getElementById('annot-overlay').addEventListener('click', closeAnnotEditor);
document.getElementById('annot-cancel').addEventListener('click', closeAnnotEditor);
document.getElementById('annot-save').addEventListener('click', saveAnnotEditor);
document.getElementById('annot-delete').addEventListener('click', deleteAnnotFromEditor);
document.getElementById('annot-group-new').addEventListener('click', promptNewGroupForEditor);

// 通用输入弹窗
document.getElementById('prompt-ok').addEventListener('click', () => _resolvePrompt(document.getElementById('prompt-input').value.trim()));
document.getElementById('prompt-cancel').addEventListener('click', () => _resolvePrompt(null));
document.getElementById('prompt-overlay').addEventListener('click', () => _resolvePrompt(null));
document.getElementById('prompt-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); _resolvePrompt(e.target.value.trim()); }
  else if (e.key === 'Escape') { e.preventDefault(); _resolvePrompt(null); }
});

// 视图切换 + 文件中心控件
document.querySelectorAll('#view-tabs .view-tab').forEach((b) =>
  b.addEventListener('click', () => switchView(b.dataset.view)));
document.querySelectorAll('[data-settings-section]').forEach((button) =>
  button.addEventListener('click', () => setSettingsSection(button.dataset.settingsSection)));
document.getElementById('fc-sort-key').addEventListener('change', (e) => { fcSortKey = e.target.value; loadFileCenter({ reset: true }); });
document.getElementById('fc-sort-dir').addEventListener('click', (e) => {
  fcSortDir = fcSortDir === 'asc' ? 'desc' : 'asc';
  e.target.textContent = fcSortDir === 'asc' ? '↑' : '↓';
  loadFileCenter({ reset: true });
});
document.querySelectorAll('[data-fc-view]').forEach((button) =>
  button.addEventListener('click', () => setFcViewMode(button.dataset.fcView)));
// 批量管理
document.getElementById('fc-batch-btn').addEventListener('click', toggleBatchMode);
document.getElementById('fc-select-all').addEventListener('click', fcSelectAll);
document.getElementById('fc-select-results').addEventListener('click', fcSelectResults);
document.getElementById('fc-clear-selection').addEventListener('click', fcClearSelection);
document.getElementById('fc-batch-edit').addEventListener('click', openBatchEditor);
document.getElementById('fc-batch-reindex').addEventListener('click', () => openBatchReindex());
document.getElementById('fc-batch-delete').addEventListener('click', fcBatchDelete);
document.getElementById('fc-search').addEventListener('input', (e) => {
  fcSearchText = e.target.value.trim();
  clearTimeout(fcSearchTimer);
  fcSearchTimer = setTimeout(() => loadFileCenter({ reset: true }), 260);
});
document.getElementById('fc-prev').addEventListener('click', () => { fcOffset = Math.max(0, fcOffset - FC_PAGE_SIZE); loadFileCenter(); });
document.getElementById('fc-next').addEventListener('click', () => { fcOffset += FC_PAGE_SIZE; loadFileCenter(); });
document.getElementById('fc-save-view').addEventListener('click', saveFcView);
document.getElementById('fc-trash-btn').addEventListener('click', openTrash);
document.getElementById('fc-audit-btn').addEventListener('click', openAudit);
document.getElementById('fc-batch-close').addEventListener('click', closeBatchEditor);
document.getElementById('fc-batch-cancel').addEventListener('click', closeBatchEditor);
document.getElementById('fc-batch-overlay').addEventListener('click', closeBatchEditor);
document.getElementById('fc-batch-apply').addEventListener('click', applyBatchSettings);
document.getElementById('fc-batch-group-new').addEventListener('click', promptNewGroupForBatch);
document.querySelectorAll('[data-batch-field]').forEach((checkbox) => {
  checkbox.addEventListener('change', () => setBatchFieldEnabled(checkbox.dataset.batchField, checkbox.checked));
});
document.getElementById('fc-batch-note-mode').addEventListener('change', syncBatchNoteInput);
document.getElementById('fc-reindex-close').addEventListener('click', closeBatchReindex);
document.getElementById('fc-reindex-cancel').addEventListener('click', closeBatchReindex);
document.getElementById('fc-reindex-overlay').addEventListener('click', closeBatchReindex);
document.getElementById('fc-reindex-apply').addEventListener('click', applyBatchReindex);
document.getElementById('fc-maint-close').addEventListener('click', closeFcMaintenance);
document.getElementById('fc-maint-overlay').addEventListener('click', closeFcMaintenance);
document.getElementById('fc-undo-close').addEventListener('click', closeFcUndo);
document.getElementById('fc-undo-action').addEventListener('click', async () => {
  const callback = fcUndoCallback;
  closeFcUndo();
  if (callback) { try { await callback(); toast('已撤销'); } catch (error) { toast('撤销失败'); } }
});
document.addEventListener('click', (event) => {
  if (!event.target.closest('#fc-context-menu')) document.getElementById('fc-context-menu').classList.add('hidden');
});
document.getElementById('wiki-search-btn').addEventListener('click', searchWiki);
document.getElementById('wiki-search-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchWiki(); });
document.getElementById('wiki-save').addEventListener('click', saveWikiPage);
document.getElementById('wiki-new-page').addEventListener('click', createWikiPage);
document.getElementById('wiki-maintain').addEventListener('click', maintainWiki);
document.getElementById('wiki-organize-selected').addEventListener('click', organizeSelectedWikiSource);
document.getElementById('setting-wiki-index-reconcile').addEventListener('click', reconcileWikiIndex);
document.querySelectorAll('#wiki-folder-tabs .wiki-folder').forEach((b) =>
  b.addEventListener('click', () => {
    document.querySelectorAll('#wiki-folder-tabs .wiki-folder').forEach((x) => x.classList.remove('active'));
    b.classList.add('active');
    wikiState.folder = b.dataset.folder || '';
    wikiState.query = '';
    document.getElementById('wiki-search-input').value = '';
    loadWikiPages();
  }));
document.getElementById('annot-tag-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ',' || e.key === '，') { e.preventDefault(); addAnnotTagFromInput(); }
});
document.getElementById('reindex-btn').addEventListener('click', () => doReindex());
document.getElementById('reindex-action').addEventListener('click', (e) => doReindex(e.target));

document.getElementById('video-close').addEventListener('click', closeVideoModal);
document.getElementById('video-overlay').addEventListener('click', closeVideoModal);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !document.getElementById('video-modal').classList.contains('hidden')) closeVideoModal();
  if (e.key === 'Escape' && !document.getElementById('annot-modal').classList.contains('hidden')) closeAnnotEditor();
  if (e.key === 'Escape' && !document.getElementById('fc-batch-modal').classList.contains('hidden')) closeBatchEditor();
  if (e.key === 'Escape' && !document.getElementById('fc-reindex-modal').classList.contains('hidden')) closeBatchReindex();
  if (e.key === 'Escape' && !document.getElementById('fc-maint-modal').classList.contains('hidden')) closeFcMaintenance();
  if (currentView === 'files' && fcBatchMode && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
    e.preventDefault(); fcSelectAll();
  }
  if (currentView === 'files' && fcBatchMode && e.key === 'Delete' && fcSelected.size && !e.target.matches('input,textarea,select')) {
    e.preventDefault(); fcBatchDelete();
  }
});

document.getElementById('search-btn').addEventListener('click', doSearch);
document.getElementById('search-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') doSearch();
});

document.querySelectorAll('.mode-opt').forEach((b) =>
  b.addEventListener('click', () => {
    document.querySelectorAll('.mode-opt').forEach((x) => x.classList.remove('active'));
    b.classList.add('active');
    currentMode = b.dataset.mode;
    if (document.getElementById('search-input').value.trim()) doSearch();
  })
);

document.getElementById('browse-btn').addEventListener('click', () => document.getElementById('file-input').click());
document.getElementById('file-input').addEventListener('change', (e) => {
  uploadFiles(e.target.files);
  e.target.value = '';
});

const dropZone = document.getElementById('drop-zone');
dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  uploadFiles(e.dataTransfer.files);
});

// ===== 身份记忆与 Agent 记忆 =====
let memoryState = {
  currentFile: 'SOUL.md',
  files: [],
  identitySync: null,
};

let agentMemoryState = {
  files: [],
  filteredFiles: [],
  selectedAgent: 'all',
  selectedMonth: 'all',
  selectedUser: 'all',
  selectedType: 'conversation',
  currentPath: '',
  detailLoadedPath: '',
  detailRequestId: 0,
  tokenManager: null,
  loadError: '',
};

const agentDisplayNames = {
  codex: 'Codex',
  claude: 'Claude',
  'claude-desktop': 'Claude Desktop',
  gemini: 'Gemini',
  hermes: 'Hermes',
  openclaw: 'OpenClaw',
  opencode: 'OpenCode',
  'open-code': 'OpenCode',
};

const agentIcons = {
  codex: '⌘',
  claude: '◈',
  'claude-desktop': '◈',
  gemini: '✦',
  hermes: 'H',
  openclaw: '🦞',
  opencode: '<>',
  'open-code': '<>',
};

const coreMemoryFiles = [
  { path: 'SOUL.md', label: 'SOUL.md', hint: '人格与价值观' },
  { path: 'AGENTS.md', label: 'AGENTS.md', hint: '执行规则' },
  { path: 'IDENTITY.md', label: 'IDENTITY.md', hint: '身份定义' },
  { path: 'USER.md', label: 'USER.md', hint: '用户画像' },
];

async function loadMemoryPanel() {
  await Promise.allSettled([loadMemoryFiles(), loadIdentitySyncStatus(), loadTokenManagerMemoryStatus()]);
  var isCore = coreMemoryFiles.some(function(item) { return item.path === memoryState.currentFile; });
  if (!isCore) memoryState.currentFile = 'SOUL.md';
  await selectMemoryFile(memoryState.currentFile);
}

async function loadMemoryFiles() {
  try {
    const data = await window.api.fetch('/api/memory/files');
    memoryState.files = (data && data.files) || [];
  } catch (e) {
    memoryState.files = [];
  }
  renderMemoryFileList();
}

function renderMemoryFileList() {
  var fileList = document.getElementById('memory-core-list');
  var filesByPath = new Map(memoryState.files.map(function(f) { return [f.path, f]; }));

  fileList.innerHTML = coreMemoryFiles.map(function(item) {
    var file = filesByPath.get(item.path);
    var size = file ? formatSize(file.size || 0) : item.hint;
    return '<li data-file="' + item.path + '" class="' +
      (item.path === memoryState.currentFile ? 'active' : '') + '">' +
      escapeHtml(item.label) + '<small>' + escapeHtml(size) + '</small></li>';
  }).join('');

  document.querySelectorAll('#memory-core-list li[data-file]')
    .forEach(function(li) {
      li.addEventListener('click', function() { selectMemoryFile(li.dataset.file); });
    });
}

function isAgentMemoryFile(file) {
  return !!(file && file.path && (
    file.memory_type === 'conversation' ||
    file.memory_type === 'agent_import' ||
    file.path.startsWith('imports/') ||
    file.path.startsWith('conversations/')
  ));
}

function isAgentMemoryPath(path) {
  var file = memoryState.files.concat(agentMemoryState.files).find(function(item) { return item.path === path; });
  return file ? isAgentMemoryFile(file) :
    String(path || '').startsWith('imports/') || String(path || '').startsWith('conversations/');
}

function agentMemoryAgent(file) {
  var parts = String(file.path || '').split('/');
  var fallback = parts[0] === 'conversations' && parts.length > 2
    ? parts[1]
    : String(parts[parts.length - 1] || 'other').replace(/\.md$/i, '');
  return String(file.agent || file.provider || fallback || 'other').trim().toLowerCase();
}

function agentMemoryAgentLabel(file) {
  var agent = agentMemoryAgent(file);
  return agentDisplayNames[agent] || agent.replace(/(^|[-_])([a-z])/g, function(_all, prefix, letter) {
    return (prefix ? ' ' : '') + letter.toUpperCase();
  }) || '其他 Agent';
}

function agentMemoryAgentIcon(file) {
  return agentIcons[agentMemoryAgent(file)] || 'A';
}

function agentMemoryTimestamp(file) {
  var numeric = Number(file.occurred_at);
  if (Number.isFinite(numeric) && numeric > 0) return numeric;
  var parsed = Date.parse(file.updated_at || '');
  return Number.isFinite(parsed) ? parsed : 0;
}

function agentMemoryUserLabel(file) {
  var name = String(file.user_name || '').trim();
  var email = String(file.user_email || '').trim();
  if (name && email && name !== email) return name + ' · ' + email;
  if (name || email) return name || email;
  var userId = String(file.user_id || '').trim();
  return userId;
}

function agentMemoryUserKey(file) {
  var userId = String(file.user_id || '').trim();
  var email = String(file.user_email || '').trim();
  var name = String(file.user_name || '').trim();
  if (userId) return 'id:' + userId;
  if (email) return 'email:' + email.toLowerCase();
  if (name) return 'name:' + name;
  return '';
}

function agentMemoryFileLabel(file) {
  var fallback = String(file.path || '').split('/').pop().replace(/\.md$/i, '');
  if (!fallback && (file.memory_type === 'agent_import' || String(file.path || '').startsWith('imports/'))) {
    fallback = agentMemoryAgentLabel(file) + ' 原生记忆';
  }
  return file.title || fallback || '未命名记忆';
}

function agentMemoryTimeLabel(timestamp) {
  if (!timestamp) return '时间未知';
  var date = new Date(timestamp);
  return date.getFullYear() + '年' + String(date.getMonth() + 1).padStart(2, '0') + '月';
}

function agentMemoryEntryDate(timestamp) {
  if (!timestamp) return '时间未知';
  return new Date(timestamp).toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
}

function agentMemoryMonthKey(file) {
  var timestamp = agentMemoryTimestamp(file);
  if (!timestamp) return 'unknown';
  var date = new Date(timestamp);
  return date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0');
}

function isConversationMemory(file) {
  return file.memory_type === 'conversation' || String(file.path || '').startsWith('conversations/');
}

function agentMemoryKindLabel(file) {
  return isConversationMemory(file) ? '对话记忆' : '原生记忆';
}

async function selectMemoryFile(path) {
  if (!path) return;
  if (isAgentMemoryPath(path)) {
    var agentFile = memoryState.files.concat(agentMemoryState.files).find(function(file) { return file.path === path; });
    agentMemoryState.selectedType = agentFile
      ? (isConversationMemory(agentFile) ? 'conversation' : 'native')
      : (String(path).startsWith('conversations/') ? 'conversation' : 'native');
    agentMemoryState.currentPath = path;
    switchView('agent-memory');
    return;
  }
  memoryState.currentFile = path;
  document.querySelectorAll('#memory-core-list li[data-file]')
    .forEach(function(li) { li.classList.toggle('active', li.dataset.file === path); });
  var editor = document.getElementById('memory-editor-text');
  var save = document.getElementById('memory-save');
  editor.readOnly = false;
  save.classList.remove('hidden');
  try {
    const data = await window.api.fetch(memoryFileUrl(path));
    if (data && data.content !== undefined) {
      editor.value = data.content;
      document.getElementById('memory-editor-title').textContent = data.path || path;
      var mtime = data.updated_at ? new Date(data.updated_at).toLocaleString('zh-CN') : '';
      document.getElementById('memory-editor-meta').textContent = formatSize(data.size || 0) + ' · ' + mtime;
    }
  } catch (e) {
    console.error('加载记忆文件失败:', e);
  }
}

async function saveMemoryFile() {
  var path = memoryState.currentFile;
  var content = document.getElementById('memory-editor-text').value;
  var btn = document.getElementById('memory-save');
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    var result = await window.api.fetch(memoryFileUrl(path), {
      method: 'PUT',
      body: { content: content, source_agent: 'vector-db-ui' },
    });
    memoryState.identitySync = result.identitySync || memoryState.identitySync;
    renderIdentitySyncStatus();
    await loadMemoryFiles();
    if (result.identitySync && result.identitySync.success) {
      var targets = (((result.identitySync || {}).result || {}).targets || []).filter(function(target) {
        return target.detected && (target.status === 'applied' || target.status === 'unchanged');
      }).length;
      toast('已保存，并同步至 ' + targets + ' 个 Agent');
    } else {
      toast('已保存；身份同步待重试');
    }
  } catch (e) {
    toast('保存失败');
  }
  btn.disabled = false;
  btn.textContent = '保存';
}

async function searchMemory() {
  var query = document.getElementById('memory-search-input').value.trim();
  if (!query) return;
  var resultsDiv = document.getElementById('memory-search-results');
  resultsDiv.innerHTML = '<div class="loading">搜索中…</div>';
  try {
    var data = await window.api.fetch('/api/memory/search', {
      method: 'POST',
      body: { query: query, n_results: 10, identity_only: true },
    });
    var results = (data && data.results) || [];
    if (!results.length) {
      resultsDiv.innerHTML = '<div class="empty-state"><p>无匹配结果</p></div>';
      return;
    }
    resultsDiv.innerHTML = results.map(function(r) {
      var emojis = { identity_soul: '✨', identity_profile: '🪪', user_profile: '👤', agents_rules: '📐' };
      return '<div class="memory-result-item" data-file="' + escapeHtml(r.rel_path) + '">' +
        '<div class="memory-result-head">' +
        '<span class="memory-result-type">' + (emojis[r.memory_type] || 'file') + ' ' + (r.memory_type || 'unknown') + '</span>' +
        '<span class="memory-result-score">' + (r.score * 100).toFixed(0) + '%</span>' +
        (r.date ? '<span class="memory-result-date">' + r.date + '</span>' : '') +
        '</div>' +
        '<div class="memory-result-text">' + escapeHtml(r.text) + '</div>' +
        '<div class="memory-result-file">' + escapeHtml(r.rel_path) + '</div>' +
        '</div>';
    }).join('');
    resultsDiv.querySelectorAll('.memory-result-item').forEach(function(item) {
      item.addEventListener('click', function() { selectMemoryFile(item.dataset.file); });
    });
  } catch (e) {
    resultsDiv.innerHTML = '<div class="empty-state"><p>搜索失败</p></div>';
  }
}

function memoryFileUrl(path) {
  return '/api/memory/files/' + path.split('/').map(encodeURIComponent).join('/');
}

async function loadIdentitySyncStatus() {
  try {
    memoryState.identitySync = await window.api.fetch('/api/memory/identity/status');
  } catch (e) {
    memoryState.identitySync = { pending: true, last_error: e.message || '读取同步状态失败' };
  }
  renderIdentitySyncStatus();
}

function renderIdentitySyncStatus() {
  var element = document.getElementById('identity-sync-status');
  var retry = document.getElementById('identity-sync-retry');
  if (!element || !retry) return;
  var state = memoryState.identitySync || {};
  var result = state.result || state.last_result || null;
  var pending = state.pending || state.identity_pending || state.state === 'pending' || !!state.error || !!state.last_error;
  var targets = result && Array.isArray(result.targets) ? result.targets.filter(function(target) {
    return target.detected && (target.status === 'applied' || target.status === 'unchanged');
  }) : [];
  if (pending) {
    element.textContent = '● 待同步';
    element.className = 'identity-sync-status pending';
    element.title = state.error || state.last_error || '等待 TokenManager 可用后重试';
    retry.classList.remove('hidden');
    return;
  }
  retry.classList.add('hidden');
  if (result) {
    element.textContent = '● 已同步 ' + targets.length + ' 个 Agent';
    element.className = 'identity-sync-status ok';
    element.title = state.revision || state.last_revision || result.revision || '';
  } else {
    element.textContent = '尚未发布';
    element.className = 'identity-sync-status';
    element.title = '首次保存任一身份文件时发布完整四文件快照';
  }
}

async function retryIdentitySync() {
  var button = document.getElementById('identity-sync-retry');
  button.disabled = true;
  button.textContent = '同步中…';
  try {
    memoryState.identitySync = await window.api.fetch('/api/memory/identity/sync', { method: 'POST' });
    renderIdentitySyncStatus();
    if (memoryState.identitySync.success) toast('统一身份同步完成');
    else toast('身份已保存在本机，将继续自动重试');
    await loadTokenManagerMemoryStatus();
  } catch (e) {
    toast('同步失败：' + (e.message || e));
    await loadIdentitySyncStatus();
  } finally {
    button.disabled = false;
    button.textContent = '重试同步';
  }
}

// ===== Agent 记忆独立页面 =====

async function loadAgentMemoryPanel() {
  await Promise.allSettled([loadAgentMemoryFiles(), loadTokenManagerMemoryStatus()]);
  renderAgentMemoryPage();
}

async function loadAgentMemoryFiles() {
  try {
    var data = await window.api.fetch('/api/memory/files');
    agentMemoryState.files = ((data && data.files) || []).filter(isAgentMemoryFile);
    agentMemoryState.loadError = '';
  } catch (e) {
    agentMemoryState.files = [];
    agentMemoryState.loadError = e.message || '读取 Agent 记忆失败';
  }
}

async function loadTokenManagerMemoryStatus() {
  try {
    agentMemoryState.tokenManager = await window.api.fetch('/api/memory/tokenmanager');
  } catch (e) {
    agentMemoryState.tokenManager = { enabled: false, last_error: e.message || '读取失败' };
  }
  renderTokenManagerMemoryStatus();
}

function tokenManagerConnectionView() {
  var state = agentMemoryState.tokenManager || {};
  if (state.running) return { label: '同步中', className: 'running' };
  if (state.last_error) return { label: '连接异常', className: 'error' };
  if (state.token_configured) return { label: '已连接', className: 'ok' };
  return { label: '未连接', className: '' };
}

function renderTokenManagerMemoryStatus() {
  var state = agentMemoryState.tokenManager || {};
  var view = tokenManagerConnectionView();
  var url = document.getElementById('tokenmanager-memory-url');
  var enabled = document.getElementById('tokenmanager-memory-enabled');
  var interval = document.getElementById('tokenmanager-memory-interval');
  if (url && !url.matches(':focus')) url.value = state.url || 'http://127.0.0.1:15722';
  if (enabled) enabled.checked = !!state.enabled;
  if (interval && !interval.matches(':focus')) interval.value = state.interval_seconds || 60;

  var badge = document.getElementById('tokenmanager-memory-badge');
  if (badge) {
    badge.textContent = view.label;
    badge.className = 'memory-sync-badge ' + view.className;
  }
  var headerStatus = document.getElementById('agent-memory-connection-status');
  var headerDot = document.getElementById('agent-memory-connection-dot');
  if (headerStatus) headerStatus.textContent = view.label;
  if (headerDot) headerDot.className = view.className;

  var status = document.getElementById('tokenmanager-memory-status');
  if (status) {
    var completed = state.last_completed_at ? new Date(state.last_completed_at).toLocaleString('zh-CN') : '尚未同步';
    var mode = state.sync_mode === 'tokenmanager-api' ? 'API 统一同步' : '兼容文件直扫';
    var identityCapability = (state.capabilities || []).includes('identity-write')
      ? '身份写入已开启'
      : '身份写入未开启';
    status.textContent = state.last_error
      ? '最近错误：' + state.last_error
      : mode + ' · 已保存 ' + (state.conversation_count || 0) + ' 条会话、' +
        (state.memory_count || 0) + ' 条 Agent 记忆 · 上次完成：' + completed +
        ' · 对话新增 ' + (state.last_conversation_imported || 0) +
        ' · 记忆新增 ' + (state.last_memory_imported || 0) +
        ' · 记忆删除 ' + (state.last_memory_deleted || 0) + ' · ' + identityCapability +
        (state.identity_pending ? ' · 统一身份待同步' : '');
  }
}

function renderAgentMemoryPage() {
  renderAgentMemoryHeader();
  renderAgentMemoryFilters();
  renderAgentMemoryTabs();
  applyAgentMemoryFilters();
}

function renderAgentMemoryHeader() {
  var agents = new Set(agentMemoryState.files.map(agentMemoryAgent));
  document.getElementById('agent-memory-agent-count').textContent = String(agents.size);
  document.getElementById('agent-memory-record-count').textContent = String(agentMemoryState.files.length);
  renderTokenManagerMemoryStatus();
}

function renderAgentMemoryTabs() {
  var tabs = document.getElementById('agent-memory-tabs');
  var typedFiles = agentMemoryState.files.filter(function(file) {
    var matchesType = agentMemoryState.selectedType === 'conversation' ? isConversationMemory(file) : !isConversationMemory(file);
    if (!matchesType) return false;
    if (agentMemoryState.selectedMonth !== 'all' && agentMemoryMonthKey(file) !== agentMemoryState.selectedMonth) return false;
    if (agentMemoryState.selectedUser !== 'all' && agentMemoryUserKey(file) !== agentMemoryState.selectedUser) return false;
    return true;
  });
  var counts = new Map();
  typedFiles.forEach(function(file) {
    var agent = agentMemoryAgent(file);
    counts.set(agent, (counts.get(agent) || 0) + 1);
  });
  var agents = Array.from(counts.keys()).sort(function(a, b) {
    return agentMemoryAgentLabel({ agent: a }).localeCompare(agentMemoryAgentLabel({ agent: b }), 'zh-CN');
  });
  if (agentMemoryState.selectedAgent !== 'all' && !counts.has(agentMemoryState.selectedAgent)) {
    agentMemoryState.selectedAgent = 'all';
  }
  var items = [{ key: 'all', label: '全部', count: typedFiles.length }]
    .concat(agents.map(function(agent) {
      return { key: agent, label: agentMemoryAgentLabel({ agent: agent }), count: counts.get(agent) };
    }));
  tabs.innerHTML = items.map(function(item) {
    return '<button type="button" data-agent="' + escapeHtml(item.key) + '" class="' +
      (item.key === agentMemoryState.selectedAgent ? 'active' : '') + '">' +
      '<span>' + escapeHtml(item.label) + '</span><b>' + item.count + '</b></button>';
  }).join('');
  tabs.querySelectorAll('button[data-agent]').forEach(function(button) {
    button.addEventListener('click', function() {
      agentMemoryState.selectedAgent = button.dataset.agent;
      tabs.querySelectorAll('button[data-agent]').forEach(function(item) {
        item.classList.toggle('active', item === button);
      });
      applyAgentMemoryFilters();
    });
  });
}

function renderAgentMemoryFilters() {
  var monthSelect = document.getElementById('agent-memory-month-filter');
  var typeFiles = agentMemoryState.files.filter(function(file) {
    return agentMemoryState.selectedType === 'conversation' ? isConversationMemory(file) : !isConversationMemory(file);
  });
  var months = Array.from(new Set(typeFiles.map(agentMemoryMonthKey))).sort(function(a, b) {
    if (a === 'unknown') return 1;
    if (b === 'unknown') return -1;
    return b.localeCompare(a);
  });
  if (agentMemoryState.selectedMonth !== 'all' && !months.includes(agentMemoryState.selectedMonth)) {
    agentMemoryState.selectedMonth = 'all';
  }
  monthSelect.innerHTML = '<option value="all">全部月份</option>' + months.map(function(month) {
    var label = month === 'unknown' ? '时间未知' : month;
    return '<option value="' + escapeHtml(month) + '">' + escapeHtml(label) + '</option>';
  }).join('');
  monthSelect.value = agentMemoryState.selectedMonth;

  var users = new Map();
  typeFiles.forEach(function(file) {
    var key = agentMemoryUserKey(file);
    var label = agentMemoryUserLabel(file);
    if (key && label && !users.has(key)) users.set(key, label);
  });
  var userWrap = document.getElementById('agent-memory-user-filter-wrap');
  var userSelect = document.getElementById('agent-memory-user-filter');
  userWrap.classList.toggle('hidden', users.size === 0);
  if (!users.size || (agentMemoryState.selectedUser !== 'all' && !users.has(agentMemoryState.selectedUser))) {
    agentMemoryState.selectedUser = 'all';
  }
  var userItems = Array.from(users.entries()).sort(function(a, b) { return a[1].localeCompare(b[1], 'zh-CN'); });
  userSelect.innerHTML = '<option value="all">全部用户</option>' + userItems.map(function(item) {
    return '<option value="' + escapeHtml(item[0]) + '">' + escapeHtml(item[1]) + '</option>';
  }).join('');
  userSelect.value = agentMemoryState.selectedUser;
}

function applyAgentMemoryFilters() {
  var baseFiltered = agentMemoryState.files.filter(function(file) {
    if (agentMemoryState.selectedAgent !== 'all' && agentMemoryAgent(file) !== agentMemoryState.selectedAgent) return false;
    if (agentMemoryState.selectedMonth !== 'all' && agentMemoryMonthKey(file) !== agentMemoryState.selectedMonth) return false;
    if (agentMemoryState.selectedUser !== 'all' && agentMemoryUserKey(file) !== agentMemoryState.selectedUser) return false;
    return true;
  });
  renderAgentMemoryTypeTabs(baseFiltered);
  var filtered = baseFiltered.filter(function(file) {
    return agentMemoryState.selectedType === 'conversation' ? isConversationMemory(file) : !isConversationMemory(file);
  }).sort(function(a, b) {
    return agentMemoryTimestamp(b) - agentMemoryTimestamp(a) || String(a.path).localeCompare(String(b.path));
  });
  agentMemoryState.filteredFiles = filtered;
  document.getElementById('agent-memory-filter-summary').textContent = filtered.length +
    (agentMemoryState.selectedType === 'conversation' ? ' 条 AI 对话' : ' 条原生记忆');

  var currentVisible = filtered.some(function(file) { return file.path === agentMemoryState.currentPath; });
  if (!currentVisible) {
    agentMemoryState.currentPath = filtered.length ? filtered[0].path : '';
    agentMemoryState.detailLoadedPath = '';
  }
  renderAgentMemoryRecordList();
  if (!filtered.length) {
    clearAgentMemoryDetail(agentMemoryState.loadError || '当前筛选下没有 Agent 记忆');
  } else if (agentMemoryState.detailLoadedPath !== agentMemoryState.currentPath) {
    void selectAgentMemoryRecord(agentMemoryState.currentPath);
  } else {
    renderAgentMemoryRecordSelection();
  }
}

function renderAgentMemoryTypeTabs(files) {
  var tabs = document.getElementById('agent-memory-type-tabs');
  var conversationCount = files.filter(isConversationMemory).length;
  var nativeCount = files.length - conversationCount;
  document.getElementById('agent-memory-conversation-count').textContent = String(conversationCount);
  document.getElementById('agent-memory-native-count').textContent = String(nativeCount);
  tabs.querySelectorAll('button[data-memory-type]').forEach(function(button) {
    var active = button.dataset.memoryType === agentMemoryState.selectedType;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
    button.onclick = function() {
      if (agentMemoryState.selectedType === button.dataset.memoryType) return;
      agentMemoryState.selectedType = button.dataset.memoryType;
      renderAgentMemoryFilters();
      renderAgentMemoryTabs();
      applyAgentMemoryFilters();
    };
  });
}

function renderAgentMemoryRecordList() {
  var list = document.getElementById('agent-memory-record-list');
  if (!agentMemoryState.filteredFiles.length) {
    list.innerHTML = '<div class="agent-memory-empty"><span>∅</span><strong>' +
      escapeHtml(agentMemoryState.loadError || '这个分类下暂无记忆') + '</strong><p>可切换记忆类型，或调整 Agent、时间和用户筛选</p></div>';
    return;
  }
  list.innerHTML = agentMemoryState.filteredFiles.map(function(file) {
    var user = agentMemoryUserLabel(file);
    var conversation = isConversationMemory(file);
    return '<button type="button" class="agent-memory-record ' +
      (file.path === agentMemoryState.currentPath ? 'active' : '') + '" data-path="' + escapeHtml(file.path) + '">' +
      '<span class="agent-memory-source-icon" aria-hidden="true">' + escapeHtml(agentMemoryAgentIcon(file)) + '</span>' +
      '<span class="agent-memory-record-main"><strong>' + escapeHtml(agentMemoryFileLabel(file)) + '</strong>' +
      '<span class="agent-memory-record-meta"><em>' + escapeHtml(agentMemoryAgentLabel(file)) + '</em>' +
      '<time>' + escapeHtml(agentMemoryEntryDate(agentMemoryTimestamp(file))) + '</time>' +
      (user ? '<span title="' + escapeHtml(user) + '">👤 ' + escapeHtml(user) + '</span>' : '') + '</span></span>' +
      '<span class="agent-memory-kind ' + (conversation ? 'conversation' : 'native') + '">' +
      (conversation ? '💬 对话' : '🧠 原生') + '</span></button>';
  }).join('');
  list.querySelectorAll('button[data-path]').forEach(function(button) {
    button.addEventListener('click', function() { void selectAgentMemoryRecord(button.dataset.path); });
  });
}

function renderAgentMemoryRecordSelection() {
  document.querySelectorAll('#agent-memory-record-list button[data-path]').forEach(function(button) {
    button.classList.toggle('active', button.dataset.path === agentMemoryState.currentPath);
  });
}

function renderAgentMemoryDetailHeading(file, data) {
  var title = document.getElementById('agent-memory-detail-title');
  var meta = document.getElementById('agent-memory-detail-meta');
  var kind = document.getElementById('agent-memory-detail-kind');
  var user = agentMemoryUserLabel(file);
  var updated = (data && data.updated_at) || file.updated_at;
  title.textContent = agentMemoryFileLabel(file);
  meta.textContent = [agentMemoryAgentLabel(file), agentMemoryEntryDate(agentMemoryTimestamp(file)), user,
    updated ? '文件更新于 ' + new Date(updated).toLocaleString('zh-CN') : '', file.path].filter(Boolean).join(' · ');
  kind.textContent = agentMemoryKindLabel(file);
  kind.className = 'agent-memory-kind ' + (isConversationMemory(file) ? 'conversation' : 'native');
}

async function selectAgentMemoryRecord(path) {
  var file = agentMemoryState.files.find(function(item) { return item.path === path; });
  if (!file) return;
  agentMemoryState.currentPath = path;
  agentMemoryState.detailLoadedPath = '';
  renderAgentMemoryRecordSelection();
  renderAgentMemoryDetailHeading(file);
  var content = document.getElementById('agent-memory-detail-content');
  var empty = document.getElementById('agent-memory-detail-empty');
  empty.classList.add('hidden');
  content.classList.remove('hidden');
  content.textContent = '正在加载…';
  var requestId = ++agentMemoryState.detailRequestId;
  try {
    var data = await window.api.fetch(memoryFileUrl(path));
    if (requestId !== agentMemoryState.detailRequestId || path !== agentMemoryState.currentPath) return;
    renderAgentMemoryDetailHeading(file, data);
    content.textContent = data && data.content !== undefined ? data.content : '';
    agentMemoryState.detailLoadedPath = path;
  } catch (e) {
    if (requestId !== agentMemoryState.detailRequestId || path !== agentMemoryState.currentPath) return;
    content.textContent = '加载失败：' + (e.message || e);
  }
}

function clearAgentMemoryDetail(message) {
  agentMemoryState.detailRequestId += 1;
  agentMemoryState.detailLoadedPath = '';
  document.getElementById('agent-memory-detail-title').textContent = '没有可预览的记忆';
  document.getElementById('agent-memory-detail-meta').textContent = '';
  document.getElementById('agent-memory-detail-kind').classList.add('hidden');
  document.getElementById('agent-memory-detail-content').classList.add('hidden');
  var empty = document.getElementById('agent-memory-detail-empty');
  empty.classList.remove('hidden');
  empty.querySelector('strong').textContent = message;
  empty.querySelector('p').textContent = '调整筛选条件后，最新记录会自动在这里打开';
}

function openTokenManagerMemoryDrawer() {
  var drawer = document.getElementById('tokenmanager-memory-drawer');
  drawer.classList.remove('hidden');
  drawer.setAttribute('aria-hidden', 'false');
  renderTokenManagerMemoryStatus();
  setTimeout(function() { document.getElementById('tokenmanager-memory-close').focus(); }, 0);
}

function closeTokenManagerMemoryDrawer() {
  var drawer = document.getElementById('tokenmanager-memory-drawer');
  drawer.classList.add('hidden');
  drawer.setAttribute('aria-hidden', 'true');
}

function tokenManagerMemoryConfigFromForm() {
  return {
    enabled: document.getElementById('tokenmanager-memory-enabled').checked,
    url: document.getElementById('tokenmanager-memory-url').value.trim(),
    token: document.getElementById('tokenmanager-memory-token').value.trim() || null,
    interval_seconds: Number(document.getElementById('tokenmanager-memory-interval').value) || 60,
  };
}

async function persistTokenManagerMemoryConfig() {
  agentMemoryState.tokenManager = await window.api.fetch('/api/memory/tokenmanager/config', {
    method: 'POST',
    body: tokenManagerMemoryConfigFromForm(),
  });
  document.getElementById('tokenmanager-memory-token').value = '';
  renderTokenManagerMemoryStatus();
  return agentMemoryState.tokenManager;
}

async function testTokenManagerMemoryConnection() {
  var button = document.getElementById('tokenmanager-memory-test');
  button.disabled = true;
  try {
    await persistTokenManagerMemoryConfig();
    await window.api.fetch('/api/memory/tokenmanager/test', { method: 'POST' });
    toast('TokenManager 连接正常');
    await loadTokenManagerMemoryStatus();
  } catch (e) {
    toast('连接失败：' + (e.message || e));
  } finally {
    button.disabled = false;
  }
}

async function syncTokenManagerMemoryNow() {
  var button = document.getElementById('tokenmanager-memory-sync');
  button.disabled = true;
  button.textContent = '同步中…';
  try {
    await persistTokenManagerMemoryConfig();
    var result = await window.api.fetch('/api/memory/tokenmanager/sync', { method: 'POST' });
    toast(
      '同步完成：对话新增 ' + (result.conversation_imported || 0) +
      '，记忆新增 ' + (result.memory_imported || 0) +
      '，记忆删除 ' + (result.memory_deleted || 0)
    );
    await Promise.all([loadTokenManagerMemoryStatus(), loadAgentMemoryFiles()]);
    renderAgentMemoryPage();
  } catch (e) {
    toast('同步失败：' + (e.message || e));
    await loadTokenManagerMemoryStatus();
  } finally {
    button.disabled = false;
    button.textContent = '立即同步';
  }
}

// bind memory events
document.getElementById('memory-save').addEventListener('click', saveMemoryFile);
document.getElementById('memory-search-btn').addEventListener('click', searchMemory);
document.getElementById('memory-search-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') searchMemory();
});
document.getElementById('agent-memory-month-filter').addEventListener('change', function(e) {
  agentMemoryState.selectedMonth = e.target.value;
  renderAgentMemoryTabs();
  applyAgentMemoryFilters();
});
document.getElementById('agent-memory-user-filter').addEventListener('change', function(e) {
  agentMemoryState.selectedUser = e.target.value;
  renderAgentMemoryTabs();
  applyAgentMemoryFilters();
});
document.getElementById('agent-memory-settings-open').addEventListener('click', openTokenManagerMemoryDrawer);
document.getElementById('identity-settings-open').addEventListener('click', openTokenManagerMemoryDrawer);
document.getElementById('identity-sync-retry').addEventListener('click', retryIdentitySync);
document.getElementById('tokenmanager-memory-close').addEventListener('click', closeTokenManagerMemoryDrawer);
document.getElementById('tokenmanager-memory-overlay').addEventListener('click', closeTokenManagerMemoryDrawer);
document.getElementById('tokenmanager-memory-test').addEventListener('click', testTokenManagerMemoryConnection);
document.getElementById('tokenmanager-memory-sync').addEventListener('click', syncTokenManagerMemoryNow);
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && !document.getElementById('tokenmanager-memory-drawer').classList.contains('hidden')) {
    closeTokenManagerMemoryDrawer();
  }
});

// LAN 设置保存
document.getElementById('setting-lan-save').addEventListener('click', async function() {
  var enabled = document.getElementById('setting-lan-enabled').checked;
  var password = document.getElementById('setting-lan-password').value;
  var msg = document.getElementById('setting-lan-msg');
  if (enabled && !password && !(latestLanConfig && latestLanConfig.password_set)) {
    msg.textContent = '请设置密码';
    return;
  }
  var btn = this; btn.disabled = true; btn.textContent = '保存中…';
  try {
    var saveResult = await window.api.fetch('/api/lan/config', {
      method: 'POST',
      body: { enabled: enabled, password: password },
    });
    var lanCfg = await window.api.fetch('/api/lan/config');
    renderLanConfig(lanCfg);
    document.getElementById('setting-lan-password').value = '';
    msg.textContent = saveResult.restart_required
      ? '✅ 已保存，重启后端后可从局域网访问'
      : (enabled ? '✅ 已保存并生效' : '✅ 已关闭并生效');
  } catch(e) { msg.textContent = '保存失败'; }
  btn.disabled = false; btn.textContent = '保存';
});

document.getElementById('setting-lan-copy').addEventListener('click', function() {
  var value = document.getElementById('setting-lan-url').value;
  if (!value || value === '未检测到局域网地址') return;
  copyText(value);
});

document.getElementById('setting-wiki-organizer-refresh').addEventListener('click', async function() {
  var btn = this;
  btn.disabled = true;
  btn.textContent = '检测中…';
  await loadWikiOrganizerStatus();
  btn.disabled = false;
  btn.textContent = '重新检测本地模型';
});

['setting-rag-default', 'setting-rag-text', 'setting-rag-image', 'setting-rag-video'].forEach(function(id) {
  var el = document.getElementById(id);
  if (el) el.addEventListener('change', renderRagStrategyDetail);
});

document.getElementById('setting-rag-save').addEventListener('click', async function() {
  var msg = document.getElementById('setting-rag-msg');
  var btn = this;
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    await window.api.fetch('/api/rag/config', {
      method: 'POST',
      body: {
        default_strategy: document.getElementById('setting-rag-default').value,
        file_type_strategies: {
          text: document.getElementById('setting-rag-text').value,
          image: document.getElementById('setting-rag-image').value,
          video: document.getElementById('setting-rag-video').value,
        },
      },
    });
    msg.textContent = '✅ 策略已保存（下次重建索引生效）';
  } catch(e) {
    msg.textContent = '保存失败：' + (e.message || e);
  }
  btn.disabled = false; btn.textContent = '保存策略';
});

async function saveMobileConfig(generate) {
  var msg = document.getElementById('setting-mobile-msg');
  try {
    var data = await window.api.fetch('/api/mobile/config', {
      method: 'POST',
      body: {
        enabled: document.getElementById('setting-mobile-enabled').checked,
        token: document.getElementById('setting-mobile-token').value.trim(),
        generate: !!generate,
      },
    });
    renderMobileConfig(data);
    if (data.token) {
      document.getElementById('setting-mobile-token').value = data.token;
      await copyText(data.token);
      msg.textContent = '✅ 新 Token 已生成并复制，请重启后端后在手机打开 /mobile 并保存';
    } else {
      msg.textContent = '✅ 手机导入配置已保存；若切换启用状态，请重启后端';
    }
  } catch(e) {
    msg.textContent = '保存失败：' + (e.message || e);
  }
}

document.getElementById('setting-mobile-save').addEventListener('click', () => saveMobileConfig(false));
document.getElementById('setting-mobile-generate').addEventListener('click', () => saveMobileConfig(true));
document.getElementById('setting-mobile-copy').addEventListener('click', function() {
  var value = document.getElementById('setting-mobile-url').value;
  if (value && value !== '未检测到 Tailscale/局域网地址') copyText(value);
});
document.getElementById('setting-mobile-pairing').addEventListener('click', async function() {
  var msg = document.getElementById('setting-mobile-msg');
  var btn = this;
  btn.disabled = true;
  btn.textContent = '生成中…';
  try {
    var data = await window.api.fetch('/api/mobile/pairing', { method: 'POST' });
    renderMobileConfig(await window.api.fetch('/api/mobile/config'));
    renderMobilePairing(data);
    if (data.url) {
      await copyText(data.url);
      msg.textContent = data.restart_required
        ? '✅ 配对链接已生成；请重启后端后再用手机扫码或打开'
        : '✅ 配对链接已生成，手机扫码或打开后会自动保存 Token';
    } else {
      msg.textContent = '未检测到 Tailscale/局域网地址，无法生成配对链接';
    }
  } catch(e) {
    msg.textContent = '生成配对链接失败：' + (e.message || e);
  } finally {
    btn.disabled = false;
    btn.textContent = '复制配对链接';
  }
});

document.getElementById('setting-mobile-copy-pair-url').addEventListener('click', function() {
  var value = document.getElementById('setting-mobile-pair-url').value;
  if (value) copyText(value);
});

document.getElementById('setting-mobile-open-url').addEventListener('click', function() {
  var value = document.getElementById('setting-mobile-url').value;
  if (value && value !== '未检测到 Tailscale/局域网地址') copyText(value);
});

document.getElementById('setting-mobile-clear').addEventListener('click', async function() {
  if (!confirm('关闭手机导入并清空 App Token？手机端保存的旧 Token 将失效。')) return;
  var msg = document.getElementById('setting-mobile-msg');
  var btn = this;
  btn.disabled = true;
  btn.textContent = '清空中…';
  try {
    var data = await window.api.fetch('/api/mobile/config', {
      method: 'POST',
      body: { enabled: false, clear_token: true },
    });
    renderMobileConfig(data);
    renderMobilePairing(null);
    msg.textContent = '✅ 手机导入已关闭，App Token 已清空；旧手机 Token 已失效';
  } catch(e) {
    msg.textContent = '清空失败：' + (e.message || e);
  } finally {
    btn.disabled = false;
    btn.textContent = '关闭并清空';
  }
});

document.getElementById('setting-mcp-save').addEventListener('click', async function() {
  var msg = document.getElementById('setting-mcp-msg');
  var password = document.getElementById('setting-mcp-admin-password').value;
  var selected = document.querySelector('input[name="setting-mcp-mode"]:checked');
  var mode = selected ? selected.value : 'basic';
  if (latestMcpRemote && latestMcpRemote.mode && latestMcpRemote.mode !== mode) {
    var previousLabel = latestMcpRemote.mode === 'advanced' ? '高级模式' : '普通模式';
    if (!confirm('切换后' + previousLabel + '的现有连接将立即暂停，但密钥和客户端不会被删除。继续？')) {
      renderMcpRemote(latestMcpRemote);
      return;
    }
  }
  var btn = this;
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    var data = await window.api.fetch('/api/mcp/remote/config', {
      method: 'POST',
      body: {
        enabled: document.getElementById('setting-mcp-enabled').checked,
        mode: mode,
        admin_password: password,
      },
    });
    document.getElementById('setting-mcp-admin-password').value = '';
    renderMcpRemote(data);
    msg.textContent = data.enabled
      ? '✅ 远程 MCP 已启用，当前为' + (data.mode === 'advanced' ? '高级模式' : '普通模式')
      : '✅ 远程 MCP 已停用，全部连接已暂停';
  } catch (e) {
    msg.textContent = '保存失败：' + (e.message || e);
  } finally {
    btn.disabled = false;
    btn.textContent = '保存远程设置';
  }
});

document.querySelectorAll('input[name="setting-mcp-mode"]').forEach(function(radio) {
  radio.addEventListener('change', function() {
    setMcpModePanel(radio.value);
    document.getElementById('setting-mcp-msg').textContent = '点击“应用设置”后切换模式。';
  });
});

document.getElementById('setting-mcp-basic-ca-save').addEventListener('click', saveMcpCertificate);

document.getElementById('setting-mcp-basic-token').addEventListener('click', async function() {
  var exists = !!(latestMcpRemote && latestMcpRemote.basic_key && latestMcpRemote.basic_key.exists);
  if (exists && !confirm('重新生成后，旧连接密钥立即失效。继续？')) return;
  var btn = this;
  btn.disabled = true;
  btn.textContent = '生成中…';
  try {
    var data = await window.api.fetch(exists ? '/api/mcp/basic-token/rotate' : '/api/mcp/basic-token', { method: 'POST' });
    showMcpTokenOnce(data, 'setting-mcp-basic-token-once');
    await loadMcpRemote();
    document.getElementById('setting-mcp-msg').textContent = '✅ 连接密钥已生成；离开本页后不会再显示';
  } catch (e) {
    document.getElementById('setting-mcp-msg').textContent = '生成失败：' + (e.message || e);
  } finally {
    btn.disabled = false;
    var currentExists = !!(latestMcpRemote && latestMcpRemote.basic_key && latestMcpRemote.basic_key.exists);
    btn.textContent = currentExists ? '重新生成连接密钥' : '生成连接密钥';
  }
});

document.getElementById('setting-mcp-audit-refresh').addEventListener('click', loadMcpAudit);

document.getElementById('setting-mcp-client-create').addEventListener('click', async function() {
  var label = document.getElementById('setting-mcp-client-label').value.trim();
  if (!label) {
    document.getElementById('setting-mcp-msg').textContent = '请先填写 Agent / 设备名称';
    return;
  }
  var btn = this;
  btn.disabled = true;
  btn.textContent = '生成中…';
  try {
    var data = await window.api.fetch('/api/mcp/clients', {
      method: 'POST',
      body: {
        label: label,
        tier: document.getElementById('setting-mcp-client-tier').value,
      },
    });
    showMcpTokenOnce(data);
    document.getElementById('setting-mcp-client-label').value = '';
    await loadMcpRemote();
    document.getElementById('setting-mcp-msg').textContent = '✅ 独立 Token 已生成；离开本页后不会再显示';
  } catch (e) {
    document.getElementById('setting-mcp-msg').textContent = '生成失败：' + (e.message || e);
  } finally {
    btn.disabled = false;
    btn.textContent = '生成独立 Token';
  }
});

document.getElementById('setting-context-create').addEventListener('click', async function() {
  var msg = document.getElementById('setting-context-msg');
  var btn = this;
  var name = document.getElementById('setting-context-name').value.trim();
  if (!name) {
    msg.textContent = '请填写端点名称';
    return;
  }
  btn.disabled = true; btn.textContent = '创建中…';
  try {
    var data = await window.api.fetch('/api/context/packs', {
      method: 'POST',
      body: {
        name: name,
        query: document.getElementById('setting-context-query').value.trim(),
        description: document.getElementById('setting-context-desc').value.trim(),
        include_memory: document.getElementById('setting-context-memory').checked,
        include_wiki: document.getElementById('setting-context-wiki').checked,
        include_documents: document.getElementById('setting-context-docs').checked,
        enabled: true,
        generate: true,
      },
    });
    if (data.pack && data.pack.token) {
      await copyText(data.pack.token);
      msg.textContent = '✅ Context Token 已生成并复制，请保存到对方 Agent 或手机 App';
    } else {
      msg.textContent = '✅ Context Pack 已创建';
    }
    document.getElementById('setting-context-name').value = '';
    document.getElementById('setting-context-query').value = '';
    document.getElementById('setting-context-desc').value = '';
    await loadContextPacks();
  } catch(e) {
    msg.textContent = '创建失败：' + (e.message || e);
  }
  btn.disabled = false; btn.textContent = '创建 Context Pack';
});

// ===== 索引处理指示器 =====
var _idxInterval = null;

function startIndexWatcher() {
  if (_idxInterval) return;
  _idxInterval = setInterval(refreshIndexBar, 3000);
  refreshIndexBar();
}

function refreshIndexBar() {
  window.api.fetch('/api/jobs').then(function(d) {
    var float = document.getElementById('indexing-float');
    var file = document.getElementById('idx-file');
    var progress = document.getElementById('idx-progress');
    if (!d.jobs || !d.jobs.length) {
      float.classList.add('hidden');
      return;
    }
    float.classList.remove('hidden');
    var processing = d.jobs.filter(function(j) { return j.state === 'processing'; });
    var queued = d.jobs.filter(function(j) { return j.state === 'queued'; });
    var total = d.jobs.length;
    var done = total - processing.length - queued.length;
    progress.textContent = processing.length + ' 处理中 · ' + queued.length + ' 排队';
    if (processing.length) {
      file.textContent = processing[0].name;
      if (processing.length > 1) file.textContent += ' …等' + total + '个文件';
    } else if (queued.length) {
      file.textContent = queued[0].name + ' …共' + total + '个文件';
    }
  }).catch(function() {});
}

init();
