'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createReadScheduler, MAX_SUBSCRIBERS } = require('../runtime/read-scheduler.cjs');
const { DesktopError } = require('../runtime/public-error.cjs');
const flush = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve; let reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const hasCode = (code) => (error) => error instanceof DesktopError && error.code === code;
const call = (id, extra = {}) => ({ key: `query-${id}`, callId: `call-${String(id).padStart(4, '0')}`, senderId: 1, generation: 1, isCurrent: () => true, ...extra });

test('limits actual concurrency to two and queue to eight', async () => {
  const scheduler = createReadScheduler();
  let starts = 0;
  const requests = Array.from({ length: 10 }, (_, i) => ({ hold: deferred(), i }));
  const promises = requests.map(({ hold, i }) => scheduler.schedule(call(i, { run: () => { starts += 1; return hold.promise; } })));
  await flush();
  assert.deepEqual(scheduler.stats(), { inFlight: 2, queued: 8, subscribers: 10, disposed: false });
  assert.equal(starts, 2);
  await assert.rejects(scheduler.schedule(call(11, { run: async () => null })), hasCode('RESOURCE_EXHAUSTED'));
  requests[0].hold.resolve('first');
  assert.equal(await promises[0], 'first');
  await flush();
  assert.equal(starts, 3);
  for (const { hold } of requests) hold.resolve('done');
  await Promise.all(promises);
  await flush();
  assert.equal(scheduler.stats().inFlight, 0);
  scheduler.dispose();
});

test('deduplicates same subject/query with independent cancellation and cross-sender isolation', async () => {
  const scheduler = createReadScheduler();
  const hold = deferred();
  let starts = 0;
  const run = () => { starts += 1; return hold.promise; };
  const a = scheduler.schedule(call(1, { key: 'same', run }));
  const aRejected = assert.rejects(a, hasCode('READ_CANCELLED'));
  const b = scheduler.schedule(call(2, { key: 'same', run }));
  const c = scheduler.schedule(call(3, { key: 'same', senderId: 2, run }));
  await flush();
  assert.equal(starts, 2);
  assert.equal(scheduler.cancel({ ...call(1), senderId: 2 }), false);
  assert.equal(scheduler.cancel(call(1)), true);
  await aRejected;
  assert.equal(scheduler.stats().inFlight, 2);
  hold.resolve('payload');
  assert.equal(await b, 'payload');
  assert.equal(await c, 'payload');
  assert.equal(scheduler.cancel(call(1)), false);
  scheduler.dispose();
});

test('queued cancellation never sends, running cancellation retains real slot', async () => {
  const scheduler = createReadScheduler({ maxInFlight: 1 });
  const hold = deferred();
  let forbiddenStarts = 0;
  const first = scheduler.schedule(call(1, { run: () => hold.promise }));
  const firstRejected = assert.rejects(first, hasCode('READ_CANCELLED'));
  const queued = scheduler.schedule(call(2, { run: () => { forbiddenStarts += 1; } }));
  const queuedRejected = assert.rejects(queued, hasCode('READ_CANCELLED'));
  await flush();
  scheduler.cancel(call(2));
  scheduler.cancel(call(1));
  await Promise.all([firstRejected, queuedRejected]);
  let nextStarted = false;
  const next = scheduler.schedule(call(3, { run: async () => { nextStarted = true; return 'next'; } }));
  await flush();
  assert.equal(nextStarted, false);
  assert.equal(scheduler.stats().inFlight, 1);
  hold.resolve('late');
  assert.equal(await next, 'next');
  assert.equal(forbiddenStarts, 0);
  scheduler.dispose();
});

