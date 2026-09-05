'use strict';

const { DesktopError } = require('./public-error.cjs');

/** Explicitly synthetic: no SDK, credentials, network or filesystem access. */
function createSimulationAdapter({ delayMs = 15 } = {}) {
  const accountId = 'synthetic-account';
  const devices = ['a', 'b'].map((letter) => ({
    deviceId: `synthetic-box-${letter}`,
    displayName: `模拟盒子 ${letter.toUpperCase()}`,
    availability: 'online',
  }));
  const sessions = new Set();
  return {
    async signIn() { return { accountId }; },
    async listDevices() { return devices.map((device) => ({ ...device })); },
    async connect(binding) {
      if (binding.accountId !== accountId || !devices.some((d) => d.deviceId === binding.deviceId)) {
        throw new DesktopError('ACCESS_DENIED');
      }
      let closed = false;
      const timers = new Map();
      const rows = Array.from({ length: 47 }, (_, index) => ({
        materialId: `${binding.deviceId}-material-${String(index + 1).padStart(3, '0')}`,
        fileName: `模拟资料 ${String(index + 1).padStart(3, '0')}`,
        fileType: ['document', 'image', 'audio'][index % 3],
        status: ['uploaded', 'queued', 'processing', 'available', 'failed'][index % 5],
        createdAt: '2026-09-06T00:00:00Z',
        folderId: 'synthetic-private-folder',
        folder: '模拟私有目录',
        previewUrl: '/synthetic-private-preview',
      }));
      const session = {
        async authorize() {
          if (closed) throw new DesktopError('SESSION_EXPIRED');
          return { ...binding };
        },
        request(request) {
          if (closed) return Promise.reject(new DesktopError('SESSION_EXPIRED'));
          return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
              timers.delete(timer);
              try {
                const url = new URL(request.path, 'https://synthetic.invalid');
                if (request.method !== 'GET' || url.pathname !== '/api/mindos/materials') {
                  throw new DesktopError('OPERATION_NOT_ALLOWED');
                }
                const query = url.searchParams;
                const limit = Number(query.get('limit'));
                const offset = Number(query.get('offset'));
                const filtered = rows.filter((row) =>
                  (!query.has('keyword') || row.fileName.includes(query.get('keyword')))
                  && (!query.has('type') || row.fileType === query.get('type'))
                  && (!query.has('status') || row.status === query.get('status')));
                const items = filtered.slice(offset, offset + limit);
                const body = { items, total: filtered.length, limit, offset,
                  hasMore: offset + items.length < filtered.length,
                  folders: [{ folderId: 'synthetic-private-folder', name: '模拟私有目录' }] };
                resolve({ status: 200, headers: { 'content-type': 'application/json' },
                  body: new TextEncoder().encode(JSON.stringify(body)) });
              } catch (error) { reject(error); }
            }, delayMs);
            timers.set(timer, reject);
          });
        },
        async close() {
          closed = true;
          for (const [timer, reject] of timers) {
            clearTimeout(timer);
            reject(new DesktopError('SESSION_EXPIRED'));
          }
          timers.clear();
          sessions.delete(session);
        },
      };
      sessions.add(session);
      return session;
    },
    async signOut() { await Promise.all([...sessions].map((session) => session.close())); },
  };
}

module.exports = { createSimulationAdapter };
