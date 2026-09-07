'use strict';
// Build-time input only. The installed host never imports a neighboring repo.
const fs = require('node:fs/promises');
const { constants } = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { validateConfig } = require('../production/config.cjs');
const product = require('../config/zhijun-product.example.json');
const binding = require('../config/zhijun-connectivity.json');
const binaries = Object.freeze({
  'darwin-arm64': '9613fd07bdd6db4146bf8ddd54d062d513e8171b22b811b29b0f5e9ded80ea59',
  'darwin-x64': 'f4cbc846a369a3e0c7340dcafa7e17660887e6b644fe28d666cc737b3a36aa60',
  'linux-arm64': '3939cd8d0a68a171893921d0bfb6aa480d5afb779bbfbd55d4914acc59669f95',
  'linux-x64': 'f94c754bc6c5a10e2083935884605f58bb787af0d6ecf0c571f114275dc892bd',
  'win32-arm64': 'd346257eae35a6fd2e2e4743f8182c1003fcd665a36f613ead1d3d5c3acdecc0',
  'win32-x64': '4f9000c21a867a516c3961b08931cddb68e5bf3d21c5fd71b0012f85cf57c14a',
});
const sha256 = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
async function readRegular(filename) {
  const file = await fs.open(filename, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size > 128 * 1024 * 1024 || (stat.mode & 0o022)) throw new Error('UNTRUSTED_SIDECAR_FILE');
    const bytes = await file.readFile();
    if (bytes.length > 128 * 1024 * 1024) throw new Error('SIDECAR_TOO_LARGE');
    return bytes;
  } finally { await file.close(); }
}
async function ensureDirectory(directory) {
  // Check ancestors before creating anything, so data/ symlinks cannot redirect
  // the generated config or executable outside the selected canonical root.
  const parent = path.dirname(directory);
  if (parent !== directory) {
    const ancestors = [];
    for (let current = parent; ; current = path.dirname(current)) {
      ancestors.push(current);
      if (path.dirname(current) === current) break;
    }
    for (const ancestor of ancestors.reverse()) {
      let stat;
      try { stat = await fs.lstat(ancestor); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      if (stat && (!stat.isDirectory() || stat.isSymbolicLink())) throw new Error('UNTRUSTED_OUTPUT_DIRECTORY');
      if (!stat) await fs.mkdir(ancestor, { mode: 0o700 });
    }
  }
  await fs.mkdir(directory, { mode: 0o700 }).catch(error => { if (error.code !== 'EEXIST') throw error; });
  const stat = await fs.lstat(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o022)) throw new Error('UNTRUSTED_OUTPUT_DIRECTORY');
}
async function put(filename, bytes, mode, expectedHash) {
  try {
    const existing = await readRegular(filename);
    if (sha256(existing) !== expectedHash) throw new Error('EXISTING_OUTPUT_DIFFERS');
    if (mode & 0o100) {
      await fs.access(filename, constants.X_OK).catch(() => { throw new Error('SIDECAR_NOT_EXECUTABLE'); });
    }
    return;
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  // Exclusive creation: never overwrite local custom configuration or binaries.
  await fs.writeFile(filename, bytes, { flag: 'wx', mode });
}
async function prepare({ releaseDirectory, outputDirectory, platform = process.platform, arch = process.arch }) {
  const expected = binaries[`${platform}-${arch}`];
  if (!expected || !['darwin', 'linux'].includes(platform)) throw new Error('PLATFORM_NOT_SUPPORTED');
  const target = `${platform === 'win32' ? 'windows' : platform}-${arch === 'x64' ? 'amd64' : arch}`;
  const executable = `nexusaos-connectivity-sidecar${platform === 'win32' ? '.exe' : ''}`;
  const output = path.resolve(outputDirectory);
  const sidecarDirectory = path.join(output, 'sidecar', target);
  const sidecarPath = path.join(sidecarDirectory, executable);
  let bytes;
  try { bytes = await readRegular(sidecarPath); }
  catch (error) {
    if (error.code !== 'ENOENT') throw error;
    bytes = await readRegular(path.join(releaseDirectory, target, executable));
  }
  if (sha256(bytes) !== expected) throw new Error('SIDECAR_SHA256_MISMATCH');
  const config = validateConfig({ ...product, connectivity: { ...binding, sidecarPath, sidecarSha256: expected } });
  await ensureDirectory(output);
  await ensureDirectory(path.join(output, 'sidecar'));
  await ensureDirectory(sidecarDirectory);
  await put(sidecarPath, bytes, 0o700, expected);
  // v1 remains opt-in through ZHIJUN_DESKTOP_CONFIG; never rewrite it during
  // preparation for the separately registered full-workspace application.
  const configPath = path.join(output, 'zhijun-product-v2.json');
  const configBytes = Buffer.from(JSON.stringify(config, null, 2) + '\n');
  await put(configPath, configBytes, 0o600, sha256(configBytes));
  return { configPath, applicationId: binding.applicationId, purpose: binding.purpose,
    requestedScopes: binding.requestedScopes, sidecarSha256: expected, target,
    artifactMode: 'development-ad-hoc', productionReleaseValidated: false };
}
if (require.main === module) {
  const root = path.resolve(__dirname, '../../..');
  const args = process.argv.slice(2);
  if (args.length && !(args.length === 2 && args[0] === '--sdk-release')) {
    console.error('用法：node frontend/shell/scripts/prepare-real.cjs [--sdk-release <SDK release目录>]');
    process.exitCode = 2;
  } else {
    prepare({ releaseDirectory: args[1] ? path.resolve(args[1]) : path.resolve(root, '../nexusaos-centuarai-conn-sdks/release/electron-sidecars-1.2.1'),
      outputDirectory: path.join(root, 'data/desktop') }).then(result => console.log(JSON.stringify(result, null, 2)), error => {
      console.error(JSON.stringify({ result: 'failed', code: /^[A-Z_]+$/.test(error.message) ? error.message : 'PREPARE_FAILED' }));
      process.exitCode = 1;
    });
  }
}
module.exports = { prepare };
