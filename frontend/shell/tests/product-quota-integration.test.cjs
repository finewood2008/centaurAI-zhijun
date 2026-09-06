'use strict';
// Independent integration review: real renderer/main/adapter/bridge modules;
// only the monotonic clock and native Agent peer are isolated test fixtures.
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { createProductionAdapter } = require('../production/adapter.cjs');
const { createBusinessBridge } = require('../production/business-bridge.cjs');
const { createProductSession } = require('../runtime/product-session.cjs');

const MiB = 1024 * 1024;
const CHUNK = 524288;
const SUBJECT = { accountId: 'quota-owner', clientId: 'quota-client', deviceId: 'quota-device' };
const WORKSPACE = 'f'.repeat(64);
const response = value => ({ status: 200, headers: { 'content-type': 'application/json' }, body: Buffer.from(JSON.stringify(value)) });
const remoteError = code => Object.assign(new Error(code), { code });

function virtualClock() {
  let now = 0;
  const pending = new Map();
  function schedule(fn, ms, interval = false) {
    const token = { unref() {} };
    pending.set(token, { fn, at: now + Math.max(0, ms), every: interval ? ms : null });
    return token;
  }
  const timers = {
    setTimeout: (fn, ms) => schedule(fn, ms), clearTimeout: token => pending.delete(token),
    setInterval: (fn, ms) => schedule(fn, ms, true), clearInterval: token => pending.delete(token),
  };
  async function run(work, maximum = 900000) {
    let settled = false, value, error;
    Promise.resolve(work).then(result => { settled = true; value = result; }, caught => { settled = true; error = caught; });
    const deadline = now + maximum;
    while (!settled) {
      await new Promise(resolve => setImmediate(resolve));
      if (settled) break;
      const next = Math.min(...[...pending.values()].map(item => item.at));
      assert.ok(Number.isFinite(next) && next <= deadline, 'virtual task did not settle within its bounded deadline');
      now = next;
      for (const [token, item] of [...pending]) if (item.at <= now) {
        if (item.every === null) pending.delete(token); else item.at = now + item.every;
        item.fn();
      }
    }
    if (error) throw error;
    return value;
  }
  return { timers, run, now: () => now, wall: () => 1800000000000 + now, pending };
}

