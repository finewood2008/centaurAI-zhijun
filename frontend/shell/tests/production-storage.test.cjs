'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { createCredentialStore } = require('../production/credential-store.cjs');
const { validateConfig, loadConfig } = require('../production/config.cjs');
const { createProductionAdapter } = require('../production/adapter.cjs');
const { DesktopError } = require('../runtime/public-error.cjs');
const { createDesktopRuntime } = require('../runtime/desktop-runtime.cjs');
const base = 'https://consumer.example.test/prod-api';
function syntheticStorage() {
  const key = crypto.randomBytes(32);
  return { isEncryptionAvailable: () => true,
    encryptString(value) {
      const iv = crypto.randomBytes(12); const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
      return Buffer.concat([iv, cipher.update(value), cipher.final(), cipher.getAuthTag()]);
    },
    decryptString(bytes) {
      const decipher = crypto.createDecipheriv('aes-256-gcm', key, bytes.subarray(0, 12));
      decipher.setAuthTag(bytes.subarray(-16));
      return Buffer.concat([decipher.update(bytes.subarray(12, -16)), decipher.final()]).toString();
    },
  };
}
async function temporary(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-credentials-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true })); return directory;
}

test('encrypted identity is stable, EC signatures verify, sessions erase independently and origins isolate', async t => {
  const directory = await temporary(t); const safeStorage = syntheticStorage();
  const options = { directory, safeStorage, consumerBaseUrl: base };
  const store = createCredentialStore(options);
  const [first, second] = await Promise.all([store.identity(), store.identity()]);
  const accountA = await store.identity('13800000000');
  const accountB = await store.identity('13900000000');
  assert.notEqual(accountA.clientId, accountB.clientId);
  assert.equal((await store.identity('13800000000')).clientId, accountA.clientId);
  assert.equal(first.clientId, second.clientId);
  assert.ok(crypto.verify('sha256', Buffer.from('synthetic canonical'), first.publicKey, Buffer.from(first.sign(Buffer.from('synthetic canonical')), 'base64')));
  const tokens = { accessToken: 'SYNTHETIC_ACCESS', refreshToken: 'SYNTHETIC_REFRESH' };
  await store.save(tokens); assert.deepEqual(await store.load(), tokens);
  const folder = path.join(directory, 'consumer', crypto.createHash('sha256').update(base).digest('hex'));
  for (const name of await fs.readdir(folder)) {
    const content = await fs.readFile(path.join(folder, name));
    assert.equal(content.includes(Buffer.from('13800000000')), false);
    assert.equal(content.includes(Buffer.from('SYNTHETIC_')), false); assert.equal(content.includes(Buffer.from('PRIVATE KEY')), false);
    assert.equal((await fs.stat(path.join(folder, name))).mode & 0o777, 0o600);
  }
  const reloaded = createCredentialStore(options); assert.equal((await reloaded.identity()).clientId, first.clientId);
  await store.remove(); assert.equal(await reloaded.load(), undefined); assert.equal((await reloaded.identity()).clientId, first.clientId);
  const other = createCredentialStore({ ...options, consumerBaseUrl: 'https://other.example.test' });
  assert.notEqual((await other.identity()).clientId, first.clientId);
});

