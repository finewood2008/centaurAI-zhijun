'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const crypto = require('node:crypto');
const { createConsumerClient } = require('../production/consumer-client.cjs');
const { createSdkRuntime, createTicketProvider, mapConnectionError } = require('../production/sdk-runtime.cjs');
const { createProductionAdapter } = require('../production/adapter.cjs');
const { DesktopError, toPublicError } = require('../runtime/public-error.cjs');
const binding = { device_id: 'synthetic-device', application_id: 'zhijun-desktop', client_platform: 'electron',
  requested_scopes: ['remote.p2p'], purpose: 'zhijun.workspace', profile: 'SOVEREIGN_DIRECT_ONLY', transport_policy: 'DIRECT_ONLY' };
const success = data => new Response(JSON.stringify({ code: 200, data }), { headers: { 'content-type': 'application/json' } });
const rejection = (code, msg, status = 200) => new Response(JSON.stringify({ code, msg, success: false,
  data: { token: 'SYNTHETIC_PRIVATE_SENTINEL' } }), { status, headers: { 'content-type': 'application/json' } });
const validTicket = body => {
  const relay = body.transportPolicy === 'TURN_ONLY';
  return { ...body, sessionId: relay ? 'synthetic-relay-session' : 'synthetic-session',
    streamId: relay ? 'synthetic-relay-stream' : 'synthetic-stream', gatewayUrl: 'wss://synthetic.invalid',
    token: 'SYNTHETIC_PRIVATE_SENTINEL', transportPolicy: relay ? 'TURN_ONLY' : 'DIRECT_ONLY',
    connectBefore: '2099-01-01T00:00:00Z', expiresAt: '2099-01-01T01:00:00Z',
    iceServers: relay ? [{ urls: ['turns:synthetic.invalid'], username: 'synthetic-user',
      credential: 'SYNTHETIC_PRIVATE_SENTINEL', expiresAt: '2099-01-01T00:30:00Z' }]
      : [{ urls: ['stun:synthetic.invalid'] }] };
};

async function fixture(t, respond, nativeResult, failClose = false) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-connect-errors-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const executable = path.join(directory, 'never-executed-sidecar'); const executableBytes = Buffer.from('isolated metadata fixture; never spawned');
  await fs.writeFile(executable, executableBytes, { mode: 0o700 });
  let sessionRequests = 0, nativeRequests = 0, closed = 0;
  const config = { consumerBaseUrl: 'https://synthetic.invalid/prod-api', connectivity: { applicationId: 'zhijun-desktop', purpose: 'zhijun.workspace',
    requestedScopes: ['remote.p2p'], profile: 'SOVEREIGN_DIRECT_ONLY', gatewayHost: 'synthetic.invalid', iceHost: 'synthetic.invalid',
    sidecarPath: executable, sidecarSha256: crypto.createHash('sha256').update(executableBytes).digest('hex') } };
  const store = { identity: async () => ({ clientId: 'synthetic-client', publicKey: 'synthetic-public-key', sign: async () => 'synthetic-signature' }),
    load: async () => undefined, save: async () => {}, remove: async () => {} };
  const client = await createConsumerClient({ config, store, timeoutMs: 5, fetchImpl: async (url, init) => {
    if (url.endsWith('/login')) return success({ accountId: 'synthetic-account', clientId: 'synthetic-client', accessToken: 'synthetic-access',
      refreshToken: 'synthetic-refresh-' + 'a'.repeat(40), expiresIn: 3600 });
    assert.ok(url.endsWith('/connectivity/sessions')); sessionRequests++;
    return respond(JSON.parse(init.body), init.signal);
  } });
  t.after(() => client.dispose());
  await client.signIn({ phone: '13800000000', password: 'synthetic-password' });
  const sdkNative = await import(pathToFileURL(path.join(path.dirname(require.resolve('@nexusaos/connectivity-electron')), 'native-host.js')).href);
  const adapter = await createProductionAdapter({ config, consumer: client, bridge: { authorize: async () => { throw Error('not reached'); } },
    runtimeFactory: args => createSdkRuntime({ ...args, moduleLoader: async name => {
      if (!name.endsWith('/process')) return import(name); // Real SDK facade and strict Admin parser.
      return { createElectronProcessNativeHost: options => ({
        host: sdkNative.createElectronSidecarNativeHost({ runtime: 'electron-main', tickets: options.tickets,
          createRequestId: () => crypto.randomUUID(), sidecar: { exchange: async message => {
            nativeRequests++;
            return nativeResult ? nativeResult(message) : { protocol_version: 1, type: 'response', operation: 'connect', request_id: message.request_id, session_id: 'synthetic-native' };
          } } }),
        close: async () => { closed++; if (failClose) throw Error('SYNTHETIC_PRIVATE_SENTINEL cleanup error'); },
      }) };
    } }) });
  t.after(() => adapter.dispose());
  return { connect: () => adapter.connect({ accountId: 'synthetic-account', deviceId: binding.device_id }),
    counts: () => ({ sessionRequests, nativeRequests, closed }) };
}

