'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const common = require('../scripts/windows-release-common.cjs');
const content = require('../scripts/verify-packaged-content.cjs');
const configModule = require('../production/config.cjs');

test('Windows application verifier checks staged flavor, paths, hashes and signatures with distinct pre-sign semantics', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'zhijun-win-verify-unit-'));
  const previous = { stage: common.STAGE_DIR, shell: common.SHELL_ROOT, source: common.SOURCE_SHA256 };
  const verifierPath = require.resolve('../scripts/verify-windows-package.cjs');
  t.after(() => { common.STAGE_DIR = previous.stage; common.SHELL_ROOT = previous.shell; common.SOURCE_SHA256 = previous.source;
    delete require.cache[verifierPath]; fs.rmSync(root, { recursive: true, force: true }); });
  const write = (file, text) => { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, text); };
  common.STAGE_DIR = path.join(root, 'stage'); common.SHELL_ROOT = path.join(root, 'frontend/shell');
  const app = path.join(root, 'app'), resources = path.join(app, 'resources');
  const exe = path.join(app, '知君.exe'), sidecar = path.join(resources, common.SIDECAR_RELATIVE);
  const pe = Buffer.alloc(128); pe.write('MZ'); pe.writeUInt32LE(64, 0x3c); pe.writeUInt32LE(0x4550, 64);
  pe.writeUInt16LE(0x8664, 68); pe.writeUInt16LE(0x20b, 88);
  write(exe, pe); write(sidecar, pe); write(path.join(resources, 'app.asar'), 'synthetic');
  const product = { consumerBaseUrl: 'https://boss.nexusaos.qitus.cn/prod-api', connectivity: {
    applicationId: 'zhijun-desktop', gatewayHost: 'gateway.remote.qeeshu.com', iceHost: 'gateway.remote.qeeshu.com',
    directConnectTimeoutMs: 8000, sidecarPath: sidecar, sidecarSha256: common.digest(sidecar),
  } };
  const version = require('../package.json').version;
  write(path.join(common.STAGE_DIR, 'windows-release.json'), JSON.stringify({ unsigned: false, version }));
  write(path.join(common.STAGE_DIR, 'zhijun-product.json'), 'synthetic config');
  write(path.join(resources, 'zhijun-product.json'), 'synthetic config');
  for (const base of [resources, path.join(common.SHELL_ROOT, '..')]) {
    const web = base === resources ? 'mindos-web-dist' : 'mindos-web/dist-desktop';
    write(path.join(base, web, 'desktop.html'), 'synthetic page');
    write(path.join(base, 'shared/product-operations.json'), 'synthetic catalog');
  }
  t.mock.method(configModule, 'loadConfig', async () => product);
  const contentCheck = t.mock.method(content, 'verifyPackagedContent', () => ({ version, asarEntries: 1 }));
  const signatures = t.mock.method(common, 'verifySignature', () => true);
  delete require.cache[verifierPath];
  const { verifyWindowsApp } = require(verifierPath);
  await verifyWindowsApp(app, { afterPack: true });
  assert.deepEqual(signatures.mock.calls.map(call => call.arguments[0]), [sidecar]);
  await verifyWindowsApp(app);
  assert.deepEqual(signatures.mock.calls.slice(1).map(call => call.arguments[0]), [sidecar, exe]);
  assert.equal(contentCheck.mock.callCount(), 2);
  await assert.rejects(verifyWindowsApp(app, { unsigned: true }), /WINDOWS_STAGE_FLAVOR_INVALID/);
  write(path.join(resources, 'mindos-web-dist', 'unexpected.js'), 'stale');
  await assert.rejects(verifyWindowsApp(app), /PACKAGE_WEB_ASSETS_MISMATCH/);
  fs.unlinkSync(path.join(resources, 'mindos-web-dist', 'unexpected.js'));
  fs.appendFileSync(sidecar, 'tampered');
  await assert.rejects(verifyWindowsApp(app), /SIDECAR_DIGEST_INVALID/);
  write(sidecar, pe);
  write(path.join(common.STAGE_DIR, 'windows-release.json'), JSON.stringify({ unsigned: true, version }));
  common.SOURCE_SHA256 = common.digest(sidecar);
  const before = signatures.mock.callCount();
  await verifyWindowsApp(app, { unsigned: true });
  assert.equal(signatures.mock.callCount(), before, 'explicit unsigned mode performs no signing operations');
  common.SOURCE_SHA256 = '0'.repeat(64);
  await assert.rejects(verifyWindowsApp(app, { unsigned: true }), /SIDECAR_SOURCE_INVALID/);
});
