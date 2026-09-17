'use strict';
const fs = require('node:fs/promises');
const { constants } = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { DesktopError } = require('../runtime/public-error.cjs');
const { protectPrivate } = require('./file-security.cjs');

const STAGES = new Set(['runtime_prepare', 'direct_ticket', 'direct_connect', 'relay_ticket', 'relay_connect', 'context_authorize', 'total_connect']);
const SDK_CODES = new Set(['SDK_DIRECT_UNAVAILABLE', 'SDK_CONNECT_TIMEOUT', 'SDK_SESSION_PROVIDER_FAILED', 'SDK_INVALID_SESSION',
  'SDK_PROTOCOL_VIOLATION', 'SDK_PATH_POLICY_FAILED', 'SDK_CONNECTION_CLOSED', 'IPC_SIDECAR_TIMEOUT']);
const MAX_BYTES = 256 * 1024;

function sanitizeTiming(value) {
  if (!value || !STAGES.has(value.stage) || !['ok', 'error', 'cancelled'].includes(value.status)
      || !/^[a-f0-9]{24}$/.test(value.attemptId) || !Number.isFinite(value.ms) || value.ms < 0) return null;
  const result = { attemptId: value.attemptId, stage: value.stage, ms: Math.min(86400000, Math.round(value.ms)), status: value.status };
  if (['DIRECT', 'RELAY'].includes(value.selectedPath)) result.selectedPath = value.selectedPath;
  if (Number.isInteger(value.directBudgetMs) && value.directBudgetMs >= 2000 && value.directBudgetMs <= 8000) result.directBudgetMs = value.directBudgetMs;
  if (value.stage === 'direct_connect' && value.status === 'error' && value.code === 'SDK_DIRECT_UNAVAILABLE'
      && ['DIRECT_TIMEOUT', 'ICE_FAILED'].includes(value.fallbackReason)) result.fallbackReason = value.fallbackReason;
  if (['direct_connect', 'relay_connect'].includes(value.stage)) result.includesTicket = true;
  if (typeof value.code === 'string' && (SDK_CODES.has(value.code) || new DesktopError(value.code).code === value.code)) result.code = value.code;
  return result;
}

function createConnectionTimingLogger(directory) {
  let queue = Promise.resolve(); let pending = 0;
  let securedDirectory = false; let securedFile = false;
  return value => {
    const record = sanitizeTiming(value);
    if (!directory || !record || pending >= 128) return Promise.resolve();
    const line = `${JSON.stringify(record)}\n`;
    pending++;
    queue = queue.then(async () => {
      await fs.mkdir(directory, { recursive: true, mode: 0o700 });
      if (!securedDirectory) { await protectPrivate(directory, 0o700); securedDirectory = true; }
      const filename = path.join(directory, 'connection-timing.jsonl');
      const stat = await fs.lstat(filename).catch(error => { if (error.code !== 'ENOENT') throw error; return null; });
      if (stat && (!stat.isFile() || stat.isSymbolicLink())) return;
      if (stat && stat.size + Buffer.byteLength(line) > MAX_BYTES) {
        // Reject oversized pre-existing files rather than retain an unbounded backup.
        if (stat.size > MAX_BYTES) return;
        await fs.rename(filename, `${filename}.1`);
        securedFile = false;
      }
      const file = await fs.open(filename, constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND | constants.O_NOFOLLOW, 0o600);
      try {
        if (!securedFile || !stat) { await protectPrivate(filename, 0o600); securedFile = true; }
        await file.writeFile(line);
      } finally { await file.close(); }
    }).catch(() => { /* Diagnostics must never fail or delay a connection. */ }).finally(() => { pending--; });
    return queue;
  };
}

function createConnectionTimingAttempt(observer, now = () => performance.now(), directBudgetMs) {
  const attemptId = crypto.randomBytes(12).toString('hex');
  const started = now(); let finished = false;
  function emit(stage, start, status, metadata = {}) {
    const record = sanitizeTiming({ ...metadata, directBudgetMs, attemptId, stage, ms: Math.max(0, now() - start), status });
    if (!record || typeof observer !== 'function') return;
    try { Promise.resolve(observer(record)).catch(() => {}); } catch { /* Observer isolation. */ }
  }
  return Object.freeze({
    async measure(stage, work) {
      const start = now();
      try { const value = await work(); emit(stage, start, 'ok'); return value; }
      catch (error) { emit(stage, start, 'error', { code: error?.code, fallbackReason: error?.detailCode }); throw error; }
    },
    finish(status, metadata) { if (!finished) { finished = true; emit('total_connect', started, status, metadata); } },
  });
}

module.exports = { createConnectionTimingLogger, createConnectionTimingAttempt, sanitizeTiming, MAX_BYTES };
