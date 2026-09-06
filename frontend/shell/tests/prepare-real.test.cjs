'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { prepare } = require('../scripts/prepare-real.cjs');

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
