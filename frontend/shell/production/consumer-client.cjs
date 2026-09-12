'use strict';
const crypto = require('node:crypto');
const { DesktopError } = require('../runtime/public-error.cjs');
const LIMIT = 256 * 1024;
const LOGIN_SESSION_MS = 7 * 24 * 60 * 60 * 1000;
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value, max) => typeof value === 'string' && value.length > 0 && value.length <= max && !/[\u0000-\u001f\u007f]/u.test(value);
function fail(code = 'CONTRACT_MISMATCH') { throw new DesktopError(code); }
function validatePassword(value) {
  if (!plain(value) || Object.keys(value).length !== 2 || !/^1\d{10}$/.test(value.phone) || typeof value.phone !== 'string'
    || typeof value.password !== 'string' || /[\r\n\0]/u.test(value.password)
    || Buffer.byteLength(value.password) < 8 || Buffer.byteLength(value.password) > 72) fail('INVALID_REQUEST');
  return { phone: value.phone, password: value.password };
}
function validatePhone(value) {
  if (typeof value !== 'string' || !/^1\d{10}$/.test(value)) fail('INVALID_REQUEST');
  return value;
}
function validateRegistration(value) {
  if (!plain(value) || Object.keys(value).length !== 3 || !/^\d{6}$/.test(value.code)) fail('INVALID_REQUEST');
  return { ...validatePassword({ phone: value.phone, password: value.password }), code: value.code };
}
function tokens(value, clientId, accountId, now = Date.now) {
  if (!plain(value) || !text(value.accessToken, 16384) || !text(value.refreshToken, 256) || value.refreshToken.length < 40
    || !text(value.accountId, 256) || value.clientId !== clientId || (accountId && value.accountId !== accountId)
    || !Number.isInteger(value.expiresIn) || value.expiresIn < 1 || value.expiresIn > 86400) fail();
  return Object.freeze({ accountId: value.accountId, clientId, accessToken: value.accessToken,
    refreshToken: value.refreshToken, expiresAt: now() + value.expiresIn * 1000 });
}
function storedSession(value, now) {
  if (!plain(value) || !text(value.accountId, 256) || !text(value.clientId, 256)
    || !text(value.accessToken, 16384) || !text(value.refreshToken, 256) || value.refreshToken.length < 40
    || !text(value.identityKey, 128) || !Number.isSafeInteger(value.expiresAt)
    || !Number.isSafeInteger(value.sessionExpiresAt) || value.sessionExpiresAt <= now
    || value.sessionExpiresAt > now + LOGIN_SESSION_MS || value.expiresAt > value.sessionExpiresAt) fail('AUTHENTICATION_REQUIRED');
  return Object.freeze({ accountId: value.accountId, clientId: value.clientId, accessToken: value.accessToken,
    refreshToken: value.refreshToken, identityKey: value.identityKey, expiresAt: value.expiresAt,
    sessionExpiresAt: value.sessionExpiresAt });
}
function checkEnvelope(result, connectivity = false, purpose = 'auth') {
  const remoteCode = plain(result.data) && text(result.data.errorCode, 128) ? result.data.errorCode : undefined;
  const reject = code => { throw new DesktopError(code, { httpStatus: result.httpStatus, ...(remoteCode ? { remoteCode } : {}) }); };
  if (connectivity && result.applicationDenied) throw new DesktopError('APPLICATION_AUTHORIZATION_DENIED', { phase: 'ticket', httpStatus: result.httpStatus });
  if (result.code === 401) reject('AUTHENTICATION_REQUIRED');
  if (result.code === 403) reject('ACCESS_DENIED');
  if (result.code === 429) reject('RATE_LIMITED');
  if (purpose === 'registration' && (result.code === 602 || ['AUTH_RATE_LIMITED', 'SMS_DAILY_LIMIT'].includes(remoteCode))) reject('RATE_LIMITED');
  if (purpose === 'claim' && ['DEVICE_ALREADY_CLAIMED', 'DEVICE_ALREADY_BOUND', 'CLAIM_TOKEN_ALREADY_CONSUMED'].includes(remoteCode)) reject('DEVICE_ALREADY_CLAIMED');
  if (purpose === 'claim' && remoteCode === 'CLAIM_TOKEN_EXPIRED') reject('CLAIM_CODE_EXPIRED');
  if (purpose === 'claim' && ['CLAIM_TOKEN_INVALID', 'CLAIM_TOKEN_REVOKED', 'CLAIM_TOKEN_STATE_INVALID'].includes(remoteCode)) reject('CLAIM_CODE_INVALID');
  if (purpose === 'claim' && [400, 404, 409, 410, 422].includes(result.code)) reject('INVALID_REQUEST');
  if (purpose === 'registration' && ([400, 409, 422].includes(result.code)
      || ['SMS_CODE_INVALID', 'PASSWORD_ALREADY_SET', 'PASSWORD_INVALID'].includes(remoteCode))) reject('INVALID_REQUEST');
  if (result.code !== 200 || result.success === false) {
    if (connectivity) throw new DesktopError('ACCOUNT_SERVICE_UNAVAILABLE', { phase: 'ticket', httpStatus: result.httpStatus });
    fail('AUTHENTICATION_FAILED');
  }
  return result.data;
}

