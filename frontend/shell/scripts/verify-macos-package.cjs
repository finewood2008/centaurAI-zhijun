'use strict'
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const crypto = require('node:crypto')
const { execFileSync, spawnSync } = require('node:child_process')
const asar = require('@electron/asar')
const { loadConfig } = require('../production/config.cjs')

const shell = path.resolve(__dirname, '..')
const metadata = require('../package.json')
const testFlavor = process.env.ZHIJUN_PACKAGE_FLAVOR === 'test'
const release = path.join(shell, testFlavor ? 'release-test' : 'release')
const stem = testFlavor
  ? `Zhijun-Provisioning-Test-${metadata.version}-mac-arm64`
  : `Zhijun-${metadata.version}-mac-arm64`
const appName = testFlavor ? '知君配网测试版.app' : '知君.app'
const expectedBundleId = testFlavor ? 'com.qeeshu.zhijun.provisioning-test' : 'com.qeeshu.zhijun'
const expectedTeam = 'GLHM545ZLS'
const digest = filename => crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex')
const productionPackages = new Map([
  ['@nexusaos/connectivity-contracts', '1.1.0'],
  ['@nexusaos/connectivity-electron', '1.3.1'],
  ['@nexusaos/consumer-contracts', '1.0.0'],
  ['@nexusaos/device-discovery-contracts', '1.0.0'],
  ['@nexusaos/device-discovery-electron', '1.0.0'],
  ['@nexusaos/local-provisioning-contracts', '2.0.0'],
  ['@nexusaos/local-provisioning-core', '2.0.0'],
  ['@nexusaos/local-provisioning-electron-ble', '2.0.0'],
  ['@noble/hashes', '1.8.0'],
  ['@peculiar/asn1-cms', '2.9.4'],
  ['@peculiar/asn1-csr', '2.9.4'],
  ['@peculiar/asn1-ecc', '2.9.4'],
  ['@peculiar/asn1-pfx', '2.9.4'],
  ['@peculiar/asn1-pkcs8', '2.9.4'],
  ['@peculiar/asn1-pkcs9', '2.9.4'],
  ['@peculiar/asn1-rsa', '2.9.4'],
  ['@peculiar/asn1-schema', '2.9.4'],
  ['@peculiar/asn1-x509', '2.9.4'],
  ['@peculiar/asn1-x509-attr', '2.9.4'],
  ['@peculiar/utils', '2.0.3'],
  ['@peculiar/x509', '1.14.0'],
  ['asn1js', '3.0.10'],
  ['pvtsutils', '1.3.6'],
  ['pvutils', '1.2.0'],
  ['reflect-metadata', '0.2.2'],
  ['tslib', '2.8.1'],
  ['tsyringe', '4.10.0'],
  ['tsyringe/node_modules/tslib', '1.14.1'],
])
const legacyTestPackages = new Map([
  ['@nexusaos/device-provisioning-electron', '1.0.0'],
  ['@nexusaos/device-provisioning-uni', '1.0.1'],
])
const packagedDependencies = new Map([...productionPackages,
  ...(testFlavor ? legacyTestPackages : [])])
