'use strict'
const fs = require('node:fs')
const path = require('node:path')
const common = require('./windows-release-common.cjs')

module.exports = async configuration => {
  if (process.env.ZHIJUN_WINDOWS_UNSIGNED === '1') throw new Error('UNSIGNED_BUILD_MUST_DISABLE_SIGN_EXECUTABLE')
  const target = path.resolve(configuration.path)
  // extraResources are copied byte-for-byte after staging. Re-signing this file would
  // invalidate the pinned hash in zhijun-product.json. Verify it instead.
  if (target.toLowerCase().endsWith(common.SIDECAR_RELATIVE.replaceAll('/', path.sep).toLowerCase())) {
    const config = JSON.parse(fs.readFileSync(path.join(common.STAGE_DIR, 'zhijun-product.json'), 'utf8'))
    if (common.digest(target) !== config.connectivity.sidecarSha256) throw new Error('SIDECAR_SIGNING_HASH_MISMATCH')
    common.verifySignature(target)
    return
  }
  common.signFile(target)
}
