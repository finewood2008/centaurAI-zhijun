'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');
const { loadConfig } = require('../production/config.cjs');
const { verifyPackagedContent } = require('./verify-packaged-content.cjs');
const common = require('./windows-release-common.cjs');

function regularFile(filename) {
  const stat = fs.lstatSync(filename);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('PACKAGE_FILE_INVALID');
}
function verifyResourceTree(directory, expected) {
  // Hash the complete frontend/catalog, not just desktop.html; catch stale or extra assets.
  const inventory = root => {
    const result = new Map();
    function walk(dir, relative = '') {
      for (const name of fs.readdirSync(dir)) {
        const full = path.join(dir, name), key = relative ? `${relative}/${name}` : name;
        const stat = fs.lstatSync(full);
        if (stat.isSymbolicLink()) throw new Error('PACKAGE_SYMLINK_INVALID');
        if (stat.isDirectory()) walk(full, key);
        else { regularFile(full); result.set(key, common.digest(full)); }
      }
    }
    walk(root); return result;
  };
  const actual = inventory(directory), wanted = inventory(expected);
  if (actual.size !== wanted.size || [...wanted].some(([name, hash]) => actual.get(name) !== hash)) throw new Error('PACKAGE_WEB_ASSETS_MISMATCH');
}
async function verifyWindowsApp(appDirectory, { unsigned = false, afterPack = false } = {}) {
  const stage = JSON.parse(fs.readFileSync(path.join(common.STAGE_DIR, 'windows-release.json')));
  if (stage.unsigned !== unsigned || stage.version !== require('../package.json').version) throw new Error('WINDOWS_STAGE_FLAVOR_INVALID');
  const resources = path.join(appDirectory, 'resources');
  const executable = path.join(appDirectory, '知君.exe');
  const configFile = path.join(resources, 'zhijun-product.json');
  for (const file of [executable, configFile, path.join(resources, 'app.asar'), path.join(resources, common.SIDECAR_RELATIVE)]) regularFile(file);
  common.assertPeX64(executable);
  const config = await loadConfig(configFile, { resourceRoot: resources });
  if (config.consumerBaseUrl !== 'https://boss.nexusaos.qitus.cn/prod-api'
      || config.connectivity?.applicationId !== 'zhijun-desktop'
      || config.connectivity.gatewayHost !== 'gateway.remote.qeeshu.com'
      || config.connectivity.iceHost !== 'gateway.remote.qeeshu.com'
      || config.connectivity.directConnectTimeoutMs !== 8000
      || config.connectivity.sidecarPath !== path.join(resources, common.SIDECAR_RELATIVE)) throw new Error('PACKAGE_PRODUCTION_CONFIG_INVALID');
  if (!fs.readFileSync(configFile).equals(fs.readFileSync(path.join(common.STAGE_DIR, 'zhijun-product.json')))) throw new Error('PACKAGE_STAGED_CONFIG_MISMATCH');
  const sidecar = config.connectivity.sidecarPath;
  common.assertPeX64(sidecar);
  if (common.digest(sidecar) !== config.connectivity.sidecarSha256) throw new Error('SIDECAR_DIGEST_INVALID');
  if (unsigned) {
    if (common.digest(sidecar) !== common.SOURCE_SHA256) throw new Error('SIDECAR_SOURCE_INVALID');
  } else {
    common.verifySignature(sidecar);
    if (!afterPack) common.verifySignature(executable);
  }
  const content = verifyPackagedContent(path.join(resources, 'app.asar'));
  verifyResourceTree(path.join(resources, 'mindos-web-dist'), path.join(common.SHELL_ROOT, '../mindos-web/dist-desktop'));
  if (common.digest(path.join(resources, 'shared/product-operations.json')) !== common.digest(path.join(common.SHELL_ROOT, '../shared/product-operations.json'))) throw new Error('PACKAGE_CATALOG_MISMATCH');
  if (fs.existsSync(path.join(resources, 'connectivity-sidecar/darwin-arm64'))) throw new Error('MAC_RESOURCE_IN_WINDOWS_PACKAGE');
  return { ...content, sidecarSha256: config.connectivity.sidecarSha256 };
}
async function main(argv = process.argv.slice(2)) {
  common.assertBuildHost();
  const unsigned = common.parseUnsigned(argv);
  const release = common.releaseDir(unsigned);
  const stem = common.artifactStem(unsigned);
  const installer = path.join(release, `${stem}.exe`), zip = path.join(release, `${stem}.zip`);
  for (const file of [installer, zip]) regularFile(file);
  // NSIS's bootstrap executable can be x86 while installing an x64 application.
  if (!unsigned) common.verifySignature(installer);
  const result = await verifyWindowsApp(path.join(release, 'win-unpacked'), { unsigned });
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'zhijun-win-verify-'));
  try {
    const script = '$ErrorActionPreference="Stop"; Expand-Archive -LiteralPath $env:ZHIJUN_VERIFY_ZIP -DestinationPath $env:ZHIJUN_VERIFY_DIR';
    execFileSync(path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe'),
      ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')],
      { env: { ...process.env, ZHIJUN_VERIFY_ZIP: zip, ZHIJUN_VERIFY_DIR: temporary }, stdio: 'pipe', timeout: 120000 });
    await verifyWindowsApp(temporary, { unsigned });
  } finally { fs.rmSync(temporary, { recursive: true, force: true, maxRetries: 4, retryDelay: 250 }); }
  const report = { ok: true, platform: 'win32', arch: 'x64', signed: !unsigned, distributionClass: unsigned ? 'unsigned-internal-only' : 'signed',
    ...result, installerInstallTested: false,
    artifacts: [installer, zip].map(file => ({ file: path.basename(file), sha256: common.digest(file) })) };
  fs.writeFileSync(path.join(release, 'verification-windows.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify(report));
  return report;
}
if (require.main === module) main().catch(error => { console.error(/^[A-Z0-9_]+$/.test(error.message) ? error.message : 'WINDOWS_PACKAGE_VERIFICATION_FAILED'); process.exitCode = 1; });
module.exports = { verifyWindowsApp, verifyResourceTree, main };
