'use strict';
const fs = require('node:fs/promises');
const crypto = require('node:crypto');
const path = require('node:path');
const { DesktopError } = require('../runtime/public-error.cjs');
const { assertSafe } = require('./file-security.cjs');

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

function createTicketProvider(admin, consumer, timing) {
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
      try {
        const issue = () => provider.issue(request);
        return await (timing ? timing.measure(request.transport_policy === 'TURN_ONLY' ? 'relay_ticket' : 'direct_ticket', issue) : issue());
      }
      catch (error) {
        if (consumerFailure && error?.code === 'SDK_SESSION_PROVIDER_FAILED') {
          throw new DesktopError(consumerFailure.code, { ...consumerFailure, phase: 'ticket', sdkCode: 'SDK_SESSION_PROVIDER_FAILED' });
        }
        throw mapConnectionError(error, 'ticket');
      }
    },
  };
}

const FALLBACK_DETAILS = new Set(['DIRECT_TIMEOUT', 'ICE_FAILED']);
const validFailedSessionId = value => typeof value === 'string' && value.length > 0 && value.length <= 128;
function relayFallback(error) {
  if (error?.code !== 'SDK_DIRECT_UNAVAILABLE' || !FALLBACK_DETAILS.has(error?.detailCode)
      || !validFailedSessionId(error?.failedSessionId)) return null;
  return Object.freeze({ profile: 'REMOTEOPS_COMPATIBILITY', transport_policy: 'TURN_ONLY',
    fallback_from_session_id: error.failedSessionId, fallback_reason: error.detailCode });
}

async function projectSession(session, expectedPath) {
  const selectedPath = session?.selectedPath;
  if (!session || typeof session.request !== 'function' || typeof session.close !== 'function'
      || selectedPath !== expectedPath) {
    try { await session?.close?.(); } catch { /* A contract error remains authoritative. */ }
    throw Object.assign(new Error('SDK session contract mismatch'), { code: 'SDK_PROTOCOL_VIOLATION' });
  }
  return Object.freeze({ selectedPath,
    request: request => session.request(request), close: () => session.close() });
}

async function createSdkRuntime({ config, consumer, timing, moduleLoader = name => import(name) }) {
  if (!config || process.env.SSL_CERT_FILE || process.env.SSL_CERT_DIR) throw new DesktopError('CONFIGURATION_REQUIRED');
  const stat = await fs.lstat(config.sidecarPath).catch(() => null);
  if (!stat?.isFile() || stat.isSymbolicLink() || stat.size > 128 * 1024 * 1024) throw new DesktopError('CONFIGURATION_REQUIRED');
  try { await assertSafe(config.sidecarPath, stat); } catch { throw new DesktopError('CONFIGURATION_REQUIRED'); }
  const digest = crypto.createHash('sha256');
  const file = await fs.open(config.sidecarPath, require('node:fs').constants.O_RDONLY | require('node:fs').constants.O_NOFOLLOW);
  try { for await (const bytes of file.createReadStream({ autoClose: false })) digest.update(bytes); } finally { await file.close(); }
  if (digest.digest('hex') !== config.sidecarSha256) throw new DesktopError('CONFIGURATION_REQUIRED');
  const [facade, admin, processModule] = await Promise.all([
    moduleLoader('@nexusaos/connectivity-electron'), moduleLoader('@nexusaos/connectivity-electron/admin'),
    moduleLoader('@nexusaos/connectivity-electron/process'),
  ]);
  const args = ['--gateway-host', config.gatewayHost, '--ice-host', config.iceHost];
  if (config.directConnectTimeoutMs !== undefined) {
    if (!Number.isInteger(config.directConnectTimeoutMs) || config.directConnectTimeoutMs < 2000 || config.directConnectTimeoutMs > 8000) {
      throw new DesktopError('CONFIGURATION_REQUIRED');
    }
    args.push('--direct-connect-timeout-ms', String(config.directConnectTimeoutMs));
  }
  const native = processModule.createElectronProcessNativeHost({ executable: config.sidecarPath,
    cwd: path.dirname(config.sidecarPath), args,
    tickets: createTicketProvider(admin, consumer, timing),
    operationTimeoutMs: { connect: 12000, request: 15000, close: 1000 } });
  const main = facade.createElectronMainFacade(native.host);
  let closed = false;
  return Object.freeze({
    async connect(deviceId) {
      if (closed) throw new DesktopError('SESSION_NOT_READY');
      const request = { device_id: deviceId, application_id: config.applicationId, client_platform: 'electron',
        requested_scopes: [...config.requestedScopes], purpose: config.purpose,
        profile: config.profile, transport_policy: 'DIRECT_ONLY' };
      const measure = (stage, work) => timing ? timing.measure(stage, work) : work();
      // Facade connect includes ticket issuance. Logs explicitly mark this overlap;
      // ticket duration must not be added to direct_connect / relay_connect.
      try { return await measure('direct_connect', async () => projectSession(await main.connect(request), 'DIRECT')); }
      catch (error) {
        const fallback = relayFallback(error);
        if (!fallback) throw mapConnectionError(error);
        try { return await measure('relay_connect', async () => projectSession(await main.connect({ ...request, ...fallback }), 'RELAY')); }
        catch (fallbackError) { throw mapConnectionError(fallbackError); }
      }
    },
    async close() { if (!closed) { closed = true; await native.close(); } },
  });
}
module.exports = { createSdkRuntime, mapConnectionError, createTicketProvider, relayFallback };
