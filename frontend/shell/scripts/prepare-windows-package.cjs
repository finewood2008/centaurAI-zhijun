'use strict'
const fs = require('node:fs')
const path = require('node:path')
const common = require('./windows-release-common.cjs')
const { loadProvisioningReleaseConfig } = require('./provisioning-release-config.cjs')

function prepareWindowsPackage({ unsigned = false } = {}) {
  common.assertBuildHost()
  const metadata = require('../package.json')
  if (require('../node_modules/electron/package.json').version !== common.ELECTRON_VERSION
      || metadata.devDependencies.electron !== common.ELECTRON_VERSION) throw new Error('ELECTRON_VERSION_INVALID')
  common.assertPeX64(path.join(common.SHELL_ROOT, 'node_modules/electron/dist/electron.exe'))
  if (!unsigned) common.signingSettings()
  const source = path.resolve(process.env.ZHIJUN_SIDECAR_SOURCE || path.join(common.WORKSPACE_ROOT,
    'data/desktop/native-direct-budget-20260913/windows-amd64/nexusaos-connectivity-sidecar.exe'))
  common.assertPeX64(source)
  if (common.digest(source) !== common.SOURCE_SHA256) throw new Error('SIDECAR_SOURCE_INVALID')
  const provisioning = loadProvisioningReleaseConfig()
  const target = path.join(common.STAGE_DIR, common.SIDECAR_RELATIVE)
  // This directory is Windows-only; never touch Mac package resources or releases.
  if (fs.existsSync(common.STAGE_DIR) && fs.lstatSync(common.STAGE_DIR).isSymbolicLink()) throw new Error('WINDOWS_STAGE_INVALID')
  fs.rmSync(common.STAGE_DIR, { recursive: true, force: true })
  fs.mkdirSync(path.dirname(target), { recursive: true })
  fs.copyFileSync(source, target)
  if (!unsigned) common.signFile(target)
  const product = {
    version: 1,
    consumerBaseUrl: 'https://boss.nexusaos.qitus.cn/prod-api',
    provisioning,
    connectivity: {
      applicationId: 'zhijun-desktop', purpose: 'zhijun.workspace', requestedScopes: ['remote.p2p'],
      gatewayHost: 'gateway.remote.qeeshu.com', iceHost: 'gateway.remote.qeeshu.com',
      sidecarPath: common.SIDECAR_RELATIVE, sidecarSha256: common.digest(target),
      profile: 'SOVEREIGN_DIRECT_ONLY', directConnectTimeoutMs: 8000,
    },
  }
  fs.writeFileSync(path.join(common.STAGE_DIR, 'zhijun-product.json'), `${JSON.stringify(product, null, 2)}\n`)
  fs.writeFileSync(path.join(common.STAGE_DIR, 'windows-release.json'), `${JSON.stringify({ unsigned, version: metadata.version })}\n`)
  console.log(unsigned ? 'Windows UNSIGNED resources prepared: internal testing only, not for customer distribution.'
    : 'Windows production resources prepared; sidecar signature and digest verified.')
  return product
}

if (require.main === module) {
  try { prepareWindowsPackage({ unsigned: common.parseUnsigned() }) }
  catch (error) { console.error(error.message); process.exitCode = 1 }
}
module.exports = { prepareWindowsPackage }
