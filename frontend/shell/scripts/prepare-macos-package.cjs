'use strict'
const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const { execFileSync } = require('node:child_process')

const shell = path.resolve(__dirname, '..')
const root = path.resolve(shell, '../..')
const source = path.resolve(process.env.ZHIJUN_SIDECAR_SOURCE ||
  path.join(root, 'data/desktop/native-1.2.1/sidecar/darwin-arm64/nexusaos-connectivity-sidecar'))
const stage = path.join(shell, 'package-resources')
const target = path.join(stage, 'connectivity-sidecar/darwin-arm64/nexusaos-connectivity-sidecar')
const identity = process.env.CSC_NAME || 'Developer ID Application: Zhuhai Qeeshu Technology Co., Ltd. (GLHM545ZLS)'
const expectedSource = '9613fd07bdd6db4146bf8ddd54d062d513e8171b22b811b29b0f5e9ded80ea59'
const digest = filename => crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex')

if (process.platform !== 'darwin' || process.arch !== 'arm64') throw new Error('PACKAGE_PLATFORM_UNSUPPORTED')
const electron = require('../node_modules/electron/package.json')
const metadata = require('../package.json')
if (electron.version !== metadata.devDependencies.electron) throw new Error('ELECTRON_VERSION_INVALID')
const electronBinary = path.join(shell, 'node_modules/electron/dist/Electron.app/Contents/MacOS/Electron')
const electronInfo = execFileSync('/usr/bin/file', [electronBinary], { encoding: 'utf8', timeout: 10_000 })
if (!electronInfo.includes('arm64')) throw new Error('ELECTRON_ARCH_INVALID')
const stat = fs.lstatSync(source)
if (!stat.isFile() || stat.isSymbolicLink() || digest(source) !== expectedSource) throw new Error('SIDECAR_SOURCE_INVALID')
fs.rmSync(stage, { recursive: true, force: true })
fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 })
fs.copyFileSync(source, target)
fs.chmodSync(target, 0o755)
execFileSync('/usr/bin/codesign', ['--force', '--sign', identity, '--options', 'runtime', '--timestamp', target], {
  stdio: ['ignore', 'pipe', 'pipe'], timeout: 60_000,
})
execFileSync('/usr/bin/codesign', ['--verify', '--strict', '--verbose=2', target], {
  stdio: ['ignore', 'pipe', 'pipe'], timeout: 15_000,
})
const sidecarSha256 = digest(target)
const product = {
  version: 1,
  consumerBaseUrl: 'https://boss.nexusaos.qitus.cn/prod-api',
  connectivity: {
    applicationId: 'zhijun-desktop',
    purpose: 'zhijun.workspace',
    requestedScopes: ['remote.p2p'],
    gatewayHost: 'gateway.remote.qeeshu.com',
    iceHost: 'gateway.remote.qeeshu.com',
    sidecarPath: 'connectivity-sidecar/darwin-arm64/nexusaos-connectivity-sidecar',
    sidecarSha256,
    profile: 'SOVEREIGN_DIRECT_ONLY',
  },
}
fs.writeFileSync(path.join(stage, 'zhijun-product.json'), `${JSON.stringify(product, null, 2)}\n`, { mode: 0o600 })
console.log(`macOS package resources prepared; signed sidecar sha256=${sidecarSha256}`)
