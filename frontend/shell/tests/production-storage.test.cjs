'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { createCredentialStore } = require('../production/credential-store.cjs');
const { validateConfig, loadConfig } = require('../production/config.cjs');
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
  }
  const safeStorage = syntheticStorage(); const store = createCredentialStore({ directory, consumerBaseUrl: base, safeStorage });
  await store.identity();
  const filename = path.join(directory, 'consumer', crypto.createHash('sha256').update(base).digest('hex'), 'identities.enc');
  await fs.writeFile(filename, 'corrupt synthetic');
  await assert.rejects(createCredentialStore({ directory, consumerBaseUrl: base, safeStorage }).identity(), { code: 'SECURE_STORAGE_UNAVAILABLE' });
  assert.equal(await fs.readFile(filename, 'utf8'), 'corrupt synthetic');
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