async function createConsumerClient({ config, store, fetchImpl = fetch, timeoutMs = 10000, platform = process.platform,
  authFactory, now = Date.now }) {
  const factory = authFactory || (await import('@nexusaos/connectivity-electron/auth')).createElectronConsumerAuth;
  let epoch = 0;
  let disposed = false;
  const requests = new Set();
  const claimAttempts = new Map();
  function invalidate() { ++epoch; for (const controller of requests) controller.abort(); }

  async function request(method, route, body, signed, extraHeaders = undefined) {
    const expected = epoch;
    if (requests.size >= 8) fail('RESOURCE_EXHAUSTED');
    const bytes = body === undefined ? Buffer.alloc(0) : Buffer.from(JSON.stringify(body));
    if (bytes.length > LIMIT) fail('INVALID_REQUEST');
    const headers = { accept: 'application/json', 'cache-control': 'no-store', ...(bytes.length ? { 'content-type': 'application/json' } : {}),
      ...(extraHeaders || {}) };
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
      // Gateways may return an HTML error page. The HTTP status is sufficient
      // to classify a service outage; never parse or reflect that response.
      if (response.status >= 500 && response.status <= 599) {
        await response.body?.cancel();
        throw new DesktopError('ACCOUNT_SERVICE_UNAVAILABLE', { phase: 'account_service', httpStatus: response.status });
      }
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
        success: envelope.success, data: envelope.data, httpStatus: response.status,
        // Legacy Admin has no machine error code for this policy rejection.
        // Match only this exact established response; unknown 601 messages are
        // never guessed to mean an application is unregistered or undeployed.
        applicationDenied: method === 'POST' && /^\/app-api\/devices\/(?:[A-Za-z0-9._-]|%3A)+\/connectivity\/sessions$/.test(route)
          && response.status === 200 && envelope.code === 601 && envelope.msg === 'Connectivity应用或权限未获准' };
    } catch (error) {
      if (controller.signal.aborted) fail('REQUEST_TIMEOUT');
      if (error instanceof DesktopError) throw error;
      throw new DesktopError('ACCOUNT_SERVICE_UNAVAILABLE', { phase: 'account_service' });
    } finally { clearTimeout(timer); requests.delete(controller); }
  }
  const auth = factory({
    load: async () => {
      const persisted = await store.load();
      if (persisted === undefined) return undefined;
      try { return storedSession(persisted, now()); }
      catch (error) {
        await store.remove();
        if (error instanceof DesktopError && error.code === 'AUTHENTICATION_REQUIRED') return undefined;
        throw error;
      }
    },
    save: store.save, remove: store.remove,
    exchange: async current => {
      if (!Number.isSafeInteger(current.sessionExpiresAt) || current.sessionExpiresAt <= now()) fail('SESSION_EXPIRED');
      return { ...tokens(checkEnvelope(await request('POST', '/app-api/auth/refresh',
        { refreshToken: current.refreshToken })), current.clientId, current.accountId, now), identityKey: current.identityKey,
        sessionExpiresAt: current.sessionExpiresAt };
    },
    isSessionRejected: error => error instanceof DesktopError && ['AUTHENTICATION_REQUIRED', 'SESSION_EXPIRED'].includes(error.code),
  });
  async function protectedRequest(method, route, body, extraHeaders) {
    const expected = epoch;
    let response;
    try { response = await auth.authorized(current => request(method, route, body, current, extraHeaders), result => result.code === 401); }
    catch (error) {
      if (['AUTH_SESSION_MISSING', 'AUTH_SESSION_CHANGED', 'AUTH_INVALID_SESSION'].includes(error?.code)) fail('SESSION_EXPIRED');
      if (error?.code === 'AUTH_SESSION_STORAGE_FAILED') fail('SECURE_STORAGE_UNAVAILABLE');
      if (error instanceof DesktopError && error.code === 'AUTHENTICATION_REQUIRED') {
        await auth.clear();
        fail('SESSION_EXPIRED');
      }
      throw error;
    }
    if (disposed || expected !== epoch) fail('AUTHENTICATION_REQUIRED');
    if (response.code === 401) { await auth.clear(); fail('SESSION_EXPIRED'); }
    return checkEnvelope(response, route.endsWith('/connectivity/sessions'), route === '/app-api/device-claims/redeem' ? 'claim' : 'auth');
  }
  async function establishSession(credentials, route, body, guard) {
    invalidate(); const expected = epoch;
    const valid = () => !disposed && expected === epoch && guard();
    await auth.clear();
    const identity = await store.identity(credentials.phone);
    if (!valid()) fail('STALE_GENERATION');
    const data = checkEnvelope(await request('POST', route, { ...body,
      clientId: identity.clientId, clientPublicKey: identity.publicKey,
      platform: { darwin: 'macos', win32: 'windows', linux: 'linux' }[platform] || 'unknown',
      deviceModel: 'Zhijun Desktop', displayName: '知君桌面' }), false, route.endsWith('/register') ? 'registration' : 'auth');
    if (!valid()) fail('STALE_GENERATION');
    const session = { ...tokens(data, identity.clientId, undefined, now), identityKey: credentials.phone,
      sessionExpiresAt: now() + LOGIN_SESSION_MS };
    await auth.replace(session);
    if (!valid()) {
      if (expected === epoch) await auth.clear();
      fail('STALE_GENERATION');
    }
    return { accountId: session.accountId };
  }
  return Object.freeze({
    async signIn(input, guard = () => true) {
      if (disposed) fail('OPERATION_NOT_ALLOWED');
      const credentials = validatePassword(input);
      return establishSession(credentials, '/app-api/auth/password/login', credentials, guard);
    },
    async sendRegistrationCode(input) {
      if (disposed) fail('OPERATION_NOT_ALLOWED');
      const phone = validatePhone(input);
      const data = checkEnvelope(await request('POST', '/app-api/auth/sms/send', { phone }), false, 'registration');
      if (!plain(data) || !Number.isInteger(data.expiresIn) || data.expiresIn < 1 || data.expiresIn > 3600) fail();
      return { expiresIn: data.expiresIn };
    },
    async register(input, guard = () => true) {
      if (disposed) fail('OPERATION_NOT_ALLOWED');
      const credentials = validateRegistration(input);
      return establishSession(credentials, '/app-api/auth/password/register', credentials, guard);
    },
    async restore() {
      if (disposed) fail('OPERATION_NOT_ALLOWED');
      try {
        const current = await auth.current();
        if (!Number.isSafeInteger(current.sessionExpiresAt) || current.sessionExpiresAt <= now()) {
          await auth.clear();
          return null;
        }
        return { accountId: current.accountId };
      } catch (error) {
        if (['AUTH_SESSION_MISSING', 'AUTH_INVALID_SESSION'].includes(error?.code)) return null;
        if (error?.code === 'AUTH_SESSION_STORAGE_FAILED') fail('SECURE_STORAGE_UNAVAILABLE');
        throw error;
      }
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
    async claimDevice(input) {
      if (typeof input !== 'string' || input.length > 64 || /[\u0000-\u001f\u007f]/u.test(input)) fail('INVALID_REQUEST');
      const claimToken = input.trim();
      if (!/^\d{6}$/.test(claimToken)) fail('INVALID_REQUEST');
      let idempotencyKey = claimAttempts.get(claimToken);
      if (!idempotencyKey) {
        if (claimAttempts.size >= 32) claimAttempts.delete(claimAttempts.keys().next().value);
        idempotencyKey = crypto.randomUUID();
        claimAttempts.set(claimToken, idempotencyKey);
      }
      let data;
      try {
        data = await protectedRequest('POST', '/app-api/device-claims/redeem',
          { claimToken, idempotencyKey }, { 'idempotency-key': idempotencyKey });
      } catch (error) {
        if (!(error instanceof DesktopError) || !['REQUEST_TIMEOUT', 'ACCOUNT_SERVICE_UNAVAILABLE'].includes(error.code)) {
          claimAttempts.delete(claimToken);
        }
        throw error;
      }
      claimAttempts.delete(claimToken);
      if (!plain(data) || !text(data.deviceId, 64) || !/^[A-Za-z0-9._:-]{8,64}$/.test(data.deviceId)
        || (data.deviceName != null && !text(data.deviceName, 128)) || data.state !== 'consumed') fail();
      return { deviceId: data.deviceId, displayName: data.deviceName || data.deviceId, availability: 'unknown' };
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
    async dispose() { disposed = true; claimAttempts.clear(); invalidate(); },
  });
}
module.exports = { createConsumerClient, validatePassword, validateRegistration, validatePhone, LOGIN_SESSION_MS };
