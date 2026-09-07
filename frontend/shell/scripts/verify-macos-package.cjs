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
const release = path.join(shell, 'release')
const stem = `Zhijun-${metadata.version}-mac-arm64`
const appName = '知君.app'
const expectedTeam = 'GLHM545ZLS'
const digest = filename => crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex')
const expectedAsarEntries = new Set([
  '/node_modules', '/node_modules/@nexusaos',
  '/node_modules/@nexusaos/connectivity-contracts', '/node_modules/@nexusaos/connectivity-contracts/dist',
  '/node_modules/@nexusaos/connectivity-contracts/dist/index.js', '/node_modules/@nexusaos/connectivity-contracts/package.json',
  '/node_modules/@nexusaos/connectivity-electron', '/node_modules/@nexusaos/connectivity-electron/dist',
  '/node_modules/@nexusaos/connectivity-electron/dist/admin-ticket-provider.js',
  '/node_modules/@nexusaos/connectivity-electron/dist/consumer-auth.js',
  '/node_modules/@nexusaos/connectivity-electron/dist/index.js',
  '/node_modules/@nexusaos/connectivity-electron/dist/native-host.js',
  '/node_modules/@nexusaos/connectivity-electron/dist/process-exchange.js',
  '/node_modules/@nexusaos/connectivity-electron/dist/sidecar-protocol.js',
  '/node_modules/@nexusaos/connectivity-electron/package.json',
  '/app-icon.cjs', '/assets', '/assets/centaur.png', '/main.js', '/package.json', '/preload.cjs',
  '/production', '/production/adapter.cjs', '/production/business-bridge.cjs', '/production/config.cjs',
  '/production/consumer-client.cjs', '/production/credential-store.cjs', '/production/sdk-runtime.cjs',
  '/runtime', '/runtime/adapters.cjs', '/runtime/desktop-runtime.cjs', '/runtime/materials.cjs',
  '/runtime/microphone-permission.cjs', '/runtime/native-save.cjs', '/runtime/product-policy.cjs',
  '/runtime/product-session.cjs', '/runtime/public-error.cjs', '/runtime/read-scheduler.cjs',
  '/runtime/v2-request-scheduler.cjs', '/security.cjs',
])
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
  if (plist.CFBundleIdentifier !== 'com.qeeshu.zhijun' || plist.CFBundleShortVersionString !== metadata.version ||
      typeof plist.NSMicrophoneUsageDescription !== 'string' || !plist.NSMicrophoneUsageDescription) throw new Error('INFO_PLIST_INVALID')
  const resources = path.join(appPath, 'Contents/Resources')
  const configPath = path.join(resources, 'zhijun-product.json')
  const asarPath = path.join(resources, 'app.asar')
  const desktopEntry = path.join(resources, 'mindos-web-dist/desktop.html')
  const operationCatalog = path.join(resources, 'shared/product-operations.json')
  for (const filename of [configPath, asarPath, desktopEntry, operationCatalog]) regularFile(filename)
  const config = await loadConfig(configPath, { resourceRoot: resources })
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
  for (const entry of entries) if (!expectedAsarEntries.has(entry)) throw new Error(`ASAR_ENTRY_UNEXPECTED:${entry}`)
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
  console.log(JSON.stringify({ ok: true, version: metadata.version, app: rawApp,
    artifacts: [{ path: dmg, sha256: digest(dmg) }, { path: zip, sha256: digest(zip) }],
    sidecarSha256: result.sidecarSha256, asarEntries: result.asarEntries }))
}
main().catch(error => { console.error(error.message); process.exit(1) })
