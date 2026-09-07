'use strict';
const fs = require('node:fs/promises');
const crypto = require('node:crypto');
const path = require('node:path');
const { DesktopError } = require('../runtime/public-error.cjs');

function mapConnectionError(error, phase = 'native') {
  if (error instanceof DesktopError) return error;
  const sdkCode = error?.code;
  const mapping = {
    SDK_SESSION_PROVIDER_FAILED: 'ACCOUNT_SERVICE_UNAVAILABLE', SDK_INVALID_SESSION: 'CONTRACT_MISMATCH',
    SDK_INVALID_CONFIG: 'CONFIGURATION_REQUIRED', SDK_DIRECT_UNAVAILABLE: 'DIRECT_CONNECTION_UNAVAILABLE',
    SDK_CONNECT_TIMEOUT: 'REQUEST_TIMEOUT', SDK_REQUEST_TIMEOUT: 'REQUEST_TIMEOUT', IPC_SIDECAR_TIMEOUT: 'REQUEST_TIMEOUT',
    SDK_PROTOCOL_VIOLATION: 'CONTRACT_MISMATCH', IPC_PROTOCOL_MISMATCH: 'CONTRACT_MISMATCH', IPC_INVALID_MESSAGE: 'CONTRACT_MISMATCH',
    SDK_PATH_POLICY_FAILED: 'ACCESS_DENIED', SDK_CONNECTION_CLOSED: 'CONNECTIVITY_SESSION_EXPIRED', IPC_SIDECAR_CLOSED: 'SESSION_NOT_READY',
  };
  const code = typeof sdkCode === 'string' && Object.hasOwn(mapping, sdkCode) ? mapping[sdkCode] : 'TRANSPORT_UNAVAILABLE';
  return new DesktopError(code, { phase: ['SDK_SESSION_PROVIDER_FAILED', 'SDK_INVALID_SESSION'].includes(sdkCode) ? 'ticket' : phase,
    sdkCode, detailCode: error?.detailCode });
}

function createTicketProvider(admin, consumer) {
  return {
    async issue(request) {
      // SDK deliberately masks provider failures. Capture only an already-safe
      // Consumer DesktopError in this invocation, so concurrent tickets cannot
      // borrow another account/request's error. Keep SDK parsing unchanged.
      let consumerFailure;
      const provider = admin.createElectronAdminTicketProvider({ client: {
        async createSession(deviceId, body) {
          try { return await consumer.createSession(deviceId, body); }
          catch (error) { if (error instanceof DesktopError) consumerFailure = error; throw error; }
        },
      } });
      try { return await provider.issue(request); }
      catch (error) {
        if (consumerFailure && error?.code === 'SDK_SESSION_PROVIDER_FAILED') {
          throw new DesktopError(consumerFailure.code, { ...consumerFailure, phase: 'ticket', sdkCode: 'SDK_SESSION_PROVIDER_FAILED' });
        }
        throw mapConnectionError(error, 'ticket');
      }
    },
  };
}

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
    tickets: createTicketProvider(admin, consumer),
    operationTimeoutMs: { connect: 12000, request: 15000, close: 1000 } });
  const main = facade.createElectronMainFacade(native.host);
  let closed = false;
  return Object.freeze({
    async connect(deviceId) {
      if (closed) throw new DesktopError('SESSION_NOT_READY');
      try { return await main.connect({ device_id: deviceId, application_id: config.applicationId, client_platform: 'electron',
        requested_scopes: [...config.requestedScopes], purpose: config.purpose,
        profile: config.profile, transport_policy: 'DIRECT_ONLY' }); }
      catch (error) { throw mapConnectionError(error); }
    },
    async close() { if (!closed) { closed = true; await native.close(); } },
  });
}
module.exports = { createSdkRuntime, mapConnectionError, createTicketProvider };