test('legacy Admin 601 policy rejection survives real Consumer, SDK provider and adapter without native dispatch', async t => {
  const f = await fixture(t, () => rejection(601, 'Connectivity应用或权限未获准'));
  await assert.rejects(f.connect(), error => {
    assert.deepEqual(toPublicError(error), { code: 'APPLICATION_AUTHORIZATION_DENIED',
      message: '桌面应用或所申请的权限未获账号服务批准，请联系管理员核对应用登记和授权。', recovery: 'none',
      httpStatus: 200, phase: 'ticket', sdkCode: 'SDK_SESSION_PROVIDER_FAILED' });
    assert.equal(JSON.stringify(toPublicError(error)).includes('SYNTHETIC_PRIVATE_SENTINEL'), false); return true;
  });
  assert.deepEqual(f.counts(), { sessionRequests: 1, nativeRequests: 0, closed: 1 });
});

test('unknown Admin failures do not guess app registration; timeout and access rejection stay distinct', async t => {
  for (const [respond, code] of [
    [() => rejection(601, 'unknown SYNTHETIC_PRIVATE_SENTINEL'), 'ACCOUNT_SERVICE_UNAVAILABLE'],
    [() => rejection(500, 'SYNTHETIC_PRIVATE_SENTINEL', 503), 'ACCOUNT_SERVICE_UNAVAILABLE'],
    [() => new Response('<html>SYNTHETIC_PRIVATE_SENTINEL</html>', { status: 502, headers: { 'content-type': 'text/html' } }), 'ACCOUNT_SERVICE_UNAVAILABLE'],
    [() => rejection(403, 'SYNTHETIC_PRIVATE_SENTINEL', 403), 'ACCESS_DENIED'],
    [() => { throw Error('https://synthetic.invalid/SYNTHETIC_PRIVATE_SENTINEL'); }, 'ACCOUNT_SERVICE_UNAVAILABLE'],
    [(_body, signal) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(Error('SYNTHETIC_PRIVATE_SENTINEL')), { once: true })), 'REQUEST_TIMEOUT'],
  ]) {
    const f = await fixture(t, respond);
    await assert.rejects(f.connect(), error => {
      const safe = toPublicError(error); assert.equal(safe.code, code); assert.equal(safe.phase, 'ticket');
      assert.equal(JSON.stringify(safe).includes('SYNTHETIC_PRIVATE_SENTINEL'), false); return true;
    });
    assert.deepEqual(f.counts(), { sessionRequests: 1, nativeRequests: 0, closed: 1 });
  }
});

test('native cleanup failure cannot mask the original account policy rejection', async t => {
  const f = await fixture(t, () => rejection(601, 'Connectivity应用或权限未获准'), undefined, true);
  await assert.rejects(f.connect(), error => {
    assert.equal(error.code, 'APPLICATION_AUTHORIZATION_DENIED');
    assert.equal(error.sdkCode, 'SDK_SESSION_PROVIDER_FAILED');
    assert.equal(JSON.stringify(toPublicError(error)).includes('SYNTHETIC_PRIVATE_SENTINEL'), false); return true;
  });
  assert.deepEqual(f.counts(), { sessionRequests: 1, nativeRequests: 0, closed: 1 });
});

test('ticket binding, required fields and STUN-only policy remain enforced by the real SDK parser', async t => {
  for (const change of [value => ({ ...value, applicationId: 'wrong-app' }), value => ({ ...value, profile: undefined }),
    value => ({ ...value, iceServers: [{ urls: ['turn:synthetic.invalid'], credential: 'SYNTHETIC_PRIVATE_SENTINEL' }] })]) {
    const f = await fixture(t, body => success(change(validTicket(body))));
    await assert.rejects(f.connect(), error => {
      assert.equal(error.code, 'CONTRACT_MISMATCH'); assert.equal(error.phase, 'ticket'); assert.equal(error.sdkCode, 'SDK_INVALID_SESSION');
      assert.equal(JSON.stringify(toPublicError(error)).includes('SYNTHETIC_PRIVATE_SENTINEL'), false); return true;
    });
    assert.equal(f.counts().nativeRequests, 0);
  }
});

