'use strict';

const path = require('node:path');
const crypto = require('node:crypto');
const { app, safeStorage } = require('electron');
const { loadConfig } = require('../production/config.cjs');
const { createProductionAdapter } = require('../production/adapter.cjs');
const { createBusinessBridge } = require('../production/business-bridge.cjs');
const { createProductSession } = require('../runtime/product-session.cjs');
const {
  AcceptanceError, parseArgs, loadManifest, collectResourceEvidence, createReceiptWriter,
  compareResourceEvidence, executeRound, safePlan,
} = require('./product-stability-core.cjs');

let args;
try { args = parseArgs(process.argv.slice(2)); }
catch (error) {
  const code = typeof error?.code === 'string' ? error.code : 'INVALID_ARGUMENT';
  process.stderr.write(`${JSON.stringify({ status: 'failed', errorCode: code, stage: 'arguments' })}\n`);
  app.exit(2);
  return;
}
app.setName('知君桌面');
app.setPath('userData', args.userDataPath || process.env.ZHIJUN_DESKTOP_USER_DATA
  || path.join(app.getPath('appData'), 'zhijun-desktop'));

const safeError = error => ({
  code: typeof error?.code === 'string' && /^[A-Z][A-Z0-9_]{1,99}$/.test(error.code) ? error.code : 'ACCEPTANCE_FAILED',
  stage: typeof error?.stage === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(error.stage) ? error.stage : 'runner',
});

function connectionGuard(connected) {
  let failure;
  let rejectFailure;
  const pending = new Promise((_, reject) => { rejectFailure = reject; });
  pending.catch(() => {});
  connected.onFailure(error => {
    if (failure) return;
    failure = new AcceptanceError(
      typeof error?.code === 'string' ? error.code : 'CONNECTIVITY_SESSION_EXPIRED', 'connectivity',
    );
    rejectFailure(failure);
  });
  return {
    guard(work) { if (failure) throw failure; return Promise.race([Promise.resolve(work), pending]); },
    check() { if (failure) throw failure; },
  };
}

async function openProduct(adapter, identity, manifest) {
  const connected = await adapter.connect({ accountId: identity.accountId, deviceId: manifest.deviceId });
  const gate = connectionGuard(connected);
  try {
    const proof = await gate.guard(connected.authorize());
    if (proof.product !== true || typeof proof.workspaceId !== 'string') throw new AcceptanceError('PRODUCT_AUTHORIZATION_REQUIRED', 'authorize');
    const product = createProductSession({ session: connected, isCurrent: () => true,
      budget: { active: new Set() }, onTerminal: error => {
        if (error?.code === 'CONNECTIVITY_SESSION_EXPIRED') {
          // The connection callback owns the public failure; this hook only keeps
          // product-session terminal handling active for the same production path.
        }
      } });
    return { connected, product, gate };
  } catch (error) { await connected.close().catch(() => {}); throw error; }
}

async function authenticate(adapter, manifest, allowSavedLogin) {
  let identity = await adapter.restore();
  if (!identity && allowSavedLogin) identity = await adapter.signInSaved(() => true, true);
  if (!identity) throw new AcceptanceError('AUTHENTICATION_REQUIRED', 'authenticate');
  const devices = await adapter.listDevices();
  const selected = devices.find(device => device.deviceId === manifest.deviceId);
  if (!selected) throw new AcceptanceError('DEVICE_NOT_AUTHORIZED', 'device-selection');
  if (selected.availability !== 'online') throw new AcceptanceError('DEVICE_OFFLINE', 'device-selection');
  return identity;
}

