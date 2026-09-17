'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { decodeJson } = require('../runtime/product-policy.cjs');
const { toPublicError } = require('../runtime/public-error.cjs');

const reply = (status, code) => ({ status, headers: { 'content-type': 'application/json' },
  body: Buffer.from(JSON.stringify({ code, message: 'private upstream diagnostic', secret: 'private-sentinel' })) });

test('unknown Gateway operation retains only its precise safe code through public errors', () => {
  assert.throws(() => decodeJson(reply(403, 'WORKSPACE_OPERATION_DENIED')), error => {
    const value = toPublicError(error);
    assert.equal(value.code, 'ACCESS_DENIED');
    assert.equal(value.httpStatus, 403);
    assert.equal(value.remoteCode, 'WORKSPACE_OPERATION_DENIED');
    assert.equal(JSON.stringify(value).includes('private'), false);
    return true;
  });
});

test('other denials, inconsistent statuses and malformed bodies never indicate a missing operation', () => {
  for (const response of [reply(403, 'WORKSPACE_AUTHORIZATION_REVOKED'), reply(403, 'UNKNOWN_PRIVATE_CODE'),
    reply(401, 'WORKSPACE_OPERATION_DENIED'), reply(500, 'WORKSPACE_OPERATION_DENIED'),
    { ...reply(403), body: Buffer.from('not-json') },
    { ...reply(403), body: Buffer.from('[{"code":"WORKSPACE_OPERATION_DENIED"}]') },
    { ...reply(403), body: Buffer.alloc(8193, 32) }]) {
    assert.throws(() => decodeJson(response), error => {
      assert.equal(toPublicError(error).remoteCode, undefined);
      assert.equal(error.code, response.status === 500 ? 'REMOTE_ERROR' : 'ACCESS_DENIED');
      return true;
    });
  }
});