test('native direct failures retain only fixed SDK diagnostics, separately from account-service errors', async t => {
  const cases = [['SDK_DIRECT_UNAVAILABLE', 'ICE_FAILED', 'DIRECT_CONNECTION_UNAVAILABLE'],
    ['SDK_DIRECT_UNAVAILABLE', 'DIRECT_TIMEOUT', 'DIRECT_CONNECTION_UNAVAILABLE'],
    ['SDK_CONNECT_TIMEOUT', undefined, 'REQUEST_TIMEOUT'], ['IPC_SIDECAR_EXITED', undefined, 'TRANSPORT_UNAVAILABLE'],
    ['UNKNOWN_PRIVATE_SENTINEL', undefined, 'CONTRACT_MISMATCH', 'IPC_INVALID_MESSAGE']];
  for (const [sdkCode, detailCode, code, expectedSdkCode = sdkCode] of cases) {
    const f = await fixture(t, body => success(validTicket(body)), request => ({ protocol_version: 1, type: 'error',
      operation: 'connect', request_id: request.request_id, code: sdkCode, ...(detailCode ? { detail_code: detailCode } : {}) }));
    await assert.rejects(f.connect(), error => {
      const safe = toPublicError(error); assert.equal(safe.code, code); assert.equal(safe.phase, 'native');
      assert.equal(safe.sdkCode, expectedSdkCode);
      assert.equal(safe.detailCode, detailCode); assert.equal(JSON.stringify(safe).includes('PRIVATE_SENTINEL'), false); return true;
    });
    assert.deepEqual(f.counts(), { sessionRequests: 1, nativeRequests: 1, closed: 1 });
  }
});

test('real SDK binds one eligible Direct failure into exactly one TURN fallback', async t => {
  const tickets = [];
  const f = await fixture(t, body => { tickets.push(body); return success(validTicket(body)); }, request => {
    if (request.binding.transport_policy === 'DIRECT_ONLY') {
      return { protocol_version: 1, type: 'error', operation: 'connect', request_id: request.request_id,
        code: 'SDK_DIRECT_UNAVAILABLE', detail_code: 'ICE_FAILED', failed_session_id: 'synthetic-session' };
    }
    assert.equal(request.binding.transport_policy, 'TURN_ONLY');
    return { protocol_version: 1, type: 'response', operation: 'connect', request_id: request.request_id,
      session_id: 'synthetic-relay-native', policy: 'TURN_ONLY', selected_path: 'RELAY' };
  });
  const connected = await f.connect();
  assert.equal(connected.selectedPath, 'RELAY');
  assert.equal(tickets.length, 2);
  assert.equal(tickets[0].transportPolicy, 'DIRECT_ONLY');
  assert.equal(tickets[0].fallbackFromSessionId, undefined);
  assert.deepEqual({ profile: tickets[1].profile, transportPolicy: tickets[1].transportPolicy,
    fallbackFromSessionId: tickets[1].fallbackFromSessionId, fallbackReason: tickets[1].fallbackReason }, {
    profile: 'REMOTEOPS_COMPATIBILITY', transportPolicy: 'TURN_ONLY',
    fallbackFromSessionId: 'synthetic-session', fallbackReason: 'ICE_FAILED',
  });
  assert.deepEqual(f.counts(), { sessionRequests: 2, nativeRequests: 2, closed: 0 });
  await connected.close();
});

test('ticket failures are invocation-local and public diagnostic allowlists reject arbitrary strings', async () => {
  const admin = await import('@nexusaos/connectivity-electron/admin');
  let failFirst;
  const provider = createTicketProvider(admin, { createSession: (deviceId, body) => deviceId === 'synthetic-first'
    ? new Promise((_, reject) => { failFirst = () => reject(new DesktopError('APPLICATION_AUTHORIZATION_DENIED')); })
    : Promise.resolve(validTicket(body)) });
  const first = provider.issue({ ...binding, device_id: 'synthetic-first' });
  const failed = assert.rejects(first, { code: 'APPLICATION_AUTHORIZATION_DENIED' });
  assert.equal(typeof (await provider.issue(binding)).value, 'string'); failFirst(); await failed;
  const safe = toPublicError(new DesktopError('TRANSPORT_UNAVAILABLE', { phase: 'SYNTHETIC_PRIVATE_SENTINEL', sdkCode: 'SYNTHETIC_PRIVATE_SENTINEL', detailCode: 'SYNTHETIC_PRIVATE_SENTINEL' }));
  assert.equal(safe.phase, undefined); assert.equal(safe.sdkCode, undefined); assert.equal(safe.detailCode, undefined);
  assert.equal(mapConnectionError({ code: '__proto__' }).code, 'TRANSPORT_UNAVAILABLE');
});