test('encryption unavailable/basic_text and corrupt records never downgrade or generate a replacement identity', async t => {
  const directory = await temporary(t);
  for (const safeStorage of [{ isEncryptionAvailable: () => false }, { ...syntheticStorage(), getSelectedStorageBackend: () => 'basic_text' }]) {
    const store = createCredentialStore({ directory, consumerBaseUrl: base, safeStorage });
    await assert.rejects(store.identity(), { code: 'SECURE_STORAGE_UNAVAILABLE' });
    await assert.rejects(store.save({ secret: 'synthetic' }), { code: 'SECURE_STORAGE_UNAVAILABLE' });
    await assert.rejects(store.saveRememberedLogin({ phone: '13800000000', password: 'Synthetic-remembered-1' }), { code: 'SECURE_STORAGE_UNAVAILABLE' });
    await assert.rejects(store.loadRememberedLogin(), { code: 'SECURE_STORAGE_UNAVAILABLE' });
  }
  const safeStorage = syntheticStorage(); const store = createCredentialStore({ directory, consumerBaseUrl: base, safeStorage });
  await store.identity();
  const filename = path.join(directory, 'consumer', crypto.createHash('sha256').update(base).digest('hex'), 'identities.enc');
  await fs.writeFile(filename, 'corrupt synthetic');
  await assert.rejects(createCredentialStore({ directory, consumerBaseUrl: base, safeStorage }).identity(), { code: 'SECURE_STORAGE_UNAVAILABLE' });
  assert.equal(await fs.readFile(filename, 'utf8'), 'corrupt synthetic');
});

test('remembered login is independently encrypted, origin scoped, and an unchecked save removes its old password', async t => {
  const directory = await temporary(t), safeStorage = syntheticStorage();
  const options = { directory, safeStorage, consumerBaseUrl: base };
  const store = createCredentialStore(options), credentials = { phone: '13800000000', password: 'Synthetic-remembered-1' };
  assert.equal(await store.loadRememberedLogin(), undefined);
  await store.saveRememberedLogin(credentials);
  await store.save({ accessToken: 'synthetic-session' });
  await store.remove();
  assert.deepEqual(await createCredentialStore(options).loadRememberedLogin(), credentials);
  const other = createCredentialStore({ ...options, consumerBaseUrl: 'https://other.example.test' });
  assert.equal(await other.loadRememberedLogin(), undefined);
  const folder = path.join(directory, 'consumer', crypto.createHash('sha256').update(base).digest('hex'));
  const file = path.join(folder, 'remembered-login.enc'), ciphertext = await fs.readFile(file);
  assert.equal(ciphertext.includes(Buffer.from(credentials.phone)), false);
  assert.equal(ciphertext.includes(Buffer.from(credentials.password)), false);
  assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
  await store.saveRememberedLogin({ phone: credentials.phone });
  assert.deepEqual(await store.loadRememberedLogin(), { phone: credentials.phone });
  await fs.writeFile(file, 'synthetic-corrupt-ciphertext');
  await assert.rejects(store.loadRememberedLogin(), { code: 'SECURE_STORAGE_UNAVAILABLE' });
  assert.equal(await fs.readFile(file, 'utf8'), 'synthetic-corrupt-ciphertext');
});

test('production adapter remembers only successful login and preserves remembered login across sign-out without automatic sign-in', async t => {
  const directory = await temporary(t), safeStorage = syntheticStorage();
  const store = createCredentialStore({ directory, safeStorage, consumerBaseUrl: base });
  const credentials = { phone: '13800000000', password: 'Synthetic-remembered-1' };
  const attempts = []; let rejected = false, signOuts = 0;
  const adapter = await createProductionAdapter({ config: { consumerBaseUrl: base }, directory, safeStorage,
    consumer: { restore: async () => null, dispose: async () => {},
      signIn: async input => { attempts.push(input); if (rejected) throw new DesktopError('AUTHENTICATION_FAILED'); return { accountId: 'synthetic-account' }; },
      signOut: async () => { signOuts++; await store.remove(); } } });
  t.after(() => adapter.dispose());
  assert.equal(await adapter.getRememberedLogin(), null);
  await adapter.signIn(credentials, () => true, true);
  assert.deepEqual(await store.loadRememberedLogin(), credentials);
  assert.deepEqual(await adapter.getRememberedLogin(), { phone: credentials.phone, passwordSaved: true });
  await adapter.signOut();
  assert.equal(signOuts, 1);
  assert.equal(await adapter.restore(), null);
  assert.deepEqual(await store.loadRememberedLogin(), credentials);
  assert.equal(attempts.length, 1, 'reading a saved record or restoring an expired session never invokes password login');
  rejected = true;
  await assert.rejects(adapter.signIn({ phone: '13900000000', password: 'Synthetic-wrong-1' }, () => true, true), { code: 'AUTHENTICATION_FAILED' });
  assert.deepEqual(await store.loadRememberedLogin(), credentials);
  rejected = false;
  await adapter.signInSaved(() => true, false);
  assert.deepEqual(attempts.at(-1), credentials);
  assert.deepEqual(await store.loadRememberedLogin(), { phone: credentials.phone });
  assert.deepEqual(await adapter.getRememberedLogin(), { phone: credentials.phone, passwordSaved: false });
  const count = attempts.length;
  await assert.rejects(adapter.signInSaved(() => true, true), { code: 'AUTHENTICATION_REQUIRED' });
  assert.equal(attempts.length, count);
  await assert.rejects(adapter.signIn({ phone: '13900000000', password: 'Synthetic-late-1' }, () => false, true), { code: 'STALE_GENERATION' });
  assert.deepEqual(await store.loadRememberedLogin(), { phone: credentials.phone });
});

