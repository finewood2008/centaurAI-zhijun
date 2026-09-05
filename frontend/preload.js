const { contextBridge, ipcRenderer } = require('electron');

// 媒体预览（图片 / 视频 / 下载这类流式）仍可直接用 loopback HTTP 更稳；JSON 类请求全部走
// 主进程 → 后端子进程的 stdio 点对点通道（--stdio-rpc），不再由渲染端直连 8618。
const API_BASE = 'http://127.0.0.1:8618';

// 改动型请求带的 CSRF 头（后端 require_local 校验）；走 ASGI 中间件仍生效，需照带。
const CSRF = { 'X-Requested-By': 'centaur-vdb' };

/** 经主进程点到点通道发送一个请求帧：HTTP 4xx/5xx 时抛错（供原语义为"显式抛错"的方法）。 */
function invoke(method, uri, { body, form, file } = {}) {
  return ipcRenderer
    .invoke('rpc', { method, uri, headers: method === 'GET' ? undefined : CSRF, body, form, file })
    .then(toData);
}

/** 同 invoke，但 HTTP 4xx/5xx 只返回解析后的 body（兼容原"直接 .json() 不抛错"的方法）。 */
function invokeSoft(method, uri, { body, form, file } = {}) {
  return ipcRenderer
    .invoke('rpc', { method, uri, headers: method === 'GET' ? undefined : CSRF, body, form, file })
    .then(toDataSoft);
}

/** 把后端返回的协议帧转成业务数据（兼容旧 fetch 的抛错语义）。 */
function toData(frame) {
  if (!frame) throw new Error('后端无响应');
  if (frame.error) throw new Error(frame.error.message);
  if (frame.status >= 400) {
    let detail = '';
    try {
      detail = JSON.parse(frame.body || '{}').detail || '';
    } catch {}
    throw new Error(detail || `HTTP ${frame.status}`);
  }
  return parseBody(frame.body);
}

/** 不因 HTTP 状态码抛错，仅返回解析后的 body（通道层错误仍抛）。 */
function toDataSoft(frame) {
  if (!frame) throw new Error('后端无响应');
  if (frame.error) throw new Error(frame.error.message);
  return parseBody(frame.body);
}

function parseBody(body) {
  try {
    return JSON.parse(body);
  } catch {
    return body; // 非 JSON（纯文本等）
  }
}

/** File => base64（上传经 P2P 帧携带，主进程再按 multipart 重放）。 */
function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result || '';
      const b64 = typeof result === 'string' && result.indexOf(',') >= 0 ? result.split(',')[1] : result;
      resolve(b64);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function serializeBody(value) {
  if (value === undefined || value === null) return undefined;
  return typeof value === 'string' ? value : JSON.stringify(value);
}

