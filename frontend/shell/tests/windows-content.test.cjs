'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const asar = require('@electron/asar');
const { verifyPackagedContent, normalizeAsarPath, dependencies } = require('../scripts/verify-packaged-content.cjs');

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-win-content-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const shell = path.join(root, 'source'), packed = path.join(root, 'packed');
  const write = async (base, name, data) => { await fs.mkdir(path.dirname(path.join(base, name)), { recursive: true }); await fs.writeFile(path.join(base, name), data); };
  const files = ['app-icon.cjs', 'assets/centaur.png', 'main.js', 'preload.cjs', 'security.cjs',
    'provisioning-window.cjs', 'provisioning-broker.cjs', 'provisioning/preload.cjs', 'provisioning/renderer.js',
    'provisioning/setup.css', 'provisioning/setup.html', 'production/config.cjs', 'runtime/desktop-runtime.cjs'];
  for (const name of files) await write(shell, name, 'synthetic source');
  const metadata = { name: 'synthetic-shell', version: '1.2.3', main: 'main.js' };
  await write(shell, 'package.json', JSON.stringify(metadata));
  await fs.cp(shell, packed, { recursive: true });
  const lock = { packages: {} };
  for (const name of dependencies) {
    lock.packages[`node_modules/${name}`] = { version: '1.0.0' };
    await write(packed, `node_modules/${name}/package.json`, JSON.stringify({ name, version: '1.0.0' }));
  }
  await write(shell, 'package-lock.json', JSON.stringify(lock));
  let count = 0;
  return { shell, packed, write, metadata, verify: async () => {
    const archive = path.join(root, `app-${count++}.asar`);
    await asar.createPackage(packed, archive);
    return verifyPackagedContent(archive, shell);
  } };
}
test('Windows content verifier accepts matching source and lockfile, rejects stale code', async t => {
  const f = await fixture(t);
  assert.equal((await f.verify()).version, '1.2.3');
  await f.write(f.packed, 'production/config.cjs', 'stale code');
  await assert.rejects(f.verify(), /ASAR_SOURCE_MISMATCH/);
});
test('Windows ASAR backslash listing is normalized without changing allowed paths', () => {
  for (const name of ['production/config.cjs', 'node_modules/@nexusaos/connectivity-electron/package.json', 'main.js']) {
    assert.equal(normalizeAsarPath(`\\${name.replaceAll('/', '\\')}`), name);
    assert.equal(normalizeAsarPath(`/${name}`), name);
  }
});
test('Windows content verifier rejects extra files and embedded secrets', async t => {
  for (const name of ['private-token.txt', 'node_modules/tslib/tests/fixture.json', 'node_modules/tslib/certificate.pfx', 'node_modules/tslib/node_modules/extra/package.json']) {
    const f = await fixture(t);
    await f.write(f.packed, name, '{}');
    await assert.rejects(f.verify(), /ASAR_(UNEXPECTED_ENTRY|TEST_OR_KEY_MATERIAL|UNDECLARED_DEPENDENCY)/);
  }
});
test('Windows content verifier rejects mismatched dependencies, version and test marker', async t => {
  const f = await fixture(t);
  await f.write(f.packed, 'package.json', JSON.stringify({ ...f.metadata, zhijunProvisioningTestBuild: true }));
  await assert.rejects(f.verify(), /ASAR_METADATA_INVALID/);
  await f.write(f.packed, 'package.json', JSON.stringify({ ...f.metadata, version: '0.0.0' }));
  await assert.rejects(f.verify(), /ASAR_METADATA_INVALID/);
  await f.write(f.packed, 'package.json', JSON.stringify(f.metadata));
  await f.write(f.packed, 'node_modules/tslib/package.json', JSON.stringify({ version: '9.0.0' }));
  await assert.rejects(f.verify(), /ASAR_DEPENDENCY_VERSION_INVALID/);
});