test('a remembered record with extra keys or a numeric phone fails closed without disclosing a password', async t => {
  const directory = await temporary(t), safeStorage = syntheticStorage();
  const store = createCredentialStore({ directory, safeStorage, consumerBaseUrl: base });
  const adapter = await createProductionAdapter({ config: { consumerBaseUrl: base }, directory, safeStorage,
    consumer: { dispose: async () => {} } });
  t.after(() => adapter.dispose());
  for (const record of [{ phone: 13800000000 }, { phone: '13800000000', token: 'synthetic-extra' },
    { phone: '13800000000', password: 'Synthetic-password', token: 'synthetic-extra' }]) {
    await store.saveRememberedLogin(record);
    await assert.rejects(adapter.getRememberedLogin(), error => error.code === 'SECURE_STORAGE_UNAVAILABLE' && !error.message.includes('Synthetic-password'));
  }
});

test('both session expiry paths clear runtime login while retaining remembered credentials without auto login', async t => {
  for (const code of ['SESSION_EXPIRED', 'CONNECTIVITY_SESSION_EXPIRED']) {
    const directory = await temporary(t), safeStorage = syntheticStorage();
    const store = createCredentialStore({ directory, safeStorage, consumerBaseUrl: base });
    const credentials = { phone: '13800000000', password: 'Synthetic-remembered-1' };
    let passwordLogins = 0, signOuts = 0;
    const adapter = await createProductionAdapter({ config: { consumerBaseUrl: base }, directory, safeStorage,
      consumer: { restore: async () => null, dispose: async () => {},
        signIn: async () => { passwordLogins++; return { accountId: 'synthetic-account' }; },
        signOut: async () => { signOuts++; await store.remove(); },
        listDevices: async () => [{ deviceId: 'synthetic-device', displayName: 'synthetic', availability: 'online' }] } });
    const runtime = createDesktopRuntime({ mode: 'production', adapter: { ...adapter,
      connect: async binding => ({ authorize: async () => binding,
        request: async () => { throw new DesktopError(code); }, close: async () => {} }) } });
    t.after(() => runtime.dispose());
    let calls = 0;
    const invoke = (operation, ...args) => runtime.invoke(operation,
      [{ callId: `remember-expiry-${++calls}`, expectedGeneration: runtime.snapshot().generation }, ...args], 1);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal((await invoke('signInWithPassword', { ...credentials, rememberPassword: true })).ok, true);
    assert.equal((await invoke('listDevices')).ok, true);
    assert.equal((await invoke('connect', 'synthetic-device')).ok, true);
    assert.equal((await invoke('materials.list', { limit: 20, offset: 0 })).error.code, code);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(runtime.snapshot().subject, null);
    assert.equal(runtime.snapshot().phase, 'failed');
    assert.equal(signOuts, 1);
    assert.deepEqual(await store.loadRememberedLogin(), credentials);
    assert.deepEqual((await invoke('getRememberedLogin')).data, { phone: credentials.phone, passwordSaved: true });
    assert.equal(passwordLogins, 1, 'expiration and reading login metadata must not perform another login');
  }
});

