'use strict';
const crypto = require('node:crypto');
const { performance } = require('node:perf_hooks');
const { DesktopError } = require('../runtime/public-error.cjs');
const LIMIT = 256 * 1024;
const LOGIN_SESSION_MS = 7 * 24 * 60 * 60 * 1000;
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value, max) => typeof value === 'string' && value.length > 0 && value.length <= max && !/[\u0000-\u001f\u007f]/u.test(value);
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const DEVICE_ID = /^[A-Za-z0-9._-]{1,36}$/;
const SHA256_HEX = /^[a-f0-9]{64}$/;
const CONSOLE_CLAIM_CODE = /^(?:[A-Z2-7]{10}|[A-Z2-7]{20})$/;
const CONSOLE_CLAIM_ROUTE = '/app-api/device-console-claims/redeem';
const CLAIM_STATES = new Set(['waitingAppProof', 'waitingDeviceProof', 'ownershipCommitted', 'projectionPending',
  'deviceAckPending', 'completed', 'cancelled', 'expired', 'attentionRequired', 'failed']);
const PAIRING_ERROR_CODES = new Set(['AUTH_REQUIRED', 'AUTH_SESSION_EXPIRED', 'CLIENT_REVOKED', 'CLIENT_KEY_INVALID',
  'CLIENT_KEY_MISMATCH', 'ACCOUNT_NOT_ACTIVE', 'CLIENT_UPGRADE_REQUIRED', 'REQUEST_SIGNATURE_INVALID',
  'REQUEST_EXPIRED', 'NONCE_REPLAYED', 'IDEMPOTENCY_CONFLICT', 'DEVICE_IDENTITY_MISMATCH',
  'DEVICE_NOT_ENROLLED', 'DEVICE_DISABLED', 'DEVICE_NOT_CLAIMABLE', 'DEVICE_ALREADY_OWNED',
  'PAIRING_NOT_FOUND', 'PAIRING_EXPIRED', 'PAIRING_CANCELLED', 'PAIRING_MISMATCH', 'PAIRING_SUPERSEDED',
  'ACCESS_PROJECTION_PENDING', 'ACCESS_PROJECTION_FAILED', 'DEVICE_ACK_TIMEOUT', 'DEVICE_OFFLINE',
  'FEATURE_NOT_AVAILABLE', 'SERVICE_TEMPORARILY_UNAVAILABLE']);
const RETRYABLE_PAIRING_ERRORS = new Set(['ACCESS_PROJECTION_PENDING', 'DEVICE_ACK_TIMEOUT', 'DEVICE_OFFLINE',
  'SERVICE_TEMPORARILY_UNAVAILABLE']);