contextBridge.exposeInMainWorld('api', {
  base: API_BASE,

  /** 通用 JSON 请求（自动带 CSRF、自动序列化/反序列化 body） */
  fetch: async (pathOrUrl, opts = {}) => {
    const uri = pathOrUrl;
    const headers = { ...(opts.headers || {}) };
    if (opts.method && opts.method !== 'GET') Object.assign(headers, CSRF);
    const hasBody = opts.body !== undefined && opts.body !== null;
    // 有 body 未显式指定 content-type 时补 application/json（旧行为）
    if (hasBody && !Object.keys(headers).some((k) => k.toLowerCase() === 'content-type')) {
      headers['Content-Type'] = 'application/json';
    }
    return invoke(opts.method || 'GET', uri, { ...opts, headers, body: serializeBody(opts.body) });
  },

  health: () => invokeSoft('GET', '/api/health'),

  upload: async (file) => {
    const base64 = await fileToBase64(file);
    return invoke('POST', '/api/upload', {
      file: { name: 'file', filename: file.name, type: file.type || 'application/octet-stream', base64 },
    });
  },

  // 视频上传后台索引任务状态：queued / processing / done / failed / unknown
  jobStatus: (docId) => invokeSoft('GET', `/api/jobs/${encodeURIComponent(docId)}`),

  // mode: 'text'(默认) | 'visual'(以文搜图) | 'hybrid'(合并)；tags=标签筛选(AND)
  search: (query, { mode = 'text', nResults = 8, tags = null } = {}) =>
    invokeSoft('POST', '/api/search', { body: { query, n_results: nResults, mode, tags } }),

  // ===== 用户标注（标签/重要度/置顶/备注/说明）=====
  getAnnotations: (sourcePath = null) =>
    invokeSoft('GET', '/api/annotations' + (sourcePath ? `?source_path=${encodeURIComponent(sourcePath)}` : '')),

  setAnnotation: (sourcePath, patch = {}) =>
    invoke('POST', '/api/annotations', { body: { source_path: sourcePath, merge: true, ...patch } }),

  setAnnotationsBatch: (sourcePaths, patch = {}, options = {}) =>
    invoke('POST', '/api/annotations/batch', { body: { source_paths: sourcePaths, patch, ...options } }),

  undoAudit: (auditId) => invoke('POST', `/api/audit/${auditId}/undo`, { body: undefined }),

  deleteAnnotation: (sourcePath) =>
    invokeSoft('DELETE', `/api/annotations/${encodeURIComponent(sourcePath)}`, { body: undefined }),

  // ===== 分组（文件中心）=====
  getGroups: () => invokeSoft('GET', '/api/groups'),
  createGroup: (name) => invokeSoft('POST', '/api/groups', { body: { name } }),
  renameGroup: (name, newName) => invokeSoft('POST', '/api/groups', { body: { name, new_name: newName } }),
  deleteGroup: (name) => invokeSoft('DELETE', `/api/groups/${encodeURIComponent(name)}`, { body: undefined }),

  listDocuments: (options = {}) => {
    const params = new URLSearchParams();
    Object.entries(options || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
    });
    return invokeSoft('GET', `/api/documents?${params.toString()}`);
  },

  listDocumentIds: (options = {}) => {
    const params = new URLSearchParams();
    Object.entries(options || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
    });
    return invokeSoft('GET', `/api/documents/ids?${params.toString()}`);
  },

  deleteDocument: (docId) =>
    invokeSoft('DELETE', `/api/documents/${encodeURIComponent(docId)}`, { body: undefined }),

  deleteDocumentsBatch: (sourcePaths) =>
    invoke('POST', '/api/documents/batch-delete', { body: { source_paths: sourcePaths } }),

  reindexDocuments: (sourcePaths, strategyId = null, dryRun = false) =>
    invoke('POST', '/api/documents/reindex', {
      body: { source_paths: sourcePaths, strategy_id: strategyId, force: true, dry_run: dryRun },
    }),

  listTrash: () => invokeSoft('GET', '/api/trash'),
  restoreTrash: (trashId) => invokeSoft('POST', `/api/trash/${encodeURIComponent(trashId)}/restore`, { body: undefined }),
  purgeTrash: (trashId) => invokeSoft('DELETE', `/api/trash/${encodeURIComponent(trashId)}`, { body: undefined }),
  listAudit: () => invokeSoft('GET', '/api/audit'),

  stats: () => invokeSoft('GET', '/api/stats'),
  config: () => invokeSoft('GET', '/api/config'),

  saveMcpCA: () => ipcRenderer.invoke('save-mcp-ca'),

  reindex: () => invokeSoft('POST', '/api/reindex', { body: undefined }),
});

// 阶段 2 受控连通票据桥：MindOS 前端 api.ts 经 window.__MINDOS_ACCESS__.getTicket()
// 读取宿主（App/Electron Consumer Client）经主进程投放的一次性票据，再与 MindOS 后端
// 交换为会话。登录/认领/票据创建均在主进程侧完成，前端不参与 Owner 控制面。
contextBridge.exposeInMainWorld('__MINDOS_ACCESS__', {
  getTicket: () => ipcRenderer.invoke('mindos:connectivity-ticket'),
});

// 阶段 3（WP M）：Setup 窗口受控桥。仅当窗口 URL 指向 setup.html 时暴露；
// 主窗口不获得该通道。所有调用在 main 侧经 Feature Gate 校验，未开启即拒绝。
// Renderer 只经此通道消费 ClaimCoordinatorSnapshot 派生状态，不持有任何秘密。
if (typeof window !== 'undefined' && window.location && window.location.pathname.includes('setup.html')) {
  contextBridge.exposeInMainWorld('__SETUP_ACCESS__', {
    getState: () => ipcRenderer.invoke('setup:state'),
    start: (deviceInfo) => ipcRenderer.invoke('setup:start-provisioning', deviceInfo),
    selectCandidate: (candidateId) => ipcRenderer.invoke('setup:select-candidate', candidateId),
    authenticate: () => ipcRenderer.invoke('setup:authenticate'),
    provisionWifi: (ssid, password) => ipcRenderer.invoke('setup:provision-wifi', { ssid, password }),
    appProof: () => ipcRenderer.invoke('setup:app-proof'),
    acknowledge: () => ipcRenderer.invoke('setup:acknowledge'),
    cancel: (reason) => ipcRenderer.invoke('setup:cancel', reason),
    resume: () => ipcRenderer.invoke('setup:resume'),
    onStateChanged: (callback) => {
      ipcRenderer.on('setup:state-changed', (_event, snapshot) => callback(snapshot));
    },
  });
}