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
  'darwin-arm64': 'e8d05b9c6cba3a59651a8c98679a26ecb1739ab6e6b8badb3b1a894884c263f2',
  'darwin-x64': 'd77ced85753d2e1575f7b4077941a51cee500a4ac7bff4d98fb7953ef526ba1c',
  'linux-arm64': 'c1dac4dc1bf5daf1da2ba98a9a2b9d5dfb2e0c9e717983f07e1bcc1f2f61f7fa',
  'linux-x64': 'f4b77d753da6807b28c844e842afca59c98a06408262c69f285c3eb339a99eca',
  'win32-arm64': '79589fe313f3c4dd19695eb7bd34349a612b5f1b2a86076093abba46a35c2315',
  'win32-x64': '251a8a15df84137849d6ac5168bd2bd2978db2ada10a35e4db1834f112beea3a',
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
    prepare({ releaseDirectory: args[1] ? path.resolve(args[1]) : path.resolve(root, '../nexusaos-centuarai-conn-sdks/release/electron-sidecars-1.2.0'),
      outputDirectory: path.join(root, 'data/desktop') }).then(result => console.log(JSON.stringify(result, null, 2)), error => {
      console.error(JSON.stringify({ result: 'failed', code: /^[A-Z_]+$/.test(error.message) ? error.message : 'PREPARE_FAILED' }));
      process.exitCode = 1;
    });
  }
}
module.exports = { prepare };
