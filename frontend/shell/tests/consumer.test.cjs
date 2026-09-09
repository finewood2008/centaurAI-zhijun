'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { createConsumerClient, validatePassword, LOGIN_SESSION_MS } = require('../production/consumer-client.cjs');
const { createDesktopRuntime } = require('../runtime/desktop-runtime.cjs');
const config = { consumerBaseUrl: 'https://consumer.example.test/prod-api' };
const credentials = { phone: '13800000000', password: 'Synthetic-pass-1' };
const device = { deviceId: 'device-synthetic-1', deviceName: '合成盒子', role: 'owner', scopes: ['remote.p2p'], online: true, secret: 'must-not-project' };
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
  const route = new URL(url).pathname.replace('/prod-api', '');
  const canonical = ['NEXUSAOS-CONSUMER-V1', 'account-synthetic', 'client-synthetic', init.method, route,
    h['x-nexus-timestamp'], h['x-nexus-nonce'], digest].join('\n');
  assert.ok(crypto.verify('sha256', Buffer.from(canonical), store.publicKey, Buffer.from(h['x-nexus-signature'], 'base64')));
  assert.equal(init.redirect, 'error');
}

test('password input follows Admin UTF-8 byte budget and rejects extra identity fields', () => {
  assert.deepEqual(validatePassword(credentials), credentials);
  for (const value of [{ ...credentials, token: 'secret' }, { ...credentials, password: '汉'.repeat(25) },
    { ...credentials, phone: 13800000000 }, { ...credentials, password: 'a\npassword' }, { ...credentials, password: 'short' }]) {
    assert.throws(() => validatePassword(value), { code: 'INVALID_REQUEST' });
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
    if (url.endsWith('/devices')) return reply([device]);
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
      assert.deepEqual(JSON.parse(init.body), { phone: credentials.phone });
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
    if (url.endsWith('/device-claims/redeem')) {
      const body = JSON.parse(init.body);
      assert.equal(init.headers['idempotency-key'], body.idempotencyKey);
      assert.match(body.idempotencyKey, /^[0-9a-f-]{36}$/);
      assert.equal(body.claimToken, '123456');
      return reply({ deviceId: device.deviceId, deviceName: '认领盒子', bindingId: 'binding-1', ownershipEpoch: 1,
        state: 'consumed', idempotent: false, consumedAt: '2026-09-09T00:00:00Z' });
    }
    assert.fail(url);
  } });
  const receipt = await client.sendRegistrationCode(credentials.phone);
  assert.deepEqual(receipt, { expiresIn: 300 });
  assert.equal(JSON.stringify(receipt).includes('private-debug-code'), false);
  assert.deepEqual(await client.register({ ...credentials, code: '123456' }), { accountId: 'account-synthetic' });
  assert.deepEqual(await client.claimDevice(' 123456 '),
    { deviceId: device.deviceId, displayName: '认领盒子', availability: 'unknown' });
  assert.equal(calls.length, 3);
  await client.dispose();
});

test('claim retries reuse the same idempotency key after an ambiguous network failure', async () => {
  const store = memoryStore(); const keys = []; let attempts = 0;
  const client = await createConsumerClient({ config, store, fetchImpl: async (url, init) => {
    if (url.endsWith('/login')) return reply(token());
    verifySigned(store, url, init);
    const body = JSON.parse(init.body); keys.push([body.idempotencyKey, init.headers['idempotency-key']]);
    attempts++;
    if (attempts === 1) throw new Error('synthetic disconnect');
    return reply({ deviceId: device.deviceId, deviceName: null, bindingId: 'binding-1', ownershipEpoch: 1,
      state: 'consumed', idempotent: true, consumedAt: '2026-09-09T00:00:00Z' });
  } });
  await client.signIn(credentials);
  await assert.rejects(client.claimDevice('654321'), { code: 'ACCOUNT_SERVICE_UNAVAILABLE' });
  await client.claimDevice('654321');
  assert.equal(keys[0][0], keys[0][1]); assert.equal(keys[1][0], keys[1][1]); assert.equal(keys[0][0], keys[1][0]);
  await client.dispose();
});

test('claim rejects malformed codes locally before a signed network request', async () => {
  const store = memoryStore(); let claims = 0;
  const client = await createConsumerClient({ config, store, fetchImpl: async url => {
    if (url.endsWith('/login')) return reply(token());
    claims++; return reply({});
  } });
  await client.signIn(credentials);
  for (const value of ['12345', '1234567', '123 456', '１２３４５６', 'AMD-A2A-248', 'ABCD-EFGH-JK2M-NP3Q', '12\n3456']) {
    await assert.rejects(client.claimDevice(value), { code: 'INVALID_REQUEST' });
  }
  assert.equal(claims, 0);
  await client.dispose();
});

test('claim exposes safe, actionable states for expired and already-claimed codes', async () => {
  for (const [remoteCode, code] of [
    ['CLAIM_TOKEN_EXPIRED', 'CLAIM_CODE_EXPIRED'],
    ['CLAIM_TOKEN_INVALID', 'CLAIM_CODE_INVALID'],
    ['CLAIM_TOKEN_REVOKED', 'CLAIM_CODE_INVALID'],
    ['CLAIM_TOKEN_ALREADY_CONSUMED', 'DEVICE_ALREADY_CLAIMED'],
    ['DEVICE_ALREADY_CLAIMED', 'DEVICE_ALREADY_CLAIMED'],
    ['DEVICE_ALREADY_BOUND', 'DEVICE_ALREADY_CLAIMED'],
  ]) {
    const store = memoryStore();
    const client = await createConsumerClient({ config, store, fetchImpl: async url => {
      if (url.endsWith('/login')) return reply(token());
      return reply({ errorCode: remoteCode }, remoteCode === 'CLAIM_TOKEN_EXPIRED' ? 410 : 409);
    } });
    await client.signIn(credentials);
    await assert.rejects(client.claimDevice('123456'), error => {
      assert.equal(error.code, code);
      assert.equal(error.remoteCode, remoteCode);
      assert.equal(String(error.message).includes(remoteCode), false);
      return true;
    });
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
    if (url.endsWith('/devices')) return reply([unnamed]);
    assert.fail(url);
  } });
  await client.signIn(credentials);
  assert.deepEqual(await client.listDevices(), [{ deviceId: device.deviceId, displayName: device.deviceId, availability: 'online' }]);
  await client.dispose();
});

test('parallel 401s rotate once and replay only after SDK coordinator supplies the current token', async () => {
  const store = memoryStore(); let refreshes = 0; let oldReads = 0; let newReads = 0;
  const client = await createConsumerClient({ config, store, fetchImpl: async (url, init) => {
    if (url.endsWith('/login')) return reply(token());
    if (url.endsWith('/refresh')) { refreshes++; await new Promise(resolve => setImmediate(resolve)); return reply(token(2)); }
    verifySigned(store, url, init);
    if (init.headers.authorization === 'Bearer access-1') { oldReads++; return reply({}, 401); }
    newReads++; return reply([device]);
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
  for (const response of [() => new Response('not-json'), () => reply([ { ...device, role: 'stranger' } ]),
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