function agentPeer(clock, options = {}) {
  const calls = [], uploads = new Map(), jobs = new Map();
  const held = [];
  let holding = Boolean(options.holdMutation);
  let requestIds = options.initialRequestIds || 0, attempts = 0, windowStart = 0, windowRequests = 0, payloadBytes = 0, rejected = 0;
  let nextUpload = 100, nextJob = 10000;
  const counts = { chunks: 0, starts: 0, polls: 0, uploadCreates: 0, cleanup: 0 };
  const json = value => {
    const result = response(value);
    if (payloadBytes + result.body.length > 1024 ** 3) throw remoteError('SESSION_RESOURCE_EXHAUSTED');
    payloadBytes += result.body.length;
    return result;
  };
  function uploadProjection(upload) {
    return { id: upload.id, state: upload.state, size: upload.size, received: upload.received,
      nextIndex: upload.index, ...(upload.sha256 ? { sha256: upload.sha256 } : {}) };
  }
  const sdk = {
    async request(req) {
      attempts++;
      if (requestIds >= 1024) throw remoteError('SDK_REQUEST_LIMIT_REACHED');
      requestIds++;
      if (clock.now() - windowStart >= 60000) { windowStart = clock.now(); windowRequests = 0; }
      // Keep fixture diagnostics bounded too; retain exact small operation DTOs
      // but count large upload bytes without keeping 200 MiB in the test log.
      calls.push({ at: clock.now(), path: req.relative_path, method: req.method,
        body: req.body && req.body.length <= 4096 ? Buffer.from(req.body) : null,
        bodyBytes: req.body?.length || 0 });
      if (windowRequests >= 120) { rejected++; throw remoteError('TOO_MANY_REQUESTS'); }
      windowRequests++;
      if (options.rejectBeforeDispatch?.(req, requestIds)) { rejected++; throw remoteError('TOO_MANY_REQUESTS'); }
      const bytes = req.body?.length || 0;
      if (payloadBytes + bytes > 1024 ** 3) throw remoteError('SESSION_RESOURCE_EXHAUSTED');
      payloadBytes += bytes;
      const url = new URL(req.relative_path, 'http://fixture');
      const body = req.body ? JSON.parse(Buffer.from(req.body).toString()) : null;
      if (url.pathname.endsWith('/context')) return json({ version: 2, ...SUBJECT,
        applicationId: 'zhijun-desktop', workspaceId: WORKSPACE,
        capabilities: ['product.rpc', 'product.events', 'product.uploads', 'product.blobs'],
        expiresAt: Math.floor(clock.wall() / 1000) + 5 });
      if (url.pathname.endsWith('/uploads') && req.method === 'POST') {
        counts.uploadCreates++;
        const id = (nextUpload++).toString(16).padStart(32, '0');
        const upload = { id, size: body.size, state: 'open', received: 0, index: 0, hash: crypto.createHash('sha256') };
        uploads.set(id, upload); return json(uploadProjection(upload));
      }
      const uploadPath = url.pathname.match(/\/uploads\/([a-f0-9]{32})(?:\/(chunks|complete))?$/);
      if (uploadPath) {
        const upload = uploads.get(uploadPath[1]); assert.ok(upload, 'unknown upload');
        if (uploadPath[2] === 'chunks') {
          const raw = Buffer.from(body.data, 'base64');
          assert.equal(crypto.createHash('sha256').update(raw).digest('hex'), body.sha256);
          if (body.index === upload.index) {
            upload.hash.update(raw); upload.received += raw.length; upload.index++;
            upload.lastSha = body.sha256; counts.chunks++;
          } else assert.ok(body.index === upload.index - 1 && body.sha256 === upload.lastSha, 'non-idempotent chunk replay');
        } else if (uploadPath[2] === 'complete') {
          assert.equal(upload.received, upload.size);
          assert.equal(body.sha256, upload.hash.copy().digest('hex'));
          upload.sha256 = body.sha256; upload.state = 'complete';
        } else if (req.method === 'DELETE') { upload.state = 'cancelled'; counts.cleanup++; }
        return json(uploadProjection(upload));
      }
      if (url.pathname.endsWith('/operations') && req.method === 'POST') {
        counts.starts++;
        const id = (nextJob++).toString(16).padStart(32, '0'); jobs.set(id, body);
        if (options.unknownMutation) throw remoteError(typeof options.unknownMutation === 'string' ? options.unknownMutation : 'SDK_REQUEST_TIMEOUT');
        if (holding) return new Promise(resolve => held.push(() => resolve(json({ id, state: 'queued', cursor: 0 }))));
        if (options.exhaustResponsePayload) payloadBytes = 1024 ** 3 - 1;
        return json({ id, state: 'queued', cursor: 0 });
      }
      const jobPath = url.pathname.match(/\/operations\/([a-f0-9]{32})(\/cancel)?$/);
      if (jobPath) {
        const id = jobPath[1], job = jobs.get(id); assert.ok(job, 'unknown job');
        if (jobPath[2]) return json({ id, state: 'cancelled', cancelRequested: true });
        counts.polls++;
        const after = Number(url.searchParams.get('after'));
        if (job.operationId === 'post_api_mindos_conversations_conversation_id_messages') {
          const seq = after + 1, last = (options.tokens || 240) + 2;
          const event = seq === 1 ? { seq, kind: 'headers', status: 200, headers: { 'content-type': 'text/event-stream' } }
            : seq === last ? { seq, kind: 'end' }
            : { seq, kind: 'chunk', data: Buffer.from('event: token\ndata: {"text":"x"}\n\n').toString('base64') };
          return json({ id, state: seq === last ? 'succeeded' : 'running', cursor: seq, events: [event], hasMore: false });
        }
        return json({ id, state: 'succeeded', cursor: 3, hasMore: false, events: [
          { seq: 1, kind: 'headers', status: 200, headers: { 'content-type': 'application/json' } },
          { seq: 2, kind: 'chunk', data: Buffer.from('{"accepted":true}').toString('base64') }, { seq: 3, kind: 'end' },
        ] });
      }
      throw new Error('unexpected fixture route: ' + req.relative_path);
    },
  };
  return { sdk, calls, counts, uploads,
    releaseHeld() { holding = false; for (const release of held.splice(0)) release(); },
    stats: () => ({ requestIds, attempts, payloadBytes, rejected, windowRequests }) };
}

