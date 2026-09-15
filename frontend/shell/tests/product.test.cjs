'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { createProductSession } = require('../runtime/product-session.cjs');
const P = require('../runtime/product-policy.cjs');
const { createBusinessBridge } = require('../production/business-bridge.cjs');
const { createProductionAdapter } = require('../production/adapter.cjs');
const { createDesktopRuntime } = require('../runtime/desktop-runtime.cjs');
const { DesktopError } = require('../runtime/public-error.cjs');
const id = 'a'.repeat(32), uploadId = 'b'.repeat(32), blobId = 'c'.repeat(32);
const sha = data => crypto.createHash('sha256').update(data).digest('hex');
const response = data => ({ status: 200, headers: { 'content-type': 'application/json' }, body: Buffer.from(JSON.stringify(data)) });
const request = (operationId = 'get_api_mindos_zhijun_home', extra = {}) => ({ version: 1, requestId: 'action-0001', operationId, params: {}, query: {}, body: null, ...extra });
const upload = extra => ({ id: uploadId, state: 'open', size: 3, received: 0, nextIndex: 0, ...extra });
const started = { id, state: 'queued', cursor: 0 };
const tick = () => new Promise(resolve => setImmediate(resolve));
const make = (callback, extra = {}) => createProductSession({ session: { request: async req => response(await callback(req)) }, isCurrent: () => true, ...extra });
const jobPage = (events, extra = {}) => ({ id, state: 'succeeded', events, cursor: events.at(-1)?.seq || 0, hasMore: false, ...extra });
const bytesEvent = (bytes, seq = 2) => ({ seq, kind: 'chunk', data: Buffer.from(bytes).toString('base64') });
const headerEvent = (status = 200, type = 'application/json') => ({ seq: 1, kind: 'headers', status, headers: { 'content-type': type } });

test('catalog policy rejects forged route/header keys, unknown operation/query, traversal and invalid multipart', () => {
  const good = request(); assert.equal(P.operationRequest(good).operation.method, 'GET');
  for (const value of [ { ...good, path: '/etc/passwd' }, { ...good, headers: {} }, { ...good, operationId: 'shell.exec' },
    { ...good, params: { other: 'x' } }, { ...good, query: { access_token: 'x' } }, { ...good, body: {} },
    request('get_api_mindos_materials_material_id_file', { params: { materialId: '../secret' } }),
    request('get_api_mindos_materials_material_id_file', { params: { materialId: 'a%2fb' } }),
    request('post_api_mindos_uploads', { body: { kind: 'multipart', fields: {}, files: [{ field: 'file', uploadId }] } }),
  ]) assert.throws(() => P.operationRequest(value, new Map()), DesktopError);
  const recursive = {}; recursive.self = recursive;
  assert.throws(() => P.jsonBytes(recursive), { code: 'INVALID_REQUEST' });
  assert.throws(() => P.jsonBytes({ text: 'x'.repeat(P.LIMITS.request) }), { code: 'INVALID_REQUEST' });
});

test('gateway wire routes are canonical and do not accept caller identity headers', () => {
  const wire = path => ({ method: 'GET', relative_path: path, headers: { Accept: ['application/json'] } });
  assert.equal(P.validateWireRequest(wire(`${P.BASE}/operations/${id}?after=0&waitMs=8000`)).method, 'GET');
  assert.equal(P.validateWireRequest(wire(`${P.BASE}/blobs/${id}?offset=0&limit=524288`)).method, 'GET');
  for (const path of [`${P.BASE}/operations/${id}?after=00&waitMs=8`, `${P.BASE}/operations/${id}?waitMs=8&after=0`,
    `${P.BASE}/operations/${id}?after=0&waitMs=8001`, `${P.BASE}/blobs/${id}?offset=0&limit=524289`, `${P.BASE}/operations/${id}?after=0&waitMs=0&after=1`]) assert.throws(() => P.validateWireRequest(wire(path)), DesktopError);
  assert.throws(() => P.validateWireRequest({ ...wire(`${P.BASE}/context`), headers: { Accept: ['application/json'], Authorization: ['secret'] } }), DesktopError);
});

