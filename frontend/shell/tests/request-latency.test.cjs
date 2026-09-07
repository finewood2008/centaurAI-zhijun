'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createV2Scheduler } = require('../runtime/v2-request-scheduler.cjs');
const { createBusinessBridge } = require('../production/business-bridge.cjs');
const { DesktopError } = require('../runtime/public-error.cjs');

function virtualClock() {
  let now = 0;
  const pending = new Map();
  const schedule = (fn, ms, every = null) => {
    const id = { unref() {} }; pending.set(id, { fn, at: now + ms, every }); return id;
  };
  const timers = {
    setTimeout: (fn, ms) => schedule(fn, ms), clearTimeout: id => pending.delete(id),
    setInterval: (fn, ms) => schedule(fn, ms, ms), clearInterval: id => pending.delete(id),
  };
  async function run(work, maximum = 180000) {
    let done = false, result, failure, steps = 0;
    Promise.resolve(work).then(value => { result = value; done = true; }, error => { failure = error; done = true; });
    const deadline = now + maximum;
    while (!done) {
      assert.ok(++steps <= 10000, 'virtual timer step budget exhausted (possible zero-delay loop)');
      await new Promise(resolve => setImmediate(resolve));
      if (done) break;
      const next = Math.min(...[...pending.values()].map(value => value.at));
      assert.ok(Number.isFinite(next) && next <= deadline, 'bounded virtual work must finish');
      now = next;
      for (const [id, item] of [...pending]) if (item.at <= now) {
        if (item.every === null) pending.delete(id); else item.at += item.every;
        item.fn();
      }
    }
    if (failure) throw failure;
    return result;
  }
  return { timers, run, now: () => now, wall: () => 1800000000000 + now,
    sleep: ms => new Promise(resolve => timers.setTimeout(resolve, ms)), pending };
}

// The shipped native sidecar reads the next input only after the current HTTP
// response. A parallel async stub hides its head-of-line blocking entirely.
function serialPeer(clock, handle) {
  let tail = Promise.resolve();
  const calls = [];
  return { calls, request(request) {
    const work = tail.then(async () => {
      const record = { at: clock.now(), request }; calls.push(record);
      const response = await handle(request);
      record.completedAt = clock.now(); return response;
    });
    tail = work.catch(() => {}); return work;
  } };
}
const response = value => ({ status: 200, headers: { 'content-type': 'application/json' }, body: Buffer.from(JSON.stringify(value)) });
const subject = { accountId: 'latency-owner', clientId: 'latency-client', deviceId: 'latency-device' };
const id = 'a'.repeat(32);
const wire = (method, path, body) => ({ method, relative_path: '/api/mindos/zhijun' + path,
  headers: { Accept: ['application/json'], ...(body === undefined ? {} : { 'Content-Type': ['application/json'] }) },
  ...(body === undefined ? {} : { body: Buffer.from(JSON.stringify(body)) }) });

test('three page reads through the serial native lane do not add a three-second dispatch staircase', async () => {
  const clock = virtualClock();
  const peer = serialPeer(clock, async () => response({}));
  const scheduler = createV2Scheduler({ send: peer.request, clock: clock.now, timers: clock.timers });
  try {
    await clock.run(Promise.all(['status', 'stats', 'list'].map(async label => {
      await scheduler.request({ label: label + ':start' }, { priority: 2 });
      await scheduler.request({ label: label + ':poll' }, { priority: 3 });
    })));
    assert.equal(peer.calls.length, 6);
    assert.ok(clock.now() < 100, `zero-latency page acquired ${clock.now()} ms of local waiting`);
  } finally { scheduler.close(); }
});

test('a pending poll cannot monopolize serial native transport ahead of JSON, cancel and heartbeat', async () => {
  const clock = virtualClock(), failures = [];
  const peer = serialPeer(clock, async request => {
    const url = new URL(request.relative_path, 'http://synthetic');
    if (url.pathname.endsWith('/context')) return response({ version: 2, ...subject,
      applicationId: 'zhijun-desktop', workspaceId: 'f'.repeat(64),
      capabilities: ['product.rpc', 'product.events', 'product.uploads', 'product.blobs'],
      expiresAt: Math.floor(clock.wall() / 1000) + 5 });
    if (url.searchParams.has('waitMs')) await clock.sleep(Number(url.searchParams.get('waitMs')));
    return response({});
  });
  const bridge = await clock.run(createBusinessBridge({ clock: clock.wall, activityClock: clock.now,
    timers: clock.timers, heartbeatMs: 100, heartbeatTimeoutMs: 1000 }).authorize({
    session: peer, subject, applicationId: 'zhijun-desktop', onFailure: error => failures.push(error.code),
  }));
  try {
    await clock.run(Promise.all([
      bridge.request(wire('GET', `/operations/${id}?after=17&waitMs=8000`)),
      bridge.request(wire('POST', '/operations', { synthetic: 'fast-json' })),
      bridge.request(wire('POST', `/operations/${id}/cancel`, { requestId: 'latency-cancel' })),
    ]));
    // Flush the queued heartbeat behind those requests without triggering another.
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(clock.now(), 250);
    assert.deepEqual(failures, []);
    assert.equal(peer.calls.filter(call => call.request.relative_path.endsWith('/context')).length, 2);
    const poll = peer.calls.find(call => call.request.relative_path.includes('?after='));
    assert.equal(poll.request.relative_path, `/api/mindos/zhijun/operations/${id}?after=17&waitMs=250`);
    assert.ok(peer.calls.every(call => call.completedAt <= 250));
  } finally { await bridge.close(); }
  assert.equal(clock.pending.size, 0);
});