test('invalidation settles every old subscriber without releasing outstanding native work', async () => {
  const scheduler = createReadScheduler({ maxInFlight: 1 });
  const hold = deferred();
  let oldQueueStarts = 0;
  const a = scheduler.schedule(call(1, { run: () => hold.promise }));
  const b = scheduler.schedule(call(2, { run: () => { oldQueueStarts += 1; } }));
  const results = [assert.rejects(a, hasCode('STALE_GENERATION')), assert.rejects(b, hasCode('STALE_GENERATION'))];
  await flush();
  scheduler.invalidate();
  await Promise.all(results);
  assert.deepEqual(scheduler.stats(), { inFlight: 1, queued: 0, subscribers: 0, disposed: false });
  const next = scheduler.schedule(call(3, { generation: 2, run: async () => 'new' }));
  await flush();
  assert.equal(scheduler.stats().queued, 1);
  hold.resolve('old-private-result');
  assert.equal(await next, 'new');
  assert.equal(oldQueueStarts, 0);
  scheduler.dispose();
});

test('validity checked before enqueue, before send and before delivery', async () => {
  const scheduler = createReadScheduler({ maxInFlight: 1 });
  await assert.rejects(scheduler.schedule(call(0, { isCurrent: () => false, run: async () => 'bad' })), hasCode('STALE_GENERATION'));
  const hold = deferred();
  let valid = true;
  let starts = 0;
  const a = scheduler.schedule(call(1, { isCurrent: () => valid, run: () => hold.promise }));
  const b = scheduler.schedule(call(2, { isCurrent: () => valid, run: () => { starts += 1; } }));
  const results = [assert.rejects(a, hasCode('STALE_GENERATION')), assert.rejects(b, hasCode('STALE_GENERATION'))];
  await flush();
  valid = false;
  hold.resolve('private-old-data');
  await Promise.all(results);
  assert.equal(starts, 0);
  scheduler.dispose();
});

test('timeout ends delivery but slot persists until actual completion', async () => {
  const scheduler = createReadScheduler({ maxInFlight: 1, timeoutMs: 15 });
  const hold = deferred();
  const a = scheduler.schedule(call(1, { run: () => hold.promise }));
  await assert.rejects(a, hasCode('REQUEST_TIMEOUT'));
  assert.equal(scheduler.stats().inFlight, 1);
  let starts = 0;
  const b = scheduler.schedule(call(2, { run: () => { starts += 1; } }));
  await assert.rejects(b, hasCode('REQUEST_TIMEOUT'));
  assert.equal(starts, 0);
  hold.reject(new Error('late-private-sdk-error'));
  await flush();
  assert.equal(scheduler.stats().inFlight, 0);
  scheduler.dispose();
});

test('duplicate call ids and unlimited dedup subscribers are rejected', async () => {
  const scheduler = createReadScheduler();
  const hold = deferred();
  const requests = Array.from({ length: MAX_SUBSCRIBERS }, (_, i) => scheduler.schedule(call(i, { key: 'same', run: () => hold.promise })));
  await assert.rejects(scheduler.schedule(call(0, { key: 'same', run: () => hold.promise })), hasCode('INVALID_REQUEST'));
  await assert.rejects(scheduler.schedule(call(100, { key: 'same', run: () => hold.promise })), hasCode('RESOURCE_EXHAUSTED'));
  assert.equal(scheduler.stats().subscribers, 64);
  hold.resolve('shared');
  const data = await Promise.all(requests);
  assert.equal(data.length, 64);
  scheduler.dispose();
});

test('dispose settles work; synchronous and falsy native failures become safe errors', async () => {
  const scheduler = createReadScheduler();
  await assert.rejects(scheduler.schedule(call(1, { run: () => { throw new Error('private'); } })), hasCode('REMOTE_ERROR'));
  await assert.rejects(scheduler.schedule(call(2, { run: () => Promise.reject(null) })), hasCode('REMOTE_ERROR'));
  const pending = scheduler.schedule(call(3, { run: async () => 'must-not-run' }));
  const result = assert.rejects(pending, hasCode('STALE_GENERATION'));
  scheduler.dispose();
  await result;
  await assert.rejects(scheduler.schedule(call(4, { run: async () => null })), hasCode('OPERATION_NOT_ALLOWED'));
  await flush();
  assert.equal(scheduler.stats().inFlight, 0);
});
