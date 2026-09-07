'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createBusinessBridge } = require('../production/business-bridge.cjs');
const { createProductionAdapter } = require('../production/adapter.cjs');
const { createDesktopRuntime } = require('../runtime/desktop-runtime.cjs');
const subject = { accountId: 'account-vector', clientId: 'client-vector', deviceId: 'device-vector' };
const applicationId = 'mindos-person-data-pc';
const clock = () => 1893456002000;
const context = () => ({ version: 1, ...subject, applicationId, capabilities: ['materials.read'], expiresAt: 1893456005 });
const response = value => ({ status: 200, headers: { 'content-type': 'application/json' }, body: Buffer.from(JSON.stringify(value)) });
const read = () => ({ method: 'GET', relative_path: '/api/mindos/materials?limit=20&offset=0', headers: { Accept: ['application/json'] } });

test('v2 serial-sidecar poll cap preserves zero/short waits and validates the original canonical route first', async () => {
  const calls = [], id = 'b'.repeat(32);
  const ctx = { ...context(), version: 2, applicationId: 'zhijun-desktop', workspaceId: 'f'.repeat(64),
    capabilities: ['product.rpc', 'product.events', 'product.uploads', 'product.blobs'] };
  const bridge = await createBusinessBridge({ clock, schedulerOptions: { intervalMs: 1 } }).authorize({
    applicationId: 'zhijun-desktop', subject, session: { request: async input => {
      calls.push(input); return response(input.relative_path.endsWith('/context') ? ctx : {});
    } },
  });
  try {
    for (const wait of [0, 1, 249, 250, 251, 8000]) {
      const path = `/api/mindos/zhijun/operations/${id}?after=42&waitMs=${wait}`;
      const input = { method: 'GET', relative_path: path, headers: { Accept: ['application/json'] } };
      await bridge.request(input);
      assert.equal(calls.at(-1).relative_path, path.replace(/waitMs=\d+$/, `waitMs=${Math.min(wait, 250)}`));
      assert.equal(input.relative_path, path, 'caller request must remain unchanged');
    }
    const count = calls.length;
    for (const query of ['after=42&waitMs=8001', 'after=42&waitMs=0250', 'waitMs=8000&after=42', 'after=42&waitMs=8000&x=1']) {
      await assert.rejects(bridge.request({ method: 'GET', relative_path: `/api/mindos/zhijun/operations/${id}?${query}`,
        headers: { Accept: ['application/json'] } }), { code: 'INVALID_REQUEST' });
    }
    assert.equal(calls.length, count);
  } finally { await bridge.close(); }
});

test('production bridge verifies context through the same SDK session before permitting a material read', async () => {
  const requests = [];
  const bridge = createBusinessBridge({ clock });
  const authorized = await bridge.authorize({ applicationId, subject, session: { request: async input => {
    requests.push(input); return response(input.relative_path.endsWith('/context') ? context() : { items: [] });
  } } });
  assert.equal(authorized.accountId, subject.accountId);
  assert.equal(authorized.clientId, subject.clientId);
  await authorized.request(read());
  assert.deepEqual(requests, [
    { method: 'GET', relative_path: '/api/mindos/connectivity/context', headers: { Accept: ['application/json'] } }, read(),
  ]);
  assert.equal(JSON.stringify(requests).includes(subject.accountId), false, 'client must not self-assert an identity header');
  await authorized.close();
  await assert.rejects(authorized.request(read()), { code: 'SESSION_NOT_READY' });
  assert.equal(requests.length, 2);
});

