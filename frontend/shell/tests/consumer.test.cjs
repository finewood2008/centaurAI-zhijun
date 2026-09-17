'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { createConsumerClient, validatePassword, validatePasswordReset, LOGIN_SESSION_MS } = require('../production/consumer-client.cjs');
const { createDesktopRuntime } = require('../runtime/desktop-runtime.cjs');
const { toPublicError } = require('../runtime/public-error.cjs');
const config = { consumerBaseUrl: 'https://consumer.example.test/prod-api' };
const credentials = { phone: '13800000000', password: 'Synthetic-pass-1' };
const device = { deviceId: 'device-synthetic-1', deviceName: '合成盒子', ownershipStatus: 'active',
  accessStatus: 'ready', securityStatus: 'normal', cloudPresence: 'online',
  capabilities: { canConnect: { enabled: true } }, secret: 'must-not-project' };
const reply = (data, code = 200) => new Response(JSON.stringify({ code, success: code === 200, data }), { headers: { 'content-type': 'application/json' } });
const token = (number = 1) => ({ accountId: 'account-synthetic', clientId: 'client-synthetic', accessToken: `access-${number}`,
  refreshToken: `refresh-${number}-${'s'.repeat(40)}`, expiresIn: 3600 });
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
function memoryStore() {
  let persisted;
  const keys = crypto.generateKeyPairSync('ec', { namedCurve: 'prime256v1' });
  return {
    identity: async () => ({ clientId: 'client-synthetic', publicKey: keys.publicKey.export({ type: 'spki', format: 'pem' }),
      sign: bytes => crypto.sign('sha256', bytes, keys.privateKey).toString('base64') }),
    publicKey: keys.publicKey,
    load: async () => persisted, save: async value => { persisted = value; }, remove: async () => { persisted = undefined; },
  };
}
function verifySigned(store, url, init) {
  const h = init.headers;
  const digest = crypto.createHash('sha256').update(init.body || Buffer.alloc(0)).digest('hex');
  assert.equal(h['x-nexus-body-sha256'], digest);
  const parsed = new URL(url);
  const route = parsed.pathname.replace('/prod-api', '');
  const versioned = route.startsWith('/app-api/v1/');
  const version = versioned ? 'NEXUSAOS-CONSUMER-APP-V1' : 'NEXUSAOS-CONSUMER-V1';
  assert.equal(h['x-nexus-signature-version'], versioned ? version : undefined);
  const encode = value => encodeURIComponent(value).replace(/[!'()*]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase());
  const query = [...parsed.searchParams].sort(([ak, av], [bk, bv]) =>
    Buffer.compare(Buffer.from(ak), Buffer.from(bk)) || Buffer.compare(Buffer.from(av), Buffer.from(bv)))
    .map(([key, value]) => `${encode(key)}=${encode(value)}`).join('&');
  const canonical = [version, 'account-synthetic', 'client-synthetic', init.method, route,
    ...(versioned ? [query] : []),
    h['x-nexus-timestamp'], h['x-nexus-nonce'], digest].join('\n');
  assert.ok(crypto.verify('sha256', Buffer.from(canonical), store.publicKey, Buffer.from(h['x-nexus-signature'], 'base64')));
  assert.equal(init.redirect, 'error');
}

const pairingView = (claimState = 'waitingAppProof') => ({
  pairingSessionId: '11111111-2222-4333-8444-555555555555',
  deviceId: device.deviceId,
  claimState,
  expiresAt: '2026-09-10T12:05:00Z',
  retryable: true,
  pollAfterMs: 1000,
  resourceVersion: 1,
  updatedAt: '2026-09-10T12:00:00Z',
  ...(['ownershipCommitted', 'projectionPending', 'deviceAckPending', 'completed', 'attentionRequired'].includes(claimState)
    ? { ownershipEpoch: 1, taskId: 'task-synthetic' } : {}),
});
const pairingReply = data => new Response(JSON.stringify({
  code: 200, success: true, data, time: '2026-09-10T12:00:00Z', requestId: 'request-synthetic-0001',
}), { headers: { 'content-type': 'application/json', 'x-request-id': 'request-synthetic-0001' } });
const modernReply = (data, status = 200, errorCode = null) => new Response(JSON.stringify({
  success: status === 200, data, errorCode, serverTime: '2026-09-14T12:00:00Z', requestId: 'request-synthetic-0001',
}), { status, headers: { 'content-type': 'application/json', 'x-request-id': 'request-synthetic-0001' } });
const bootstrapAccount = () => ({ accountId: 'account-synthetic', accountStatus: 'active',
  currentClient: { clientId: 'client-synthetic', clientStatus: 'active', isCurrent: true, hasActiveSession: true } });
const bootstrapReply = (devices, account = bootstrapAccount()) => modernReply({ account, devices,
  clients: [{ clientId: 'client-synthetic', clientStatus: 'active' }], activePairings: [], cursor: 'cursor-synthetic', snapshotSequence: 1 });
const claimReceipt = (overrides = {}) => ({ deviceId: device.deviceId, ownership: 'claimed',
  claim: { sessionId: '11111111-2222-4333-8444-555555555555', clientAttemptId: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
    status: 'redeemed', claimKind: 'factory_sticker', expiresAt: null, bindingId: 'binding-synthetic' },
  authorization: { state: 'deviceAckPending' }, ...overrides });

test('password input follows Admin UTF-8 byte budget and rejects extra identity fields', () => {
  assert.deepEqual(validatePassword(credentials), credentials);
  for (const value of [{ ...credentials, token: 'secret' }, { ...credentials, password: '汉'.repeat(25) },
    { ...credentials, phone: 13800000000 }, { ...credentials, password: 'a\npassword' }, { ...credentials, password: 'short' }]) {
    assert.throws(() => validatePassword(value), { code: 'INVALID_REQUEST' });
  }
});

test('password reset input is exact and follows the Admin phone, code and UTF-8 password contract', () => {
  const reset = { ...credentials, code: '123456' };
  assert.deepEqual(validatePasswordReset(reset), reset);
  assert.deepEqual(validatePasswordReset({ phone: credentials.phone, code: '123456', password: '安全密码' }),
    { phone: credentials.phone, code: '123456', password: '安全密码' });
  for (const value of [{ ...reset, extra: true }, { ...reset, code: '12345' }, { ...reset, code: 123456 },
    { ...reset, phone: '23800000000' }, { ...reset, password: '1234567' },
    { ...reset, password: '汉'.repeat(25) }, { ...reset, password: 'valid\rpassword' },
    { ...reset, password: 'valid\npassword' }, { ...reset, password: 'valid\0password' }]) {
    assert.throws(() => validatePasswordReset(value), { code: 'INVALID_REQUEST' });
  }
});

test('real SDK auth coordinates login, signed devices/ticket, safe projection and logout', async () => {
  const store = memoryStore(); const calls = [];
  const client = await createConsumerClient({ config, store, fetchImpl: async (url, init) => {
    calls.push(url);
    if (url.endsWith('/auth/password/login')) {
      const body = JSON.parse(init.body); assert.equal(body.clientId, 'client-synthetic');
      assert.equal(body.password, credentials.password); assert.ok(body.clientPublicKey.startsWith('-----BEGIN PUBLIC KEY-----'));
      assert.equal(init.headers.authorization, undefined); return reply(token());
    }
    verifySigned(store, url, init);
    if (url.endsWith('/sync/bootstrap')) { assert.equal(init.body, undefined); return bootstrapReply([device]); }
    if (url.endsWith('/connectivity/sessions')) return reply({ sessionId: 'synthetic' });
    if (url.endsWith('/logout')) return reply(null);
    assert.fail(url);
  } });
  assert.deepEqual(await client.signIn(credentials), { accountId: 'account-synthetic' });
  assert.deepEqual(await client.listDevices(), [{ deviceId: device.deviceId, displayName: '合成盒子', availability: 'online' }]);
  assert.deepEqual(await client.createSession(device.deviceId, { applicationId: 'synthetic.app' }), { sessionId: 'synthetic' });
  await client.signOut(); assert.equal(await store.load(), undefined);
  assert.equal(calls.length, 4); await client.dispose();
});

test('registration drops debug SMS data and claim binds one idempotency key into signed body and header', async () => {
  const store = memoryStore(); const calls = [];
  const client = await createConsumerClient({ config, store, fetchImpl: async (url, init) => {
    calls.push({ url, init });
    if (url.endsWith('/auth/sms/send')) {
      assert.deepEqual(JSON.parse(init.body), { phone: credentials.phone, scene: 'consumer_login' });
      assert.equal(init.headers.authorization, undefined);
      return reply({ expiresIn: 300, debugCode: 'private-debug-code' });
    }
    if (url.endsWith('/auth/password/register')) {
      const body = JSON.parse(init.body);
      assert.equal(body.phone, credentials.phone); assert.equal(body.code, '123456');
      assert.equal(body.password, credentials.password); assert.equal(body.clientId, 'client-synthetic');
      return reply(token());
    }
    verifySigned(store, url, init);
    if (url.endsWith('/device-console-claims/redeem')) {
      const body = JSON.parse(init.body);
      assert.equal(init.headers['idempotency-key'], body.clientAttemptId);
      assert.match(body.clientAttemptId, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
      assert.equal(body.claimCode, 'ABCDEFGH23');
      assert.deepEqual(Object.keys(body).sort(), ['claimCode', 'clientAttemptId']);
      return modernReply(claimReceipt());
    }
    assert.fail(url);
  } });
  const receipt = await client.sendRegistrationCode(credentials.phone);
  assert.deepEqual(receipt, { expiresIn: 300 });
  assert.equal(JSON.stringify(receipt).includes('private-debug-code'), false);
  assert.deepEqual(await client.register({ ...credentials, code: '123456' }), { accountId: 'account-synthetic' });
  assert.deepEqual(await client.claimDevice('ABCDEFGH23'),
    { deviceId: device.deviceId, displayName: device.deviceId, availability: 'unknown' });
  assert.equal(calls.length, 3);
  await client.dispose();
});

test('registration maps incorrect and expired SMS proofs to a safe actionable error with trace evidence', async t => {
  // Admin deliberately uses SMS_CODE_INVALID for both an incorrect code and
  // an expired/consumed proof. Cover its legacy envelope and HTTP error forms.
  for (const [reason, status, resultCode, codeLocation] of [
    ['incorrect', 200, 601, 'data'],
    ['expired', 200, 601, 'data'],
    ['incorrect', 400, 400, 'top'],
    ['expired', 422, 422, 'data'],
  ]) {
    await t.test(`${reason}: HTTP ${status}, code ${resultCode}, ${codeLocation}`, async () => {
      const store = memoryStore();
      const traceId = '175dd7a6a6fa49d8961ff666f0e41774';
      const privateMessage = `private-${reason}-${credentials.phone}-${credentials.password}-123456`;
      let calls = 0;
      const client = await createConsumerClient({ config, store, fetchImpl: async url => {
        ++calls;
        assert.ok(url.endsWith('/auth/password/register'));
        return new Response(JSON.stringify({ code: resultCode, success: false,
          ...(codeLocation === 'top' ? { errorCode: 'SMS_CODE_INVALID' } : { data: { errorCode: 'SMS_CODE_INVALID' } }),
          msg: privateMessage, requestId: traceId,
        }), { status, headers: { 'content-type': 'application/json', 'x-request-id': traceId } });
      } });
      try {
        await assert.rejects(client.register({ ...credentials, code: '123456' }), error => {
          const projected = toPublicError(error);
          assert.equal(projected.code, 'VERIFICATION_CODE_INVALID');
          assert.equal(projected.message, '验证码错误或已过期，请重新获取后重试。');
          assert.equal(projected.traceId, traceId);
          assert.equal(projected.httpStatus, status);
          assert.equal(projected.remoteCode, 'SMS_CODE_INVALID');
          for (const secret of [privateMessage, credentials.phone, credentials.password, '123456']) {
            assert.equal(JSON.stringify(projected).includes(secret), false);
            assert.equal(String(error).includes(secret), false);
          }
          return true;
        });
        assert.equal(calls, 1, 'a rejected registration must not retry or log in');
        assert.equal(await store.load(), undefined, 'a rejected proof must not establish a session');
      } finally { await client.dispose(); }
    });
  }
});

test('registration does not mislabel other input failures as SMS errors or infer from remote prose', async () => {
  for (const [status, resultCode, remoteCode, expectedCode] of [
    [400, 400, undefined, 'INVALID_REQUEST'],
    [422, 422, 'VALIDATION_ERROR', 'INVALID_REQUEST'],
    [200, 601, 'PASSWORD_ALREADY_SET', 'INVALID_REQUEST'],
    [200, 601, 'PASSWORD_INVALID', 'INVALID_REQUEST'],
    [200, 601, 'UNKNOWN_ERROR', 'AUTHENTICATION_FAILED'],
    [429, 429, 'SMS_RATE_LIMITED', 'RATE_LIMITED'],
  ]) {
    const client = await createConsumerClient({ config, store: memoryStore(), fetchImpl: async () =>
      new Response(JSON.stringify({ code: resultCode, success: false,
        data: { errorCode: remoteCode }, msg: '验证码错误或已过期',
      }), { status, headers: { 'content-type': 'application/json' } }),
    });
    try {
      await assert.rejects(client.register({ ...credentials, code: '123456' }), { code: expectedCode });
    } finally { await client.dispose(); }
  }
});

test('SMS requests select only supported templates and default to the legacy login scene', async t => {
  const calls = [];
  const client = await createConsumerClient({ config, store: memoryStore(), fetchImpl: async (url, init) => {
    assert.ok(url.endsWith('/auth/sms/send'));
    calls.push(JSON.parse(init.body));
    return reply({ expiresIn: 300 });
  } });
  t.after(() => client.dispose());
  for (const scene of [undefined, 'consumer_login', 'consumer_register', 'consumer_reset_password']) {
    assert.deepEqual(await client.sendRegistrationCode(credentials.phone, scene), { expiresIn: 300 });
  }
  assert.deepEqual(calls.map(call => call.scene),
    ['consumer_login', 'consumer_login', 'consumer_register', 'consumer_reset_password']);
  for (const scene of [null, '', 'register', 'consumer_register ', {}, [], 1]) {
    await assert.rejects(client.sendRegistrationCode(credentials.phone, scene), { code: 'INVALID_REQUEST' });
  }
  assert.equal(calls.length, 4, 'invalid scenes must not reach the account service');
});

test('password reset accepts an account-opaque response without data, clears the local session and never logs in', async () => {
  const store = memoryStore(); const calls = [];
  const client = await createConsumerClient({ config, store, fetchImpl: async (url, init) => {
    calls.push({ url, init });
    if (url.endsWith('/auth/password/login')) return reply(token());
    if (url.endsWith('/auth/password/reset')) {
      assert.equal(init.headers.authorization, undefined);
      assert.deepEqual(JSON.parse(init.body), { ...credentials, code: '123456' });
      return new Response(JSON.stringify({ code: 200, success: true, msg: 'processed' }),
        { headers: { 'content-type': 'application/json' } });
    }
    assert.fail(url);
  } });
  await client.signIn(credentials);
  assert.notEqual(await store.load(), undefined);
  const receipt = await client.resetPassword({ ...credentials, code: '123456' });
  assert.deepEqual(receipt, { processed: true });
  assert.equal(await store.load(), undefined);
  assert.equal(JSON.stringify(receipt).includes(credentials.phone), false);
  assert.equal(JSON.stringify(receipt).includes(credentials.password), false);
  assert.equal(calls.length, 2);
  await client.dispose();
});

test('password reset maps verification and reset throttling without reflecting remote messages', async () => {
  for (const [remoteCode, resultCode, code] of [
    ['SMS_CODE_INVALID', 601, 'VERIFICATION_CODE_INVALID'],
    ['PASSWORD_RESET_RATE_LIMITED', 601, 'RATE_LIMITED'],
    ['SMS_RATE_LIMITED', 601, 'RATE_LIMITED'],
    ['SMS_DAILY_LIMIT', 601, 'RATE_LIMITED'],
    [undefined, 602, 'RATE_LIMITED'],
  ]) {
    const store = memoryStore();
    const client = await createConsumerClient({ config, store, fetchImpl: async url => {
      assert.ok(url.endsWith('/auth/password/reset'));
      return reply(remoteCode ? { errorCode: remoteCode } : undefined, resultCode);
    } });
    await assert.rejects(client.resetPassword({ ...credentials, code: '123456' }), error => {
      assert.equal(error.code, code);
      assert.equal(error.remoteCode, remoteCode);
      if (remoteCode) assert.equal(String(error.message).includes(remoteCode), false);
      return true;
    });
    await client.dispose();
  }
});

test('password reset maps Admin service failures without misreporting authentication and keeps the request id', async () => {
  const store = memoryStore();
  const client = await createConsumerClient({ config, store, fetchImpl: async url => {
    assert.ok(url.endsWith('/auth/password/reset'));
    return new Response(JSON.stringify({ code: 500, success: false, msg: 'private-server-error' }), {
      headers: { 'content-type': 'application/json', 'request-id': 'reset-request-1234' },
    });
  } });

  await assert.rejects(client.resetPassword({ ...credentials, code: '123456' }), error => {
    assert.equal(error.code, 'ACCOUNT_SERVICE_UNAVAILABLE');
    assert.equal(error.traceId, 'reset-request-1234');
    assert.equal(String(error.message).includes('private-server-error'), false);
    return true;
  });
  await client.dispose();
});

test('v2 pairing API binds signed client, transport, proof hashes and authoritative response metadata', async () => {
  const store = memoryStore(); const calls = [];
  const client = await createConsumerClient({ config, store, monotonicNow: () => 1234.5,
    fetchImpl: async (url, init) => {
      if (url.endsWith('/auth/password/login')) return reply(token());
      verifySigned(store, url, init);
      const parsed = new URL(url); const body = init.body ? JSON.parse(init.body) : undefined;
      calls.push({ path: parsed.pathname + parsed.search, body, key: init.headers['idempotency-key'] });
      if (parsed.pathname.endsWith('/device-pairing/prepare')) return pairingReply({ ...pairingView(), futureField: 'ignored' });
      if (parsed.pathname.endsWith('/device-attestations/device-synthetic-1')) return pairingReply({
        deviceId: device.deviceId,
        certificatePem: '-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----',
        certificateChainPem: [], serialNumber: '01', publicKeySha256: 'a'.repeat(64),
        expiresAt: '2027-09-10T12:00:00Z', certificateStatus: 'active', statusCheckedAt: '2026-09-10T12:00:00Z',
        futureField: { ignored: true },
      });
      if (parsed.pathname.endsWith('/device-pairing/prove')) return pairingReply(pairingView('waitingDeviceProof'));
      if (parsed.pathname.endsWith('/sync/bootstrap')) return pairingReply({
        account: { accountId: 'account-synthetic', features: { electronWebBluetoothDiscoveryV1: true,
          electronBleProvisioningV2: true } }, devices: [],
        clients: [{ clientId: 'client-synthetic', clientStatus: 'active' }],
        activePairings: [pairingView('waitingDeviceProof')], cursor: 'cursor-synthetic', snapshotSequence: 1,
      });
      if (parsed.pathname.endsWith('/cancel')) return pairingReply({ cancelApplied: true, pairing: pairingView('cancelled') });
      if (parsed.pathname.endsWith('/device-pairings')) return pairingReply({ items: [pairingView('waitingDeviceProof')] });
      if (parsed.pathname.endsWith('/device-pairing/11111111-2222-4333-8444-555555555555')) return pairingReply(pairingView('waitingDeviceProof'));
      assert.fail(url);
    } });
  await client.signIn(credentials);
  const attempt = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
  const session = pairingView().pairingSessionId;
  const prepared = await client.preparePairing({ deviceId: device.deviceId, pairingSessionId: session,
    clientAttemptId: attempt, protocolVersion: 2, transport: 'ble-gatt', expiresInSeconds: 300 }, attempt);
  assert.equal(prepared.data.claimState, 'waitingAppProof');
  assert.equal(Object.hasOwn(prepared.data, 'futureField'), false);
  assert.equal(prepared.serverTime, '2026-09-10T12:00:00Z');
  assert.equal(prepared.requestId, 'request-synthetic-0001');
  assert.equal(prepared.receivedMonotonicMs, 1234.5);
  assert.match(calls.find(call => call.path.endsWith('/device-pairing/prepare')).path,
    /\/app-api\/v1\/device-pairing\/prepare$/);
  const attestation = await client.getDeviceAttestation(device.deviceId, session);
  assert.equal(attestation.data.publicKeySha256, 'a'.repeat(64));
  assert.equal(Object.hasOwn(attestation.data, 'futureField'), false);
  assert.match(calls.find(call => call.path.includes('/device-attestations/')).path,
    /\/app-api\/v1\/device-attestations\/device-synthetic-1\?pairingSessionId=/);
  const proofKey = 'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff';
  const proof = await client.provePairing({ deviceId: device.deviceId, pairingSessionId: session,
    clientId: 'client-synthetic', pairingTokenSha256: 'b'.repeat(64), protocolVersion: 2,
    transport: 'ble-gatt', securityProfile: 'NEXUSAOS_LOCAL_AEAD_V2',
    localTranscriptSha256: 'c'.repeat(64), expiresInSeconds: 300 }, proofKey);
  assert.equal(proof.data.claimState, 'waitingDeviceProof');
  assert.match(calls.find(call => call.path.endsWith('/device-pairing/prove')).path,
    /\/app-api\/v1\/device-pairing\/prove$/);
  assert.equal(calls.some(call => call.body && Object.hasOwn(call.body, 'pairingToken')), false,
    'raw pairing token must never reach Consumer API');
  assert.deepEqual((await client.getPairing(session)).data, pairingView('waitingDeviceProof'));
  assert.equal((await client.listActivePairings()).data.items.length, 1);
  assert.equal((await client.syncBootstrap()).data.snapshotSequence, 1);
  assert.equal((await client.cancelPairing(session, 'cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa')).data.cancelApplied, true);
  const cancelled = new AbortController(); cancelled.abort();
  await assert.rejects(client.getPairing(session, cancelled.signal), { name: 'AbortError' });
  assert.equal(calls[0].key, attempt);
  assert.equal(calls.find(call => call.path.endsWith('/device-pairing/prove')).key, proofKey);
  await client.dispose();
});

test('v2 pairing requires the same request id in the response header and body', async () => {
  const variants = [
    { header: null, body: 'request-synthetic-0001' },
    { header: 'request-synthetic-0001', body: null },
    { header: 'request-synthetic-0001', body: 'request-synthetic-other' },
  ];
  for (const variant of variants) {
    const store = memoryStore();
    const client = await createConsumerClient({ config, store, fetchImpl: async url => {
      if (url.endsWith('/auth/password/login')) return reply(token());
      const envelope = { code: 200, success: true, data: pairingView(), time: '2026-09-10T12:00:00Z',
        ...(variant.body ? { requestId: variant.body } : {}) };
      const headers = { 'content-type': 'application/json', ...(variant.header ? { 'x-request-id': variant.header } : {}) };
      return new Response(JSON.stringify(envelope), { headers });
    } });
    await client.signIn(credentials);
    await assert.rejects(client.getPairing(pairingView().pairingSessionId), { code: 'CONTRACT_MISMATCH' });
    await client.dispose();
  }
});

test('claim retries reuse the same idempotency key after an ambiguous network failure', async () => {
  const store = memoryStore(); const keys = []; let attempts = 0;
  const client = await createConsumerClient({ config, store, fetchImpl: async (url, init) => {
    if (url.endsWith('/login')) return reply(token());
    verifySigned(store, url, init);
    assert.ok(url.endsWith('/device-console-claims/redeem'));
    const body = JSON.parse(init.body); keys.push([body.clientAttemptId, init.headers['idempotency-key'], init.headers['x-nexus-nonce']]);
    attempts++;
    if (attempts === 1) throw new Error('synthetic disconnect');
    return modernReply(claimReceipt());
  } });
  await client.signIn(credentials);
  await assert.rejects(client.claimDevice('ZYXWVUTS76'), { code: 'ACCOUNT_SERVICE_UNAVAILABLE' });
  await client.claimDevice('ZYXWVUTS76');
  assert.equal(keys[0][0], keys[0][1]); assert.equal(keys[1][0], keys[1][1]); assert.equal(keys[0][0], keys[1][0]);
  assert.notEqual(keys[0][2], keys[1][2]);
  await client.dispose();
});

test('claim rejects malformed codes locally before a signed network request', async () => {
  const store = memoryStore(); let claims = 0;
  const client = await createConsumerClient({ config, store, fetchImpl: async url => {
    if (url.endsWith('/login')) return reply(token());
    claims++; return reply({});
  } });
  await client.signIn(credentials);
  for (const value of ['123456', '1234567890', 'ABCDEFGH2', 'ABCDEFGH234', 'A'.repeat(19), 'A'.repeat(21),
    ' ABCDEFGH23', 'ABCDEFGH23 ', 'ABCDEFGH23\n', 'abcdefgh23', 'ABCD-EFGH23', 'ABCDEF0123',
    'ABCDEFGH89', 'ＡBCDEFGH23', 1234567890]) {
    await assert.rejects(client.claimDevice(value), { code: 'INVALID_REQUEST' });
  }
  assert.equal(claims, 0);
  await client.dispose();
});

test('claim exposes safe, actionable states for expired and already-claimed codes', async () => {
  for (const [remoteCode, code] of [
    ['CONSOLE_CLAIM_EXPIRED', 'CLAIM_CODE_EXPIRED'],
    ['CONSOLE_CLAIM_NOT_FOUND', 'CLAIM_CODE_INVALID'],
    ['CONSOLE_CLAIM_INACTIVE', 'CLAIM_CODE_INVALID'],
    ['DEVICE_ALREADY_OWNED', 'DEVICE_ALREADY_CLAIMED'],
    ['OWNER_ALREADY_COMMITTED', 'DEVICE_ALREADY_CLAIMED'],
    ['OWNER_ALREADY_CHANGED', 'DEVICE_ALREADY_CLAIMED'],
    ['IDEMPOTENCY_CONFLICT', 'DEVICE_ALREADY_CLAIMED'],
  ]) {
    const store = memoryStore();
    const client = await createConsumerClient({ config, store, fetchImpl: async url => {
      if (url.endsWith('/login')) return reply(token());
      return modernReply(null, remoteCode === 'CONSOLE_CLAIM_EXPIRED' ? 410 : 409, remoteCode);
    } });
    await client.signIn(credentials);
    await assert.rejects(client.claimDevice('ABCDEFGH23'), error => {
      assert.equal(error.code, code);
      assert.equal(error.remoteCode, remoteCode);
      assert.equal(String(error.message).includes(remoteCode), false);
      return true;
    });
    await client.dispose();
  }
});

test('console claims preserve exact new 10 and existing 20 character codes and return only a receipt', async () => {
  const store = memoryStore(); const bodies = [];
  const client = await createConsumerClient({ config, store, fetchImpl: async (url, init) => {
    if (url.endsWith('/login')) return reply(token());
    assert.ok(url.endsWith('/device-console-claims/redeem'));
    verifySigned(store, url, init);
    bodies.push(JSON.parse(init.body));
    // Even a ready redeem view is not the current bootstrap authorization.
    return modernReply(claimReceipt({ authorization: { state: 'ready' }, secret: 'do-not-project' }));
  } });
  await client.signIn(credentials);
  for (const claimCode of ['ABCDEFGH23', 'ABCDEFGHIJKLMNOPQRST']) {
    assert.deepEqual(await client.claimDevice(claimCode), {
      deviceId: device.deviceId, displayName: device.deviceId, availability: 'unknown',
    });
    assert.equal(bodies.at(-1).claimCode, claimCode);
  }
  assert.notEqual(bodies[0].clientAttemptId, bodies[1].clientAttemptId);
  await client.dispose();
});

test('console gate and service errors are explicit, redacted, and never fall back to legacy redeem', async () => {
  for (const [status, remoteCode, code] of [
    [403, 'CONSOLE_CLAIM_DISABLED', 'DEVICE_AUTHORIZATION_NOT_ENABLED'],
    [403, 'FEATURE_NOT_AVAILABLE', 'DEVICE_AUTHORIZATION_NOT_ENABLED'],
    [429, 'CONSOLE_CLAIM_RATE_LIMITED', 'RATE_LIMITED'],
    [503, 'CONSOLE_CLAIM_UNAVAILABLE', 'ACCOUNT_SERVICE_UNAVAILABLE'],
    [503, 'CONSOLE_CLAIM_RATE_LIMIT_UNAVAILABLE', 'ACCOUNT_SERVICE_UNAVAILABLE'],
    [422, 'VALIDATION_ERROR', 'INVALID_REQUEST'],
  ]) {
    const store = memoryStore(); const routes = [];
    const client = await createConsumerClient({ config, store, fetchImpl: async url => {
      if (url.endsWith('/login')) return reply(token());
      routes.push(new URL(url).pathname);
      return modernReply({ private: 'server-secret' }, status, remoteCode);
    } });
    await client.signIn(credentials);
    await assert.rejects(client.claimDevice('ABCDEFGH23'), error => {
      assert.equal(error.code, code);
      assert.equal(error.remoteCode, remoteCode);
      assert.equal(error.traceId, 'request-synthetic-0001');
      assert.equal(String(error).includes('server-secret'), false);
      return true;
    });
    assert.deepEqual(routes, ['/prod-api/app-api/device-console-claims/redeem']);
    await client.dispose();
  }
});

test('ambiguous malformed console responses keep the original body and attempt for manual recovery', async () => {
  for (const response of [
    () => new Response('{', { headers: { 'content-type': 'application/json' } }),
    () => modernReply(claimReceipt({ ownership: 'unclaimed' })),
    () => modernReply(claimReceipt({ claim: { status: 'pending' } })),
    () => modernReply(claimReceipt({ authorization: { state: 'made-up' } })),
  ]) {
    const store = memoryStore(); const bodies = []; let claims = 0;
    const client = await createConsumerClient({ config, store, fetchImpl: async (url, init) => {
      if (url.endsWith('/login')) return reply(token());
      bodies.push(JSON.parse(init.body));
      return ++claims === 1 ? response() : modernReply(claimReceipt());
    } });
    await client.signIn(credentials);
    await assert.rejects(client.claimDevice('ABCDEFGH23'), { code: 'CONTRACT_MISMATCH' });
    assert.equal(claims, 1, 'no automatic retry of a potentially committed Owner write');
    await client.claimDevice('ABCDEFGH23');
    assert.deepEqual(bodies[1], bodies[0]);
    await client.dispose();
  }
});

test('claim attempts cannot carry over to another account/client login', async () => {
  const original = memoryStore(); const attempts = [];
  const secondPhone = '13900000000';
  const store = { ...original, identity: async phone => ({ ...await original.identity(phone),
    clientId: phone === secondPhone ? 'client-second' : 'client-synthetic' }) };
  const client = await createConsumerClient({ config, store, fetchImpl: async (url, init) => {
    if (url.endsWith('/login')) {
      const second = JSON.parse(init.body).phone === secondPhone;
      return reply({ ...token(), accountId: second ? 'account-second' : 'account-synthetic',
        clientId: second ? 'client-second' : 'client-synthetic' });
    }
    attempts.push({ ...JSON.parse(init.body), clientId: init.headers['x-nexus-client-id'] });
    throw new Error('synthetic response lost');
  } });
  await client.signIn(credentials);
  await assert.rejects(client.claimDevice('ABCDEFGH23'), { code: 'ACCOUNT_SERVICE_UNAVAILABLE' });
  await client.signIn({ ...credentials, phone: secondPhone });
  await assert.rejects(client.claimDevice('ABCDEFGH23'), { code: 'ACCOUNT_SERVICE_UNAVAILABLE' });
  assert.equal(attempts[0].claimCode, attempts[1].claimCode);
  assert.notEqual(attempts[0].clientAttemptId, attempts[1].clientAttemptId);
  assert.equal(attempts[1].clientId, 'client-second');
  await client.dispose();
});

test('device list only admits bootstrap devices with all four latest authorization conditions', async () => {
  const variants = [
    { ...device, deviceId: 'pending-online', accessStatus: 'deviceAckPending' },
    { ...device, deviceId: 'projection-online', accessStatus: 'projectionPending' },
    { ...device, deviceId: 'blocked-online', accessStatus: 'blocked' },
    { ...device, deviceId: 'owner-pending', ownershipStatus: 'claimPending' },
    { ...device, deviceId: 'owner-release', ownershipStatus: 'releasePending' },
    { ...device, deviceId: 'quarantined-online', securityStatus: 'quarantined' },
    { ...device, deviceId: 'disabled-online', securityStatus: 'disabled' },
    { ...device, deviceId: 'capability-disabled', capabilities: { canConnect: { enabled: false } } },
    { ...device, deviceId: 'ready-offline', cloudPresence: 'offline' },
    { ...device, deviceId: 'ready-unknown', cloudPresence: 'unknown' }, device,
  ];
  const store = memoryStore(); const routes = [];
  const client = await createConsumerClient({ config, store, fetchImpl: async (url, init) => {
    if (url.endsWith('/login')) return reply(token());
    routes.push(new URL(url).pathname); verifySigned(store, url, init);
    return bootstrapReply(variants);
  } });
  await client.signIn(credentials);
  const listed = await client.listDevices();
  assert.deepEqual(listed.map(item => [item.deviceId, item.availability]), [
    ['ready-offline', 'offline'], ['ready-unknown', 'unknown'], [device.deviceId, 'online'],
  ]);
  assert.equal(JSON.stringify(listed).includes('secret'), false);
  assert.deepEqual(routes, ['/prod-api/app-api/v1/sync/bootstrap']);
  await client.dispose();
});

test('bootstrap subject, current-client identity and account state fail closed', async () => {
  const active = bootstrapAccount();
  for (const [account, code] of [
    [{ ...active, accountId: 'other-account' }, 'CONTRACT_MISMATCH'],
    [{ ...active, currentClient: undefined }, 'CONTRACT_MISMATCH'],
    [{ ...active, currentClient: { ...active.currentClient, clientId: 'other-client' } }, 'CONTRACT_MISMATCH'],
    [{ ...active, currentClient: { ...active.currentClient, isCurrent: false } }, 'CONTRACT_MISMATCH'],
    [{ ...active, currentClient: { ...active.currentClient, clientStatus: 'revoked' } }, 'ACCESS_DENIED'],
    [{ ...active, currentClient: { ...active.currentClient, hasActiveSession: false } }, 'SESSION_EXPIRED'],
    ...['securityLocked', 'deletionPending', 'deleted'].map(accountStatus => [{ ...active, accountStatus }, 'ACCESS_DENIED']),
  ]) {
    const store = memoryStore();
    const client = await createConsumerClient({ config, store, fetchImpl: async url =>
      url.endsWith('/login') ? reply(token()) : bootstrapReply([device], account) });
    await client.signIn(credentials);
    await assert.rejects(client.listDevices(), { code });
    await client.dispose();
  }
});

test('missing or malformed bootstrap permission fields cannot be rescued by online presence', async () => {
  for (const devices of [
    [{ ...device, capabilities: {} }], [{ ...device, accessStatus: undefined }],
    [{ ...device, capabilities: { canConnect: { enabled: 'true' } } }], [device, device],
  ]) {
    const store = memoryStore();
    const client = await createConsumerClient({ config, store, fetchImpl: async url =>
      url.endsWith('/login') ? reply(token()) : bootstrapReply(devices) });
    await client.signIn(credentials);
    await assert.rejects(client.listDevices(), { code: 'CONTRACT_MISMATCH' });
    await client.dispose();
  }
});

test('a gated bootstrap never falls back to the legacy device list', async () => {
  const store = memoryStore(); const routes = [];
  const client = await createConsumerClient({ config, store, fetchImpl: async url => {
    if (url.endsWith('/login')) return reply(token());
    routes.push(new URL(url).pathname);
    return modernReply(null, 403, 'FEATURE_NOT_AVAILABLE');
  } });
  await client.signIn(credentials);
  await assert.rejects(client.listDevices(), { code: 'DEVICE_AUTHORIZATION_NOT_ENABLED', remoteCode: 'FEATURE_NOT_AVAILABLE' });
  assert.deepEqual(routes, ['/prod-api/app-api/v1/sync/bootstrap']);
  await client.dispose();
});

test('a late bootstrap result after another login cannot expose cached authorization', async () => {
  const store = memoryStore(); const entered = deferred(); const pending = deferred();
  const client = await createConsumerClient({ config, store, fetchImpl: async url => {
    if (url.endsWith('/login')) return reply(token());
    entered.resolve(); return pending.promise;
  } });
  await client.signIn(credentials);
  const listing = client.listDevices();
  const rejected = assert.rejects(listing, { code: 'SESSION_EXPIRED' });
  await entered.promise;
  await client.signIn(credentials);
  pending.resolve(bootstrapReply([device]));
  await rejected;
  assert.equal((await client.current()).accountId, 'account-synthetic', 'the replacement login remains intact');
  await client.dispose();
});

test('password login preserves safe Admin rejection codes and distinguishes rate limiting', async () => {
  for (const [remoteCode, code] of [
    ['PASSWORD_INVALID', 'AUTHENTICATION_FAILED'],
    ['AUTH_RATE_LIMITED', 'RATE_LIMITED'],
  ]) {
    const store = memoryStore();
    const client = await createConsumerClient({ config, store, fetchImpl: async url => {
      assert.ok(url.endsWith('/auth/password/login'));
      return reply({ errorCode: remoteCode }, 601);
    } });
    await assert.rejects(client.signIn(credentials), error => {
      assert.equal(error.code, code);
      assert.equal(error.remoteCode, remoteCode);
      assert.equal(String(error.message).includes(remoteCode), false);
      return true;
    });
    assert.equal(await store.load(), undefined);
    await client.dispose();
  }
});

test('encrypted login state restores for at most seven days and normal disposal preserves it', async () => {
  const store = memoryStore(); let currentNow = 1893456000000;
  const first = await createConsumerClient({ config, store, now: () => currentNow,
    fetchImpl: async url => url.endsWith('/login') ? reply(token()) : response() });
  await first.signIn(credentials);
  const saved = await store.load();
  assert.equal(saved.sessionExpiresAt, currentNow + LOGIN_SESSION_MS);
  await first.dispose();
  assert.ok(await store.load(), 'closing the app must preserve the encrypted login session');

  const restored = await createConsumerClient({ config, store, now: () => currentNow,
    fetchImpl: async () => response() });
  assert.deepEqual(await restored.restore(), { accountId: 'account-synthetic' });
  await restored.dispose();

  currentNow += LOGIN_SESSION_MS;
  const expired = await createConsumerClient({ config, store, now: () => currentNow,
    fetchImpl: async () => { assert.fail('expired local state must not reach the network'); } });
  assert.equal(await expired.restore(), null);
  assert.equal(await store.load(), undefined);
  await expired.dispose();
});

test('a device without an Admin name falls back to its stable id', async () => {
  const store = memoryStore();
  const unnamed = { ...device, deviceName: null };
  const client = await createConsumerClient({ config, store, fetchImpl: async (url, init) => {
    if (url.endsWith('/auth/password/login')) return reply(token());
    verifySigned(store, url, init);
    if (url.endsWith('/sync/bootstrap')) return bootstrapReply([unnamed]);
    assert.fail(url);
  } });
  await client.signIn(credentials);
  assert.deepEqual(await client.listDevices(), [{ deviceId: device.deviceId, displayName: device.deviceId, availability: 'online' }]);
  await client.dispose();
});

test('cached login deadline blocks current, tickets, reads and pairing without contacting the server', async () => {
  for (const operation of [client => client.current(), client => client.listDevices(),
    client => client.createSession(device.deviceId, { applicationId: 'synthetic.app' }),
    client => client.getPairing(pairingView().pairingSessionId)]) {
    const store = memoryStore(); let currentNow = 1893456000000; let calls = 0;
    const client = await createConsumerClient({ config, store, now: () => currentNow,
      fetchImpl: async url => {
        calls++;
        assert.ok(url.endsWith('/login'), 'expired cached credentials must not reach any API or logout');
        return reply(token());
      } });
    await client.signIn(credentials);
    assert.equal((await client.current()).accountId, 'account-synthetic');
    currentNow += LOGIN_SESSION_MS;
    await assert.rejects(operation(client), { code: 'SESSION_EXPIRED' });
    assert.equal(calls, 1);
    assert.equal(await store.load(), undefined);
    await client.dispose();
  }
});

test('refresh near seven days preserves the original deadline and remains restorable until that deadline', async () => {
  const store = memoryStore(); let currentNow = 1893456000000; let refreshes = 0;
  const client = await createConsumerClient({ config, store, now: () => currentNow,
    fetchImpl: async (url, init) => {
      if (url.endsWith('/login')) return reply(token());
      if (url.endsWith('/refresh')) { refreshes++; return reply(token(2)); }
      return init.headers.authorization === 'Bearer access-1' ? reply({}, 401) : bootstrapReply([device]);
    } });
  await client.signIn(credentials);
  const deadline = currentNow + LOGIN_SESSION_MS;
  currentNow = deadline - 60000;
  await client.listDevices();
  const saved = await store.load();
  assert.equal(refreshes, 1);
  assert.equal(saved.sessionExpiresAt, deadline);
  assert.equal(saved.expiresAt, deadline, 'server access lifetime cannot extend the local deadline');
  const restored = await createConsumerClient({ config, store, now: () => currentNow,
    fetchImpl: async () => assert.fail('restore and local expiration must not make network requests') });
  assert.deepEqual(await restored.restore(), { accountId: 'account-synthetic' });
  await restored.dispose();
  currentNow = deadline;
  await assert.rejects(client.createSession(device.deviceId, {}), { code: 'SESSION_EXPIRED' });
  assert.equal(refreshes, 1);
  assert.equal(await store.load(), undefined);
  await client.dispose();
});

test('a refresh response crossing the fixed login deadline cannot restore credentials or replay the request', async () => {
  const store = memoryStore(); let currentNow = 1893456000000; let reads = 0;
  const pending = deferred(); const entered = deferred();
  const client = await createConsumerClient({ config, store, now: () => currentNow,
    fetchImpl: async url => {
      if (url.endsWith('/login')) return reply(token());
      if (url.endsWith('/refresh')) { entered.resolve(); return pending.promise; }
      reads++; return reply({}, 401);
    } });
  await client.signIn(credentials);
  const deadline = currentNow + LOGIN_SESSION_MS;
  currentNow = deadline - 1;
  const read = client.listDevices();
  const rejected = assert.rejects(read, { code: 'SESSION_EXPIRED' });
  await entered.promise;
  currentNow = deadline;
  pending.resolve(reply(token(2)));
  await rejected;
  assert.equal(reads, 1);
  assert.equal(await store.load(), undefined);
  await client.dispose();
});

test('temporary refresh network failure keeps the login and its original deadline for a later retry', async () => {
  const store = memoryStore(); let refreshes = 0;
  const client = await createConsumerClient({ config, store, fetchImpl: async (url, init) => {
    if (url.endsWith('/login')) return reply(token());
    if (url.endsWith('/refresh')) {
      if (++refreshes === 1) throw new Error('synthetic temporary network failure');
      return reply(token(2));
    }
    return init.headers.authorization === 'Bearer access-1' ? reply({}, 401) : bootstrapReply([device]);
  } });
  await client.signIn(credentials);
  const deadline = (await store.load()).sessionExpiresAt;
  await assert.rejects(client.listDevices(), { code: 'ACCOUNT_SERVICE_UNAVAILABLE' });
  assert.equal((await client.current()).sessionExpiresAt, deadline);
  assert.equal((await store.load()).refreshToken, token().refreshToken);
  await client.listDevices();
  assert.equal(refreshes, 2);
  assert.equal((await store.load()).sessionExpiresAt, deadline);
  await client.dispose();
});

test('parallel 401s rotate once and replay only after SDK coordinator supplies the current token', async () => {
  const store = memoryStore(); let refreshes = 0; let oldReads = 0; let newReads = 0;
  const client = await createConsumerClient({ config, store, fetchImpl: async (url, init) => {
    if (url.endsWith('/login')) return reply(token());
    if (url.endsWith('/refresh')) { refreshes++; await new Promise(resolve => setImmediate(resolve)); return reply(token(2)); }
    verifySigned(store, url, init);
    if (init.headers.authorization === 'Bearer access-1') { oldReads++; return reply({}, 401); }
    newReads++; return bootstrapReply([device]);
  } });
  await client.signIn(credentials);
  const results = await Promise.all([client.listDevices(), client.listDevices()]);
  assert.equal(results.length, 2); assert.equal(refreshes, 1); assert.equal(oldReads, 2); assert.equal(newReads, 2);
  assert.equal((await store.load()).accessToken, 'access-2'); await client.dispose();
});

test('sign-out wins over a late login response even when a test transport ignores abort', async () => {
  const store = memoryStore(); const pending = deferred(); const entered = deferred();
  const client = await createConsumerClient({ config, store, fetchImpl: async () => { entered.resolve(); return pending.promise; } });
  const login = client.signIn(credentials); await entered.promise;
  await client.signOut(); pending.resolve(reply(token()));
  await assert.rejects(login, { code: 'STALE_GENERATION' }); assert.equal(await store.load(), undefined); await client.dispose();
});

test('runtime deadline prevents late real login from persisting a hidden authenticated session', async () => {
  const store = memoryStore(); const pending = deferred(); const entered = deferred();
  const client = await createConsumerClient({ config, store, fetchImpl: async () => { entered.resolve(); return pending.promise; } });
  const runtime = createDesktopRuntime({ mode: 'production', adapter: client, timeoutMs: 20 });
  const result = runtime.invoke('signInWithPassword', [{ callId: 'synthetic-login-1', expectedGeneration: 0 }, credentials], 1);
  await entered.promise; assert.equal((await result).error.code, 'REQUEST_TIMEOUT');
  pending.resolve(reply(token())); await new Promise(resolve => setImmediate(resolve));
  assert.equal(await store.load(), undefined); assert.equal(runtime.snapshot().subject, null); await runtime.dispose();
});

test('logout failure still clears local tokens and never reflects raw server errors', async () => {
  const store = memoryStore();
  const client = await createConsumerClient({ config, store, fetchImpl: async url => url.endsWith('/login')
    ? reply(token()) : reply({ message: 'secret-server-details', token: 'secret' }, 500) });
  await client.signIn(credentials);
  await assert.rejects(client.signOut(), error => error.code === 'AUTHENTICATION_FAILED' && !error.message.includes('secret'));
  assert.equal(await store.load(), undefined); await client.dispose();
});

test('bad response types, oversized streamed data and unknown device ownership fail closed', async () => {
  for (const response of [() => new Response('not-json'), () => bootstrapReply([ { ...device, ownershipStatus: 'stranger' } ]),
    () => new Response('x'.repeat(256 * 1024 + 1), { headers: { 'content-type': 'application/json' } })]) {
    const store = memoryStore();
    const client = await createConsumerClient({ config, store, fetchImpl: async url => url.endsWith('/login') ? reply(token()) : response() });
    await client.signIn(credentials); await assert.rejects(client.listDevices()); await client.dispose();
  }
});

test('refresh subject substitution and late refresh after logout never restore credentials', async () => {
  const store = memoryStore(); const refresh = deferred(); const entered = deferred();
  const client = await createConsumerClient({ config, store, fetchImpl: async url => {
    if (url.endsWith('/login')) return reply(token());
    if (url.endsWith('/refresh')) { entered.resolve(); return refresh.promise; }
    return url.endsWith('/logout') ? reply(null) : reply({}, 401);
  } });
  await client.signIn(credentials); const read = client.listDevices();
  const rejection = assert.rejects(read); await entered.promise; await client.signOut();
  refresh.resolve(reply({ ...token(2), accountId: 'other-account' })); await rejection;
  assert.equal(await store.load(), undefined); await client.dispose();
});

test('credential rejection clears the public runtime subject so the user can sign in again', async () => {
  const store = memoryStore();
  const client = await createConsumerClient({ config, store, fetchImpl: async url => url.endsWith('/login') ? reply(token()) : reply({}, 401) });
  const runtime = createDesktopRuntime({ mode: 'production', adapter: client });
  const context = callId => ({ callId, expectedGeneration: runtime.snapshot().generation });
  assert.equal((await runtime.invoke('signInWithPassword', [context('synthetic-login'), credentials], 1)).ok, true);
  assert.equal((await runtime.invoke('listDevices', [context('synthetic-list')], 1)).error.code, 'SESSION_EXPIRED');
  assert.equal(runtime.snapshot().subject, null); assert.equal(runtime.snapshot().phase, 'failed');
  assert.equal(await store.load(), undefined); await runtime.dispose();
});

for (const rejection of ['html401', 'AUTH_SESSION_EXPIRED', 'AUTH_REQUIRED', 'AUTHENTICATION_REQUIRED']) {
  test(`restored login ${rejection} refreshes once, then returns to login on refresh rejection`, async () => {
    const store = memoryStore(); let refreshes = 0, reads = 0;
    const first = await createConsumerClient({ config, store, fetchImpl: async () => reply(token()) });
    await first.signIn(credentials); await first.dispose();
    const client = await createConsumerClient({ config, store, fetchImpl: async url => {
      if (url.endsWith('/refresh')) { refreshes++; return new Response('expired', { status:401 }); }
      reads++;
      return rejection === 'html401' ? new Response('<html>private</html>', { status:401 })
        : new Response(JSON.stringify({ success:false, errorCode:rejection, requestId:'request-synthetic-0001',
          serverTime:'2026-09-14T12:00:00Z' }), { headers:{ 'content-type':'application/json' } });
    } });
    const runtime = createDesktopRuntime({ mode:'production', adapter:client });
    for (let i = 0; i < 20 && runtime.snapshot().phase === 'authenticating'; i++) await new Promise(r => setImmediate(r));
    assert.ok(runtime.snapshot().subject, 'persisted identity initially restores');
    const result = await runtime.invoke('listDevices', [{ callId:'restored-read', expectedGeneration:runtime.snapshot().generation }], 1);
    assert.equal(result.error.code, 'SESSION_EXPIRED');
    assert.equal(runtime.snapshot().subject, null);
    assert.equal(await store.load(), undefined);
    assert.equal(refreshes, 1); assert.equal(reads, 1);
    await runtime.dispose();
  });
}

test('HTML access-token rejection can refresh successfully without signing the user out', async () => {
  const store=memoryStore(); let refreshes=0;
  const client=await createConsumerClient({config,store,fetchImpl:async(url,init)=>{
    if(url.endsWith('/login'))return reply(token());
    if(url.endsWith('/refresh')){refreshes++;return reply(token(2));}
    return init.headers.authorization==='Bearer access-1' ? new Response('',{status:401}) : bootstrapReply([device]);
  }});
  await client.signIn(credentials);
  assert.equal((await client.listDevices()).length,1);assert.equal(refreshes,1);
  assert.equal((await store.load()).accessToken,'access-2');await client.dispose();
});

test('revoked client and explicit inactive session require login, without retrying revoked credentials', async () => {
  for(const kind of ['revoked','inactive']){
    const store=memoryStore();let refreshes=0;
    const client=await createConsumerClient({config,store,fetchImpl:async url=>{
      if(url.endsWith('/login'))return reply(token());
      if(url.endsWith('/refresh')){refreshes++;return reply(token(2));}
      const account=bootstrapAccount();account.currentClient.hasActiveSession=false;
      return kind==='revoked' ? modernReply(null,401,'CLIENT_REVOKED') : bootstrapReply([],account);
    }});
    await client.signIn(credentials);await assert.rejects(client.listDevices(),{code:'SESSION_EXPIRED'});
    assert.equal(refreshes,0);assert.equal(await store.load(),undefined);await client.dispose();
  }
});

test('service outages, throttling and ordinary forbidden responses preserve the saved login without refresh loops', async () => {
  for (const kind of ['network', 'server', 'throttled', 'forbidden', 'missing-session-field']) {
    const store = memoryStore(); let refreshes = 0;
    const client = await createConsumerClient({ config, store, fetchImpl: async url => {
      if (url.endsWith('/login')) return reply(token());
      if (url.endsWith('/refresh')) { refreshes++; return reply(token(2)); }
      if (kind === 'network') throw new TypeError('synthetic network failure');
      if (kind === 'server') return modernReply(null, 503, 'AUTHENTICATION_REQUIRED');
      if (kind === 'throttled') return modernReply(null, 429, 'RATE_LIMITED');
      if (kind === 'forbidden') return modernReply(null, 403, 'ACCESS_DENIED');
      const account = bootstrapAccount(); delete account.currentClient.hasActiveSession;
      return bootstrapReply([], account);
    } });
    await client.signIn(credentials);
    await assert.rejects(client.listDevices(), error => !['SESSION_EXPIRED', 'AUTHENTICATION_REQUIRED'].includes(error.code));
    assert.equal((await store.load()).accessToken, 'access-1', kind);
    assert.equal(refreshes, 0, kind);
    await client.dispose();
  }
});