test('v2 authorization requires independent app, workspace and capabilities and transports real body bytes', async () => {
  const subject = { accountId: 'account-test', clientId: 'client-test', deviceId: 'device-test' };
  const ctx = { version: 2, ...subject, applicationId: 'zhijun-desktop', workspaceId: 'f'.repeat(64),
    capabilities: ['product.rpc', 'product.events', 'product.uploads', 'product.blobs'], expiresAt: 1005 };
  const requests = [];
  const adapter = await createProductionAdapter({ config: { connectivity: { applicationId: 'zhijun-desktop' } },
    consumer: { current: async () => subject }, bridge: createBusinessBridge({ clock: () => 1000000 }),
    runtimeFactory: async () => ({ connect: async () => ({ request: async req => { requests.push(req); return response(ctx); } }), close: async () => {} }) });
  const session = await adapter.connect(subject);
  const authorized = await session.authorize(); assert.equal(authorized.workspaceId, ctx.workspaceId);
  const mutation = P.request('POST', '/operations', request('post_api_mindos_zhijun_onboarding', { body: { accepted: true } }));
  await session.request(mutation);
  assert.equal(requests[0].relative_path, '/api/mindos/zhijun/context');
  assert.deepEqual(Buffer.from(requests[1].body), mutation.body); assert.deepEqual(requests[1].headers['Content-Type'], ['application/json']);
  await session.close();
  for (const value of [{ ...ctx, workspaceId: '../root' }, { ...ctx, capabilities: ['materials.read'] }, { ...ctx, version: 1 }, { ...ctx, accountId: 'other' }]) {
    await assert.rejects(createBusinessBridge({ clock: () => 1000000 }).authorize({ applicationId: 'zhijun-desktop', subject,
      session: { request: async () => response(value) } }), DesktopError);
  }
});

test('domain HTTP 409 and JSON/SSE bytes remain intact without replaying writes', async () => {
  let sends = 0;
  const payload = Buffer.from('{"code":"PREVIEW_STALE","nextPreview":{"revision":2}}');
  const manager = make(req => {
    if (req.method === 'POST') { sends++; assert.deepEqual(JSON.parse(req.body).body, { accepted: true }); return started; }
    return jobPage([headerEvent(409), bytesEvent(payload), { seq: 3, kind: 'end' }]);
  });
  await manager.invoke('start', request('post_api_mindos_zhijun_onboarding', { body: { accepted: true } }));
  const page = await manager.invoke('poll', { id, after: 0, waitMs: 8000 });
  assert.equal(page.events[0].status, 409); assert.deepEqual(Buffer.from(page.events[1].data), payload); assert.equal(sends, 1);
  manager.close();
});

test('eleven completed jobs admit three genuinely concurrent reads without increasing the twelve active-job limit', async t => {
  let dispatched = 0, holdStarts = false;
  const finishes = [];
  const manager = make(req => {
    if (req.path.endsWith('/operations')) {
      const result = { id: (++dispatched).toString(16).padStart(32, '0'), state: 'queued', cursor: 0 };
      return holdStarts ? new Promise(resolve => finishes.push(() => resolve(result))) : result;
    }
    const jobId = req.path.split('/operations/')[1].split('?')[0];
    return jobPage([headerEvent(), bytesEvent('{}'), { seq: 3, kind: 'end' }], { id: jobId });
  });
  t.after(() => manager.close());
  for (let i = 0; i < 11; i++) {
    const job = await manager.invoke('start', request(undefined, { requestId: `history-${i}-read` }));
    await manager.invoke('poll', { id: job.id, after: 0, waitMs: 0 });
  }
  holdStarts = true;
  const simultaneous = Promise.allSettled(Array.from({ length: 3 }, (_, i) =>
    manager.invoke('start', request(undefined, { requestId: `ontology-${i}-read` }))));
  await tick();
  assert.equal(finishes.length, 3, 'terminal history must yield space while the earlier starts are still pending');
  assert.equal(dispatched, 14);
  finishes.splice(0).forEach(finish => finish());
  const results = await simultaneous;
  assert.deepEqual(results.map(result => result.status), ['fulfilled', 'fulfilled', 'fulfilled']);
  holdStarts = false;
  // Three newly admitted jobs are still active. Only nine more may start.
  for (let i = 0; i < 9; i++) await manager.invoke('start', request(undefined, { requestId: `active-${i}-read` }));
  const before = dispatched;
  await assert.rejects(manager.invoke('start', request(undefined, { requestId: 'active-over-limit' })), { code: 'RESOURCE_EXHAUSTED' });
  assert.equal(dispatched, before, 'genuine active capacity rejects before transport dispatch');
});

