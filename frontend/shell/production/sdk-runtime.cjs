'use strict';
const fs = require('node:fs/promises');
const crypto = require('node:crypto');
const path = require('node:path');
const { DesktopError } = require('../runtime/public-error.cjs');

async function createSdkRuntime({ config, consumer, moduleLoader = name => import(name) }) {
  if (!config || process.env.SSL_CERT_FILE || process.env.SSL_CERT_DIR) throw new DesktopError('CONFIGURATION_REQUIRED');
  const stat = await fs.lstat(config.sidecarPath).catch(() => null);
  if (!stat?.isFile() || stat.isSymbolicLink() || (stat.mode & 0o022) || stat.size > 128 * 1024 * 1024) throw new DesktopError('CONFIGURATION_REQUIRED');
  const digest = crypto.createHash('sha256');
  const file = await fs.open(config.sidecarPath, require('node:fs').constants.O_RDONLY | require('node:fs').constants.O_NOFOLLOW);
  try { for await (const bytes of file.createReadStream({ autoClose: false })) digest.update(bytes); } finally { await file.close(); }
  if (digest.digest('hex') !== config.sidecarSha256) throw new DesktopError('CONFIGURATION_REQUIRED');
  const [facade, admin, processModule] = await Promise.all([
    moduleLoader('@nexusaos/connectivity-electron'), moduleLoader('@nexusaos/connectivity-electron/admin'),
    moduleLoader('@nexusaos/connectivity-electron/process'),
  ]);
  const native = processModule.createElectronProcessNativeHost({ executable: config.sidecarPath,
    cwd: path.dirname(config.sidecarPath), args: ['--gateway-host', config.gatewayHost, '--ice-host', config.iceHost],
    tickets: admin.createElectronAdminTicketProvider({ client: consumer }),
    operationTimeoutMs: { connect: 12000, request: 15000, close: 1000 } });
  const main = facade.createElectronMainFacade(native.host);
  let closed = false;
  return Object.freeze({
    async connect(deviceId) {
      if (closed) throw new DesktopError('SESSION_NOT_READY');
      return main.connect({ device_id: deviceId, application_id: config.applicationId, client_platform: 'electron',
        requested_scopes: [...config.requestedScopes], purpose: config.purpose,
        profile: config.profile, transport_policy: 'DIRECT_ONLY' });
    },
    async close() { if (!closed) { closed = true; await native.close(); } },
  });
}
module.exports = { createSdkRuntime };