test('invalidation during remembered-password encryption cannot commit stale credentials', async t => {
  const directory = await temporary(t), underlying = syntheticStorage();
  let current = true, invalidateOnSave = false;
  const safeStorage = { ...underlying, encryptString(value) {
    if (invalidateOnSave && JSON.parse(value).phone) current = false;
    return underlying.encryptString(value);
  } };
  const store = createCredentialStore({ directory, safeStorage, consumerBaseUrl: base });
  const previous = { phone: '13800000000', password: 'Synthetic-old-password' };
  await store.saveRememberedLogin(previous);
  const adapter = await createProductionAdapter({ config: { consumerBaseUrl: base }, directory, safeStorage,
    consumer: { signIn: async () => ({ accountId: 'synthetic-account' }), dispose: async () => {} } });
  t.after(() => adapter.dispose());
  invalidateOnSave = true;
  await assert.rejects(adapter.signIn({ phone: '13900000000', password: 'Synthetic-late-password' }, () => current, true), { code: 'STALE_GENERATION' });
  assert.deepEqual(await store.loadRememberedLogin(), previous, 'an invalidated login cannot replace another saved account');
});

test('config is explicit HTTPS only, bounded and does not accept credentials or a fake bridge flag', async t => {
  assert.equal(await loadConfig(undefined), null);
  assert.equal(validateConfig({ version: 1, consumerBaseUrl: base + '/' }).consumerBaseUrl, base);
  for (const value of [{ version: 1, consumerBaseUrl: 'http://127.0.0.1' }, { version: 1, consumerBaseUrl: 'https://user:password@example.test' },
    { version: 1, consumerBaseUrl: base, token: 'secret' }, { version: 1, consumerBaseUrl: base, bridge: true },
    { version: 1, consumerBaseUrl: base + '?token=secret' }]) assert.throws(() => validateConfig(value), { code: 'CONFIGURATION_REQUIRED' });
  const directory = await temporary(t); const file = path.join(directory, 'config.json');
  await fs.writeFile(file, JSON.stringify({ version: 1, consumerBaseUrl: base })); assert.equal((await loadConfig(file)).consumerBaseUrl, base);
  await fs.writeFile(file, ' '.repeat(16385)); await assert.rejects(loadConfig(file), { code: 'CONFIGURATION_REQUIRED' });
});

test('packaged config resolves only a relative sidecar inside the resources root', async t => {
  const directory = await temporary(t); const resources = path.join(directory, 'Resources');
  await fs.mkdir(resources);
  const file = path.join(resources, 'zhijun-product.json');
  const connectivity = { applicationId: 'zhijun-desktop', purpose: 'zhijun.workspace', requestedScopes: ['remote.p2p'],
    gatewayHost: 'gateway.remote.qeeshu.com', iceHost: 'gateway.remote.qeeshu.com',
    sidecarPath: 'connectivity-sidecar/darwin-arm64/nexusaos-connectivity-sidecar', sidecarSha256: 'a'.repeat(64),
    profile: 'SOVEREIGN_DIRECT_ONLY' };
  await fs.writeFile(file, JSON.stringify({ version: 1, consumerBaseUrl: base, connectivity }));
  const loaded = await loadConfig(file, { resourceRoot: resources });
  assert.equal(loaded.connectivity.sidecarPath,
    path.join(resources, 'connectivity-sidecar/darwin-arm64/nexusaos-connectivity-sidecar'));
  for (const sidecarPath of ['/tmp/sidecar', '../sidecar', 'connectivity-sidecar/../sidecar', 'sidecar\\evil']) {
    await fs.writeFile(file, JSON.stringify({ version: 1, consumerBaseUrl: base,
      connectivity: { ...connectivity, sidecarPath } }));
    await assert.rejects(loadConfig(file, { resourceRoot: resources }), { code: 'CONFIGURATION_REQUIRED' });
  }
});
