'use strict';
const { TextDecoder } = require('node:util');
const { DesktopError } = require('../runtime/public-error.cjs');
const { buildMaterialsRequest } = require('../runtime/materials.cjs');
const APPLICATION = 'mindos-person-data-pc';
const PRODUCT_APPLICATION = 'zhijun-desktop';
const PRODUCT_CAPABILITIES = ['product.rpc', 'product.events', 'product.uploads', 'product.blobs'];
const { validateWireRequest } = require('../runtime/product-policy.cjs');
const { createV2Scheduler } = require('../runtime/v2-request-scheduler.cjs');
const CONTEXT_PATH = '/api/mindos/connectivity/context';
const identifier = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = code => { throw new DesktopError(code); };
async function sdkRequest(session, request, handshake = false) {
  try { return await session.request(request); }
  catch (error) {
    if (error instanceof DesktopError) throw error;
    const code = error?.code;
    if (code === 'REQUEST_TARGET_NOT_ALLOWED') fail(handshake ? 'BUSINESS_BRIDGE_REQUIRED' : 'ACCESS_DENIED');
    if (['SDK_REQUEST_TIMEOUT', 'IPC_SIDECAR_TIMEOUT'].includes(code)) fail('REQUEST_TIMEOUT');
    if (['SDK_TOO_MANY_REQUESTS', 'TOO_MANY_REQUESTS'].includes(code)) throw new DesktopError('RATE_LIMITED', { definitelyNotSent: true, remoteCode: code });
    if (['SDK_REQUEST_LIMIT_REACHED', 'REQUEST_REPLAYED'].includes(code)) throw new DesktopError('SESSION_QUOTA_EXHAUSTED', { definitelyNotSent: true, remoteCode: code });
    // Agent payload quota can be exhausted while reading the response, after a
    // mutating handler executed. Never attach pre-dispatch evidence to it.
    if (code === 'SESSION_RESOURCE_EXHAUSTED') throw new DesktopError('SESSION_QUOTA_EXHAUSTED', { remoteCode: code });
    if (['SDK_RESPONSE_TOO_LARGE', 'SDK_RESPONSE_BUFFER_FULL'].includes(code)) fail('RESPONSE_TOO_LARGE');
    // This native code confirms terminal closure. Reuse the runtime's session
    // invalidation path; a generic not-ready or HTTP 403 does not prove closure.
    if (code === 'SDK_CONNECTION_CLOSED') fail('SESSION_EXPIRED');
    fail('TRANSPORT_UNAVAILABLE');
  }
}

function contextResponse(response, subject, clock, product = false) {
  if (!record(response) || !(response.body instanceof Uint8Array)) fail('CONTRACT_MISMATCH');
  if (response.body.byteLength > 8192) fail('RESPONSE_TOO_LARGE');
  if (response.status !== 200) {
    const code = [404, 501, 503].includes(response.status) ? 'BUSINESS_BRIDGE_REQUIRED'
      : [401, 403].includes(response.status) ? 'ACCESS_DENIED'
      : response.status === 429 ? 'RESOURCE_EXHAUSTED' : 'REMOTE_ERROR';
    throw new DesktopError(code, { httpStatus: response.status });
  }
  if (!record(response.headers)) fail('CONTRACT_MISMATCH');
  const contentTypes = Object.entries(response.headers).filter(([key]) => key.toLowerCase() === 'content-type');
  if (contentTypes.length !== 1 || typeof contentTypes[0][1] !== 'string'
    || !/^application\/json(?:\s*;\s*charset\s*=\s*utf-8)?\s*$/i.test(contentTypes[0][1])) fail('CONTRACT_MISMATCH');
  let value;
  try { value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(response.body)); }
  catch { fail('CONTRACT_MISMATCH'); }
  const keys = ['version', 'accountId', 'clientId', 'deviceId', 'applicationId', 'capabilities', 'expiresAt', ...(product ? ['workspaceId'] : [])];
  const capabilities = product ? PRODUCT_CAPABILITIES : ['materials.read'];
  if (!record(value) || Object.keys(value).length !== keys.length || !keys.every(key => Object.hasOwn(value, key))
    || value.version !== (product ? 2 : 1) || !Array.isArray(value.capabilities) || value.capabilities.length !== capabilities.length
    || new Set(value.capabilities).size !== capabilities.length || !capabilities.every(item => value.capabilities.includes(item))
    || !Number.isSafeInteger(value.expiresAt) || (product && (typeof value.workspaceId !== 'string' || !/^[a-f0-9]{64}$/.test(value.workspaceId)))) fail('CONTRACT_MISMATCH');
  if (value.accountId !== subject.accountId || value.clientId !== subject.clientId
    || value.deviceId !== subject.deviceId || value.applicationId !== (product ? PRODUCT_APPLICATION : APPLICATION)) fail('ACCESS_DENIED');
  const now = Math.floor(clock() / 1000);
  if (value.expiresAt <= now) fail('SESSION_EXPIRED');
  // The box enforces a <=5-second proof; allow modest desktop clock skew here.
  if (value.expiresAt > now + 30) fail('CONTRACT_MISMATCH');
  return product ? { workspaceId: value.workspaceId, product: true } : { product: false };
}