const packagePath = name => `/node_modules/${name}`
const expectedAsarEntries = new Set([
  '/node_modules', '/node_modules/@nexusaos', '/node_modules/@noble', '/node_modules/@peculiar',
  '/app-icon.cjs', '/assets', '/assets/centaur.png', '/main.js', '/package.json', '/preload.cjs',
  '/provisioning-window.cjs', '/provisioning-broker.cjs', '/provisioning', '/provisioning/preload.cjs',
  ...(testFlavor ? ['/provisioning/preload.mjs'] : []), '/provisioning/renderer.js',
  '/provisioning/picker.cjs', '/provisioning/setup.css', '/provisioning/setup.html',
  '/production', '/production/adapter.cjs', '/production/business-bridge.cjs', '/production/config.cjs',
  '/production/consumer-client.cjs', '/production/credential-store.cjs', '/production/sdk-runtime.cjs',
  '/production/connection-timing.cjs',
  '/production/file-security.cjs',
  '/runtime', '/runtime/adapters.cjs', '/runtime/desktop-runtime.cjs', '/runtime/materials.cjs',
  '/runtime/microphone-permission.cjs', '/runtime/native-save.cjs', '/runtime/product-policy.cjs',
  '/runtime/product-session.cjs', '/runtime/public-error.cjs', '/runtime/read-scheduler.cjs',
  '/runtime/v2-request-scheduler.cjs', '/security.cjs',
  ...[...packagedDependencies.keys()].flatMap(name => [packagePath(name), `${packagePath(name)}/package.json`]),
])
// Each packaged dependency is integrity-locked in package-lock.json and checked
// again by exact name/version below. No undeclared package root is accepted.
const allowedAsarPrefixes = [...packagedDependencies.keys()].map(name => `${packagePath(name)}/`)
const unsafeDependencyMaterial = /\/(?:test|tests|fixtures|vectors)(?:\/|$)|\.(?:pem|key|crt|cer|der|p12|pfx)$/i
function regularFile(filename, executable = false) {
  const stat = fs.lstatSync(filename)
  if (stat.isSymbolicLink() || !stat.isFile() || (stat.mode & 0o022) || (executable && !(stat.mode & 0o100))) {
    throw new Error(`PACKAGE_FILE_INVALID:${filename}`)
  }
}
async function verifyApp(appPath) {
  execFileSync('/usr/bin/codesign', ['--verify', '--deep', '--strict', '--verbose=2', appPath], { stdio: ['ignore', 'pipe', 'pipe'], timeout: 60_000 })
  const signature = spawnSync('/usr/bin/codesign', ['-dv', '--verbose=4', appPath], { encoding: 'utf8', timeout: 15_000 })
  if (signature.status !== 0 || !`${signature.stdout}\n${signature.stderr}`.includes(`TeamIdentifier=${expectedTeam}`)) throw new Error('APP_SIGNATURE_INVALID')
  const plist = JSON.parse(execFileSync('/usr/bin/plutil', ['-convert', 'json', '-o', '-', path.join(appPath, 'Contents/Info.plist')], { encoding: 'utf8', timeout: 10_000 }))
  if (plist.CFBundleIdentifier !== expectedBundleId || plist.CFBundleShortVersionString !== metadata.version ||
      typeof plist.NSMicrophoneUsageDescription !== 'string' || !plist.NSMicrophoneUsageDescription ||
      typeof plist.NSBluetoothAlwaysUsageDescription !== 'string' || !plist.NSBluetoothAlwaysUsageDescription) throw new Error('INFO_PLIST_INVALID')
  const resources = path.join(appPath, 'Contents/Resources')
  const configPath = path.join(resources, 'zhijun-product.json')
  const asarPath = path.join(resources, 'app.asar')
  const desktopEntry = path.join(resources, 'mindos-web-dist/desktop.html')
  const operationCatalog = path.join(resources, 'shared/product-operations.json')
  for (const filename of [configPath, asarPath, desktopEntry, operationCatalog]) regularFile(filename)
  const config = await loadConfig(configPath, { resourceRoot: resources })
  if (!testFlavor && config.connectivity.directConnectTimeoutMs !== 8000) throw new Error('DIRECT_BUDGET_INVALID')
  const sidecar = config.connectivity.sidecarPath
  regularFile(sidecar, true)
  if (digest(sidecar) !== config.connectivity.sidecarSha256) throw new Error('SIDECAR_DIGEST_INVALID')
  const sidecarSignature = spawnSync('/usr/bin/codesign', ['-dv', '--verbose=4', sidecar], { encoding: 'utf8', timeout: 15_000 })
  const sidecarInfo = `${sidecarSignature.stdout || ''}\n${sidecarSignature.stderr || ''}`
  if (sidecarSignature.status !== 0 || !sidecarInfo.includes(`TeamIdentifier=${expectedTeam}`)) throw new Error('SIDECAR_SIGNATURE_INVALID')
  const fileInfo = execFileSync('/usr/bin/file', [sidecar], { encoding: 'utf8' })
  if (!fileInfo.includes('arm64')) throw new Error('SIDECAR_ARCH_INVALID')
  const entries = new Set(asar.listPackage(asarPath))
  for (const entry of expectedAsarEntries) if (!entries.has(entry)) throw new Error(`ASAR_ENTRY_MISSING:${entry}`)
  for (const entry of entries) {
    if (entry.startsWith('/node_modules/') && unsafeDependencyMaterial.test(entry)) {
      throw new Error(`ASAR_TEST_OR_KEY_MATERIAL:${entry}`)
    }
    if (!expectedAsarEntries.has(entry) && !allowedAsarPrefixes.some(prefix => entry.startsWith(prefix))) {
      throw new Error(`ASAR_ENTRY_UNEXPECTED:${entry}`)
    }
  }
  for (const [name, version] of packagedDependencies) {
    const entry = `node_modules/${name}/package.json`
    const value = JSON.parse(asar.extractFile(asarPath, entry).toString('utf8'))
    if (value.version !== version) throw new Error(`ASAR_DEPENDENCY_VERSION_INVALID:${entry}`)
  }
  for (const name of legacyTestPackages.keys()) {
    if (entries.has(packagePath(name)) !== testFlavor) throw new Error(`ASAR_LEGACY_FLAVOR_INVALID:${name}`)
  }
  const packagedMetadata = JSON.parse(asar.extractFile(asarPath, 'package.json').toString('utf8'))
  if ((packagedMetadata.zhijunProvisioningTestBuild === true) !== testFlavor) {
    throw new Error('PACKAGE_FLAVOR_MARKER_INVALID')
  }
  return { appPath, sidecarSha256: config.connectivity.sidecarSha256, asarEntries: entries.size }
}
async function main() {
  const rawApp = path.join(release, 'mac-arm64', appName)
  const dmg = path.join(release, `${stem}.dmg`); const zip = path.join(release, `${stem}.zip`)
  for (const target of [rawApp, dmg, zip]) if (!fs.existsSync(target)) throw new Error(`ARTIFACT_MISSING:${target}`)
  const result = await verifyApp(rawApp)
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'zhijun-package-'))
  const mount = path.join(temporary, 'dmg'); const unzip = path.join(temporary, 'zip'); let mounted = false
  try {
    fs.mkdirSync(mount); fs.mkdirSync(unzip)
    execFileSync('/usr/bin/hdiutil', ['attach', '-readonly', '-nobrowse', '-noautoopen', '-mountpoint', mount, dmg], { stdio: ['ignore', 'pipe', 'pipe'], timeout: 60_000 }); mounted = true
    await verifyApp(path.join(mount, appName))
    execFileSync('/usr/bin/ditto', ['-x', '-k', zip, unzip], { stdio: ['ignore', 'pipe', 'pipe'], timeout: 60_000 })
    await verifyApp(path.join(unzip, appName))
  } finally {
    if (mounted) execFileSync('/usr/bin/hdiutil', ['detach', mount], { stdio: ['ignore', 'pipe', 'pipe'], timeout: 30_000 })
    fs.rmSync(temporary, { recursive: true, force: true })
  }
  console.log(JSON.stringify({ ok: true, flavor: testFlavor ? 'test' : 'production', version: metadata.version, app: rawApp,
    artifacts: [{ path: dmg, sha256: digest(dmg) }, { path: zip, sha256: digest(zip) }],
    sidecarSha256: result.sidecarSha256, asarEntries: result.asarEntries }))
}
main().catch(error => { console.error(error.message); process.exit(1) })
