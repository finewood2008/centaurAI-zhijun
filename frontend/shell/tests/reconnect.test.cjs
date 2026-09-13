'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createDesktopRuntime } = require('../runtime/desktop-runtime.cjs');
const { DesktopError } = require('../runtime/public-error.cjs');

const tick = () => new Promise(resolve => setImmediate(resolve));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check, message) {
  const deadline = Date.now() + 2000;
  while (!check()) {
    assert.ok(Date.now() < deadline, message || 'condition did not settle');
    await delay(2);
  }
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function fixture(t, options = {}) {
  const sessions = [], bindings = [], snapshots = [];
  let connects = 0, signOuts = 0, signs = 0, requests = 0, authorizes = 0;
  function connection(binding) {
    const value = { failures: [], closes: 0,
      authorize: async () => { authorizes++; return { ...binding,
        ...(options.product ? { product: true, workspaceId: 'a'.repeat(64) } : {}) }; },
      request: async (...args) => { requests++; return options.request?.(...args); },
      close: async () => { value.closes++; },
      onFailure: handler => value.failures.push(handler),
      fail: code => value.failures.forEach(handler => handler(new DesktopError(code))),
    };
    sessions.push(value); return value;
  }
  const runtime = createDesktopRuntime({ mode: 'production', timeoutMs: 200,
    reconnectDelaysMs: options.delays || [1, 2, 3], reconnectStabilityMs: options.stabilityMs || 60000,
    adapter: {
      signIn: async () => { signs++; return { accountId: 'synthetic-account' }; },
      signOut: async () => { signOuts++; }, dispose: async () => {},
      listDevices: async () => ['device-a', 'device-b'].map(deviceId => ({ deviceId, displayName: deviceId, availability: 'online' })),
      connect: async binding => {
        bindings.push({ ...binding }); connects++;
        return options.connect ? options.connect(connects, binding, connection) : connection(binding);
      },
    } });
  t.after(() => runtime.dispose());
  runtime.subscribe(value => snapshots.push(value));
  let call = 0;
  const invoke = (operation, ...args) => runtime.invoke(operation,
    [{ callId: `reconnect-test-${++call}`, expectedGeneration: runtime.snapshot().generation }, ...args], 1);
  assert.equal((await invoke('signInWithPassword', { phone: '13800000000', password: 'Synthetic-password-1' })).ok, true);
  assert.equal((await invoke('listDevices')).ok, true);
  assert.equal((await invoke('connect', 'device-a')).ok, true);
  await tick();
  return { runtime, invoke, sessions, bindings, snapshots,
    counts: () => ({ connects, signOuts, signs, requests, authorizes }) };
}

for (const code of ['CONNECTIVITY_SESSION_EXPIRED', 'TRANSPORT_UNAVAILABLE']) {
  test(`${code} reconnects the same account/device without password login or logout`, async t => {
    const f = await fixture(t);
    const offset = f.snapshots.length;
    f.sessions[0].fail(code);
    assert.equal(f.runtime.snapshot().subject.accountId, 'synthetic-account');
    assert.equal(f.runtime.snapshot().capabilities.product, false);
    await until(() => f.counts().connects === 2 && f.runtime.snapshot().phase === 'ready');
    assert.deepEqual(f.bindings, Array(2).fill({ accountId: 'synthetic-account', deviceId: 'device-a' }));
    assert.equal(f.counts().authorizes, 2, 'a fresh session must independently authorize');
    assert.equal(f.counts().signOuts, 0);
    assert.equal(f.counts().signs, 1);
    assert.ok(f.sessions[0].closes >= 1);
    await tick();
    assert.ok(f.snapshots.slice(offset).every(s => s.subject?.accountId === 'synthetic-account'));
  });
}

for (const code of ['SESSION_EXPIRED', 'AUTHENTICATION_REQUIRED']) {
  test(`${code} is an account terminal and never reconnects`, async t => {
    const f = await fixture(t);
    f.sessions[0].fail(code);
    await until(() => f.counts().signOuts === 1);
    await delay(20);
    assert.equal(f.runtime.snapshot().subject, null);
    assert.equal(f.counts().connects, 1);
  });
}

test('duplicate old-session callbacks and pending reconnect callbacks remain single-flight', async t => {
  const pending = deferred();
  const f = await fixture(t, { connect: (count, binding, create) => count === 2
    ? pending.promise.then(() => create(binding)) : create(binding) });
  for (let index = 0; index < 10; index++) f.sessions[0].fail('CONNECTIVITY_SESSION_EXPIRED');
  await until(() => f.counts().connects === 2);
  for (let index = 0; index < 10; index++) f.sessions[0].fail('TRANSPORT_UNAVAILABLE');
  await delay(20);
  assert.equal(f.counts().connects, 2);
  pending.resolve();
  await until(() => f.runtime.snapshot().phase === 'ready');
  assert.equal(f.counts().signOuts, 0);
});

for (const action of ['disconnect', 'signOut', 'dispose', 'switch-device']) {
  test(`${action} cancels pending reconnect and closes its late successful transport`, async t => {
    const pending = deferred();
    const f = await fixture(t, { connect: (count, binding, create) => count === 2
      ? pending.promise.then(() => create(binding)) : create(binding) });
    f.sessions[0].fail('CONNECTIVITY_SESSION_EXPIRED');
    await until(() => f.counts().connects === 2);
    if (action === 'dispose') await f.runtime.dispose();
    else assert.equal((await f.invoke(action === 'switch-device' ? 'connect' : action,
      ...(action === 'switch-device' ? ['device-b'] : []))).ok, true);
    const state = f.runtime.snapshot();
    pending.resolve();
    await until(() => f.sessions.some(s => s !== f.sessions[0] && s.closes > 0));
    await delay(20);
    assert.equal(f.runtime.snapshot().generation, state.generation);
    assert.equal(f.runtime.snapshot().phase, state.phase);
    assert.deepEqual(f.runtime.snapshot().subject, state.subject);
    assert.equal(f.counts().connects, action === 'switch-device' ? 3 : 2);
    assert.equal(f.counts().signOuts, action === 'signOut' ? 1 : 0);
    if (action === 'switch-device') assert.equal(state.subject.deviceId, 'device-b');
  });
}

test('explicit disconnect cancels a scheduled backoff before another adapter call', async t => {
  const f = await fixture(t, { delays: [30, 30, 30] });
  f.sessions[0].fail('TRANSPORT_UNAVAILABLE');
  assert.equal((await f.invoke('disconnect')).ok, true);
  await delay(60);
  assert.equal(f.counts().connects, 1);
  assert.equal(f.runtime.snapshot().subject.accountId, 'synthetic-account');
  assert.equal(f.counts().signOuts, 0);
});

test('transient retry exhaustion stops after the bounded budget and retains the account', async t => {
  const f = await fixture(t, { connect: (count, binding, create) => {
    if (count > 1) throw new DesktopError('TRANSPORT_UNAVAILABLE');
    return create(binding);
  } });
  f.sessions[0].fail('CONNECTIVITY_SESSION_EXPIRED');
  await until(() => f.counts().connects === 4 && f.runtime.snapshot().phase === 'failed');
  await delay(30);
  assert.equal(f.counts().connects, 4);
  assert.equal(f.runtime.snapshot().subject.accountId, 'synthetic-account');
  assert.equal(f.counts().signOuts, 0);
});

test('authorization rejection stops retrying immediately without revoking the account login', async t => {
  const f = await fixture(t, { connect: (count, binding, create) => {
    const connected = create(binding);
    if (count > 1) connected.authorize = async () => { throw new DesktopError('ACCESS_DENIED'); };
    return connected;
  } });
  f.sessions[0].fail('CONNECTIVITY_SESSION_EXPIRED');
  await until(() => f.runtime.snapshot().phase === 'failed');
  await delay(30);
  assert.equal(f.runtime.snapshot().error.code, 'ACCESS_DENIED');
  assert.equal(f.counts().connects, 2);
  assert.equal(f.counts().signOuts, 0);
});

test('short-lived successful transports do not reset the bounded retry budget', async t => {
  const f = await fixture(t, { delays: [1, 1], stabilityMs: 60000 });
  f.sessions[0].fail('TRANSPORT_UNAVAILABLE');
  await until(() => f.counts().connects === 2 && f.runtime.snapshot().phase === 'ready');
  f.sessions[1].fail('TRANSPORT_UNAVAILABLE');
  await until(() => f.counts().connects === 3 && f.runtime.snapshot().phase === 'ready');
  f.sessions[2].fail('TRANSPORT_UNAVAILABLE');
  await until(() => f.runtime.snapshot().phase === 'failed');
  await delay(20);
  assert.equal(f.counts().connects, 3);
  assert.equal(f.counts().signOuts, 0);
});

test('a stable recovered connection replenishes the retry budget for a later outage', async t => {
  const f = await fixture(t, { delays: [1], stabilityMs: 15 });
  f.sessions[0].fail('TRANSPORT_UNAVAILABLE');
  await until(() => f.counts().connects === 2 && f.runtime.snapshot().phase === 'ready');
  await delay(30);
  f.sessions[1].fail('CONNECTIVITY_SESSION_EXPIRED');
  await until(() => f.counts().connects === 3 && f.runtime.snapshot().phase === 'ready');
  assert.equal(f.counts().signOuts, 0);
});

test('fresh authorization cannot silently move a recovered session to another workspace', async t => {
  const f = await fixture(t, { product: true, connect: (count, binding, create) => {
    const connected = create(binding);
    if (count > 1) connected.authorize = async () => ({ ...binding, product: true, workspaceId: 'b'.repeat(64) });
    return connected;
  } });
  f.sessions[0].fail('CONNECTIVITY_SESSION_EXPIRED');
  await until(() => f.runtime.snapshot().phase === 'failed');
  assert.equal(f.runtime.snapshot().error.code, 'ACCESS_DENIED');
  assert.equal(f.runtime.snapshot().capabilities.product, false);
  assert.equal(f.counts().signOuts, 0);
});

test('account expiry while requesting a replacement transport logs out and stops recovery', async t => {
  const f = await fixture(t, { connect: (count, binding, create) => {
    if (count > 1) throw new DesktopError('SESSION_EXPIRED');
    return create(binding);
  } });
  f.sessions[0].fail('CONNECTIVITY_SESSION_EXPIRED');
  await until(() => f.counts().signOuts === 1);
  await delay(20);
  assert.equal(f.runtime.snapshot().subject, null);
  assert.equal(f.counts().connects, 2);
});

test('an uncertain write remains unknown and is never replayed after automatic reconnection', async t => {
  const f = await fixture(t, { product: true, request: async () => { throw new DesktopError('CONNECTIVITY_SESSION_EXPIRED'); } });
  const result = await f.invoke('product.uploadCreate', { requestId: 'synthetic-upload-request',
    fileName: 'synthetic.txt', contentType: 'text/plain', size: 1 });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, 'WRITE_OUTCOME_UNKNOWN');
  await until(() => f.counts().connects === 2 && f.runtime.snapshot().phase === 'ready');
  await delay(20);
  assert.equal(f.counts().requests, 1, 'new transport authorization must not resend the unknown write');
  assert.equal(f.counts().signOuts, 0);
});

test('heartbeat failure during an in-flight write preserves unknown outcome without replay', async t => {
  const pending = deferred();
  const f = await fixture(t, { product: true, request: () => pending.promise });
  const write = f.invoke('product.uploadCreate', { requestId: 'synthetic-heartbeat-upload',
    fileName: 'synthetic.txt', contentType: 'text/plain', size: 1 });
  await until(() => f.counts().requests === 1);
  f.sessions[0].fail('TRANSPORT_UNAVAILABLE');
  const result = await write;
  assert.equal(result.ok, false);
  assert.equal(result.error.code, 'WRITE_OUTCOME_UNKNOWN');
  await until(() => f.counts().connects === 2 && f.runtime.snapshot().phase === 'ready');
  pending.reject(new DesktopError('TRANSPORT_UNAVAILABLE'));
  await delay(20);
  assert.equal(f.counts().requests, 1);
  assert.equal(f.counts().signOuts, 0);
});