test('requested cancellation and an unknown cancel outcome do not free an active job or replay its mutation', async t => {
  let dispatched = 0, cancelCalls = 0;
  const manager = make(req => {
    if (req.path.endsWith('/cancel')) {
      cancelCalls++;
      if (cancelCalls === 2) throw new DesktopError('TRANSPORT_UNAVAILABLE');
      const jobId = req.path.split('/operations/')[1].split('/')[0];
      return { id: jobId, state: cancelCalls === 1 ? 'running' : 'cancelled', cancelRequested: true };
    }
    return { id: (++dispatched).toString(16).padStart(32, '0'), state: 'queued', cursor: 0 };
  });
  t.after(() => manager.close());
  let first;
  for (let i = 0; i < 12; i++) {
    const job = await manager.invoke('start', request('post_api_mindos_zhijun_onboarding', { requestId: `active-write-${i}`, body: {} }));
    first ??= job;
  }
  const pendingCancel = await manager.invoke('cancel', { id: first.id, requestId: 'cancel-running' });
  assert.equal(pendingCancel.state, 'running');
  await assert.rejects(manager.invoke('start', request(undefined, { requestId: 'after-cancel-request' })), { code: 'RESOURCE_EXHAUSTED' });
  await assert.rejects(manager.invoke('cancel', { id: first.id, requestId: 'cancel-unknown' }), { code: 'WRITE_OUTCOME_UNKNOWN' });
  await assert.rejects(manager.invoke('start', request(undefined, { requestId: 'after-cancel-unknown' })), { code: 'RESOURCE_EXHAUSTED' });
  assert.equal(dispatched, 12); assert.equal(cancelCalls, 2, 'unknown cancellation is never replayed');
  assert.equal((await manager.invoke('cancel', { id: first.id, requestId: 'cancel-confirmed' })).state, 'cancelled');
  await manager.invoke('start', request(undefined, { requestId: 'after-confirmed-cancel' }));
  assert.equal(dispatched, 13, 'confirmed terminal state can yield one place');
});

test('poll rejects credential-bearing headers, unbounded data, invalid base64 and conflicting event order', async () => {
  for (const events of [
    [headerEvent(), { seq: 2, kind: 'chunk', data: '%%%=' }],
    [{ ...headerEvent(), headers: { 'set-cookie': 'secret=value' } }],
    [bytesEvent('before headers', 1)],
    [headerEvent(), headerEvent()],
    [headerEvent(), bytesEvent(Buffer.alloc(P.LIMITS.eventPage + 1))],
  ]) {
    const manager = make(req => req.method === 'POST' ? started : jobPage(events));
    await manager.invoke('start', request()); await assert.rejects(manager.invoke('poll', { id, after: 0, waitMs: 0 }), DesktopError); manager.close();
  }
});

test('rejected poll pages do not commit terminal state or cursor before a corrected read', async t => {
  let page = jobPage([headerEvent(), bytesEvent('first'), { seq: 3, kind: 'end' }], { cursor: 4 });
  const manager = make(req => req.method === 'POST' ? started : page);
  t.after(() => manager.close());
  await manager.invoke('start', request());
  await assert.rejects(manager.invoke('poll', { id, after: 0, waitMs: 0 }), { code: 'CONTRACT_MISMATCH' });
  page = jobPage([headerEvent(), bytesEvent('first'), bytesEvent('second', 3), { seq: 4, kind: 'end' }]);
  const corrected = await manager.invoke('poll', { id, after: 0, waitMs: 0 });
  assert.equal(corrected.cursor, 4);
  assert.equal(Buffer.concat(corrected.events.filter(e => e.kind === 'chunk').map(e => e.data)).toString(), 'firstsecond');
});

