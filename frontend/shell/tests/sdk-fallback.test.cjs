'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { createSdkRuntime } = require('../production/sdk-runtime.cjs');

async function fixture(t, connect, extraConfig = {}, timing) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-sdk-fallback-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const sidecarPath = path.join(directory, 'synthetic-sidecar');
  const bytes = Buffer.from('synthetic sidecar; never executed');
  await fs.writeFile(sidecarPath, bytes, { mode: 0o700 });
  let nativeClosed = 0;
  let nativeOptions;
  const runtime = await createSdkRuntime({
    config: { applicationId: 'zhijun-desktop', purpose: 'zhijun.workspace', requestedScopes: ['remote.p2p'],
      profile: 'SOVEREIGN_DIRECT_ONLY', gatewayHost: 'gateway.invalid', iceHost: 'ice.invalid', sidecarPath,
      sidecarSha256: crypto.createHash('sha256').update(bytes).digest('hex'), ...extraConfig },
    timing,
    consumer: { createSession: async () => { throw new Error('facade mock must not request a real ticket'); } },
    moduleLoader: async name => {
      if (name.endsWith('/admin')) return {};
      if (name.endsWith('/process')) return { createElectronProcessNativeHost: options => { nativeOptions = options; return ({
        host: Object.freeze({}), close: async () => { nativeClosed++; },
      }); } };
      return { createElectronMainFacade: () => ({ connect }) };
    },
  });
  t.after(() => runtime.close());
  return { runtime, nativeClosed: () => nativeClosed, nativeOptions: () => nativeOptions };
}

const session = selectedPath => ({ selectedPath, request: async () => ({}), close: async () => {} });
const directFailure = (detailCode = 'DIRECT_TIMEOUT', failedSessionId = 'failed-session-1') =>
  Object.assign(new Error('private transport details'), { code: 'SDK_DIRECT_UNAVAILABLE', detailCode, failedSessionId });

test('direct success does not request a fallback and projects only DIRECT', async t => {
  const requests = [];
  const f = await fixture(t, async request => { requests.push(request); return session('DIRECT'); });
  const connected = await f.runtime.connect('synthetic-device');
  assert.equal(connected.selectedPath, 'DIRECT');
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0], { device_id: 'synthetic-device', application_id: 'zhijun-desktop', client_platform: 'electron',
    requested_scopes: ['remote.p2p'], purpose: 'zhijun.workspace', profile: 'SOVEREIGN_DIRECT_ONLY', transport_policy: 'DIRECT_ONLY' });
});

test('eligible Direct failure performs exactly one bound TURN fallback and projects RELAY', async t => {
  for (const detailCode of ['DIRECT_TIMEOUT', 'ICE_FAILED']) {
    const requests = [];
    const f = await fixture(t, async request => {
      requests.push(request);
      if (requests.length === 1) throw directFailure(detailCode, `failed-${detailCode.toLowerCase()}`);
      return session('RELAY');
    });
    const connected = await f.runtime.connect('synthetic-device');
    assert.equal(connected.selectedPath, 'RELAY');
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[1], { ...requests[0], profile: 'REMOTEOPS_COMPATIBILITY', transport_policy: 'TURN_ONLY',
      fallback_from_session_id: `failed-${detailCode.toLowerCase()}`, fallback_reason: detailCode });
    await f.runtime.close();
  }
});

test('unqualified failures never fall back', async t => {
  for (const error of [
    Object.assign(new Error('timeout'), { code: 'SDK_CONNECT_TIMEOUT' }),
    directFailure('OTHER_DETAIL'), directFailure('DIRECT_TIMEOUT', ''),
    directFailure('ICE_FAILED', 'x'.repeat(129)),
  ]) {
    let calls = 0;
    const f = await fixture(t, async () => { calls++; throw error; });
    await assert.rejects(f.runtime.connect('synthetic-device'));
    assert.equal(calls, 1);
    await f.runtime.close();
  }
});

test('a failed relay attempt is terminal and is never retried', async t => {
  const requests = [];
  const f = await fixture(t, async request => {
    requests.push(request);
    throw requests.length === 1 ? directFailure() : Object.assign(new Error('relay unavailable'), { code: 'SDK_CONNECT_TIMEOUT' });
  });
  await assert.rejects(f.runtime.connect('synthetic-device'), { code: 'REQUEST_TIMEOUT' });
  assert.equal(requests.length, 2);
  assert.equal(requests[1].transport_policy, 'TURN_ONLY');
});

test('a facade cannot report a path different from the requested policy', async t => {
  let closed = 0;
  const f = await fixture(t, async () => ({ selectedPath: 'RELAY', request: async () => ({}), close: async () => { closed++; } }));
  await assert.rejects(f.runtime.connect('synthetic-device'), { code: 'CONTRACT_MISMATCH' });
  assert.equal(closed, 1);
});

test('a facade must explicitly report its selected path', async t => {
  let closed = 0;
  const f = await fixture(t, async () => ({ request: async () => ({}), close: async () => { closed++; } }));
  await assert.rejects(f.runtime.connect('synthetic-device'), { code: 'CONTRACT_MISMATCH' });
  assert.equal(closed, 1);
});

test('configured four-second Direct probe leaves the twelve-second native watchdog intact', async t => {
  const f = await fixture(t, async () => session('DIRECT'), { directConnectTimeoutMs: 4000 });
  assert.deepEqual(f.nativeOptions().args, ['--gateway-host', 'gateway.invalid', '--ice-host', 'ice.invalid', '--direct-connect-timeout-ms', '4000']);
  assert.equal(f.nativeOptions().operationTimeoutMs.connect, 12000);
  const legacy = await fixture(t, async () => session('DIRECT'));
  assert.equal(legacy.nativeOptions().args.includes('--direct-connect-timeout-ms'), false);
});

test('eight-second direct-first policy reaches native Core without changing the watchdog', async t => {
  const f = await fixture(t, async () => session('DIRECT'), { directConnectTimeoutMs: 8000 });
  assert.deepEqual(f.nativeOptions().args.slice(-2), ['--direct-connect-timeout-ms', '8000']);
  assert.equal(f.nativeOptions().operationTimeoutMs.connect, 12000);
});

test('timing captures a failed Direct and one successful relay without affecting bound fallback', async t => {
  const { createConnectionTimingAttempt } = require('../production/connection-timing.cjs');
  const events = []; let calls = 0;
  const timing = createConnectionTimingAttempt(event => { events.push(event); throw new Error('observer unavailable'); });
  const f = await fixture(t, async () => {
    calls++; if (calls === 1) throw directFailure(); return session('RELAY');
  }, { directConnectTimeoutMs: 4000 }, timing);
  assert.equal((await f.runtime.connect('private-device')).selectedPath, 'RELAY');
  assert.equal(calls, 2);
  assert.deepEqual(events.map(({ stage, status, includesTicket }) => ({ stage, status, includesTicket })), [
    { stage: 'direct_connect', status: 'error', includesTicket: true },
    { stage: 'relay_connect', status: 'ok', includesTicket: true },
  ]);
  assert.equal(JSON.stringify(events).includes('private-device'), false);
});

test('invalid Direct probe budgets are rejected before spawning a sidecar', async t => {
  for (const directConnectTimeoutMs of [0, 1000, 1999, 8001, '4000', 4000.5]) {
    await assert.rejects(fixture(t, async () => session('DIRECT'), { directConnectTimeoutMs }), { code: 'CONFIGURATION_REQUIRED' });
  }
});