class ConsumerPairingError extends Error {
  constructor(code, response) {
    super(code); this.name = 'ConsumerPairingError'; this.code = code;
    this.retryable = RETRYABLE_PAIRING_ERRORS.has(code); this.requestId = response.requestId;
    this.serverTime = response.serverTime; this.receivedMonotonicMs = response.receivedMonotonicMs;
  }
}
function fail(code = 'CONTRACT_MISMATCH') { throw new DesktopError(code); }
function signingTarget(route) {
  const parsed = new URL(route, 'https://consumer.invalid');
  const encode = value => encodeURIComponent(value).replace(/[!'()*]/g,
    character => `%${character.charCodeAt(0).toString(16).toUpperCase()}`);
  const pairs = [...parsed.searchParams].sort(([ak, av], [bk, bv]) =>
    Buffer.compare(Buffer.from(ak), Buffer.from(bk)) || Buffer.compare(Buffer.from(av), Buffer.from(bv)));
  return { path: parsed.pathname, query: pairs.map(([key, value]) => `${encode(key)}=${encode(value)}`).join('&') };
}
function exact(value, required, optional = []) {
  if (!plain(value) || required.some(key => !Object.hasOwn(value, key))
      || Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) fail();
}
function required(value, keys) {
  if (!plain(value) || keys.some(key => !Object.hasOwn(value, key))) fail();
}
function isoTime(value) {
  return text(value, 64) && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$/.test(value)
    && Number.isFinite(Date.parse(value));
}
function certificatePem(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 16384 && !/[\u0000\r]/u.test(value)
    && /^-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+\n-----END CERTIFICATE-----\n?$/.test(value);
}
function validatePairingView(value) {
  required(value, ['pairingSessionId', 'deviceId', 'claimState', 'expiresAt', 'retryable', 'pollAfterMs',
    'resourceVersion', 'updatedAt']);
  if (!UUID_V4.test(value.pairingSessionId) || !DEVICE_ID.test(value.deviceId) || !CLAIM_STATES.has(value.claimState)
      || !isoTime(value.expiresAt) || !isoTime(value.updatedAt) || typeof value.retryable !== 'boolean'
      || !Number.isInteger(value.pollAfterMs) || value.pollAfterMs < 1000 || value.pollAfterMs > 10000
      || !Number.isSafeInteger(value.resourceVersion) || value.resourceVersion < 1
      || (value.ownershipEpoch !== undefined && (!Number.isSafeInteger(value.ownershipEpoch) || value.ownershipEpoch < 1))
      || (value.taskId !== undefined && !text(value.taskId, 128))
      || (value.errorCode !== undefined && !/^[A-Z][A-Z0-9_]{1,127}$/.test(value.errorCode))) fail();
  if (['ownershipCommitted', 'projectionPending', 'deviceAckPending', 'completed', 'attentionRequired'].includes(value.claimState)
      && !text(value.taskId, 128)) fail();
  return Object.freeze({ pairingSessionId: value.pairingSessionId, deviceId: value.deviceId,
    claimState: value.claimState, expiresAt: value.expiresAt, retryable: value.retryable,
    pollAfterMs: value.pollAfterMs, resourceVersion: value.resourceVersion, updatedAt: value.updatedAt,
    ...(value.ownershipEpoch === undefined ? {} : { ownershipEpoch: value.ownershipEpoch }),
    ...(value.taskId === undefined ? {} : { taskId: value.taskId }),
    ...(value.errorCode === undefined ? {} : { errorCode: value.errorCode }) });
}
function validateAttestation(value) {
  required(value, ['deviceId', 'certificatePem', 'certificateChainPem', 'serialNumber', 'publicKeySha256',
    'expiresAt', 'certificateStatus', 'statusCheckedAt']);
  if (!DEVICE_ID.test(value.deviceId) || !certificatePem(value.certificatePem)
      || !Array.isArray(value.certificateChainPem) || value.certificateChainPem.length > 8
      || !value.certificateChainPem.every(certificatePem)
      || !text(value.serialNumber, 128) || !SHA256_HEX.test(value.publicKeySha256)
      || !isoTime(value.expiresAt) || value.certificateStatus !== 'active' || !isoTime(value.statusCheckedAt)) fail();
  return Object.freeze({ deviceId: value.deviceId, certificatePem: value.certificatePem,
    certificateChainPem: Object.freeze([...value.certificateChainPem]), serialNumber: value.serialNumber,
    publicKeySha256: value.publicKeySha256, expiresAt: value.expiresAt,
    certificateStatus: value.certificateStatus, statusCheckedAt: value.statusCheckedAt });
}
function validateSyncBootstrap(value) {
  required(value, ['account', 'devices', 'clients', 'activePairings', 'cursor', 'snapshotSequence']);
  if (!plain(value.account) || !Array.isArray(value.devices) || value.devices.length > 128
      || !value.devices.every(plain) || !Array.isArray(value.clients) || value.clients.length > 128
      || !value.clients.every(plain) || !Array.isArray(value.activePairings) || value.activePairings.length > 32
      || !text(value.cursor, 4096) || !Number.isSafeInteger(value.snapshotSequence) || value.snapshotSequence < 0) fail();
  return Object.freeze({ account: Object.freeze({ ...value.account }),
    devices: Object.freeze(value.devices.map(item => Object.freeze({ ...item }))),
    clients: Object.freeze(value.clients.map(item => Object.freeze({ ...item }))),
    activePairings: Object.freeze(value.activePairings.map(validatePairingView)),
    cursor: value.cursor, snapshotSequence: value.snapshotSequence });
}
function validatePairingPrepare(input, idempotencyKey) {
  exact(input, ['deviceId', 'pairingSessionId', 'clientAttemptId', 'protocolVersion', 'transport', 'expiresInSeconds']);
  if (!DEVICE_ID.test(input.deviceId) || !UUID_V4.test(input.pairingSessionId)
      || !UUID_V4.test(input.clientAttemptId) || input.clientAttemptId !== idempotencyKey
      || input.protocolVersion !== 2 || input.transport !== 'ble-gatt'
      || !Number.isInteger(input.expiresInSeconds) || input.expiresInSeconds < 1 || input.expiresInSeconds > 300) fail('INVALID_REQUEST');
  return { ...input };
}
function validatePairingProof(input, idempotencyKey) {
  exact(input, ['deviceId', 'pairingSessionId', 'clientId', 'pairingTokenSha256', 'protocolVersion',
    'transport', 'securityProfile', 'localTranscriptSha256', 'expiresInSeconds']);
  if (!UUID_V4.test(idempotencyKey) || !DEVICE_ID.test(input.deviceId) || !UUID_V4.test(input.pairingSessionId)
      || !text(input.clientId, 128) || !SHA256_HEX.test(input.pairingTokenSha256)
      || input.protocolVersion !== 2 || input.transport !== 'ble-gatt'
      || input.securityProfile !== 'NEXUSAOS_LOCAL_AEAD_V2' || !SHA256_HEX.test(input.localTranscriptSha256)
      || input.expiresInSeconds !== 300) fail('INVALID_REQUEST');
  return { ...input };
}
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
function validatePasswordReset(value) {
  if (!plain(value) || Object.keys(value).length !== 3
      || typeof value.code !== 'string' || !/^\d{6}$/.test(value.code)) fail('INVALID_REQUEST');
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
  const remoteCode = text(result.errorCode, 128) ? result.errorCode
    : plain(result.data) && text(result.data.errorCode, 128) ? result.data.errorCode : undefined;
  const reject = code => { throw new DesktopError(code, { httpStatus: result.httpStatus,
    ...(remoteCode ? { remoteCode } : {}), ...(text(result.requestId, 128) ? { traceId: result.requestId } : {}) }); };
  if (connectivity && result.applicationDenied) throw new DesktopError('APPLICATION_AUTHORIZATION_DENIED', { phase: 'ticket', httpStatus: result.httpStatus });
  if (result.code === 401) reject('AUTHENTICATION_REQUIRED');
  if (purpose === 'console-claim' && result.code === 403
    && ['CONSOLE_CLAIM_DISABLED', 'FEATURE_NOT_AVAILABLE'].includes(remoteCode)) reject('DEVICE_AUTHORIZATION_NOT_ENABLED');
  if (result.code === 403) reject('ACCESS_DENIED');
  if (result.code === 429) reject('RATE_LIMITED');
  if (purpose === 'password-login' && remoteCode === 'AUTH_RATE_LIMITED') reject('RATE_LIMITED');
  if (purpose === 'password-login' && remoteCode === 'PASSWORD_INVALID') reject('AUTHENTICATION_FAILED');
  if (purpose === 'password-reset' && remoteCode === 'SMS_CODE_INVALID') reject('VERIFICATION_CODE_INVALID');
  if (purpose === 'password-reset' && ([400, 422].includes(result.code)
      || ['PASSWORD_WEAK', 'VALIDATION_ERROR'].includes(remoteCode))) reject('INVALID_REQUEST');
  if (['registration', 'password-reset'].includes(purpose)
      && (result.code === 602 || ['AUTH_RATE_LIMITED', 'SMS_RATE_LIMITED', 'SMS_DAILY_LIMIT',
        'PASSWORD_RESET_RATE_LIMITED'].includes(remoteCode))) reject('RATE_LIMITED');
  if (purpose === 'console-claim' && ['DEVICE_ALREADY_OWNED', 'OWNER_ALREADY_COMMITTED', 'OWNER_ALREADY_CHANGED',
    'CONSOLE_CLAIM_CONFLICT', 'IDEMPOTENCY_CONFLICT'].includes(remoteCode)) reject('DEVICE_ALREADY_CLAIMED');
  if (purpose === 'console-claim' && remoteCode === 'CONSOLE_CLAIM_EXPIRED') reject('CLAIM_CODE_EXPIRED');
  if (purpose === 'console-claim' && ['CONSOLE_CLAIM_NOT_FOUND', 'CONSOLE_CLAIM_INACTIVE'].includes(remoteCode)) reject('CLAIM_CODE_INVALID');
  if (purpose === 'console-claim' && result.code >= 500) reject('ACCOUNT_SERVICE_UNAVAILABLE');
  if (purpose === 'console-claim' && [400, 404, 409, 410, 422].includes(result.code)) reject('INVALID_REQUEST');
  if (purpose === 'registration' && ([400, 409, 422].includes(result.code)
      || ['SMS_CODE_INVALID', 'PASSWORD_ALREADY_SET', 'PASSWORD_INVALID'].includes(remoteCode))) reject('INVALID_REQUEST');
  if (result.code !== 200 || result.success === false) {
    if (connectivity) throw new DesktopError('ACCOUNT_SERVICE_UNAVAILABLE', { phase: 'ticket', httpStatus: result.httpStatus });
    if (purpose === 'password-reset') reject('ACCOUNT_SERVICE_UNAVAILABLE');
    fail('AUTHENTICATION_FAILED');
  }
  return result.data;
}

async function createConsumerClient({ config, store, fetchImpl = fetch, timeoutMs = 10000, platform = process.platform,
  authFactory, now = Date.now, monotonicNow = () => performance.now() }) {
  const factory = authFactory || (await import('@nexusaos/connectivity-electron/auth')).createElectronConsumerAuth;
  let epoch = 0;
  let disposed = false;
  const requests = new Set();
  const claimAttempts = new Map();
  let claimSubject;
  function clearClaimAttempts() { claimAttempts.clear(); claimSubject = undefined; }
  function invalidate() { ++epoch; clearClaimAttempts(); for (const controller of requests) controller.abort(); }
  function requireLoginDeadline(current) {
    if (!Number.isSafeInteger(current.sessionExpiresAt) || current.sessionExpiresAt <= now()) fail('SESSION_EXPIRED');
    return current;
  }

  async function request(method, route, body, signed, extraHeaders = undefined, externalSignal = undefined) {
    const expected = epoch;
    if (externalSignal?.aborted) throw new DOMException('Aborted', 'AbortError');
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
      const target = signingTarget(route);
      const versioned = target.path.startsWith('/app-api/v1/');
      const version = versioned ? 'NEXUSAOS-CONSUMER-APP-V1' : 'NEXUSAOS-CONSUMER-V1';
      const canonical = [version, signed.accountId, signed.clientId, method, target.path,
        ...(versioned ? [target.query] : []), timestamp, nonce, digest].join('\n');
      Object.assign(headers, { authorization: `Bearer ${signed.accessToken}`, 'x-nexus-client-id': signed.clientId,
        'x-nexus-timestamp': timestamp, 'x-nexus-nonce': nonce, 'x-nexus-body-sha256': digest,
        ...(versioned ? { 'x-nexus-signature-version': version } : {}),
        'x-nexus-signature': await identity.sign(Buffer.from(canonical)) });
    }
    if (disposed || expected !== epoch) fail('STALE_GENERATION');
    if (requests.size >= 8) fail('RESOURCE_EXHAUSTED');
    const controller = new AbortController(); requests.add(controller);
    let timedOut = false;
    const externalAbort = () => controller.abort();
    externalSignal?.addEventListener('abort', externalAbort, { once: true });
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    try {
      const response = await fetchImpl(config.consumerBaseUrl + route, { method, headers, redirect: 'error',
        signal: controller.signal, ...(bytes.length ? { body: bytes } : {}) });
      // Gateways may return an HTML error page. The HTTP status is sufficient
      // to classify a service outage; never parse or reflect that response.
      const modernEnvelope = route === CONSOLE_CLAIM_ROUTE || route.startsWith('/app-api/v1/');
      if (response.status >= 500 && response.status <= 599
          && !(modernEnvelope && /^application\/json(?:\s*;|$)/i.test(response.headers.get('content-type') || ''))) {
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
      if (!plain(envelope) || (!Number.isInteger(envelope.code)
        && !(modernEnvelope && envelope.code === undefined && typeof envelope.success === 'boolean'))
        || (modernEnvelope && (typeof envelope.success !== 'boolean'
          || (envelope.success && envelope.errorCode != null)))) fail();
      const headerRequestId = response.headers.get('x-request-id') || response.headers.get('request-id');
      if (headerRequestId && envelope.requestId && headerRequestId !== envelope.requestId) fail();
      return { code: response.ok ? (envelope.code === undefined || envelope.code === 0 ? 200 : envelope.code) : response.status,
        success: envelope.success, data: envelope.data, httpStatus: response.status,
        errorCode: envelope.errorCode,
        requestIdHeader: headerRequestId, requestIdBody: envelope.requestId,
        requestId: headerRequestId || envelope.requestId,
        serverTime: envelope.serverTime || envelope.time,
        receivedMonotonicMs: monotonicNow(),
        // Legacy Admin has no machine error code for this policy rejection.
        // Match only this exact established response; unknown 601 messages are
        // never guessed to mean an application is unregistered or undeployed.
        applicationDenied: method === 'POST' && /^\/app-api\/devices\/(?:[A-Za-z0-9._-]|%3A)+\/connectivity\/sessions$/.test(route)
          && response.status === 200 && envelope.code === 601 && envelope.msg === 'Connectivity应用或权限未获准' };
    } catch (error) {
      if (externalSignal?.aborted) throw new DOMException('Aborted', 'AbortError');
      if (timedOut) fail('REQUEST_TIMEOUT');
      if (error instanceof DesktopError) throw error;
      throw new DesktopError('ACCOUNT_SERVICE_UNAVAILABLE', { phase: 'account_service' });
    } finally {
      clearTimeout(timer); externalSignal?.removeEventListener('abort', externalAbort); requests.delete(controller);
    }
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
    save: store.save,
    remove: async () => { clearClaimAttempts(); await store.remove(); },
    exchange: async current => {
      requireLoginDeadline(current);
      const refreshed = tokens(checkEnvelope(await request('POST', '/app-api/auth/refresh',
        { refreshToken: current.refreshToken })), current.clientId, current.accountId, now);
      // A sliding server refresh must not extend this desktop login's fixed deadline,
      // including when the refresh response arrives after that deadline.
      requireLoginDeadline(current);
      return { ...refreshed, expiresAt: Math.min(refreshed.expiresAt, current.sessionExpiresAt), identityKey: current.identityKey,
        sessionExpiresAt: current.sessionExpiresAt };
    },
    isSessionRejected: error => error instanceof DesktopError && ['AUTHENTICATION_REQUIRED', 'SESSION_EXPIRED'].includes(error.code),
  });
  async function protectedRequest(method, route, body, extraHeaders) {
    const expected = epoch;
    let response;
    try { response = await auth.authorized(current => request(method, route, body, requireLoginDeadline(current), extraHeaders), result => result.code === 401); }
    catch (error) {
      if (['AUTH_SESSION_MISSING', 'AUTH_SESSION_CHANGED', 'AUTH_INVALID_SESSION'].includes(error?.code)) fail('SESSION_EXPIRED');
      if (error?.code === 'AUTH_SESSION_STORAGE_FAILED') fail('SECURE_STORAGE_UNAVAILABLE');
      if (error instanceof DesktopError && ['AUTHENTICATION_REQUIRED', 'SESSION_EXPIRED'].includes(error.code)) {
        await auth.clear();
        fail('SESSION_EXPIRED');
      }
      throw error;
    }
    if (disposed || expected !== epoch) fail('AUTHENTICATION_REQUIRED');
    if (response.code === 401) { await auth.clear(); fail('SESSION_EXPIRED'); }
    return checkEnvelope(response, route.endsWith('/connectivity/sessions'));
  }
  async function protectedPairingRequest(method, route, body, extraHeaders, validate, signal) {
    const response = await protectedResponse(method, route, body, extraHeaders, signal);
    if (!text(response.requestIdHeader, 128) || !text(response.requestIdBody, 128)
        || response.requestIdHeader !== response.requestIdBody) fail();
    if ((response.code !== 200 || response.success === false) && PAIRING_ERROR_CODES.has(response.errorCode)) {
      if (!text(response.requestId, 128) || !isoTime(response.serverTime)
          || typeof response.receivedMonotonicMs !== 'number' || !Number.isFinite(response.receivedMonotonicMs)) fail();
      throw new ConsumerPairingError(response.errorCode, response);
    }
    const data = checkEnvelope(response);
    if (!text(response.requestId, 128) || !isoTime(response.serverTime)
        || typeof response.receivedMonotonicMs !== 'number' || !Number.isFinite(response.receivedMonotonicMs)) fail();
    return Object.freeze({ data: validate(data), serverTime: response.serverTime,
      requestId: response.requestId, receivedMonotonicMs: response.receivedMonotonicMs });
  }
  async function protectedResponse(method, route, body, extraHeaders, signal, subject) {
    const expected = epoch;
    if (subject && subject.epoch !== expected) fail('STALE_GENERATION');
    let response;
    try { response = await auth.authorized(current => {
      if (subject && (subject.epoch !== epoch || current.accountId !== subject.accountId || current.clientId !== subject.clientId)) {
        fail('STALE_GENERATION');
      }
      return request(method, route, body, requireLoginDeadline(current), extraHeaders, signal);
    }, result => result.code === 401); }
    catch (error) {
      if (['AUTH_SESSION_MISSING', 'AUTH_SESSION_CHANGED', 'AUTH_INVALID_SESSION'].includes(error?.code)) fail('SESSION_EXPIRED');
      if (error?.code === 'AUTH_SESSION_STORAGE_FAILED') fail('SECURE_STORAGE_UNAVAILABLE');
      if (error instanceof DesktopError && ['AUTHENTICATION_REQUIRED', 'SESSION_EXPIRED'].includes(error.code)) {
        await auth.clear(); fail('SESSION_EXPIRED');
      }
      throw error;
    }
    if (disposed || expected !== epoch) fail('AUTHENTICATION_REQUIRED');
    if (response.code === 401) { await auth.clear(); fail('SESSION_EXPIRED'); }
    return response;
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
      deviceModel: 'Zhijun Desktop', displayName: '知君桌面' }), false,
    route.endsWith('/register') ? 'registration' : 'password-login');
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
    async resetPassword(input, guard = () => true) {
      if (disposed) fail('OPERATION_NOT_ALLOWED');
      const credentials = validatePasswordReset(input);
      checkEnvelope(await request('POST', '/app-api/auth/password/reset', credentials), false, 'password-reset');
      // A reset revokes every server session. Remove any local token copy before
      // returning the deliberately account-opaque acknowledgement.
      await auth.clear();
      clearClaimAttempts();
      if (!guard()) fail('STALE_GENERATION');
      return Object.freeze({ processed: true });
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
      // Cloud presence and old device-list membership cannot replace a real,
      // latest device authorization ACK. Never fall back to the old list.
      const expected = epoch;
      let data;
      try {
        ({ data } = await protectedPairingRequest('GET', '/app-api/v1/sync/bootstrap',
          undefined, undefined, validateSyncBootstrap));
      } catch (error) {
        if (error instanceof ConsumerPairingError && error.code === 'FEATURE_NOT_AVAILABLE') {
          throw new DesktopError('DEVICE_AUTHORIZATION_NOT_ENABLED', { remoteCode: error.code, traceId: error.requestId });
        }
        throw error;
      }
      const current = await auth.current();
      if (disposed || expected !== epoch) fail('STALE_GENERATION');
      if (data.account.accountId !== current.accountId || !plain(data.account.currentClient)
        || data.account.currentClient.clientId !== current.clientId || data.account.currentClient.isCurrent !== true) fail();
      if (data.account.accountStatus !== 'active' || data.account.currentClient.clientStatus !== 'active'
        || data.account.currentClient.hasActiveSession !== true) fail('ACCESS_DENIED');
      const ids = new Set();
      return data.devices.map(device => {
        if (!plain(device) || !DEVICE_ID.test(device.deviceId)
          || ids.has(device.deviceId)
          || !['claimPending', 'active', 'releasePending'].includes(device.ownershipStatus)
          || !['projectionPending', 'deviceAckPending', 'ready', 'blocked'].includes(device.accessStatus)
          || !['normal', 'disabled', 'quarantined'].includes(device.securityStatus)
          || !['online', 'offline', 'unknown'].includes(device.cloudPresence)
          || !plain(device.capabilities) || !plain(device.capabilities.canConnect)
          || typeof device.capabilities.canConnect.enabled !== 'boolean'
          || (device.deviceName != null && !text(device.deviceName, 128))) fail();
        ids.add(device.deviceId);
        return { deviceId: device.deviceId, displayName: device.deviceName || device.deviceId,
          availability: device.cloudPresence, allowed: device.ownershipStatus === 'active'
            && device.accessStatus === 'ready' && device.securityStatus === 'normal'
            && device.capabilities.canConnect.enabled === true };
      }).filter(device => device.allowed).map(({ allowed, ...device }) => device);
    },
    async claimDevice(input) {
      // Factory sticker codes are opaque, exact Base32 strings. Never trim,
      // uppercase, truncate or route them through the legacy six-digit API.
      if (typeof input !== 'string' || ![10, 20].includes(input.length) || !CONSOLE_CLAIM_CODE.test(input)) fail('INVALID_REQUEST');
      const claimCode = input;
      const expected = epoch;
      const current = requireLoginDeadline(await auth.current());
      if (disposed || expected !== epoch) fail('STALE_GENERATION');
      const subject = JSON.stringify([current.accountId, current.clientId]);
      if (claimSubject !== subject) { clearClaimAttempts(); claimSubject = subject; }
      let idempotencyKey = claimAttempts.get(claimCode);
      if (!idempotencyKey) {
        if (claimAttempts.size >= 32) fail('RESOURCE_EXHAUSTED');
        idempotencyKey = crypto.randomUUID();
        claimAttempts.set(claimCode, idempotencyKey);
      }
      let data;
      try {
        const response = await protectedResponse('POST', CONSOLE_CLAIM_ROUTE,
          { claimCode, clientAttemptId: idempotencyKey }, { 'idempotency-key': idempotencyKey }, undefined,
          { epoch: expected, accountId: current.accountId, clientId: current.clientId });
        data = checkEnvelope(response, false, 'console-claim');
        if (!text(response.requestIdHeader, 128) || response.requestIdHeader !== response.requestIdBody
          || !isoTime(response.serverTime)) fail();
        required(data, ['deviceId', 'ownership', 'claim', 'authorization']);
        if (!DEVICE_ID.test(data.deviceId) || data.ownership !== 'claimed' || !plain(data.claim)
          || !UUID_V4.test(data.claim.sessionId) || data.claim.status !== 'redeemed'
          || !text(data.claim.bindingId, 128)
          || (data.authorization !== null && (!plain(data.authorization)
            || !['deviceAckPending', 'ready', 'blocked'].includes(data.authorization.state)))) fail();
      } catch (error) {
        // A lost or malformed response may follow a committed Owner write.
        // Keep its attempt/body for manual recovery, never automatically replay.
        if (expected === epoch && claimSubject === subject && (!(error instanceof DesktopError)
          || !['REQUEST_TIMEOUT', 'ACCOUNT_SERVICE_UNAVAILABLE', 'CONTRACT_MISMATCH', 'RESPONSE_TOO_LARGE'].includes(error.code))) {
          claimAttempts.delete(claimCode);
        }
        throw error;
      }
      // Receipt only. The runtime must not admit this to its connection list;
      // listDevices/bootstrap is the sole authority for authorization readiness.
      return { deviceId: data.deviceId, displayName: data.deviceId, availability: 'unknown' };
    },
    async preparePairing(input, idempotencyKey, signal) {
      if (!UUID_V4.test(idempotencyKey)) fail('INVALID_REQUEST');
      const body = validatePairingPrepare(input, idempotencyKey);
      return protectedPairingRequest('POST', '/app-api/v1/device-pairing/prepare', body,
        { 'idempotency-key': idempotencyKey }, validatePairingView, signal);
    },
    async getDeviceAttestation(deviceId, pairingSessionId, signal) {
      if (!DEVICE_ID.test(deviceId) || !UUID_V4.test(pairingSessionId)) fail('INVALID_REQUEST');
      return protectedPairingRequest('GET', `/app-api/v1/device-attestations/${encodeURIComponent(deviceId)}?pairingSessionId=${encodeURIComponent(pairingSessionId)}`,
        undefined, undefined, validateAttestation, signal);
    },
    async provePairing(input, idempotencyKey, signal) {
      const body = validatePairingProof(input, idempotencyKey);
      const current = await auth.current();
      if (current.clientId !== body.clientId) fail('AUTHENTICATION_REQUIRED');
      return protectedPairingRequest('POST', '/app-api/v1/device-pairing/prove', body,
        { 'idempotency-key': idempotencyKey }, validatePairingView, signal);
    },
    async cancelPairing(pairingSessionId, idempotencyKey, signal) {
      if (!UUID_V4.test(pairingSessionId) || !UUID_V4.test(idempotencyKey)) fail('INVALID_REQUEST');
      return protectedPairingRequest('POST', `/app-api/v1/device-pairing/${encodeURIComponent(pairingSessionId)}/cancel`,
        { reason: 'user_cancelled' }, { 'idempotency-key': idempotencyKey }, value => {
          required(value, ['cancelApplied', 'pairing']);
          if (typeof value.cancelApplied !== 'boolean') fail();
          return Object.freeze({ cancelApplied: value.cancelApplied, pairing: validatePairingView(value.pairing) });
        }, signal);
    },
    async getPairing(pairingSessionId, signal) {
      if (!UUID_V4.test(pairingSessionId)) fail('INVALID_REQUEST');
      return protectedPairingRequest('GET', `/app-api/v1/device-pairing/${encodeURIComponent(pairingSessionId)}`,
        undefined, undefined, validatePairingView, signal);
    },
    async listActivePairings(signal) {
      return protectedPairingRequest('GET', '/app-api/v1/device-pairings?state=active', undefined, undefined, value => {
        required(value, ['items']);
        if (!Array.isArray(value.items) || value.items.length > 32) fail();
        return Object.freeze({ items: Object.freeze(value.items.map(validatePairingView)) });
      }, signal);
    },
    async syncBootstrap(signal) {
      return protectedPairingRequest('GET', '/app-api/v1/sync/bootstrap', undefined, undefined, validateSyncBootstrap, signal);
    },
    async createSession(deviceId, body) {
      if (!DEVICE_ID.test(deviceId)) fail('INVALID_REQUEST');
      // This POST only retries an explicit pre-handler 401, never a lost result or business write.
      return protectedRequest('POST', `/app-api/devices/${encodeURIComponent(deviceId)}/connectivity/sessions`, body);
    },
    async current() {
      const current = await auth.current();
      try { return requireLoginDeadline(current); }
      catch (error) { await auth.clear(); throw error; }
    },
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
module.exports = { createConsumerClient, validatePassword, validateRegistration, validatePasswordReset, validatePhone,
  LOGIN_SESSION_MS };