test('failed header pages cannot authorize subsequent headerless chunks or skip event numbers', async t => {
  let page = jobPage([headerEvent()], { cursor: 2 });
  const manager = make(req => req.method === 'POST' ? started : page);
  t.after(() => manager.close());
  await manager.invoke('start', request());
  await assert.rejects(manager.invoke('poll', { id, after: 0, waitMs: 0 }), { code: 'CONTRACT_MISMATCH' });
  page = jobPage([bytesEvent('not authorized by the rejected header', 1)]);
  await assert.rejects(manager.invoke('poll', { id, after: 0, waitMs: 0 }), { code: 'CONTRACT_MISMATCH' });
  page = jobPage([headerEvent(), bytesEvent('gap', 3), { seq: 4, kind: 'end' }]);
  await assert.rejects(manager.invoke('poll', { id, after: 0, waitMs: 0 }), { code: 'CONTRACT_MISMATCH' });
  page = jobPage([headerEvent(), bytesEvent('valid'), { seq: 3, kind: 'end' }]);
  assert.equal((await manager.invoke('poll', { id, after: 0, waitMs: 0 })).cursor, 3);
});

test('rejected response-size accounting is rolled back and repeated valid pages are counted once', async t => {
  let page = jobPage([headerEvent(), bytesEvent(Buffer.alloc(170000))], { state: 'running', hasMore: true });
  const manager = make(req => req.method === 'POST' ? started : page);
  t.after(() => manager.close());
  await manager.invoke('start', request());
  await manager.invoke('poll', { id, after: 0, waitMs: 0 });
  await manager.invoke('poll', { id, after: 0, waitMs: 0 });
  page = jobPage([bytesEvent(Buffer.alloc(170000), 3)], { state: 'running', hasMore: true });
  await manager.invoke('poll', { id, after: 2, waitMs: 0 });
  page = jobPage([bytesEvent(Buffer.alloc(190000), 4)], { state: 'running', hasMore: true });
  assert.ok(response(page).body.length < P.LIMITS.eventPage);
  await assert.rejects(manager.invoke('poll', { id, after: 3, waitMs: 0 }), { code: 'RESPONSE_TOO_LARGE' });
  page = jobPage([bytesEvent('valid tail', 4), { seq: 5, kind: 'end' }]);
  assert.equal((await manager.invoke('poll', { id, after: 3, waitMs: 0 })).cursor, 5);
});

test('a rejected blob page does not grant blob reads or retain conflicting descriptors', async t => {
  const content = Buffer.from('test');
  const descriptor = { id: blobId, size: content.length, sha256: sha(content), contentType: 'text/plain' };
  let blobReads = 0;
  let page = jobPage([headerEvent(), { seq: 2, kind: 'blob', blob: descriptor }, { seq: 3, kind: 'end' }], { cursor: 4 });
  const manager = make(req => {
    if (req.method === 'POST') return started;
    if (req.path.includes('/blobs/')) {
      blobReads++;
      return { ...descriptor, offset: 0, data: content.toString('base64'), hasMore: false };
    }
    return page;
  });
  t.after(() => manager.close());
  await manager.invoke('start', request());
  await assert.rejects(manager.invoke('poll', { id, after: 0, waitMs: 0 }), { code: 'CONTRACT_MISMATCH' });
  await assert.rejects(manager.invoke('blobRead', { id: blobId, offset: 0, limit: 4 }), { code: 'OPERATION_NOT_ALLOWED' });
  assert.equal(blobReads, 0);
  page = jobPage([headerEvent(), { seq: 2, kind: 'blob', blob: descriptor }, { seq: 3, kind: 'end' }]);
  await manager.invoke('poll', { id, after: 0, waitMs: 0 });
  assert.deepEqual(Buffer.from((await manager.invoke('blobRead', { id: blobId, offset: 0, limit: 4 })).data), content);
});

