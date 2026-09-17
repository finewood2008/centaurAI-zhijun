'use strict'
const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const { execFileSync } = require('node:child_process')
const { loadConfig } = require('../production/config.cjs')

module.exports = async context => {
  if (context.electronPlatformName !== 'darwin') return
  const appName = `${context.packager.appInfo.productFilename}.app`
  const resources = path.join(context.appOutDir, appName, 'Contents/Resources')
  const requiredFiles = ['app.asar', 'mindos-web-dist/desktop.html', 'shared/product-operations.json',
    'zhijun-product.json', 'connectivity-sidecar/darwin-arm64/nexusaos-connectivity-sidecar']
  for (const relative of requiredFiles) {
    const current = path.join(resources, relative); const stat = fs.lstatSync(current)
    if (stat.isSymbolicLink() || !stat.isFile()) throw new Error(`PACKAGE_RESOURCE_INVALID:${relative}`)
  }
  const config = await loadConfig(path.join(resources, 'zhijun-product.json'), { resourceRoot: resources })
  const sidecar = config.connectivity.sidecarPath
  const actual = crypto.createHash('sha256').update(fs.readFileSync(sidecar)).digest('hex')
  if (actual !== config.connectivity.sidecarSha256 || (fs.statSync(sidecar).mode & 0o022)) throw new Error('PACKAGED_SIDECAR_INVALID')
  execFileSync('/usr/bin/codesign', ['--verify', '--strict', '--verbose=2', sidecar], {
    stdio: ['ignore', 'pipe', 'pipe'], timeout: 15_000,
  })
  console.log(`[zhijun-package] resources and signed sidecar verified: ${actual}`)
}
