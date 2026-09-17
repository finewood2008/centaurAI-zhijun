'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { validateConfig } = require('../production/config.cjs');
const base = { version: 1, consumerBaseUrl: 'https://consumer.example.test/prod-api', connectivity: {
  applicationId: 'zhijun-desktop', purpose: 'zhijun.workspace', requestedScopes: ['remote.p2p'],
  gatewayHost: 'gateway.example.test', iceHost: 'gateway.example.test', sidecarPath: '/tmp/synthetic-sidecar',
  sidecarSha256: 'a'.repeat(64), profile: 'SOVEREIGN_DIRECT_ONLY',
} };
test('legacy configuration stays compatible and direct budget is explicit and bounded', () => {
  assert.equal(validateConfig(base).connectivity.directConnectTimeoutMs, undefined);
  for (const value of [2000, 4000, 8000]) {
    assert.equal(validateConfig({ ...base, connectivity: { ...base.connectivity, directConnectTimeoutMs: value } }).connectivity.directConnectTimeoutMs, value);
  }
  for (const value of [undefined, null, 0, 1999, 8001, 4000.5, '4000', true]) {
    assert.throws(() => validateConfig({ ...base, connectivity: { ...base.connectivity, directConnectTimeoutMs: value } }), { code: 'CONFIGURATION_REQUIRED' });
  }
  assert.throws(() => validateConfig({ ...base, connectivity: { ...base.connectivity, skipDirect: true } }), { code: 'CONFIGURATION_REQUIRED' });
});