test('poll enforces the Gateway encoded page budget and accepts a 32-event page', async t => {
  let page = jobPage([headerEvent(), bytesEvent(Buffer.alloc(200000)), { seq: 3, kind: 'end' }]);
  assert.ok(response(page).body.length > P.LIMITS.eventPage);
  assert.ok(200000 < P.LIMITS.eventPage, 'decoded bytes alone do not enforce the wire page budget');
  const manager = make(req => req.method === 'POST' ? started : page);
  t.after(() => manager.close());
  await manager.invoke('start', request());
  await assert.rejects(manager.invoke('poll', { id, after: 0, waitMs: 0 }), { code: 'RESPONSE_TOO_LARGE' });
  page = jobPage([headerEvent(), ...Array.from({ length: 30 }, (_, i) => bytesEvent('合成片段', i + 2)), { seq: 32, kind: 'end' }]);
  assert.equal((await manager.invoke('poll', { id, after: 0, waitMs: 0 })).events.length, 32);
});

test('uploads hash actual chunks, duplicate ACK once, verify complete and reference only acknowledged files', async () => {
  let completeHash; let index = 0, received = 0;
  const first = new Uint8Array(P.LIMITS.chunk).fill(97), last = new Uint8Array([98]);
  const size = first.length + last.length;
  const manager = make(req => {
    if (req.path.endsWith('/uploads')) return upload({ size });
    if (req.path.endsWith('/chunks')) {
      const body = JSON.parse(req.body); const bytes = Buffer.from(body.data, 'base64'); assert.equal(body.sha256, sha(bytes));
      if (body.index === index) { received += bytes.length; index++; }
      return upload({ size, received, nextIndex: index });
    }
    if (req.path.endsWith('/complete')) { completeHash = JSON.parse(req.body).sha256; return upload({ state: 'complete', size, received: size, nextIndex: 2, sha256: completeHash }); }
    return started;
  });
  await manager.invoke('uploadCreate', { requestId: 'file-00001', fileName: 'sample.txt', contentType: 'text/plain', size });
  await assert.rejects(manager.invoke('uploadChunk', { id: uploadId, index: 0, bytes: new Uint8Array([1]) }), { code: 'INVALID_REQUEST' });
  await manager.invoke('uploadChunk', { id: uploadId, index: 0, bytes: first });
  await manager.invoke('uploadChunk', { id: uploadId, index: 0, bytes: first });
  await assert.rejects(manager.invoke('uploadChunk', { id: uploadId, index: 0, bytes: new Uint8Array([0, 0]) }), DesktopError);
  await manager.invoke('uploadChunk', { id: uploadId, index: 1, bytes: last });
  await manager.invoke('uploadComplete', { id: uploadId }); assert.equal(completeHash, sha(Buffer.concat([first, last])));
  await manager.invoke('start', request('post_api_mindos_uploads', { body: { kind: 'multipart', fields: {}, files: [{ field: 'file', uploadId }] } }));
  manager.close();
});

test('timeout after a mutation returns unknown outcome and retains native capacity until settlement', async () => {
  const resolvers = []; const budget = { active: new Set() };
  const manager = createProductSession({ session: { request: () => new Promise(resolve => resolvers.push(resolve)) }, isCurrent: () => true, timeoutMs: 5, budget });
  await assert.rejects(manager.invoke('start', request('post_api_mindos_zhijun_onboarding', { body: {} })), { code: 'WRITE_OUTCOME_UNKNOWN' });
  assert.equal(budget.active.size, 1); manager.close();
  const next = createProductSession({ session: { request: async () => response(started) }, isCurrent: () => true, budget });
  assert.equal(budget.active.size, 1); resolvers[0](response(started)); await tick(); assert.equal(budget.active.size, 0); next.close();
});

test('generation change suppresses late task and closes handles without claiming remote cancellation', async () => {
  let finish; const manager = createProductSession({ session: { request: () => new Promise(resolve => { finish = resolve; }) }, isCurrent: () => true });
  const pending = manager.invoke('start', request()); await tick(); manager.close();
  await assert.rejects(pending, { code: 'STALE_GENERATION' }); finish(response(started));
  await assert.rejects(manager.invoke('poll', { id, after: 0, waitMs: 0 }), { code: 'STALE_GENERATION' });
});