async function stack(options = {}) {
  const clock = virtualClock(), peer = agentPeer(clock, options), failures = [];
  const bridge = createBusinessBridge({ clock: clock.wall, activityClock: clock.now, timers: clock.timers,
    schedulerOptions: options.schedulerOptions });
  const adapter = await createProductionAdapter({ config: { connectivity: { applicationId: 'zhijun-desktop' } },
    consumer: { current: async () => SUBJECT }, bridge,
    runtimeFactory: async () => ({ connect: async () => peer.sdk, close: async () => {} }) });
  const connected = await adapter.connect(SUBJECT);
  connected.onFailure(error => failures.push(error.code));
  await clock.run(connected.authorize());
  const budget = { active: new Set() };
  const session = createProductSession({ session: connected, isCurrent: () => true, budget, onTerminal: error => failures.push(error.code) });
  return { clock, peer, connected, session, failures, budget,
    async close() { session.close(); await connected.close(); },
  };
}

function request(operationId, body = null, params = {}) {
  return { version: 1, requestId: crypto.randomUUID(), operationId, params, query: {}, body };
}

function renderer(fixture) {
  const src = path.resolve(__dirname, '../../mindos-web/src');
  const ts = createRequire(path.join(src, 'review.cjs'))('typescript'), cache = new Map();
  function load(filename) {
    if (!path.extname(filename)) filename += '.ts';
    if (filename.endsWith('.json')) return JSON.parse(fs.readFileSync(filename, 'utf8'));
    if (cache.has(filename)) return cache.get(filename).exports;
    const module = { exports: {} }; cache.set(filename, module);
    const native = createRequire(filename);
    const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true,
    } }).outputText;
    new Function('require', 'module', 'exports', code)(name => name.startsWith('.') ? load(path.resolve(path.dirname(filename), name))
      : name.startsWith('@/') ? load(path.join(src, name.slice(2))) : native(name), module, module.exports);
    return module.exports;
  }
  const scope = load(path.join(src, 'shared/productScope.ts'));
  scope.enableDesktopProduct(); scope.setProductScope(WORKSPACE);
  const product = Object.fromEntries(['start', 'poll', 'cancel', 'uploadCreate', 'uploadChunk', 'uploadComplete', 'uploadCancel'].map(method => [method,
    async (_context, input) => {
      try { return { ok: true, data: await fixture.session.invoke(method, input), generation: 7 }; }
      catch (error) { return { ok: false, error: { code: error.code, message: error.message }, generation: 7 }; }
    }]));
  return load(path.join(src, 'desktop/productClient.ts')).createDesktopProductClient(product, () => ({ generation: 7, workspaceId: WORKSPACE }));
}

test('200 MiB upload traverses the production stack within native Agent quotas', async t => {
  const f = await stack();
  const startedAt = f.clock.now();
  try {
    await f.clock.run((async () => {
      const created = await f.session.invoke('uploadCreate', { requestId: 'quota-upload-001', fileName: 'synthetic.bin', contentType: 'application/octet-stream', size: 200 * MiB });
      const bytes = new Uint8Array(CHUNK);
      for (let index = 0; index < 400; index++) await f.session.invoke('uploadChunk', { id: created.id, index, bytes });
      const complete = await f.session.invoke('uploadComplete', { id: created.id });
      assert.equal(complete.state, 'complete'); assert.equal(complete.received, 200 * MiB);
      const started = await f.session.invoke('start', request('post_api_mindos_uploads', { kind: 'multipart', fields: {}, files: [{ field: 'file', uploadId: created.id }] }));
      assert.equal((await f.session.invoke('poll', { id: started.id, after: 0, waitMs: 8000 })).state, 'succeeded');
      await f.session.invoke('uploadCancel', { id: created.id });
    })());
    assert.equal(f.peer.counts.chunks, 400);
    assert.equal(f.peer.counts.starts, 1);
    assert.equal(f.peer.stats().rejected, 0);
    assert.ok(f.peer.stats().requestIds < 1024 && f.peer.stats().payloadBytes < 1024 ** 3);
    assert.deepEqual(f.failures, []);
    const elapsed = f.clock.now() - startedAt;
    assert.ok(elapsed < 600000, `full upload exceeded 600 seconds: ${elapsed} ms`);
    const rollingMaximum = Math.max(...f.peer.calls.map(first =>
      f.peer.calls.filter(call => call.at >= first.at && call.at <= first.at + 60000).length));
    assert.ok(rollingMaximum <= 101, `rolling 60-second request maximum was ${rollingMaximum}`);
    t.diagnostic(`200 MiB complete chain: ${elapsed} virtual ms; rolling 60-second maximum: ${rollingMaximum} native attempts`);
  } finally { await f.close(); }
});