function materialRequest(value) {
  if (!record(value) || Object.keys(value).length !== 3 || value.method !== 'GET'
    || typeof value.relative_path !== 'string' || !record(value.headers)
    || Object.keys(value.headers).length !== 1 || !Array.isArray(value.headers.Accept)
    || value.headers.Accept.length !== 1 || value.headers.Accept[0] !== 'application/json') fail('INVALID_REQUEST');
  const prefix = '/api/mindos/materials?';
  if (!value.relative_path.startsWith(prefix) || value.relative_path.length > 2048) fail('INVALID_REQUEST');
  const query = {};
  for (const [key, raw] of new URLSearchParams(value.relative_path.slice(prefix.length))) {
    if (Object.hasOwn(query, key) || !['limit', 'offset', 'keyword', 'type', 'status'].includes(key)) fail('INVALID_REQUEST');
    query[key] = key === 'limit' || key === 'offset' ? Number(raw) : raw;
  }
  const expected = buildMaterialsRequest(query);
  if (expected.path !== value.relative_path) fail('INVALID_REQUEST');
  return { method: 'GET', relative_path: expected.path, headers: { Accept: ['application/json'] } };
}

function createBusinessBridge({ clock = Date.now, heartbeatMs = 10000, heartbeatTimeoutMs = 12000,
  activityClock = () => performance.now(), timers = { setTimeout, clearTimeout, setInterval, clearInterval }, schedulerOptions = {} } = {}) {
  return Object.freeze({
    async authorize({ session, subject, applicationId, signal, onFailure = () => {} }) {
      const product = applicationId === PRODUCT_APPLICATION;
      if (![APPLICATION, PRODUCT_APPLICATION].includes(applicationId) || !record(subject)
        || !['accountId', 'clientId', 'deviceId'].every(key => identifier(subject[key]))) fail('ACCESS_DENIED');
      const bound = Object.freeze({ accountId: subject.accountId, clientId: subject.clientId, deviceId: subject.deviceId });
      const scheduler = product ? createV2Scheduler({ ...schedulerOptions, clock: activityClock, timers,
        send: (request, handshake) => sdkRequest(session, request, handshake) }) : undefined;
      const send = (request, options) => scheduler ? scheduler.request(request, options) : sdkRequest(session, request, options?.handshake);
      // No external identity header: the Agent signs its verified subject inside
      // the box, and data-engine verifies that proof on this and every later GET.
      let access;
      try {
        const response = await send({ method: 'GET', relative_path: product ? '/api/mindos/zhijun/context' : CONTEXT_PATH,
          headers: { Accept: ['application/json'] } }, { priority: 0, handshake: true, signal });
        access = contextResponse(response, bound, clock, product);
      } catch (error) { scheduler?.close(); throw error; }
      let closed = false, beating = false, lastActivity = activityClock(), heartbeatTimer;
      const pendingHeartbeats = new Set();
      async function heartbeat() {
        if (closed || beating || activityClock() - lastActivity < heartbeatMs) return;
        beating = true;
        let timer, cancel;
        try {
          const response = await Promise.race([
            send({ method: 'GET', relative_path: '/api/mindos/zhijun/context', headers: { Accept: ['application/json'] } }, { priority: 0, handshake: true }),
            new Promise((_, reject) => { cancel = () => reject(new DesktopError('SESSION_NOT_READY')); pendingHeartbeats.add(cancel);
              timer = timers.setTimeout(() => reject(new DesktopError('REQUEST_TIMEOUT')), heartbeatTimeoutMs); }),
          ]);
          if (closed) return;
          const renewed = contextResponse(response, bound, clock, true);
          if (renewed.workspaceId !== access.workspaceId) fail('ACCESS_DENIED');
          lastActivity = activityClock();
        } catch (error) {
          if (!closed) { closed = true; timers.clearInterval(heartbeatTimer); scheduler?.close(error);
            try { onFailure(error instanceof DesktopError ? error : new DesktopError('TRANSPORT_UNAVAILABLE')); } catch {} }
        } finally { timers.clearTimeout(timer); pendingHeartbeats.delete(cancel); beating = false; }
      }
      if (product) { heartbeatTimer = timers.setInterval(() => { void heartbeat(); }, heartbeatMs); heartbeatTimer?.unref?.(); }
      return Object.freeze({
        ...bound, ...access,
        managesRequestQueue: product,
        reserveTransfer: value => scheduler?.reserveTransfer(value),
        releaseTransfer: value => scheduler?.releaseTransfer(value),
        async request(request, options) {
          if (closed) fail('SESSION_NOT_READY');
          let normalized = product ? validateWireRequest(request) : materialRequest(request);
          const isPoll = normalized.method === 'GET' && /\/operations\/[^/?]+\?/.test(normalized.relative_path);
          // SDK 1.2's native sidecar processes requests serially. A long poll
          // must not hold its sole HTTP lane ahead of reads, close or heartbeat.
          // Validate the caller's original canonical route before shortening it;
          // retain the exact operation ID and cursor, including waitMs=0.
          if (product && isPoll) normalized = { ...normalized,
            relative_path: normalized.relative_path.replace(/&waitMs=(\d+)$/, (_, wait) => `&waitMs=${Math.min(Number(wait), 250)}`) };
          const isCancel = normalized.relative_path.endsWith('/cancel') || normalized.method === 'DELETE';
          const isContext = normalized.method === 'GET' && normalized.relative_path === '/api/mindos/zhijun/context';
          const response = await send(normalized, { ...options, priority: isContext ? 0 : isCancel ? 1 : isPoll ? 3 : 2 });
          if (closed) fail('SESSION_NOT_READY');
          if (product && response?.status >= 200 && response.status < 300) lastActivity = activityClock();
          return response;
        },
        async close() { closed = true; timers.clearInterval(heartbeatTimer); scheduler?.close(); for (const cancel of pendingHeartbeats) cancel(); },
      });
    },
  });
}
module.exports = { createBusinessBridge };