test('cancel reports actual remote state and partial uploads are not adopted for missing local hash', async () => {
  const manager = make(req => req.path.endsWith('/cancel') ? { id, state: 'running', cancelRequested: true }
    : req.method === 'POST' ? started : upload({ received: 1, nextIndex: 1 }));
  await manager.invoke('start', request());
  assert.deepEqual(await manager.invoke('cancel', { id, requestId: 'cancel-0001' }), { id, state: 'running', cancelRequested: true });
  await manager.invoke('uploadStatus', { id: uploadId });
  await assert.rejects(manager.invoke('uploadChunk', { id: uploadId, index: 1, bytes: new Uint8Array([2]) }), { code: 'OPERATION_NOT_ALLOWED' }); manager.close();
});

function binaryManager(contentType = 'image/png', corrupt = false, host = {}) {
  const bytes = Buffer.from('synthetic image bytes'); const descriptor = { id: blobId, size: bytes.length, sha256: sha(bytes), contentType, fileName: 'sample.bin' };
  return make(req => {
    if (req.path.endsWith('/operations')) return started;
    if (req.path.includes('/operations/')) return jobPage([headerEvent(200, contentType), { seq: 2, kind: 'blob', blob: descriptor }, { seq: 3, kind: 'end' }]);
    const url = new URL(req.path, 'https://synthetic.invalid'); const offset = Number(url.searchParams.get('offset')), limit = Number(url.searchParams.get('limit'));
    return { ...descriptor, offset, data: (corrupt ? Buffer.alloc(Math.min(limit, bytes.length - offset)) : bytes.subarray(offset, offset + limit)).toString('base64'), hasMore: offset + limit < bytes.length };
  }, { host });
}
const mediaRequest = () => request('get_api_mindos_materials_material_id_file', { params: { materialId: 'sample-material' } });

test('media uses generation-bound capability URLs with exact MIME, range validation and close cleanup', async () => {
  const manager = binaryManager(); const media = await manager.invoke('openMedia', mediaRequest());
  assert.match(media.url, /^zhijun-media:\/\/session\/[a-f0-9]{32}$/);
  assert.equal(media.size, 21);
  const result = await manager.mediaResponse(new Request(media.url, { headers: { Range: 'bytes=2-5' } }));
  assert.equal(result.status, 206); assert.equal(result.headers.get('content-type'), 'image/png'); assert.equal(await result.text(), 'nthe');
  assert.equal(result.headers.get('cache-control'), 'no-store'); assert.equal(result.headers.get('x-content-type-options'), 'nosniff');
  assert.equal(result.headers.get('access-control-allow-origin'), 'zhijun://desktop'); assert.equal(result.headers.get('vary'), 'Origin');
  assert.equal(result.headers.get('content-security-policy'), null);
  assert.equal((await manager.mediaResponse(new Request(media.url, { headers: { Range: 'bytes=99-' } }))).status, 416);
  const full = await manager.mediaResponse(new Request(media.url)); assert.equal(await full.text(), 'synthetic image bytes');
  assert.deepEqual(await manager.invoke('closeMedia', { handle: media.handle }), { closed: true });
  assert.equal((await manager.mediaResponse(new Request(media.url))).status, 403); manager.close();
  for (const mime of ['image/svg+xml', 'text/html', 'application/javascript']) {
    const blocked = binaryManager(mime); await assert.rejects(blocked.invoke('openMedia', mediaRequest()), { code: 'OPERATION_NOT_ALLOWED' }); blocked.close();
  }
});

test('blob save verifies whole-file digest, rejects raw paths and treats native cancel as non-error', async () => {
  let output;
  const host = { save: async ({ source }) => { const all = []; for await (const part of source) all.push(Buffer.from(part)); output = Buffer.concat(all); return true; } };
  const manager = binaryManager('image/png', false, host);
  await manager.invoke('start', mediaRequest()); await manager.invoke('poll', { id, after: 0, waitMs: 0 });
  assert.deepEqual(await manager.invoke('save', { fileName: 'sample.png', contentType: 'image/png', source: { kind: 'blob', id: blobId } }), { saved: true });
  assert.equal(output.toString(), 'synthetic image bytes');
  await assert.rejects(manager.invoke('save', { fileName: '../outside', contentType: 'image/png', source: { kind: 'blob', id: blobId } }), DesktopError); manager.close();
  const corrupted = binaryManager('image/png', true, host);
  await corrupted.invoke('start', mediaRequest()); await corrupted.invoke('poll', { id, after: 0, waitMs: 0 });
  await assert.rejects(corrupted.invoke('save', { fileName: 'sample.png', contentType: 'image/png', source: { kind: 'blob', id: blobId } }), { code: 'CONTRACT_MISMATCH' }); corrupted.close();
  const cancelled = make(() => ({}), { host: { save: async () => false } });
  assert.deepEqual(await cancelled.invoke('save', { fileName: 'notes.md', contentType: 'text/markdown;charset=utf-8', source: { kind: 'bytes', bytes: new Uint8Array([1]) } }), { saved: false }); cancelled.close();
});

