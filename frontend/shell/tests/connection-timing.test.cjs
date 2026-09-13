'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { createConnectionTimingLogger, createConnectionTimingAttempt, sanitizeTiming, MAX_BYTES } = require('../production/connection-timing.cjs');
const { createTicketProvider } = require('../production/sdk-runtime.cjs');
const { createProductionAdapter } = require('../production/adapter.cjs');

const sample = { attemptId: '0123456789abcdef01234567', stage: 'direct_connect', ms: 4000.25, status: 'error' };
test('Direct budget and fallback reasons are restricted to safe transport classifications', async () => {
  const events = [];
  const timing = createConnectionTimingAttempt(event => events.push(event), undefined, 8000);
  await assert.rejects(timing.measure('direct_connect', async () => {
    throw Object.assign(new Error('private'), { code: 'SDK_DIRECT_UNAVAILABLE', detailCode: 'DIRECT_TIMEOUT' });
  }));
  assert.equal(events[0].directBudgetMs, 8000);
  assert.equal(events[0].fallbackReason, 'DIRECT_TIMEOUT');
  for (const extra of [{ fallbackReason: 'private' }, { directBudgetMs: '8000' }, { directBudgetMs: 8001 }]) {
    const sanitized = sanitizeTiming({ ...sample, ...extra });
    assert.equal(sanitized.fallbackReason, undefined);
    assert.equal(sanitized.directBudgetMs, undefined);
  }
  assert.equal(sanitizeTiming({ ...sample, fallbackReason: 'ICE_FAILED', code: 'ACCESS_DENIED' }).fallbackReason, undefined);
});
async function temporary(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-timing-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return directory;
}

test('diagnostic allowlist discards identities, addresses, raw errors, and arbitrary codes', () => {
  assert.deepEqual(sanitizeTiming({ ...sample, code: 'https://private.example', deviceId: 'private-device', accountId: 'private-account',
    token: 'private-token', url: 'private-url', error: new Error('secret'), selectedPath: 'private-network' }),
  { ...sample, ms: 4000, includesTicket: true });
  assert.equal(sanitizeTiming({ ...sample, stage: 'private-token' }), null);
  assert.equal(sanitizeTiming({ ...sample, attemptId: 'private-account' }), null);
  assert.equal(sanitizeTiming({ ...sample, ms: Infinity }), null);
  assert.equal(sanitizeTiming({ ...sample, status: 'private-message' }), null);
  assert.equal(sanitizeTiming({ ...sample, code: 'SDK_DIRECT_UNAVAILABLE' }).code, 'SDK_DIRECT_UNAVAILABLE');
  assert.equal(sanitizeTiming({ ...sample, code: 'ACCESS_DENIED' }).code, 'ACCESS_DENIED');
});

test('safe log is private, rotates to one bounded backup, and never stores raw metadata', async t => {
  const directory = await temporary(t);
  const filename = path.join(directory, 'connection-timing.jsonl');
  await fs.writeFile(filename, ' '.repeat(MAX_BYTES - 20), { mode: 0o600 });
  const logger = createConnectionTimingLogger(directory);
  await logger({ ...sample, token: 'never-store-this' });
  for (let index = 0; index < 2200; index++) await logger(sample);
  assert.deepEqual((await fs.readdir(directory)).sort(), ['connection-timing.jsonl', 'connection-timing.jsonl.1']);
  for (const file of [filename, `${filename}.1`]) {
    const stat = await fs.stat(file);
    assert.equal(stat.mode & 0o777, 0o600);
    assert.ok(stat.size <= MAX_BYTES);
    assert.equal((await fs.readFile(file, 'utf8')).includes('never-store-this'), false);
  }
});

