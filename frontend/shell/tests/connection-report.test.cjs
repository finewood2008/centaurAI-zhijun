'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { summarize, readReport } = require('../scripts/connection-report.cjs');
const event = (id, stage, status, extra = {}) => ({ attemptId: id.toString(16).padStart(24, '0'), stage, status, ms: 1000, directBudgetMs: 8000, ...extra });

test('report separates successful path share, failures, cancellations and incomplete attempts', () => {
  const report = summarize([
    event(1, 'total_connect', 'ok', { selectedPath: 'DIRECT' }),
    event(2, 'direct_connect', 'error', { code: 'SDK_DIRECT_UNAVAILABLE', fallbackReason: 'DIRECT_TIMEOUT' }),
    event(2, 'relay_connect', 'ok'), event(2, 'total_connect', 'ok', { selectedPath: 'RELAY', ms: 9000 }),
    event(3, 'total_connect', 'error'), event(4, 'total_connect', 'cancelled'), event(5, 'direct_ticket', 'ok'),
    event(6, 'direct_connect', 'error', { code: 'SDK_DIRECT_UNAVAILABLE', fallbackReason: 'ICE_FAILED' }),
    { token: 'private-token' },
  ]);
  assert.equal(report.observedAttempts, 6);
  const group = report.byDirectBudgetMs[8000];
  assert.equal(group.directSuccessShare, 0.5);
  assert.equal(group.failed, 1); assert.equal(group.cancelled, 1); assert.equal(group.incomplete, 2);
  assert.deepEqual(group.fallbackReasons, { DIRECT_TIMEOUT: 1, ICE_FAILED: 0, unknown: 0 });
  assert.equal(group.successfulTotalP95Ms, 9000);
  assert.equal(JSON.stringify(report).includes('attemptId'), false);
  assert.equal(JSON.stringify(report).includes('private'), false);
});

test('report groups budgets and never claims a success rate without completed successes', () => {
  const report = summarize([event(1, 'total_connect', 'error', { directBudgetMs: 4000 }), event(2, 'direct_ticket', 'ok', { directBudgetMs: undefined })]);
  assert.equal(report.byDirectBudgetMs[4000].directSuccessShare, null);
  assert.equal(report.byDirectBudgetMs.unknown.incomplete, 1);
});

test('a missing log reports no observations', async () => {
  assert.deepEqual(await readReport('/nonexistent-zhijun-diagnostic-dir/log.jsonl'), { observedAttempts: 0, byDirectBudgetMs: {} });
});

test('reader merges rotation, skips malformed records and rejects symlinks and oversized files', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-report-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const filename = path.join(directory, 'connection-timing.jsonl');
  await fs.writeFile(`${filename}.1`, `${JSON.stringify(event(1, 'direct_connect', 'ok'))}\ninvalid\n`);
  await fs.writeFile(filename, `${JSON.stringify(event(1, 'total_connect', 'ok', { selectedPath: 'DIRECT' }))}\n`);
  assert.equal((await readReport(filename)).byDirectBudgetMs[8000].direct, 1);
  await fs.symlink(filename, path.join(directory, 'link'));
  await assert.rejects(readReport(path.join(directory, 'link')));
  await fs.writeFile(filename, ' '.repeat(256 * 1024 + 1));
  await assert.rejects(readReport(filename));
});