async function main() {
  const manifest = await loadManifest(args.manifestPath);
  const rounds = args.rounds ?? manifest.rounds;
  if (args.plan) {
    process.stdout.write(`${JSON.stringify(safePlan(manifest, rounds), null, 2)}\n`);
    return;
  }
  if (!app.requestSingleInstanceLock()) throw new AcceptanceError('DESKTOP_ALREADY_RUNNING', 'startup');
  await app.whenReady();
  const runId = crypto.randomBytes(8).toString('hex');
  const repoRoot = path.resolve(__dirname, '../../..');
  const configPath = args.configPath || process.env.ZHIJUN_DESKTOP_CONFIG
    || path.join(repoRoot, 'data/desktop/zhijun-product-v2.json');
  const outputPath = args.outputPath || path.join(repoRoot, `data/acceptance/product-stability-${runId}.jsonl`);
  const writer = await createReceiptWriter(outputPath, runId);
  let adapter;
  let firstEvidence;
  try {
    await writer.append({ schemaVersion: 1, kind: 'run-started', status: 'running', runId,
      deviceId: manifest.deviceId, rounds: args.preflight ? 0 : rounds, startedAt: new Date().toISOString(),
      transport: 'authenticated-connectivity-sdk-direct-only' });
    const config = await loadConfig(configPath);
    adapter = await createProductionAdapter({ config, directory: app.getPath('userData'), safeStorage,
      bridge: createBusinessBridge() });
    const identity = await authenticate(adapter, manifest, args.allowSavedLogin);
    const evidence = await collectResourceEvidence(manifest);
    firstEvidence = evidence;
    const preflightRef = await writer.resource('preflight', evidence);
    const opened = await openProduct(adapter, identity, manifest);
    opened.product.close();
    await opened.connected.close();
    await writer.append({ schemaVersion: 1, kind: 'preflight', status: 'passed', runId,
      deviceId: manifest.deviceId, checkedAt: new Date().toISOString(), bootId: preflightRef.bootId,
      resourceSample: preflightRef });
    if (args.preflight) {
      await writer.append({ schemaVersion: 1, kind: 'run-finished', status: 'passed', runId,
        deviceId: manifest.deviceId, roundsCompleted: 0, finishedAt: new Date().toISOString() });
      process.stdout.write(`${JSON.stringify({ status: 'passed', mode: 'preflight', runId, output: outputPath })}\n`);
      return;
    }

    for (let round = 1; round <= rounds; round++) {
      const before = await collectResourceEvidence(manifest);
      const beforeRef = await writer.resource(`round-${round}-before`, before);
      let openedRound;
      let outcome;
      const deadline = Date.now() + 15 * 60 * 1000;
      try {
        openedRound = await openProduct(adapter, identity, manifest);
        outcome = await executeRound({ product: openedRound.product, runId, round, deadline,
          guard: work => openedRound.gate.guard(work) });
        openedRound.gate.check();
      } finally {
        openedRound?.product.close();
        await openedRound?.connected.close().catch(() => {});
      }
      const after = await collectResourceEvidence(manifest);
      const afterRef = await writer.resource(`round-${round}-after`, after);
      const stable = compareResourceEvidence(before, after, manifest);
      if (!stable.passed) throw new AcceptanceError('RESOURCE_STABILITY_FAILED', `round-${round}`, { issues: stable.issues });
      await writer.append({ schemaVersion: 1, kind: 'round', ...outcome, runId,
        completedAt: new Date().toISOString(), bootId: stable.bootId,
        resourceSamples: { before: beforeRef, after: afterRef } });
    }
    const finalEvidence = await collectResourceEvidence(manifest);
    const finalRef = await writer.resource('final', finalEvidence);
    const overall = compareResourceEvidence(firstEvidence, finalEvidence, manifest);
    if (!overall.passed) throw new AcceptanceError('RESOURCE_STABILITY_FAILED', 'final');
    await writer.append({ schemaVersion: 1, kind: 'run-finished', status: 'passed', runId,
      deviceId: manifest.deviceId, roundsCompleted: rounds, finishedAt: new Date().toISOString(),
      bootId: overall.bootId, resourceSample: finalRef });
    process.stdout.write(`${JSON.stringify({ status: 'passed', mode: 'full', runId, rounds, output: outputPath })}\n`);
  } catch (error) {
    const safe = safeError(error);
    await writer.append({ schemaVersion: 1, kind: 'run-finished', status: 'failed', runId,
      deviceId: manifest.deviceId, finishedAt: new Date().toISOString(), errorCode: safe.code, stage: safe.stage });
    throw error;
  } finally {
    await adapter?.dispose().catch(() => {});
    await writer.close();
  }
}

main().then(() => app.exit(0), error => {
  const safe = safeError(error);
  process.stderr.write(`${JSON.stringify({ status: 'failed', errorCode: safe.code, stage: safe.stage })}\n`);
  app.exit(1);
});
