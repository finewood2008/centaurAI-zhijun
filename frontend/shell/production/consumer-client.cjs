'use strict';
const crypto = require('node:crypto');
const { DesktopError } = require('../runtime/public-error.cjs');
const LIMIT = 256 * 1024;
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value, max) => typeof value === 'string' && value.length > 0 && value.length <= max && !/[\u0000-\u001f\u007f]/u.test(value);
function fail(code = 'CONTRACT_MISMATCH') { throw new DesktopError(code); }
function validatePassword(value) {
  if (!plain(value) || Object.keys(value).length !== 2 || !/^1\d{10}$/.test(value.phone) || typeof value.phone !== 'string'
    || typeof value.password !== 'string' || /[\r\n\0]/u.test(value.password)
    || Buffer.byteLength(value.password) < 8 || Buffer.byteLength(value.password) > 72) fail('INVALID_REQUEST');
  return { phone: value.phone, password: value.password };
}
function tokens(value, clientId, accountId) {
  if (!plain(value) || !text(value.accessToken, 16384) || !text(value.refreshToken, 256) || value.refreshToken.length < 40
    || !text(value.accountId, 256) || value.clientId !== clientId || (accountId && value.accountId !== accountId)
    || !Number.isInteger(value.expiresIn) || value.expiresIn < 1 || value.expiresIn > 86400) fail();
  return Object.freeze({ accountId: value.accountId, clientId, accessToken: value.accessToken,
    refreshToken: value.refreshToken, expiresAt: Date.now() + value.expiresIn * 1000 });
}
function checkEnvelope(result) {
  if (result.code === 401) fail('AUTHENTICATION_REQUIRED');
  if (result.code === 403) fail('ACCESS_DENIED');
  if (result.code === 429) fail('RESOURCE_EXHAUSTED');
  if (result.code !== 200 || result.success === false) fail('AUTHENTICATION_FAILED');
  return result.data;
}

