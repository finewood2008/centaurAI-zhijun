'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createDesktopRuntime } = require('../runtime/desktop-runtime.cjs');
const { DesktopError } = require('../runtime/public-error.cjs');
const { createSimulationAdapter } = require('../runtime/adapters.cjs');

const deferred = () => {
  let resolve; let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const tick = () => new Promise((resolve) => setImmediate(resolve));
const response = (id = 'synthetic-material') => ({ status: 200,
  headers: { 'content-type': 'application/json' },
  body: new TextEncoder().encode(JSON.stringify({ items: [{ materialId: id, fileName: '模拟资料',
    fileType: 'document', status: 'queued', createdAt: '2026-09-06T00:00:00Z', folder: 'hidden' }],
  total: 1, limit: 20, offset: 0, hasMore: false, folders: ['hidden'] })) });
let nextCall = 0;
function call(runtime, operation, input, options = {}) {
  const context = { callId: options.callId || `test-call-${++nextCall}`,
    expectedGeneration: options.generation ?? runtime.snapshot().generation };
  return runtime.invoke(operation, [context, ...(input === undefined ? [] : [input])], options.senderId ?? 1);
}
async function ready(runtime, deviceId = 'synthetic-box-a') {
  assert.equal((await call(runtime, 'beginSignIn')).ok, true);
  assert.equal((await call(runtime, 'listDevices')).ok, true);
  const connected = await call(runtime, 'connect', deviceId);
  assert.equal(connected.ok, true, JSON.stringify(connected));
  assert.equal(runtime.snapshot().phase, 'ready');
}
function fixture(overrides = {}) {
  const sessions = [];
  const adapter = {
    async signIn() { return { accountId: 'synthetic-account' }; },
    async listDevices() { return ['a', 'b'].map((letter) => ({ deviceId: `synthetic-box-${letter}`,
      displayName: letter, availability: 'online' })); },
    async connect(binding) {
      const session = { closed: 0,
        async authorize() { return binding; },
        async request() { return response(binding.deviceId); },
        async close() { session.closed += 1; },
      };
      sessions.push(session);
      return session;
    },
    async signOut() {},
    ...overrides,
  };
  return { adapter, sessions };
}

test('production startup restores an encrypted login identity before device selection', async (t) => {
  const runtime = createDesktopRuntime({ mode: 'production', adapter: fixture({
    async restore() { return { accountId: 'restored-account' }; },
  }).adapter });
  t.after(() => runtime.dispose());
  assert.equal(runtime.snapshot().phase, 'authenticating');
  await tick();
  assert.equal(runtime.snapshot().phase, 'selecting_device');
  assert.equal(runtime.snapshot().subject.accountId, 'restored-account');
});

test('production startup without a valid encrypted login returns to sign-in', async (t) => {
  const runtime = createDesktopRuntime({ mode: 'production', adapter: fixture({
    async restore() { return null; },
  }).adapter });
  t.after(() => runtime.dispose());
  assert.equal(runtime.snapshot().phase, 'authenticating');
  await tick();
  assert.equal(runtime.snapshot().phase, 'signed_out');
  assert.equal(runtime.snapshot().subject, null);
});

test('remembered-login IPC projects metadata and validates exact shape, mode, flag and generation', async t => {
  let value = { phone: '13800000000', passwordSaved: true }, metadataReads = 0, savedLogins = 0;
  const runtime = createDesktopRuntime({ mode: 'production', adapter: fixture({
    getRememberedLogin: async () => { metadataReads++; return value; },
    signInSaved: async () => { savedLogins++; return { accountId: 'synthetic-account' }; },
  }).adapter });
  t.after(() => runtime.dispose());
  assert.deepEqual((await call(runtime, 'getRememberedLogin')).data, value);
  assert.equal(runtime.snapshot().subject, null);
  assert.equal(savedLogins, 0);
  const generation = runtime.snapshot().generation;
  for (const input of [null, 1, 'true', {}, { rememberPassword: true }]) {
    assert.equal((await call(runtime, 'signInWithSavedPassword', input)).error.code, 'INVALID_REQUEST');
  }
  assert.equal(savedLogins, 0);
  value = { phone: '13800000000', passwordSaved: true, password: 'Synthetic-private-password' };
  const malformed = await call(runtime, 'getRememberedLogin');
  assert.equal(malformed.error.code, 'CONTRACT_MISMATCH');
  assert.equal(JSON.stringify(malformed).includes('Synthetic-private-password'), false);
  value = { phone: 13800000000, passwordSaved: false };
  assert.equal((await call(runtime, 'getRememberedLogin')).error.code, 'CONTRACT_MISMATCH');
  await call(runtime, 'signOut');
  const count = metadataReads;
  assert.equal((await call(runtime, 'getRememberedLogin', undefined, { generation })).error.code, 'STALE_GENERATION');
  assert.equal(metadataReads, count);
  const simulation = createDesktopRuntime({ mode: 'simulation' });
  t.after(() => simulation.dispose());
  assert.equal((await call(simulation, 'getRememberedLogin')).error.code, 'OPERATION_NOT_ALLOWED');
});

test('remembered-login storage timeout settles the IPC without authenticating', async t => {
  const stalled = deferred();
  const runtime = createDesktopRuntime({ mode: 'production', timeoutMs: 20,
    adapter: fixture({ getRememberedLogin: () => stalled.promise }).adapter });
  t.after(() => runtime.dispose());
  const fallback = setTimeout(() => stalled.resolve({ phone: '13800000000', passwordSaved: true }), 150);
  try {
    const result = await call(runtime, 'getRememberedLogin');
    assert.equal(result.ok, false, 'a hung storage adapter must not hang or later succeed after the IPC deadline');
    assert.equal(result.error.code, 'REQUEST_TIMEOUT');
    assert.equal(runtime.snapshot().subject, null);
  } finally { clearTimeout(fallback); stalled.resolve(null); }
});

test('saved-password login is explicit, receives the exact remember flag, and stale completion cannot restore an account', async t => {
  const login = deferred(); let guard, flag, logins = 0;
  const runtime = createDesktopRuntime({ mode: 'production', adapter: fixture({
    getRememberedLogin: async () => ({ phone: '13800000000', passwordSaved: true }),
    signInSaved: (isCurrent, rememberPassword) => { guard = isCurrent; flag = rememberPassword; logins++; return login.promise; },
  }).adapter });
  t.after(() => runtime.dispose());
  await call(runtime, 'getRememberedLogin');
  assert.equal(logins, 0);
  const pending = call(runtime, 'signInWithSavedPassword', false);
  await tick();
  assert.equal(flag, false);
  assert.equal(guard(), true);
  assert.equal(runtime.snapshot().phase, 'authenticating');
  await call(runtime, 'signOut');
  assert.equal(guard(), false);
  assert.equal((await pending).error.code, 'STALE_GENERATION');
  login.resolve({ accountId: 'stale-synthetic-account' });
  await tick();
  assert.equal(runtime.snapshot().subject, null);
  assert.equal(logins, 1);
  assert.deepEqual((await call(runtime, 'getRememberedLogin')).data, { phone: '13800000000', passwordSaved: true });
});

test('legacy password login defaults remember to false and the flag stays outside the auth credential body', async t => {
  const calls = [];
  const runtime = createDesktopRuntime({ mode: 'production', adapter: fixture({
    signIn: async (credentials, guard, flag) => { calls.push({ credentials, flag, current: guard() }); return { accountId: 'synthetic-account' }; },
  }).adapter });
  t.after(() => runtime.dispose());
  const credentials = { phone: '13800000000', password: 'Synthetic-password-1' };
  for (const input of [{ ...credentials, rememberPassword: 'true' }, { ...credentials, rememberPassword: true, token: 'synthetic-forged' }]) {
    assert.equal((await call(runtime, 'signInWithPassword', input)).error.code, 'INVALID_REQUEST');
  }
  assert.equal(calls.length, 0);
  assert.equal((await call(runtime, 'signInWithPassword', credentials)).ok, true);
  assert.equal((await call(runtime, 'signInWithPassword', { ...credentials, rememberPassword: true })).ok, true);
  assert.deepEqual(calls, [{ credentials, flag: false, current: true }, { credentials, flag: true, current: true }]);
  assert.equal(JSON.stringify(runtime.snapshot()).includes(credentials.password), false);
});

test('unconfigured mode remains closed even with an injected adapter', async (t) => {
  const runtime = createDesktopRuntime({ adapter: fixture().adapter });
  t.after(() => runtime.dispose());
  assert.equal(runtime.snapshot().environment, 'unconfigured');
  assert.equal((await call(runtime, 'beginSignIn')).error.code, 'CONFIGURATION_REQUIRED');
  assert.equal(runtime.snapshot().phase, 'signed_out');
  assert.equal(runtime.snapshot().capabilities.materialsRead, false);
  assert.equal((await runtime.invoke('getSnapshot', [], 1)).ok, true);
});

test('simulation reads 47 synthetic materials through the real projection and filters queued', async (t) => {
  const runtime = createDesktopRuntime({ mode: 'simulation' });
  t.after(() => runtime.dispose());
  await ready(runtime);
  const page = await call(runtime, 'materials.list', { limit: 20, offset: 0 });
  assert.equal(page.ok, true);
  assert.equal(page.data.total, 47);
  assert.equal(page.data.items.length, 20);
  assert.equal(page.data.hasMore, true);
  assert.deepEqual(Object.keys(page.data.items[0]).sort(), ['createdAt', 'fileName', 'fileType', 'materialId', 'status']);
  assert.equal(JSON.stringify(page).includes('private'), false);
  const filtered = await call(runtime, 'materials.list', { limit: 20, offset: 0, status: 'queued' });
  assert.equal(filtered.data.total, 10);
  assert.ok(filtered.data.items.every((item) => item.status === 'queued'));
  const last = await call(runtime, 'materials.list', { limit: 20, offset: 40 });
  assert.equal(last.data.items.length, 7);
  assert.equal(last.data.hasMore, false);
  const mixed = await call(runtime, 'materials.list', { limit: 20, offset: 0, keyword: '002', type: 'image' });
  assert.equal(mixed.data.total, 1);
});

test('sign-in completion after sign-out cannot restore subject', async (t) => {
  const login = deferred();
  const runtime = createDesktopRuntime({ mode: 'simulation', adapter: fixture({ signIn: () => login.promise }).adapter });
  t.after(() => runtime.dispose());
  const pending = call(runtime, 'beginSignIn');
  await tick();
  assert.equal(runtime.snapshot().phase, 'authenticating');
  assert.equal((await call(runtime, 'signOut')).ok, true);
  assert.equal((await pending).error.code, 'STALE_GENERATION');
  login.resolve({ accountId: 'late-account' });
  await tick();
  assert.equal(runtime.snapshot().subject, null);
  assert.equal(runtime.snapshot().phase, 'signed_out');
});

test('second login wins and old promise settles before its callback arrives', async (t) => {
  const logins = [deferred(), deferred()];
  let calls = 0;
  const runtime = createDesktopRuntime({ mode: 'simulation', adapter: fixture({ signIn: () => logins[calls++].promise }).adapter });
  t.after(() => runtime.dispose());
  const first = call(runtime, 'beginSignIn');
  await tick();
  const second = call(runtime, 'beginSignIn');
  await tick();
  assert.equal((await first).error.code, 'STALE_GENERATION');
  logins[1].resolve({ accountId: 'new-account' });
  assert.equal((await second).ok, true);
  logins[0].resolve({ accountId: 'old-account' });
  await tick();
  assert.equal(runtime.snapshot().subject.accountId, 'new-account');
});

test('SDK success alone stays authorizing and wrong bridge binding fails closed', async (t) => {
  const proof = deferred();
  const base = fixture();
  const original = base.adapter.connect;
  base.adapter.connect = async (binding) => ({ ...await original(binding), authorize: () => proof.promise });
  const runtime = createDesktopRuntime({ mode: 'simulation', adapter: base.adapter });
  t.after(() => runtime.dispose());
  await call(runtime, 'beginSignIn');
  await call(runtime, 'listDevices');
  const connecting = call(runtime, 'connect', 'synthetic-box-a');
  await tick();
  assert.equal(runtime.snapshot().phase, 'authorizing');
  assert.equal((await call(runtime, 'materials.list', { limit: 20, offset: 0 })).error.code, 'SESSION_NOT_READY');
  proof.resolve({ accountId: 'wrong-account', deviceId: 'synthetic-box-a' });
  assert.equal((await connecting).error.code, 'ACCESS_DENIED');
  assert.equal(runtime.snapshot().phase, 'failed');
  assert.deepEqual(runtime.snapshot().subject, { accountId: 'synthetic-account' });
  assert.equal(runtime.snapshot().capabilities.materialsRead, false);
});

test('late connection is closed after another device wins', async (t) => {
  const firstConnect = deferred();
  const base = fixture();
  const original = base.adapter.connect;
  base.adapter.connect = (binding) => binding.deviceId.endsWith('-a') ? firstConnect.promise : original(binding);
  const runtime = createDesktopRuntime({ mode: 'simulation', adapter: base.adapter });
  t.after(() => runtime.dispose());
  await call(runtime, 'beginSignIn'); await call(runtime, 'listDevices');
  const first = call(runtime, 'connect', 'synthetic-box-a');
  await tick();
  const second = await call(runtime, 'connect', 'synthetic-box-b');
  assert.equal(second.ok, true);
  assert.equal((await first).error.code, 'STALE_GENERATION');
  const late = await original({ accountId: 'synthetic-account', deviceId: 'synthetic-box-a' });
  firstConnect.resolve(late);
  await tick();
  assert.equal(late.closed, 1);
  assert.equal(runtime.snapshot().subject.deviceId, 'synthetic-box-b');
  assert.equal(runtime.snapshot().subject.deviceName, 'b');
});

test('device switch rejects pending old read before the old response and preserves new subject', async (t) => {
  const read = deferred();
  const base = fixture();
  const original = base.adapter.connect;
  base.adapter.connect = async (binding) => {
    const value = await original(binding);
    if (binding.deviceId.endsWith('-a')) value.request = () => read.promise;
    return value;
  };
  const runtime = createDesktopRuntime({ mode: 'simulation', adapter: base.adapter });
  t.after(() => runtime.dispose());
  await ready(runtime);
  const pending = call(runtime, 'materials.list', { limit: 20, offset: 0 });
  await tick();
  await call(runtime, 'connect', 'synthetic-box-b');
  assert.equal((await pending).error.code, 'STALE_GENERATION');
  read.resolve(response('old-a-data'));
  await tick();
  const fresh = await call(runtime, 'materials.list', { limit: 20, offset: 0 });
  assert.equal(fresh.data.items[0].materialId, 'synthetic-box-b');
});

test('local read cancellation is sender scoped, settles once, and does not close session', async (t) => {
  const read = deferred();
  const base = fixture();
  const runtime = createDesktopRuntime({ mode: 'simulation', adapter: base.adapter });
  t.after(() => runtime.dispose());
  await ready(runtime);
  let requests = 0;
  base.sessions[0].request = () => { requests += 1; return read.promise; };
  const first = call(runtime, 'materials.list', { limit: 20, offset: 0 }, { callId: 'shared-read-one' });
  const second = call(runtime, 'materials.list', { limit: 20, offset: 0 }, { callId: 'shared-read-two' });
  await tick();
  assert.equal((await call(runtime, 'cancelRead', 'shared-read-one', { senderId: 2 })).data.delivery, 'not_found');
  assert.equal((await call(runtime, 'cancelRead', 'shared-read-one')).data.delivery, 'suppressed');
  assert.equal((await first).error.code, 'READ_CANCELLED');
  assert.equal(base.sessions[0].closed, 0);
  assert.equal(requests, 1);
  read.resolve(response());
  assert.equal((await second).ok, true);
});

test('disconnect preserves login, sign-out removes it, and stale contexts cannot reconnect', async (t) => {
  const runtime = createDesktopRuntime({ mode: 'simulation' });
  t.after(() => runtime.dispose());
  await ready(runtime);
  assert.deepEqual(runtime.snapshot().subject, {
    accountId: 'synthetic-account', deviceId: 'synthetic-box-a', deviceName: '模拟盒子 A',
  });
  const generation = runtime.snapshot().generation;
  assert.equal((await call(runtime, 'disconnect')).ok, true);
  assert.equal(runtime.snapshot().phase, 'selecting_device');
  assert.deepEqual(runtime.snapshot().subject, { accountId: 'synthetic-account' });
  assert.equal((await call(runtime, 'connect', 'synthetic-box-b', { generation })).error.code, 'STALE_GENERATION');
  assert.equal((await call(runtime, 'connect', 'synthetic-box-b')).ok, true);
  await call(runtime, 'signOut');
  assert.equal(runtime.snapshot().subject, null);
});

test('invalid IPC shapes, unlisted device, and duplicate active call IDs are rejected without bypass', async (t) => {
  const read = deferred();
  const base = fixture();
  const runtime = createDesktopRuntime({ mode: 'simulation', adapter: base.adapter });
  t.after(() => runtime.dispose());
  assert.equal((await runtime.invoke('getSnapshot', [], 'renderer')).error.code, 'INVALID_REQUEST');
  assert.equal((await runtime.invoke('request', [], 1)).error.code, 'INVALID_REQUEST');
  assert.equal((await runtime.invoke('beginSignIn', [{ callId: 'valid-call', expectedGeneration: 0, token: 'forbidden' }], 1)).error.code, 'INVALID_REQUEST');
  await ready(runtime);
  assert.equal((await call(runtime, 'connect', 'unlisted-box')).error.code, 'ACCESS_DENIED');
  base.sessions[0].request = () => read.promise;
  const pending = call(runtime, 'materials.list', { limit: 20, offset: 0 }, { callId: 'duplicate-call' });
  await tick();
  for (let i = 0; i < 2; i += 1) {
    assert.equal((await call(runtime, 'materials.list', { limit: 20, offset: 0 }, { callId: 'duplicate-call' })).error.code, 'INVALID_REQUEST');
  }
  read.resolve(response());
  assert.equal((await pending).ok, true);
  assert.equal((await call(runtime, 'materials.list', { limit: 20, offset: 0, headers: {} })).error.code, 'INVALID_REQUEST');
});

test('hanging authentication times out safely and a late success cannot recover state', async (t) => {
  const login = deferred();
  const runtime = createDesktopRuntime({ mode: 'simulation', timeoutMs: 20, adapter: fixture({ signIn: () => login.promise }).adapter });
  t.after(() => runtime.dispose());
  assert.equal((await call(runtime, 'beginSignIn')).error.code, 'REQUEST_TIMEOUT');
  assert.equal(runtime.snapshot().phase, 'failed');
  login.resolve({ accountId: 'late-account' });
  await tick();
  assert.equal(runtime.snapshot().subject, null);
});

test('read timeout has no retry and observers cannot corrupt snapshots or fail operations', async (t) => {
  const read = deferred();
  const base = fixture();
  const runtime = createDesktopRuntime({ mode: 'simulation', timeoutMs: 25, adapter: base.adapter });
  t.after(() => runtime.dispose());
  const sequences = [];
  runtime.subscribe((snapshot) => { snapshot.capabilities.uploads = true; throw new Error('observer'); });
  runtime.subscribe((snapshot) => { sequences.push(snapshot.sequence); assert.equal(snapshot.capabilities.uploads, false); });
  await ready(runtime);
  let requests = 0;
  base.sessions[0].request = () => { requests += 1; return read.promise; };
  assert.equal((await call(runtime, 'materials.list', { limit: 20, offset: 0 })).error.code, 'REQUEST_TIMEOUT');
  assert.equal(requests, 1);
  assert.equal(runtime.snapshot().phase, 'ready');
  assert.ok(sequences.every((value, index) => index === 0 || value > sequences[index - 1]));
  read.resolve(response());
});

test('unknown adapter errors never expose raw messages, stack or credentials', async (t) => {
  const runtime = createDesktopRuntime({ mode: 'simulation', adapter: fixture({ signIn() { throw new Error('token=SECRET /Users/private'); } }).adapter });
  t.after(() => runtime.dispose());
  const result = await call(runtime, 'beginSignIn');
  assert.equal(result.ok, false);
  assert.equal(JSON.stringify([result, runtime.snapshot()]).includes('SECRET'), false);
  assert.equal(JSON.stringify(result).includes('stack'), false);
});

test('simulation session.close settles its own request timers and denies wrong binding', async () => {
  const adapter = createSimulationAdapter({ delayMs: 1000 });
  await assert.rejects(adapter.connect({ accountId: 'foreign', deviceId: 'synthetic-box-a' }), DesktopError);
  const session = await adapter.connect({ accountId: 'synthetic-account', deviceId: 'synthetic-box-a' });
  const pending = session.request({ method: 'GET', path: '/api/mindos/materials?limit=20&offset=0' });
  await session.close();
  await assert.rejects(pending, (error) => error.code === 'CONNECTIVITY_SESSION_EXPIRED');
});

test('dispose settles pending authentication and stops new calls/subscriptions', async () => {
  const login = deferred();
  const runtime = createDesktopRuntime({ mode: 'simulation', adapter: fixture({ signIn: () => login.promise }).adapter });
  const pending = call(runtime, 'beginSignIn');
  await tick();
  await runtime.dispose();
  assert.equal((await pending).error.code, 'STALE_GENERATION');
  assert.equal((await runtime.invoke('getSnapshot', [], 1)).error.code, 'OPERATION_NOT_ALLOWED');
  assert.throws(() => runtime.subscribe(() => {}));
  login.resolve({ accountId: 'late-account' });
});

test('malformed adapter session still closes its resources', async (t) => {
  let closed = 0;
  const runtime = createDesktopRuntime({ mode: 'simulation', adapter: fixture({
    async connect() { return { async close() { closed += 1; } }; },
  }).adapter });
  t.after(() => runtime.dispose());
  await call(runtime, 'beginSignIn'); await call(runtime, 'listDevices');
  assert.equal((await call(runtime, 'connect', 'synthetic-box-a')).error.code, 'CONTRACT_MISMATCH');
  await tick();
  assert.equal(closed, 1);
  assert.equal(runtime.snapshot().phase, 'failed');
});

for (const stage of ['connecting', 'authorizing']) {
  test(`sign-out preempts ${stage} and late completion cannot restore the session`, async (t) => {
    const completion = deferred();
    const base = fixture();
    const original = base.adapter.connect;
    let connected;
    base.adapter.connect = async (binding) => {
      connected = await original(binding);
      if (stage === 'connecting') return completion.promise;
      connected.authorize = () => completion.promise;
      return connected;
    };
    const runtime = createDesktopRuntime({ mode: 'simulation', adapter: base.adapter });
    t.after(() => runtime.dispose());
    await call(runtime, 'beginSignIn'); await call(runtime, 'listDevices');
    const pending = call(runtime, 'connect', 'synthetic-box-a');
    await tick();
    assert.equal(runtime.snapshot().phase, stage);
    assert.equal((await call(runtime, 'signOut')).ok, true);
    assert.equal((await pending).error.code, 'STALE_GENERATION');
    completion.resolve(stage === 'connecting' ? connected : { accountId: 'synthetic-account', deviceId: 'synthetic-box-a' });
    await tick();
    assert.equal(connected.closed, 1);
    assert.equal(runtime.snapshot().subject, null);
    assert.equal(runtime.snapshot().phase, 'signed_out');
  });
}

test('hanging close cannot leave disconnect pending indefinitely', async (t) => {
  const close = deferred();
  const base = fixture();
  const runtime = createDesktopRuntime({ mode: 'simulation', timeoutMs: 20, adapter: base.adapter });
  t.after(() => runtime.dispose());
  await ready(runtime);
  base.sessions[0].close = () => close.promise;
  assert.equal((await call(runtime, 'disconnect')).ok, true);
  assert.equal(runtime.snapshot().phase, 'selecting_device');
  close.resolve();
});

test('queued cancellation never dispatches and cancelled in-flight reads retain their slots', async (t) => {
  const reads = [deferred(), deferred()];
  const base = fixture();
  const runtime = createDesktopRuntime({ mode: 'simulation', adapter: base.adapter });
  t.after(() => runtime.dispose());
  await ready(runtime);
  let dispatched = 0;
  base.sessions[0].request = () => reads[dispatched++].promise;
  const first = call(runtime, 'materials.list', { limit: 20, offset: 0, keyword: 'one' }, { callId: 'active-read-one' });
  const second = call(runtime, 'materials.list', { limit: 20, offset: 0, keyword: 'two' }, { callId: 'active-read-two' });
  const queued = call(runtime, 'materials.list', { limit: 20, offset: 0, keyword: 'three' }, { callId: 'queued-read-one' });
  await tick();
  assert.equal(dispatched, 2);
  await call(runtime, 'cancelRead', 'active-read-one');
  assert.equal((await first).error.code, 'READ_CANCELLED');
  await tick();
  assert.equal(dispatched, 2);
  await call(runtime, 'cancelRead', 'queued-read-one');
  assert.equal((await queued).error.code, 'READ_CANCELLED');
  reads[0].resolve(response()); reads[1].resolve(response());
  assert.equal((await second).ok, true);
  await tick();
  assert.equal(dispatched, 2);
});

test('superseded authentication calls retain a bounded adapter budget until actual completion', async (t) => {
  const logins = Array.from({ length: 8 }, deferred);
  let started = 0;
  const runtime = createDesktopRuntime({ mode: 'simulation', adapter: fixture({
    signIn: () => logins[started++].promise,
  }).adapter });
  t.after(() => runtime.dispose());
  const pending = [];
  for (let index = 0; index < 8; index += 1) {
    pending.push(call(runtime, 'beginSignIn'));
    await tick();
  }
  assert.equal((await call(runtime, 'beginSignIn')).error.code, 'RESOURCE_EXHAUSTED');
  assert.equal(started, 8);
  await Promise.all(pending);
  for (const login of logins) login.resolve({ accountId: 'retired-account' });
  await tick();
  assert.equal(runtime.snapshot().subject, null);
});

test('timed-out sign-out cleanup is coalesced and blocks new sign-in until it actually settles', async (t) => {
  const cleanup = deferred();
  let cleanups = 0;
  const runtime = createDesktopRuntime({ mode: 'simulation', timeoutMs: 20, adapter: fixture({
    signOut() { cleanups += 1; return cleanup.promise; },
  }).adapter });
  t.after(() => runtime.dispose());
  await ready(runtime);
  assert.equal((await call(runtime, 'signOut')).error.code, 'REQUEST_TIMEOUT');
  assert.equal((await call(runtime, 'beginSignIn')).error.code, 'OPERATION_NOT_ALLOWED');
  const second = call(runtime, 'signOut');
  await tick();
  assert.equal(cleanups, 1);
  cleanup.resolve();
  assert.equal((await second).ok, true);
  assert.equal((await call(runtime, 'beginSignIn')).ok, true);
});

test('sign-out starts credential cleanup even when a later disconnect supersedes slow session close', async (t) => {
  const close = deferred();
  const identityCleanup = deferred();
  let clears = 0;
  const base = fixture({ signOut() { clears += 1; return identityCleanup.promise; } });
  const runtime = createDesktopRuntime({ mode: 'simulation', adapter: base.adapter });
  t.after(async () => { close.resolve(); identityCleanup.resolve(); await runtime.dispose(); });
  await ready(runtime);
  base.sessions[0].close = () => close.promise;
  const signingOut = call(runtime, 'signOut');
  await tick();
  const disconnecting = await call(runtime, 'disconnect');
  assert.equal(disconnecting.ok, true);
  assert.equal(runtime.snapshot().phase, 'signed_out');
  assert.equal(clears, 1);
  assert.equal((await call(runtime, 'beginSignIn')).error.code, 'OPERATION_NOT_ALLOWED');
  close.resolve();
  assert.equal((await signingOut).error.code, 'STALE_GENERATION');
  identityCleanup.resolve();
  await tick();
  assert.equal((await call(runtime, 'beginSignIn')).ok, true);
});

for (const rejected of [null, undefined, false, 0]) {
  test(`falsey adapter rejection (${String(rejected)}) is never reported as successful cleanup`, async (t) => {
    const runtime = createDesktopRuntime({ mode: 'simulation', adapter: fixture({
      signOut: () => Promise.reject(rejected),
    }).adapter });
    t.after(() => runtime.dispose());
    await ready(runtime);
    const result = await call(runtime, 'signOut');
    assert.equal(result.ok, false);
    assert.equal(result.error.code, 'REMOTE_ERROR');
    assert.equal(runtime.snapshot().phase, 'failed');
  });
}