test('real renderer long SSE survives more than 120 poll pages without replaying its mutation', async () => {
  const f = await stack(), client = renderer(f);
  try {
    const text = await f.clock.run((async () => (await client.request('/api/mindos/conversations/c_test/messages', {
      method: 'POST', body: JSON.stringify({ content: 'synthetic quota question' }),
    })).text())());
    assert.equal(text.match(/event: token/g).length, 240);
    assert.equal(f.peer.counts.polls, 242);
    assert.equal(f.peer.counts.starts, 1);
    assert.equal(f.peer.stats().rejected, 0);
    assert.deepEqual(f.failures, []);
  } finally { client.dispose(); await f.close(); }
});

test('unknown business mutation result is never automatically replayed', async () => {
  const f = await stack({ unknownMutation: true });
  try {
    await assert.rejects(f.clock.run(f.session.invoke('start', request('post_api_mindos_conversations', { title: 'synthetic' }))),
      error => error.code === 'WRITE_OUTCOME_UNKNOWN');
    assert.equal(f.peer.counts.starts, 1);
    assert.equal(f.peer.calls.filter(call => call.path.endsWith('/operations')).length, 1);
  } finally { await f.close(); }
});

test('a proven pre-dispatch rate rejection retries identical request bytes only within budget', async () => {
  let refused = false;
  const f = await stack({ rejectBeforeDispatch: req => {
    if (!refused && req.relative_path.endsWith('/operations')) { refused = true; return true; }
    return false;
  } });
  try {
    await f.clock.run(f.session.invoke('start', request('post_api_mindos_conversations', { title: 'synthetic' })));
    const sent = f.peer.calls.filter(call => call.path.endsWith('/operations'));
    assert.equal(sent.length, 2); assert.deepEqual(sent[0].body, sent[1].body);
    assert.equal(f.peer.counts.starts, 1);
    assert.equal(f.peer.stats().rejected, 1);
    assert.equal(f.peer.stats().requestIds, 3); // Handshake plus both physical attempts.
    assert.ok(sent[1].at - sent[0].at >= 1200);
  } finally { await f.close(); }
});

test('insufficient remaining session budget rejects a large upload before uploadCreate dispatch', async () => {
  const f = await stack();
  try {
    await f.clock.run((async () => {
      for (let index = 0; index < 600; index++) await f.connected.request({ method: 'GET', path: '/api/mindos/zhijun/context', headers: { Accept: 'application/json' } });
      await assert.rejects(f.session.invoke('uploadCreate', { requestId: 'quota-upload-exhausted', fileName: 'synthetic.bin', contentType: 'application/octet-stream', size: 200 * MiB }),
        error => error.code === 'SESSION_QUOTA_EXHAUSTED');
    })());
    assert.equal(f.peer.counts.uploadCreates, 0);
    assert.equal(f.peer.stats().rejected, 0);
  } finally { await f.close(); }
});

test('another queued request exhausting quota cannot mark an already dispatched mutation as unsent', async () => {
  const f = await stack({ holdMutation: true, schedulerOptions: { maxRequests: 2 } });
  try {
    const results = await f.clock.run(Promise.allSettled([
      f.session.invoke('start', request('post_api_mindos_conversations', { title: 'already dispatched' })),
      f.session.invoke('start', request('post_api_mindos_conversations', { title: 'never dispatched' })),
    ]));
    assert.equal(results[0].status, 'rejected');
    assert.equal(results[0].reason.code, 'WRITE_OUTCOME_UNKNOWN');
    assert.equal(results[1].status, 'rejected');
    assert.equal(results[1].reason.code, 'SESSION_QUOTA_EXHAUSTED');
    assert.equal(f.peer.counts.starts, 1);
  } finally { await f.close(); }
});