test('desktop exposes product only after v2 matched proof and clears workspace on disconnect', async () => {
  let currentSession;
  const adapter = { signIn: async () => ({ accountId: 'account' }), signOut: async () => {},
    listDevices: async () => [{ deviceId: 'device', displayName: 'test', availability: 'online' }],
    connect: async () => currentSession = { authorize: async () => ({ accountId: 'account', deviceId: 'device', product: true, workspaceId: '1'.repeat(64) }),
      request: async () => response(started), close: async () => {} } };
  const runtime = createDesktopRuntime({ mode: 'production', adapter }); let count = 0;
  const invoke = (op, ...input) => runtime.invoke(op, [{ callId: `product-call-${++count}`, expectedGeneration: runtime.snapshot().generation }, ...input], 1);
  assert.equal(runtime.snapshot().capabilities.product, false);
  await invoke('signInWithPassword', { phone: '13800138000', password: 'synthetic-password' }); await invoke('listDevices'); await invoke('connect', 'device');
  assert.equal(runtime.snapshot().capabilities.product, true); assert.match(runtime.snapshot().subject.workspaceId, /^[a-f0-9]{64}$/);
  assert.equal((await invoke('product.start', request())).ok, true); await invoke('disconnect');
  assert.equal(runtime.snapshot().capabilities.product, false); assert.equal(runtime.snapshot().subject.workspaceId, undefined);
  assert.equal((await invoke('product.start', request())).error.code, 'SESSION_NOT_READY'); await runtime.dispose();
});


test('v2 idle heartbeat rechecks workspace and stops renewing after mismatch or close', async () => {
  const subject = { accountId: 'account-test', clientId: 'client-test', deviceId: 'device-test' };
  const ctx = { version: 2, ...subject, applicationId: 'zhijun-desktop', workspaceId: 'f'.repeat(64),
    capabilities: ['product.rpc', 'product.events', 'product.uploads', 'product.blobs'], expiresAt: 1005 };
  let calls = 0, failed;
  const bridge = await createBusinessBridge({ clock: () => 1000000, heartbeatMs: 5, heartbeatTimeoutMs: 10, schedulerOptions: { intervalMs: 1 } }).authorize({
    subject, applicationId: 'zhijun-desktop', onFailure: error => { failed = error; },
    session: { request: async () => response(++calls === 1 ? ctx : { ...ctx, workspaceId: 'e'.repeat(64) }) },
  });
  await new Promise(resolve => setTimeout(resolve, 25)); assert.equal(failed.code, 'ACCESS_DENIED'); assert.equal(calls, 2);
  await new Promise(resolve => setTimeout(resolve, 15)); assert.equal(calls, 2);
  await assert.rejects(bridge.request({}), { code: 'SESSION_NOT_READY' }); await bridge.close();
  calls = 0;
  const closing = await createBusinessBridge({ clock: () => 1000000, heartbeatMs: 5, schedulerOptions: { intervalMs: 1 } }).authorize({
    subject, applicationId: 'zhijun-desktop', session: { request: async () => { calls++; return response(ctx); } },
  });
  await closing.close(); await new Promise(resolve => setTimeout(resolve, 15)); assert.equal(calls, 1);
});