test('wrong account, client, device, application, expiry or unexpected response fields cannot authorize', async () => {
  for (const key of ['accountId', 'clientId', 'deviceId', 'applicationId']) {
    await assert.rejects(createBusinessBridge({ clock }).authorize({ applicationId, subject,
      session: { request: async () => response({ ...context(), [key]: 'wrong' }) } }), { code: 'ACCESS_DENIED' });
  }
  for (const [value, code] of [
    [{ ...context(), expiresAt: 1893456002 }, 'SESSION_EXPIRED'],
    [{ ...context(), expiresAt: 1893457000 }, 'CONTRACT_MISMATCH'],
    [{ ...context(), capabilities: ['materials.read', 'materials.write'] }, 'CONTRACT_MISMATCH'],
    [{ ...context(), token: 'synthetic-only' }, 'CONTRACT_MISMATCH'],
  ]) await assert.rejects(createBusinessBridge({ clock }).authorize({ applicationId, subject,
    session: { request: async () => response(value) } }), { code });
  let called = false;
  await assert.rejects(createBusinessBridge({ clock }).authorize({ applicationId: 'other-app', subject,
    session: { request: async () => { called = true; } } }), { code: 'ACCESS_DENIED' });
  assert.equal(called, false);
});

test('unavailable box bridge is actionable and malformed/oversized bytes are rejected before decoding', async () => {
  const cases = [
    [{ ...response({}), status: 503 }, 'BUSINESS_BRIDGE_REQUIRED'],
    [{ ...response({}), status: 404 }, 'BUSINESS_BRIDGE_REQUIRED'],
    [{ ...response({}), status: 401 }, 'ACCESS_DENIED'],
    [{ ...response({}), status: 403 }, 'ACCESS_DENIED'],
    [{ ...response({}), status: 429 }, 'RESOURCE_EXHAUSTED'],
    [{ ...response(context()), headers: { 'content-type': 'text/html' } }, 'CONTRACT_MISMATCH'],
    [{ ...response(context()), headers: { 'content-type': 'application/json', 'Content-Type': 'application/json' } }, 'CONTRACT_MISMATCH'],
    [{ ...response(context()), body: new Uint8Array([0xff]) }, 'CONTRACT_MISMATCH'],
    [{ ...response(context()), body: new Uint8Array(8193) }, 'RESPONSE_TOO_LARGE'],
  ];
  for (const [result, code] of cases) await assert.rejects(createBusinessBridge({ clock }).authorize({ applicationId, subject,
    session: { request: async () => result } }), { code });
});

test('bridge rejects write, identity headers, path escapes, duplicate query and out-of-budget query before dispatch', async () => {
  let calls = 0;
  const bridge = await createBusinessBridge({ clock }).authorize({ applicationId, subject,
    session: { request: async () => { calls++; return response(context()); } } });
  for (const request of [
    { ...read(), method: 'POST' }, { ...read(), body: 'x' },
    { ...read(), headers: { ...read().headers, 'X-Nexus-Mindos-Bridge': ['forged'] } },
    { ...read(), relative_path: 'https://other.test/api/mindos/materials?limit=20&offset=0' },
    { ...read(), relative_path: '/api/mindos/materials?limit=20&offset=0&limit=1' },
    { ...read(), relative_path: '/api/mindos/materials?limit=51&offset=0' },
    { ...read(), relative_path: '/api/mindos/materials?limit=20&offset=0&deviceScope=global' },
    { ...read(), relative_path: '/api/mindos/materials?limit=20&offset=0#secret' },
  ]) await assert.rejects(bridge.request(request), { code: 'INVALID_REQUEST' });
  assert.equal(calls, 1);
});

test('closing production authorization discards a late material response', async () => {
  let finish;
  const bridge = await createBusinessBridge({ clock }).authorize({ applicationId, subject,
    session: { request: input => input.relative_path.endsWith('/context') ? response(context()) : new Promise(resolve => { finish = resolve; }) } });
  const pending = bridge.request(read());
  await bridge.close(); finish(response({ items: [] }));
  await assert.rejects(pending, { code: 'SESSION_NOT_READY' });
});