async function createConsumerClient({ config, store, fetchImpl = fetch, timeoutMs = 10000, platform = process.platform,
  authFactory, now = Date.now }) {
  const factory = authFactory || (await import('@nexusaos/connectivity-electron/auth')).createElectronConsumerAuth;
  let epoch = 0;
  let disposed = false;
  const requests = new Set();
  function invalidate() { ++epoch; for (const controller of requests) controller.abort(); }

  async function request(method, route, body, signed) {
    const expected = epoch;
    if (requests.size >= 8) fail('RESOURCE_EXHAUSTED');
    const bytes = body === undefined ? Buffer.alloc(0) : Buffer.from(JSON.stringify(body));
    if (bytes.length > LIMIT) fail('INVALID_REQUEST');
    const headers = { accept: 'application/json', 'cache-control': 'no-store', ...(bytes.length ? { 'content-type': 'application/json' } : {}) };
    if (signed) {
      const identity = await store.identity(signed.identityKey);
      if (identity.clientId !== signed.clientId) fail('AUTHENTICATION_REQUIRED');
      const timestamp = String(Math.floor(now() / 1000));
      const nonce = crypto.randomBytes(24).toString('base64url');
      const digest = crypto.createHash('sha256').update(bytes).digest('hex');
      const canonical = ['NEXUSAOS-CONSUMER-V1', signed.accountId, signed.clientId, method, route, timestamp, nonce, digest].join('\n');
      Object.assign(headers, { authorization: `Bearer ${signed.accessToken}`, 'x-nexus-client-id': signed.clientId,
        'x-nexus-timestamp': timestamp, 'x-nexus-nonce': nonce, 'x-nexus-body-sha256': digest,
        'x-nexus-signature': await identity.sign(Buffer.from(canonical)) });
    }
    if (disposed || expected !== epoch) fail('STALE_GENERATION');
    if (requests.size >= 8) fail('RESOURCE_EXHAUSTED');
    const controller = new AbortController(); requests.add(controller);
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetchImpl(config.consumerBaseUrl + route, { method, headers, redirect: 'error',
        signal: controller.signal, ...(bytes.length ? { body: bytes } : {}) });
      if (!/^application\/json(?:\s*;|$)/i.test(response.headers.get('content-type') || '')) fail();
      const declared = Number(response.headers.get('content-length') || 0);
      if (declared > LIMIT) { await response.body?.cancel(); fail('RESPONSE_TOO_LARGE'); }
      const reader = response.body?.getReader();
      if (!reader) fail();
      let size = 0; const chunks = [];
      try {
        for (;;) {
          const { done, value } = await reader.read(); if (done) break;
          size += value.byteLength;
          if (size > LIMIT) { await reader.cancel(); fail('RESPONSE_TOO_LARGE'); }
          chunks.push(value);
        }
      } finally { reader.releaseLock(); }
      let envelope;
      try { envelope = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks))); } catch { fail(); }
      if (!plain(envelope) || !Number.isInteger(envelope.code)) fail();
      return { code: response.ok ? (envelope.code === 0 ? 200 : envelope.code) : response.status,
        success: envelope.success, data: envelope.data };
    } catch (error) {
      if (controller.signal.aborted) fail('REQUEST_TIMEOUT');
      if (error instanceof DesktopError) throw error;
      fail('TRANSPORT_UNAVAILABLE');
    } finally { clearTimeout(timer); requests.delete(controller); }
  }
  const auth = factory({
    load: async () => undefined, // No implicit login or token restoration at startup.
    save: store.save, remove: store.remove,
    exchange: async current => ({ ...tokens(checkEnvelope(await request('POST', '/app-api/auth/refresh',
      { refreshToken: current.refreshToken })), current.clientId, current.accountId), identityKey: current.identityKey }),
    isSessionRejected: error => error instanceof DesktopError && error.code === 'AUTHENTICATION_REQUIRED',
  });
  async function protectedRequest(method, route, body) {
    const expected = epoch;
    let response;
    try { response = await auth.authorized(current => request(method, route, body, current), result => result.code === 401); }
    catch (error) {
      if (['AUTH_SESSION_MISSING', 'AUTH_SESSION_CHANGED', 'AUTH_INVALID_SESSION'].includes(error?.code)) fail('AUTHENTICATION_REQUIRED');
      if (error?.code === 'AUTH_SESSION_STORAGE_FAILED') fail('SECURE_STORAGE_UNAVAILABLE');
      throw error;
    }
    if (disposed || expected !== epoch) fail('AUTHENTICATION_REQUIRED');
    if (response.code === 401) { await auth.clear(); fail('AUTHENTICATION_REQUIRED'); }
    return checkEnvelope(response);
  }
  return Object.freeze({
    async signIn(input, guard = () => true) {
      if (disposed) fail('OPERATION_NOT_ALLOWED');
      const credentials = validatePassword(input);
      invalidate(); const expected = epoch;
      const valid = () => !disposed && expected === epoch && guard();
      await auth.clear();
      const identity = await store.identity(credentials.phone);
      if (!valid()) fail('STALE_GENERATION');
      const data = checkEnvelope(await request('POST', '/app-api/auth/password/login', { ...credentials,
        clientId: identity.clientId, clientPublicKey: identity.publicKey,
        platform: { darwin: 'macos', win32: 'windows', linux: 'linux' }[platform] || 'unknown',
        deviceModel: 'Zhijun Desktop', displayName: '知君桌面' }));
      if (!valid()) fail('STALE_GENERATION');
      const session = { ...tokens(data, identity.clientId), identityKey: credentials.phone };
      await auth.replace(session);
      if (!valid()) {
        if (expected === epoch) await auth.clear();
        fail('STALE_GENERATION');
      }
      return { accountId: session.accountId };
    },
    async listDevices() {
      const data = await protectedRequest('GET', '/app-api/devices');
      if (!Array.isArray(data) || data.length > 256) fail();
      const ids = new Set();
      return data.map(device => {
        if (!plain(device) || !text(device.deviceId, 64) || !/^[A-Za-z0-9._:-]{8,64}$/.test(device.deviceId)
          || ids.has(device.deviceId) || device.role !== 'owner' || !Array.isArray(device.scopes)
          || !device.scopes.every(scope => text(scope, 128)) || typeof device.online !== 'boolean'
          || (device.deviceName != null && !text(device.deviceName, 128))) fail();
        ids.add(device.deviceId);
        return { deviceId: device.deviceId, displayName: device.deviceName || device.deviceId,
          availability: device.online ? 'online' : 'offline', allowed: device.scopes.includes('remote.p2p') };
      }).filter(device => device.allowed).map(({ allowed, ...device }) => device);
    },
    async createSession(deviceId, body) {
      if (typeof deviceId !== 'string' || !/^[A-Za-z0-9._:-]{8,64}$/.test(deviceId)) fail('INVALID_REQUEST');
      // This POST only retries an explicit pre-handler 401, never a lost result or business write.
      return protectedRequest('POST', `/app-api/devices/${encodeURIComponent(deviceId)}/connectivity/sessions`, body);
    },
    async current() { return auth.current(); },
    async signOut() {
      invalidate();
      let current;
      try { current = await auth.current(); } catch {}
      // Erase local tokens even when remote revocation is unavailable.
      await auth.clear();
      if (current) checkEnvelope(await request('POST', '/app-api/auth/logout', { refreshToken: current.refreshToken }, current));
    },
    async dispose() { disposed = true; invalidate(); await auth.clear(); },
  });
}
module.exports = { createConsumerClient, validatePassword };
