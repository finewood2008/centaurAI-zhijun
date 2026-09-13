'use strict';
// Local-only summary: never prints attempt IDs, addresses, tokens, or raw errors.
const fs = require('node:fs/promises');
const { constants } = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { sanitizeTiming, MAX_BYTES } = require('../production/connection-timing.cjs');

function summarize(records) {
  const attempts = new Map();
  for (const raw of records) {
    const record = sanitizeTiming(raw);
    if (!record) continue;
    if (!attempts.has(record.attemptId)) attempts.set(record.attemptId, []);
    attempts.get(record.attemptId).push(record);
  }
  const groups = {};
  for (const events of attempts.values()) {
    const budget = events.find(event => event.directBudgetMs)?.directBudgetMs ?? 'unknown';
    const group = groups[budget] ??= { attempts: 0, direct: 0, relay: 0, failed: 0, cancelled: 0, incomplete: 0,
      fallbackReasons: { DIRECT_TIMEOUT: 0, ICE_FAILED: 0, unknown: 0 }, totalMs: [] };
    group.attempts++;
    const total = events.find(event => event.stage === 'total_connect');
    if (events.some(event => event.stage === 'relay_connect')) {
      const reason = events.find(event => event.stage === 'direct_connect' && event.fallbackReason)?.fallbackReason ?? 'unknown';
      group.fallbackReasons[reason]++;
    }
    if (!total || (total.status === 'ok' && !total.selectedPath)) group.incomplete++;
    else if (total.status === 'error') group.failed++;
    else if (total.status === 'cancelled') group.cancelled++;
    else { group[total.selectedPath.toLowerCase()]++; group.totalMs.push(total.ms); }
  }
  for (const group of Object.values(groups)) {
    const times = group.totalMs.sort((a, b) => a - b);
    const successes = group.direct + group.relay;
    group.directSuccessShare = successes ? group.direct / successes : null;
    group.successfulTotalP50Ms = times.length ? times[Math.ceil(times.length * 0.5) - 1] : null;
    group.successfulTotalP95Ms = times.length ? times[Math.ceil(times.length * 0.95) - 1] : null;
    delete group.totalMs;
  }
  return { observedAttempts: attempts.size, byDirectBudgetMs: groups };
}

async function readReport(filename) {
  const records = [];
  for (const current of [`${filename}.1`, filename]) {
    let file;
    try {
      file = await fs.open(current, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      const stat = await file.stat();
      if (!stat.isFile() || stat.size > MAX_BYTES) throw new Error('Unsafe diagnostic file');
      const buffer = Buffer.alloc(MAX_BYTES);
      const { bytesRead } = await file.read(buffer, 0, MAX_BYTES, 0);
      for (const line of buffer.subarray(0, bytesRead).toString('utf8').split('\n')) {
        try { records.push(JSON.parse(line)); } catch { /* Rotation may leave a partial record. */ }
      }
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    finally { await file?.close(); }
  }
  return summarize(records);
}

if (require.main === module) {
  const filename = process.argv[2] || path.join(os.homedir(), 'Library/Application Support/zhijun-desktop/connection-timing.jsonl');
  readReport(filename).then(report => process.stdout.write(`${JSON.stringify(report, null, 2)}\n`)).catch(() => {
    process.stderr.write('Cannot safely read connection diagnostics.\n'); process.exitCode = 1;
  });
}
module.exports = { summarize, readReport };
