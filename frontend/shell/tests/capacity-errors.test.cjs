'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { decodeJson } = require('../runtime/product-policy.cjs');
const { toPublicError } = require('../runtime/public-error.cjs');
const { createV2Scheduler } = require('../runtime/v2-request-scheduler.cjs');
const { createBusinessBridge } = require('../production/business-bridge.cjs');
const response = value => ({ status: 429, headers: { 'content-type': 'application/json' }, body: Buffer.from(JSON.stringify(value)) });

for (const [remoteCode, code] of Object.entries({
  WORKSPACE_OPERATION_CAPACITY: 'BOX_BUSY', WORKSPACE_SESSION_CAPACITY: 'BOX_BUSY',
  WORKSPACE_WORKER_CAPACITY: 'BOX_BUSY', WORKSPACE_BACKGROUND_CAPACITY: 'BOX_BUSY',
  WORKSPACE_PREVIEW_CAPACITY: 'BOX_BUSY', WORKSPACE_OBJECT_LIMIT: 'WORKSPACE_STORAGE_FULL',
  WORKSPACE_QUOTA_EXCEEDED: 'WORKSPACE_STORAGE_FULL',
  WORKSPACE_RESULT_CAPACITY: 'BOX_BUSY',
})) test(`gateway ${remoteCode} has a distinct safe public error`, () => {
  assert.throws(() => decodeJson(response({ code: remoteCode, message: 'private server message', token: 'secret' })), error => {
    assert.equal(error.code, code);
    assert.equal(error.definitelyNotSent, undefined, 'HTTP status alone must not authorize write replay');
    const publicError = toPublicError(error);
    assert.equal(publicError.remoteCode, remoteCode);
    assert.equal(publicError.httpStatus, 429);
    assert.doesNotMatch(JSON.stringify(publicError), /private|secret|token/);
    return true;
  });
});

test('unknown, malformed and oversized gateway diagnostic bodies do not leak raw codes or text', () => {
  for (const body of [Buffer.from('{'), Buffer.from('x'.repeat(8193)),
    Buffer.from(JSON.stringify({ code: 'private_key_123', message: 'secret' })),
    Buffer.from(JSON.stringify({ code: '__proto__', traceId: 'secret' }))]) {
    assert.throws(() => decodeJson({ ...response({}), body }), error => {
      const safe = toPublicError(error);
      assert.equal(safe.code, 'REMOTE_RATE_LIMITED');
      assert.equal(safe.httpStatus, 429);
      assert.equal(safe.remoteCode, undefined);
      assert.doesNotMatch(JSON.stringify(safe), /private|secret|__proto__/);
      return true;
    });
  }
});

test('client queue saturation and timeout are not reported as remote rate limiting', async () => {
  let release;
  const scheduler = createV2Scheduler({ maxInFlight: 1, maxQueued: 1, queueTimeoutMs: 10,
    send: () => new Promise(resolve => { release = resolve; }) });
  const first = scheduler.request({}, { priority: 0 }).catch(() => undefined);
  assert.equal(typeof release, 'function', 'the first native request must actually hold the slot');
  const queued = scheduler.request({}, { priority: 0 });
  const check = error => error.code === 'CLIENT_BUSY' && error.definitelyNotSent === true && error.httpStatus === undefined;
  try {
    const timedOut = assert.rejects(queued, check);
    await assert.rejects(scheduler.request({}, { priority: 0 }), check);
    await timedOut;
  } finally { scheduler.close(); release?.({ body: new Uint8Array() }); await first; }
});

test('gateway HTTP 429 does not automatically replay an operation', async () => {
  let sends = 0;
  const scheduler = createV2Scheduler({ send: async () => { sends++; return response({ code: 'WORKSPACE_OPERATION_CAPACITY' }); } });
  try {
    const value = await scheduler.request({ method: 'POST', body: Buffer.from('synthetic mutation') });
    assert.throws(() => decodeJson(value), { code: 'BOX_BUSY' });
    assert.equal(sends, 1);
  } finally { scheduler.close(); }
});

test('product authorization context preserves the same safe capacity diagnosis', async () => {
  let sends = 0;
  await assert.rejects(createBusinessBridge().authorize({
    applicationId: 'zhijun-desktop',
    subject: { accountId: 'synthetic-account', clientId: 'synthetic-client', deviceId: 'synthetic-device' },
    session: { request: async () => { sends++; return response({ code: 'WORKSPACE_SESSION_CAPACITY', message: 'private context' }); } },
  }), error => error.code === 'BOX_BUSY' && error.remoteCode === 'WORKSPACE_SESSION_CAPACITY' && error.httpStatus === 429);
  assert.equal(sends, 1);
});