test('heartbeat deadline reports failure without claiming a pending native call completed', async () => {
  const subject = { accountId: 'account-test', clientId: 'client-test', deviceId: 'device-test' };
  let calls = 0, failed;
  const bridge = await createBusinessBridge({ clock: () => 1000000, heartbeatMs: 5, heartbeatTimeoutMs: 5, schedulerOptions: { intervalMs: 1 } }).authorize({
    subject, applicationId: 'zhijun-desktop', onFailure: error => { failed = error; }, session: { request: () => ++calls === 1
      ? response({ version: 2, ...subject, applicationId: 'zhijun-desktop', workspaceId: 'f'.repeat(64),
        capabilities: ['product.rpc', 'product.events', 'product.uploads', 'product.blobs'], expiresAt: 1005 })
      : new Promise(() => {}) },
  });
  await new Promise(resolve => setTimeout(resolve, 25)); assert.equal(failed.code, 'REQUEST_TIMEOUT'); assert.equal(calls, 2); await bridge.close();
});

test('disconnect immediately suppresses a save waiting for a native dialog', async () => {
  let finish;
  const manager = make(() => ({}), { host: { save: () => new Promise(resolve => { finish = resolve; }) } });
  const saving = manager.invoke('save', { fileName: 'notes.md', contentType: 'text/markdown;charset=utf-8', source: { kind: 'bytes', bytes: new Uint8Array([1]) } });
  await tick(); manager.close(); await assert.rejects(saving, { code: 'STALE_GENERATION' }); finish(false);
});

test('concurrent create reservations and actual native slots remain bounded', async () => {
  const finishes = [];
  const manager = createProductSession({ session: { request: () => new Promise(resolve => finishes.push(resolve)) }, isCurrent: () => true });
  const pending = Array.from({ length: 8 }, (_, i) => manager.invoke('uploadCreate', { requestId: `upload-${i}-action`, fileName: 'one.txt', contentType: 'text/plain', size: 3 }));
  await tick(); assert.equal(finishes.length, 8);
  await assert.rejects(manager.invoke('uploadCreate', { requestId: 'extra-action', fileName: 'extra.txt', contentType: 'text/plain', size: 3 }), { code: 'RESOURCE_EXHAUSTED' });
  finishes.forEach((finish, i) => finish(response(upload({ id: i.toString(16).padStart(32, '0') })))); await Promise.all(pending); manager.close();
});

test('permanent session quotas clear ready capability and require user reconnect while retaining uncertain writes', async () => {
  for (const definitelyNotSent of [true, false]) {
    let nativeCalls = 0, connects = 0;
    const adapter = { signIn: async () => ({ accountId: 'account' }), signOut: async () => {},
      listDevices: async () => [{ deviceId: 'device', displayName: 'test', availability: 'online' }],
      connect: async () => { connects++; return { authorize: async () => ({ accountId: 'account', deviceId: 'device', product: true, workspaceId: '1'.repeat(64) }),
        request: async () => { nativeCalls++; throw new DesktopError('SESSION_QUOTA_EXHAUSTED', { definitelyNotSent }); }, close: async () => {} }; } };
    const runtime = createDesktopRuntime({ mode: 'production', adapter }); let count = 0;
    const invoke = (op, ...input) => runtime.invoke(op, [{ callId: `quota-call-${++count}`, expectedGeneration: runtime.snapshot().generation }, ...input], 1);
    await invoke('signInWithPassword', { phone: '13800138000', password: 'synthetic-password' }); await invoke('listDevices'); await invoke('connect', 'device');
    const result = await invoke('product.start', request('post_api_mindos_zhijun_onboarding', { body: {} }));
    assert.equal(result.error.code, definitelyNotSent ? 'SESSION_QUOTA_EXHAUSTED' : 'WRITE_OUTCOME_UNKNOWN');
    assert.equal(runtime.snapshot().phase, 'failed'); assert.equal(runtime.snapshot().capabilities.product, false);
    assert.equal(runtime.snapshot().error.code, 'SESSION_QUOTA_EXHAUSTED'); assert.equal(runtime.snapshot().error.recovery, 'user_reconnect');
    assert.equal(nativeCalls, 1); assert.equal(connects, 1, 'no automatic session rotation');
    assert.equal('definitelyNotSent' in result.error, false); await runtime.dispose();
  }
});