test('unsafe filesystem destinations are ignored without overwriting a symlink target', async t => {
  const directory = await temporary(t);
  const target = path.join(directory, 'private-file');
  await fs.writeFile(target, 'unchanged');
  await fs.symlink(target, path.join(directory, 'connection-timing.jsonl'));
  await createConnectionTimingLogger(directory)(sample);
  assert.equal(await fs.readFile(target, 'utf8'), 'unchanged');
  await createConnectionTimingLogger(target)(sample); // ENOTDIR is diagnostic-only.
});

test('synchronous and rejected asynchronous observers cannot change measured work or failures', async () => {
  for (const observer of [() => { throw new Error('private'); }, () => Promise.reject(new Error('private'))]) {
    const timing = createConnectionTimingAttempt(observer);
    assert.equal(await timing.measure('runtime_prepare', async () => 42), 42);
    const expected = Object.assign(new Error('private transport'), { code: 'SDK_CONNECT_TIMEOUT' });
    await assert.rejects(timing.measure('direct_connect', async () => { throw expected; }), error => error === expected);
    timing.finish('error', { code: expected.code });
  }
});

test('ticket observations classify Direct and TURN and omit all ticket fields', async () => {
  const events = [];
  const timing = createConnectionTimingAttempt(value => events.push(value));
  const provider = createTicketProvider({ createElectronAdminTicketProvider: () => ({ issue: async () => ({ token: 'secret-ticket' }) }) }, {}, timing);
  await provider.issue({ transport_policy: 'DIRECT_ONLY', device_id: 'secret-device' });
  await provider.issue({ transport_policy: 'TURN_ONLY', device_id: 'secret-device' });
  assert.deepEqual(events.map(event => event.stage), ['direct_ticket', 'relay_ticket']);
  assert.equal(JSON.stringify(events).includes('secret'), false);
});

test('adapter total ends after context authorization, shares local attempt ID, and completes only once', async () => {
  const events = [];
  const adapter = await createProductionAdapter({ config: { connectivity: { applicationId: 'app' } },
    consumer: { current: async () => ({ accountId: 'private-account', clientId: 'private-client' }), dispose: async () => {} },
    connectionTimingObserver: value => events.push(value),
    runtimeFactory: async ({ timing }) => ({ connect: () => timing.measure('direct_connect', async () => ({ selectedPath: 'DIRECT' })), close: async () => {} }),
    bridge: { authorize: async ({ subject }) => ({ ...subject, request: async () => ({}) }) },
  });
  const session = await adapter.connect({ accountId: 'private-account', deviceId: 'private-device' });
  assert.equal(events.some(event => event.stage === 'total_connect'), false);
  await session.authorize();
  await session.close();
  await adapter.dispose();
  assert.deepEqual(events.map(event => event.stage), ['runtime_prepare', 'direct_connect', 'context_authorize', 'total_connect']);
  assert.equal(new Set(events.map(event => event.attemptId)).size, 1);
  assert.equal(events.at(-1).selectedPath, 'DIRECT');
  assert.equal(JSON.stringify(events).includes('private'), false);
});

test('adapter authorization failure retains safe terminal code even when observer rejects', async () => {
  const events = [];
  const expected = Object.assign(new Error('private backend body'), { code: 'ACCESS_DENIED' });
  const adapter = await createProductionAdapter({ config: { connectivity: {} },
    consumer: { current: async () => ({ accountId: 'account', clientId: 'client' }), dispose: async () => {} },
    connectionTimingObserver: value => { events.push(value); return Promise.reject(new Error('disk unavailable')); },
    runtimeFactory: async () => ({ connect: async () => ({ selectedPath: 'DIRECT' }), close: async () => {} }),
    bridge: { authorize: async () => { throw expected; } },
  });
  const session = await adapter.connect({ accountId: 'account', deviceId: 'device' });
  await assert.rejects(session.authorize(), error => error === expected);
  await session.close(); await adapter.dispose();
  const total = events.filter(event => event.stage === 'total_connect');
  assert.equal(total.length, 1); assert.equal(total[0].status, 'error'); assert.equal(total[0].code, 'ACCESS_DENIED');
});