test('upload reservation counts encoded application bytes before sending any part of the file', async () => {
  const f = await stack({ schedulerOptions: { maxBytes: 10 * MiB } });
  try {
    await assert.rejects(f.clock.run(f.session.invoke('uploadCreate', {
      requestId: 'quota-encoded-bytes', fileName: 'synthetic.bin', contentType: 'application/octet-stream', size: 8 * MiB,
    })), error => error.code === 'SESSION_QUOTA_EXHAUSTED');
    assert.equal(f.peer.counts.uploadCreates, 0);
    assert.equal(f.peer.stats().requestIds, 1);
  } finally { await f.close(); }
});

test('remote payload exhaustion after a mutating handler runs remains an unknown write outcome', async () => {
  const f = await stack({ exhaustResponsePayload: true });
  try {
    await assert.rejects(f.clock.run(f.session.invoke('start', request('post_api_mindos_conversations', { title: 'synthetic' }))),
      error => error.code === 'WRITE_OUTCOME_UNKNOWN');
    assert.equal(f.peer.counts.starts, 1);
    assert.equal(f.peer.calls.filter(call => call.path.endsWith('/operations')).length, 1);
    assert.equal(f.peer.stats().payloadBytes, 1024 ** 3 - 1);
  } finally { await f.close(); }
});

test('heartbeat obtains its reserved native slot while seven business requests are still running', async () => {
  const f = await stack({ holdMutation: true });
  const held = Promise.allSettled(Array.from({ length: 7 }, (_, index) =>
    f.session.invoke('start', request('post_api_mindos_conversations', { title: `held-${index}` }))));
  try {
    await f.clock.run(new Promise(resolve => f.clock.timers.setTimeout(resolve, 11000)));
    assert.equal(f.peer.counts.starts, 7);
    const heartbeats = f.peer.calls.filter(call => call.path.endsWith('/context'));
    assert.equal(heartbeats.length, 2);
    assert.ok(heartbeats[1].at >= 10000 && heartbeats[1].at < 11000);
    assert.deepEqual(f.failures, []);
    assert.equal(f.peer.stats().rejected, 0);
  } finally { await f.close(); await held; }
});

test('closing a generation removes queued writes before their native dispatch slot', async () => {
  const f = await stack();
  try {
    const pending = f.session.invoke('start', request('post_api_mindos_conversations', { title: 'queued synthetic' }));
    const result = Promise.allSettled([pending]);
    await new Promise(resolve => setImmediate(resolve)); // Enqueued, but the 600 ms slot has not arrived.
    f.session.close();
    assert.equal((await result)[0].reason.code, 'STALE_GENERATION');
    await f.clock.run(new Promise(resolve => f.clock.timers.setTimeout(resolve, 5000)));
    assert.equal(f.peer.counts.starts, 0);
    assert.equal(f.peer.stats().requestIds, 1);
  } finally { await f.close(); }
});

test('default main budget terminates before the native 1024 request-ID limit and never auto-reconnects', async () => {
  const f = await stack();
  try {
    const read = () => f.connected.request({ method: 'GET', path: '/api/mindos/zhijun/context', headers: { Accept: 'application/json' } });
    await assert.rejects(f.clock.run((async () => {
      for (let index = 0; index < 1024; index++) await read();
    })()), error => error.code === 'SESSION_QUOTA_EXHAUSTED');
    assert.equal(f.peer.stats().requestIds, 1000);
    assert.equal(f.peer.stats().rejected, 0);
    const attempts = f.peer.stats().attempts;
    await assert.rejects(read(), error => error.code === 'SESSION_QUOTA_EXHAUSTED');
    assert.equal(f.peer.stats().attempts, attempts);
  } finally { await f.close(); }
});

test('the SDK hard limit at exactly 1024 IDs is terminal and not a retryable rate error', async () => {
  const f = await stack({ initialRequestIds: 1023 });
  try {
    const read = () => f.connected.request({ method: 'GET', path: '/api/mindos/zhijun/context', headers: { Accept: 'application/json' } });
    await assert.rejects(f.clock.run(read()), error => error.code === 'SESSION_QUOTA_EXHAUSTED');
    assert.equal(f.peer.stats().requestIds, 1024);
    assert.equal(f.peer.stats().attempts, 2); // One successful authorize, then SDK refusal before network dispatch.
    await assert.rejects(read(), error => error.code === 'SESSION_QUOTA_EXHAUSTED');
    assert.equal(f.peer.stats().attempts, 2);
  } finally { await f.close(); }
});

