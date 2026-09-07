'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { prepare } = require('../scripts/prepare-real.cjs');
const { loadConfig } = require('../production/config.cjs');

test('real preparation refuses unsupported targets without producing a config', async () => {
  await assert.rejects(prepare({ releaseDirectory: '/unused', outputDirectory: '/unused', platform: 'unknown', arch: 'arm64' }), /PLATFORM_NOT_SUPPORTED/);
});
test('real preparation rejects a changed binary and does not trust a neighboring manifest', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-prepare-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const target = path.join(root, 'release/darwin-arm64');
  await fs.mkdir(target, { recursive: true });
  await fs.writeFile(path.join(target, 'nexusaos-connectivity-sidecar'), 'tampered', { mode: 0o700 });
  const output = path.join(root, 'output');
  await assert.rejects(prepare({ releaseDirectory: path.join(root, 'release'), outputDirectory: output, platform: 'darwin', arch: 'arm64' }), /SIDECAR_SHA256_MISMATCH/);
  await assert.rejects(fs.stat(output), { code: 'ENOENT' });
});
test('real preparation does not follow a sidecar symlink', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-prepare-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const target = path.join(root, 'darwin-arm64');
  await fs.mkdir(target);
  const other = path.join(root, 'other');
  await fs.writeFile(other, 'unexpected', { mode: 0o700 });
  await fs.symlink(other, path.join(target, 'nexusaos-connectivity-sidecar'));
  await assert.rejects(prepare({ releaseDirectory: root, outputDirectory: path.join(root, 'output'), platform: 'darwin', arch: 'arm64' }), { code: 'ELOOP' });
});

test('v2 preparation preserves the old v1 config, reuses exact output, and refuses a different existing v2 config', async t => {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-prepare-v2-')));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const target = path.join(root, 'release/darwin-arm64'), output = path.join(root, 'output');
  await fs.mkdir(target, { recursive: true }); await fs.mkdir(output, { mode: 0o700 });
  const sidecar = Buffer.from('synthetic sidecar for config migration test');
  // A hermetic fixture substitutes only the pinned executable digest. All
  // config digests, validation, permissions and exclusive writes remain real.
  const createHash = crypto.createHash;
  t.mock.method(crypto, 'createHash', (...args) => {
    const hash = createHash(...args); let fixture = false;
    return { update(bytes) { fixture = Buffer.isBuffer(bytes) && bytes.equals(sidecar); hash.update(bytes); return this; },
      digest(encoding) { return fixture ? 'e8d05b9c6cba3a59651a8c98679a26ecb1739ab6e6b8badb3b1a894884c263f2' : hash.digest(encoding); } };
  });
  await fs.writeFile(path.join(target, 'nexusaos-connectivity-sidecar'), sidecar, { mode: 0o700 });
  const legacy = { ...require('../config/zhijun-product.example.json'), connectivity: {
    ...require('../config/zhijun-connectivity.json'), applicationId: 'mindos-person-data-pc', purpose: 'person-data.read',
    sidecarPath: path.join(output, 'sidecar/darwin-arm64/nexusaos-connectivity-sidecar'),
    sidecarSha256: 'e8d05b9c6cba3a59651a8c98679a26ecb1739ab6e6b8badb3b1a894884c263f2',
  } };
  const legacyPath = path.join(output, 'zhijun-product.json'), legacyBytes = JSON.stringify(legacy) + '\n';
  await fs.writeFile(legacyPath, legacyBytes, { mode: 0o600 });
  const args = { releaseDirectory: path.join(root, 'release'), outputDirectory: output, platform: 'darwin', arch: 'arm64' };
  const result = await prepare(args);
  assert.equal(result.configPath, path.join(output, 'zhijun-product-v2.json'));
  assert.equal(result.applicationId, 'zhijun-desktop'); assert.equal(result.purpose, 'zhijun.workspace');
  assert.equal((await fs.stat(result.configPath)).mode & 0o777, 0o600);
  assert.equal((await loadConfig(result.configPath)).connectivity.applicationId, 'zhijun-desktop');
  assert.equal((await loadConfig(legacyPath)).connectivity.applicationId, 'mindos-person-data-pc', 'explicit legacy config remains valid');
  assert.equal(await fs.readFile(legacyPath, 'utf8'), legacyBytes);
  const original = await fs.readFile(result.configPath, 'utf8');
  await prepare({ ...args, releaseDirectory: path.join(root, 'absent-release') });
  assert.equal(await fs.readFile(result.configPath, 'utf8'), original);
  const custom = original.replace('https://boss.nexusaos.qitus.cn/prod-api', 'https://custom.example.test/prod-api');
  assert.notEqual(custom, original);
  await fs.writeFile(result.configPath, custom);
  await assert.rejects(prepare(args), /EXISTING_OUTPUT_DIFFERS/);
  assert.equal(await fs.readFile(result.configPath, 'utf8'), custom);
  assert.equal(await fs.readFile(legacyPath, 'utf8'), legacyBytes);
});