test('old Agent manifest and native failures have safe actionable errors without raw exception leakage', async () => {
  for (const [native, expected] of [['REQUEST_TARGET_NOT_ALLOWED', 'BUSINESS_BRIDGE_REQUIRED'],
    ['SDK_CONNECTION_CLOSED', 'SESSION_EXPIRED'], ['SDK_REQUEST_TIMEOUT', 'REQUEST_TIMEOUT'],
    ['SESSION_RESOURCE_EXHAUSTED', 'SESSION_QUOTA_EXHAUSTED'], ['SDK_REQUEST_LIMIT_REACHED', 'SESSION_QUOTA_EXHAUSTED'],
    ['REQUEST_REPLAYED', 'SESSION_QUOTA_EXHAUSTED'], ['TOO_MANY_REQUESTS', 'RATE_LIMITED'], ['SDK_TOO_MANY_REQUESTS', 'RATE_LIMITED'],
    ['SDK_RESPONSE_TOO_LARGE', 'RESPONSE_TOO_LARGE'],
    ['unknown', 'TRANSPORT_UNAVAILABLE']]) {
    await assert.rejects(createBusinessBridge({ clock }).authorize({ applicationId, subject,
      session: { request: async () => { throw Object.assign(new Error('synthetic-private-error'), { code: native }); } } }), error => {
      assert.equal(error.code, expected); assert.equal(error.message.includes('synthetic-private-error'), false); return true;
    });
  }
});

test('actual production adapter passes connect -> bridge context -> material request and owns native cleanup', async () => {
  const order = []; let closes = 0;
  const adapter = await createProductionAdapter({
    config: { connectivity: { applicationId } }, consumer: { current: async () => subject, dispose: async () => {} },
    bridge: createBusinessBridge({ clock }), runtimeFactory: async () => ({
      connect: async deviceId => { order.push(`connect:${deviceId}`); return { request: async input => {
        order.push(input.relative_path); return response(context());
      } }; }, close: async () => { closes++; },
    }),
  });
  const session = await adapter.connect(subject);
  await session.authorize();
  await session.request({ method: 'GET', path: read().relative_path, headers: { Accept: 'application/json' } });
  await session.close(); await adapter.dispose();
  assert.deepEqual(order, [`connect:${subject.deviceId}`, '/api/mindos/connectivity/context', read().relative_path]);
  assert.equal(closes, 1);
});

test('confirmed native close invalidates ready while a permission denial preserves the signed-in identity', async () => {
  for (const closedNative of [true, false]) {
    let nativeCloses = 0;
    const adapter = await createProductionAdapter({ config: { connectivity: { applicationId } },
      consumer: { current: async () => subject, signIn: async () => subject, dispose: async () => {},
        listDevices: async () => [{ deviceId: subject.deviceId, displayName: 'synthetic box', availability: 'online' }] },
      bridge: createBusinessBridge({ clock }), runtimeFactory: async () => ({ connect: async () => ({
        request: async input => {
          if (input.relative_path.endsWith('/context')) return response(context());
          if (closedNative) throw Object.assign(new Error('synthetic native closed'), { code: 'SDK_CONNECTION_CLOSED' });
          return { ...response({}), status: 403 };
        },
      }), close: async () => { nativeCloses++; } }),
    });
    const runtime = createDesktopRuntime({ mode: 'production', adapter });
    let calls = 0;
    const invoke = (operation, ...input) => runtime.invoke(operation,
      [{ callId: `bridge-review-${++calls}`, expectedGeneration: runtime.snapshot().generation }, ...input], 1);
    try {
      assert.equal((await invoke('signInWithPassword', { phone: '13800138000', password: 'synthetic-password' })).ok, true);
      assert.equal((await invoke('listDevices')).ok, true);
      assert.equal((await invoke('connect', subject.deviceId)).ok, true);
      const generation = runtime.snapshot().generation;
      const result = await invoke('materials.list', { limit: 20, offset: 0 });
      assert.equal(result.error.code, closedNative ? 'SESSION_EXPIRED' : 'ACCESS_DENIED');
      const snapshot = runtime.snapshot();
      assert.equal(snapshot.subject.accountId, subject.accountId, 'do not turn a read denial into sign-out');
      assert.equal(snapshot.capabilities.materialsRead, !closedNative);
      assert.equal(snapshot.phase, closedNative ? 'failed' : 'ready');
      assert.equal(snapshot.generation, generation + (closedNative ? 1 : 0));
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(nativeCloses, closedNative ? 1 : 0);
    } finally { await runtime.dispose(); }
  }
});