test('closing while initial context is rate-limited cancels its queued retry', async () => {
  const clock = virtualClock(), peer = agentPeer(clock, { rejectBeforeDispatch: () => true });
  const adapter = await createProductionAdapter({ config: { connectivity: { applicationId: 'zhijun-desktop' } },
    consumer: { current: async () => SUBJECT },
    bridge: createBusinessBridge({ clock: clock.wall, activityClock: clock.now, timers: clock.timers }),
    runtimeFactory: async () => ({ connect: async () => peer.sdk, close: async () => {} }) });
  const connected = await adapter.connect(SUBJECT);
  const result = Promise.allSettled([connected.authorize()]);
  try {
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(peer.stats().attempts, 1);
    await connected.close();
    assert.equal((await result)[0].status, 'rejected');
    await clock.run(new Promise(resolve => clock.timers.setTimeout(resolve, 5000)));
    assert.equal(peer.stats().attempts, 1);
  } finally { await connected.close(); }
});

test('a competing read cannot close an accepted upload merely because its remaining budget is reserved', async () => {
  const f = await stack({ schedulerOptions: { maxRequests: 40 } });
  try {
    await f.clock.run((async () => {
      const created = await f.session.invoke('uploadCreate', { requestId: 'quota-reserved-file', fileName: 'synthetic.bin', contentType: 'application/octet-stream', size: 3 * CHUNK });
      const read = () => f.connected.request({ method: 'GET', path: `/api/mindos/zhijun/uploads/${created.id}`, headers: { Accept: 'application/json' } });
      let blocked = false;
      for (let index = 0; index <= 32; index++) {
        try { await read(); }
        catch (error) { assert.equal(error.code, 'RESOURCE_EXHAUSTED'); assert.ok(error.definitelyNotSent); blocked = true; break; }
      }
      assert.ok(blocked, 'normal reads must not consume the admitted upload reservation');
      for (let index = 0; index < 3; index++) await f.session.invoke('uploadChunk', { id: created.id, index, bytes: new Uint8Array(CHUNK) });
      assert.equal((await f.session.invoke('uploadComplete', { id: created.id })).state, 'complete');
      await f.session.invoke('uploadCancel', { id: created.id });
    })());
    assert.equal(f.peer.counts.chunks, 3);
    assert.ok(f.peer.stats().requestIds <= 40);
    assert.deepEqual(f.failures, []);
  } finally { await f.close(); }
});

test('generation cancellation keeps native work in the shared capacity budget until actual settlement', async () => {
  const f = await stack({ holdMutation: true });
  const old = Promise.allSettled(Array.from({ length: 8 }, (_, index) =>
    f.session.invoke('start', request('post_api_mindos_conversations', { title: `old-${index}` }))));
  let replacement;
  try {
    await f.clock.run(new Promise(resolve => f.clock.timers.setTimeout(resolve, 5000)));
    assert.equal(f.peer.counts.starts, 7); // The eighth is queued, preserving the heartbeat slot.
    assert.equal(f.budget.active.size, 8);
    f.session.close();
    await old;
    assert.equal(f.budget.active.size, 7, 'cancelled public delivery must not retire pending native work');
    replacement = createProductSession({ session: f.connected, isCurrent: () => true, budget: f.budget });
    const next = replacement.invoke('start', request('post_api_mindos_conversations', { title: 'new queued' }));
    const guarded = next.catch(error => { throw error; });
    await assert.rejects(replacement.invoke('start', request('post_api_mindos_conversations', { title: 'over capacity' })), error => error.code === 'RESOURCE_EXHAUSTED');
    assert.equal(f.peer.counts.starts, 7);
    f.peer.releaseHeld();
    assert.equal((await f.clock.run(guarded)).state, 'queued');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.budget.active.size, 0);
    assert.equal(f.peer.counts.starts, 8);
  } finally { f.peer.releaseHeld(); replacement?.close(); await f.close(); await old; }
});
