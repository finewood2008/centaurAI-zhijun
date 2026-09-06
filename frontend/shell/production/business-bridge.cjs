'use strict';
const { TextDecoder } = require('node:util');
const { DesktopError } = require('../runtime/public-error.cjs');
const { buildMaterialsRequest } = require('../runtime/materials.cjs');
const APPLICATION = 'mindos-person-data-pc';
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
    if (['SDK_TOO_MANY_REQUESTS', 'SDK_REQUEST_LIMIT_REACHED', 'SESSION_RESOURCE_EXHAUSTED'].includes(code)) fail('RESOURCE_EXHAUSTED');
    if (['SDK_RESPONSE_TOO_LARGE', 'SDK_RESPONSE_BUFFER_FULL'].includes(code)) fail('RESPONSE_TOO_LARGE');
    // This native code confirms terminal closure. Reuse the runtime's session
    // invalidation path; a generic not-ready or HTTP 403 does not prove closure.
    if (code === 'SDK_CONNECTION_CLOSED') fail('SESSION_EXPIRED');
    fail('TRANSPORT_UNAVAILABLE');
  }
}

function contextResponse(response, subject, clock) {
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
  const keys = ['version', 'accountId', 'clientId', 'deviceId', 'applicationId', 'capabilities', 'expiresAt'];
  if (!record(value) || Object.keys(value).length !== keys.length || !keys.every(key => Object.hasOwn(value, key))
    || value.version !== 1 || !Array.isArray(value.capabilities) || value.capabilities.length !== 1
    || value.capabilities[0] !== 'materials.read' || !Number.isSafeInteger(value.expiresAt)) fail('CONTRACT_MISMATCH');
  if (value.accountId !== subject.accountId || value.clientId !== subject.clientId
    || value.deviceId !== subject.deviceId || value.applicationId !== APPLICATION) fail('ACCESS_DENIED');
  const now = Math.floor(clock() / 1000);
  if (value.expiresAt <= now) fail('SESSION_EXPIRED');
  // The box enforces a <=5-second proof; allow modest desktop clock skew here.
  if (value.expiresAt > now + 30) fail('CONTRACT_MISMATCH');
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

function createBusinessBridge({ clock = Date.now } = {}) {
  return Object.freeze({
    async authorize({ session, subject, applicationId }) {
      if (applicationId !== APPLICATION || !record(subject)
        || !['accountId', 'clientId', 'deviceId'].every(key => identifier(subject[key]))) fail('ACCESS_DENIED');
      const bound = Object.freeze({ accountId: subject.accountId, clientId: subject.clientId, deviceId: subject.deviceId });
      // No external identity header: the Agent signs its verified subject inside
      // the box, and data-engine verifies that proof on this and every later GET.
      const response = await sdkRequest(session, { method: 'GET', relative_path: CONTEXT_PATH,
        headers: { Accept: ['application/json'] } }, true);
      contextResponse(response, bound, clock);
      let closed = false;
      return Object.freeze({
        ...bound,
        async request(request) {
          if (closed) fail('SESSION_NOT_READY');
          const response = await sdkRequest(session, materialRequest(request));
          if (closed) fail('SESSION_NOT_READY');
          return response;
        },
        async close() { closed = true; },
      });
    },
  });
}
module.exports = { createBusinessBridge };