test('burst business work leaves a native slot and token for control, with no ninth native request', async () => {
  const clock = virtualClock(), sent = [], held = [];
  const scheduler = createV2Scheduler({ clock: clock.now, timers: clock.timers, send: request => {
    sent.push(request.label); return new Promise(resolve => held.push(resolve));
  } });
  const pending = [];
  try {
    for (let index = 0; index < 8; index++) pending.push(scheduler.request({ label: 'business-' + index }));
    pending.push(scheduler.request({ label: 'cancel' }, { priority: 1 }));
    // Seven business requests leave the eighth native slot for cancellation.
    assert.deepEqual(sent, ['business-0', 'business-1', 'business-2', 'business-3', 'business-4', 'business-5', 'business-6', 'cancel']);
    const outcomes = Promise.allSettled(pending);
    scheduler.close();
    for (const resolve of held) resolve(response({}));
    await clock.run(outcomes);
    assert.equal(sent.length, 8);
  } finally { scheduler.close(); }
});

test('a full bucket discards idle fractional refill before a burst at a nonzero phase', async () => {
  for (const idleMs of [599, 599.5, 1799]) {
    const clock = virtualClock(), attempts = [];
    const scheduler = createV2Scheduler({ clock: clock.now, timers: clock.timers,
      send: async () => { attempts.push(clock.now()); return response({}); } });
    try {
      await clock.run(clock.sleep(idleMs));
      await clock.run(Promise.all(Array.from({ length: 9 }, () => scheduler.request({}, { priority: 0 }))));
      assert.deepEqual(attempts, [...Array(8).fill(idleMs), idleMs + 600]);
    } finally { scheduler.close(); }
  }
});

test('all attempts obey the rolling window and eight window positions remain available to control', async () => {
  const clock = virtualClock(), sent = [];
  const scheduler = createV2Scheduler({ clock: clock.now, timers: clock.timers,
    send: async request => { sent.push({ at: clock.now(), label: request.label }); return response({}); } });
  try {
    await clock.run((async () => {
      for (let index = 0; index < 92; index++) await scheduler.request({ label: 'business' });
    })());
    const waitingBusiness = scheduler.request({ label: 'waiting-business' });
    let businessDone = false; waitingBusiness.then(() => { businessDone = true; });
    await clock.run((async () => {
      for (let index = 0; index < 8; index++) await scheduler.request({ label: 'control' }, { priority: index % 2 });
    })());
    assert.equal(sent.length, 100);
    assert.equal(businessDone, false, 'business must not take the eight reserved window positions');
    await clock.run(Promise.all([waitingBusiness, scheduler.request({ label: 'boundary-control' }, { priority: 0 })]));
    assert.ok(sent.find(call => call.label === 'boundary-control').at > 60000);
    assert.ok(sent.find(call => call.label === 'waiting-business').at > 60000);
    for (const first of sent) assert.ok(sent.filter(call => call.at >= first.at && call.at <= first.at + 60000).length <= 100);
  } finally { scheduler.close(); }
});

test('definitely-unsent retries consume burst and session counts while retaining their bounded backoff', async () => {
  const clock = virtualClock(), attempts = [];
  const scheduler = createV2Scheduler({ clock: clock.now, timers: clock.timers, maxRequests: 3,
    send: async () => {
      attempts.push(clock.now());
      if (attempts.length < 3) throw new DesktopError('RATE_LIMITED', { definitelyNotSent: true });
      return response({});
    } });
  try {
    await clock.run(scheduler.request({}));
    assert.deepEqual(attempts, [0, 1200, 3600]);
    await assert.rejects(clock.run(scheduler.request({})), { code: 'SESSION_QUOTA_EXHAUSTED' });
    assert.equal(attempts.length, 3);
  } finally { scheduler.close(); }
});

test('aborting queued work preserves tokens and counts; aborting native work retains onWork until settlement', async () => {
  const clock = virtualClock(), held = [], sent = [], pending = [], work = [];
  const scheduler = createV2Scheduler({ clock: clock.now, timers: clock.timers, send: request => {
    sent.push(request); return new Promise(resolve => held.push(resolve));
  } });
  try {
    const activeAbort = new AbortController();
    for (let index = 0; index < 7; index++) pending.push(scheduler.request({ index }, {
      signal: index === 0 ? activeAbort.signal : undefined, onWork: value => work.push(value),
    }));
    const queueAbort = new AbortController();
    const queued = scheduler.request({ index: 7 }, { signal: queueAbort.signal });
    const outcomes = Promise.allSettled([...pending, queued]);
    queueAbort.abort(); activeAbort.abort();
    let nativeDone = false; work[0].then(() => { nativeDone = true; });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(nativeDone, false);
    assert.equal(sent.length, 7);
    const control = scheduler.request({ index: 8 }, { priority: 0 });
    assert.equal(sent.length, 8, 'queued cancellation must not consume the reserved token');
    for (const resolve of held) resolve(response({}));
    const result = await clock.run(outcomes); await clock.run(control);
    assert.equal(nativeDone, true);
    assert.notEqual(result[0].reason.definitelyNotSent, true);
    assert.equal(result[7].reason.definitelyNotSent, true);
  } finally { scheduler.close(); }
});
