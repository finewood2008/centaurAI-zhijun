'use strict';

const { DesktopError, toPublicError } = require('./public-error.cjs');
const { normalizeMaterialsQuery, buildMaterialsRequest, projectMaterialsResponse } = require('./materials.cjs');
const { createReadScheduler } = require('./read-scheduler.cjs');
const { createSimulationAdapter } = require('./adapters.cjs');
const { createProductSession, productMethods } = require('./product-session.cjs');
const { validatePassword } = require('../production/consumer-client.cjs');

const ARG_COUNTS = { getSnapshot: 0, getRememberedLogin: 1, signInWithPassword: 2, signInWithSavedPassword: 2, beginSignIn: 1, listDevices: 1, connect: 2,
  disconnect: 1, signOut: 1, 'materials.list': 2, cancelRead: 2,
  ...Object.fromEntries(productMethods.map(method => [`product.${method}`, method === 'requestMicrophone' ? 1 : 2])) };
const CALL_ID = /^[A-Za-z0-9_-]{8,100}$/;
const plain = (value) => value !== null && typeof value === 'object'
  && !Array.isArray(value) && [Object.prototype, null].includes(Object.getPrototypeOf(value));
const safeText = (value, max = 256) => typeof value === 'string' && value.trim().length > 0
  && value.length <= max && !/[\u0000-\u001f\u007f-\u009f]/u.test(value);
function exact(value, keys) {
  return plain(value) && Reflect.ownKeys(value).length === keys.length
    && keys.every((key) => Object.hasOwn(value, key));
}
function assert(condition, code = 'INVALID_REQUEST') {
  if (!condition) throw new DesktopError(code);
}

function createDesktopRuntime({ mode = 'unconfigured', adapter, timeoutMs = 15000, productHost = {} } = {}) {
  assert(['unconfigured', 'simulation', 'production'].includes(mode));
  assert(mode !== 'production' || adapter, 'CONFIGURATION_REQUIRED');
  assert(Number.isSafeInteger(timeoutMs) && timeoutMs > 0 && timeoutMs <= 120000);
  const auth = mode === 'simulation' ? (adapter || createSimulationAdapter()) : mode === 'production' ? adapter : null;
  const scheduler = createReadScheduler({ timeoutMs });
  let generation = 0;
  let sequence = 0;
  let phase = 'signed_out';
  let accountId = null;
  let deviceId = null;
  let deviceName = null;
  let session = null;
  let productSession = null;
  let workspaceId = null;
  let publicError;
  let devices = [];
  let devicesRevision = 0;
  let disposed = false;
  const listeners = new Set();
  const waits = new Set();
  const pending = new Set();
  const closing = new Set();
  const adapterCalls = new Set();
  const productBudget = { active: new Set() };
  let signingOut = null;

  function callAdapter(fn) {
    // Invalidating an IPC waiter cannot cancel a native authentication call.
    // Keep a real outstanding-call budget until the adapter itself settles.
    assert(adapterCalls.size < 8, 'RESOURCE_EXHAUSTED');
    const promise = Promise.resolve().then(fn);
    adapterCalls.add(promise);
    promise.then(() => adapterCalls.delete(promise), () => adapterCalls.delete(promise));
    return promise;
  }
  function clearIdentity() {
    if (!auth) return Promise.resolve();
    // Cleanup remains available at capacity, but repeated cleanup is coalesced.
    if (!signingOut) {
      const promise = Promise.resolve().then(() => auth.signOut());
      signingOut = promise;
      const clear = () => { if (signingOut === promise) signingOut = null; };
      promise.then(clear, clear);
    }
    return signingOut;
  }

  function snapshot() {
    const result = { protocolVersion: 1, environment: mode, generation, sequence, phase,
      subject: accountId ? { accountId, ...(deviceId ? { deviceId } : {}), ...(deviceName ? { deviceName } : {}), ...(workspaceId ? { workspaceId } : {}) } : null,
      capabilities: { materialsRead: phase === 'ready', product: phase === 'ready' && Boolean(productSession), streamChat: phase === 'ready' && Boolean(productSession),
        uploads: phase === 'ready' && Boolean(productSession), matters: phase === 'ready' && Boolean(productSession), provisioning: false } };
    if (publicError && phase !== 'ready') result.error = { ...publicError };
    return result;
  }
  function publish(nextPhase, error) {
    phase = nextPhase;
    publicError = error ? toPublicError(error) : undefined;
    sequence += 1;
    const value = snapshot();
    // An observer cannot throw into a control operation or mutate another observer's copy.
    queueMicrotask(() => {
      if (disposed) return;
      for (const listener of [...listeners]) {
        try { Promise.resolve(listener(structuredClone(value))).catch(() => {}); } catch {}
      }
    });
  }
  function isCurrent(value) { return !disposed && value === generation; }
  function ensureCurrent(value) { assert(isCurrent(value), 'STALE_GENERATION'); }
  function invalidate() {
    generation += 1;
    productSession?.close();
    productSession = null;
    workspaceId = null;
    devicesRevision += 1;
    scheduler.invalidate();
    for (const cancel of [...waits]) cancel(new DesktopError('STALE_GENERATION'));
  }
  function bounded(promise, gen, onLate) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (error, value) => {
        if (settled) return false;
        settled = true;
        clearTimeout(timer);
        waits.delete(cancel);
        error ? reject(error) : resolve(value);
        return true;
      };
      const cancel = (error) => finish(error);
      const timer = setTimeout(() => finish(new DesktopError('REQUEST_TIMEOUT')), timeoutMs);
      waits.add(cancel);
      Promise.resolve(promise).then((value) => {
        if (!isCurrent(gen)) {
          finish(new DesktopError('STALE_GENERATION'));
          if (onLate) onLate(value);
        } else if (!finish(null, value) && onLate) onLate(value);
      }, (error) => finish(error instanceof DesktopError ? error : new DesktopError('REMOTE_ERROR')));
    });
  }
  function closeSession(value) {
    if (!value || typeof value.close !== 'function') return Promise.resolve();
    const task = new Promise((resolve) => {
      const timer = setTimeout(resolve, Math.min(timeoutMs, 1000));
      Promise.resolve().then(() => value.close()).catch(() => {}).finally(() => {
        clearTimeout(timer);
        resolve();
      });
    });
    closing.add(task);
    task.finally(() => closing.delete(task));
    return task;
  }
  function detachSession() {
    const old = session;
    session = null;
    deviceId = null;
    deviceName = null;
    return closeSession(old);
  }
  function failure(error, gen) {
    if (!isCurrent(gen)) return;
    invalidate();
    void detachSession();
    if (error instanceof DesktopError && ['SESSION_EXPIRED', 'CONNECTIVITY_SESSION_EXPIRED'].includes(error.code)) {
      accountId = null;
      devices = [];
      // Credential cleanup belongs to the main process. Start it before the
      // renderer sees the logged-out snapshot; remote revocation may finish later.
      void clearIdentity().catch(() => {});
    }
    publish('failed', error);
  }
  function needAccount() { assert(accountId, 'AUTHENTICATION_REQUIRED'); }
  function validateDevices(values) {
    assert(Array.isArray(values) && values.length <= 256, 'CONTRACT_MISMATCH');
    const ids = new Set();
    return values.map((value) => {
      assert(plain(value) && safeText(value.deviceId) && safeText(value.displayName, 128)
        && ['online', 'offline', 'unknown'].includes(value.availability)
        && !ids.has(value.deviceId), 'CONTRACT_MISMATCH');
      ids.add(value.deviceId);
      return { deviceId: value.deviceId, displayName: value.displayName, availability: value.availability };
    });
  }

  async function execute(operation, args, senderId, ticket) {
    if (operation === 'getSnapshot') return snapshot();
    const [context, input] = args;
    ensureCurrent(context.expectedGeneration);
    if (operation === 'getRememberedLogin') {
      assert(mode === 'production' && auth && typeof auth.getRememberedLogin === 'function', 'OPERATION_NOT_ALLOWED');
      const gen = generation;
      const value = await bounded(callAdapter(() => auth.getRememberedLogin()), gen);
      ensureCurrent(gen);
      assert(value === null || (plain(value) && exact(value, ['phone', 'passwordSaved'])
        && typeof value.phone === 'string' && /^1\d{10}$/.test(value.phone)
        && typeof value.passwordSaved === 'boolean'), 'CONTRACT_MISMATCH');
      return value;
    }
    if (operation === 'cancelRead') {
      assert(CALL_ID.test(input) && typeof input === 'string');
      return { delivery: scheduler.cancel({ callId: input, senderId, generation }) ? 'suppressed' : 'not_found',
        remoteCancellation: 'not_supported' };
    }
    if (operation === 'signOut' || operation === 'disconnect') {
      invalidate();
      ticket.generation = generation;
      const gen = generation;
      const cleanup = detachSession();
      if (operation === 'signOut') { accountId = null; devices = []; }
      // Start credential cleanup at control acceptance, before any await can be
      // superseded. A later disconnect must never bypass a requested sign-out.
      const identityCleanup = operation === 'signOut' && auth ? clearIdentity() : null;
      publish('disconnecting');
      await cleanup;
      ensureCurrent(gen);
      if (operation === 'signOut' && auth) {
        try {
          await bounded(identityCleanup, gen);
          ensureCurrent(gen);
        } catch (error) { failure(error, gen); throw error; }
      }
      publish(accountId ? 'selecting_device' : 'signed_out');
      return snapshot();
    }
    assert(auth, 'CONFIGURATION_REQUIRED');
    if (operation === 'beginSignIn' || operation === 'signInWithPassword' || operation === 'signInWithSavedPassword') {
      assert((mode === 'production') === (operation !== 'beginSignIn'), 'OPERATION_NOT_ALLOWED');
      const credentials = operation === 'signInWithPassword'
        ? validatePassword({ phone: input?.phone, password: input?.password }) : undefined;
      const rememberPassword = operation === 'signInWithPassword'
        ? (assert(exact(input, ['phone', 'password']) || exact(input, ['phone', 'password', 'rememberPassword'])),
          assert(input.rememberPassword === undefined || typeof input.rememberPassword === 'boolean'), input.rememberPassword === true)
        : operation === 'signInWithSavedPassword'
          ? (assert(typeof input === 'boolean'), input) : false;
      assert(!signingOut, 'OPERATION_NOT_ALLOWED');
      assert(['signed_out', 'authenticating', 'selecting_device', 'failed'].includes(phase), 'OPERATION_NOT_ALLOWED');
      invalidate();
      ticket.generation = generation;
      const gen = generation;
      void detachSession();
      accountId = null;
      devices = [];
      publish('authenticating');
      try {
        const guard = () => isCurrent(gen);
        const identity = await bounded(callAdapter(() => operation === 'signInWithSavedPassword'
          ? auth.signInSaved(guard, rememberPassword)
          : auth.signIn(credentials, guard, rememberPassword)), gen);
        ensureCurrent(gen);
        assert(plain(identity) && safeText(identity.accountId), 'CONTRACT_MISMATCH');
        accountId = identity.accountId;
        publish('selecting_device');
        return snapshot();
      } catch (error) { failure(error, gen); throw error; }
    }
    needAccount();
    if (operation === 'listDevices') {
      assert(['selecting_device', 'ready', 'failed'].includes(phase), 'OPERATION_NOT_ALLOWED');
      const gen = generation;
      const revision = ++devicesRevision;
      const result = await bounded(callAdapter(() => auth.listDevices()), gen);
      ensureCurrent(gen);
      assert(revision === devicesRevision, 'STALE_GENERATION');
      devices = validateDevices(result);
      return devices.map((value) => ({ ...value }));
    }
    if (operation === 'connect') {
      assert(safeText(input));
      assert(['selecting_device', 'ready', 'connecting', 'authorizing', 'failed'].includes(phase), 'OPERATION_NOT_ALLOWED');
      const selectedDevice = devices.find((value) => value.deviceId === input);
      assert(selectedDevice, 'ACCESS_DENIED');
      invalidate();
      ticket.generation = generation;
      const gen = generation;
      void detachSession();
      deviceId = input;
      deviceName = selectedDevice.displayName.trim() || input;
      const binding = { accountId, deviceId };
      publish('connecting');
      try {
        const connected = await bounded(callAdapter(() => auth.connect({ ...binding })), gen, closeSession);
        // A generation can change between promise settlement and this continuation.
        if (!isCurrent(gen)) { void closeSession(connected); throw new DesktopError('STALE_GENERATION'); }
        if (!connected || typeof connected.authorize !== 'function'
            || typeof connected.request !== 'function' || typeof connected.close !== 'function') {
          void closeSession(connected);
          throw new DesktopError('CONTRACT_MISMATCH');
        }
        session = connected;
        connected.onFailure?.(error => failure(error, gen));
        ensureCurrent(gen);
        publish('authorizing');
        const proof = await bounded(callAdapter(() => connected.authorize()), gen);
        ensureCurrent(gen);
        assert(plain(proof) && proof.accountId === binding.accountId && proof.deviceId === binding.deviceId, 'ACCESS_DENIED');
        if (proof.product === true) {
          assert(typeof proof.workspaceId === 'string' && /^[a-f0-9]{64}$/.test(proof.workspaceId), 'CONTRACT_MISMATCH');
          workspaceId = proof.workspaceId;
          productSession = createProductSession({ session: connected, isCurrent: () => isCurrent(gen) && session === connected,
            host: productHost, budget: productBudget, timeoutMs: Math.min(timeoutMs, 12000), onTerminal: error => failure(error, gen) });
        }
        publish('ready');
        return snapshot();
      } catch (error) { failure(error, gen); throw error; }
    }
    if (operation.startsWith('product.')) {
      assert(phase === 'ready' && session && productSession, 'SESSION_NOT_READY');
      return productSession.invoke(operation.slice(8), input);
    }
    if (operation === 'materials.list') {
      assert(phase === 'ready' && session, 'SESSION_NOT_READY');
      const query = normalizeMaterialsQuery(input);
      const gen = generation;
      const currentSession = session;
      try {
        return await scheduler.schedule({ key: JSON.stringify([accountId, deviceId, query]),
          callId: context.callId, senderId, generation: gen,
          isCurrent: () => isCurrent(gen) && session === currentSession && phase === 'ready',
          run: async () => {
            ensureCurrent(gen);
            const response = await currentSession.request(buildMaterialsRequest(query));
            ensureCurrent(gen);
            return projectMaterialsResponse(response, query);
          } });
      } catch (error) {
        if (error instanceof DesktopError && ['SESSION_EXPIRED', 'CONNECTIVITY_SESSION_EXPIRED'].includes(error.code)) failure(error, gen);
        throw error;
      }
    }
    throw new DesktopError('OPERATION_NOT_ALLOWED');
  }

  async function invoke(operation, args, senderId) {
    const ticket = { generation };
    let pendingKey;
    let registered = false;
    try {
      assert(!disposed, 'OPERATION_NOT_ALLOWED');
      assert(typeof operation === 'string' && Object.hasOwn(ARG_COUNTS, operation)
        && Array.isArray(args) && args.length === ARG_COUNTS[operation]);
      assert(typeof senderId === 'number' && Number.isSafeInteger(senderId) && senderId >= 0);
      if (operation !== 'getSnapshot') {
        const context = args[0];
        assert(exact(context, ['callId', 'expectedGeneration']) && typeof context.callId === 'string'
          && CALL_ID.test(context.callId) && Number.isSafeInteger(context.expectedGeneration)
          && context.expectedGeneration >= 0);
        ticket.generation = context.expectedGeneration;
        pendingKey = JSON.stringify([senderId, context.callId]);
        assert(!pending.has(pendingKey));
        assert(pending.size < 128, 'RESOURCE_EXHAUSTED');
        pending.add(pendingKey);
        registered = true;
      }
      const data = await execute(operation, args, senderId, ticket);
      if (operation === 'getSnapshot') return { ok: true, generation: data.generation, data };
      ensureCurrent(ticket.generation);
      return { ok: true, generation: ticket.generation, data };
    } catch (error) {
      if (mode === 'production' && isCurrent(ticket.generation) && error instanceof DesktopError
          && ['AUTHENTICATION_REQUIRED', 'SESSION_EXPIRED', 'CONNECTIVITY_SESSION_EXPIRED', 'SECURE_STORAGE_UNAVAILABLE'].includes(error.code)) {
        accountId = null;
        devices = [];
        failure(error, ticket.generation);
      }
      return { ok: false, generation: ticket.generation, error: toPublicError(error) };
    } finally { if (registered) pending.delete(pendingKey); }
  }
  function subscribe(listener) {
    assert(typeof listener === 'function');
    assert(!disposed, 'OPERATION_NOT_ALLOWED');
    assert(listeners.size < 64, 'RESOURCE_EXHAUSTED');
    listeners.add(listener);
    return () => listeners.delete(listener);
  }
  async function dispose() {
    if (disposed) return;
    disposed = true;
    invalidate();
    listeners.clear();
    scheduler.dispose();
    await detachSession();
    await Promise.all([...closing]);
    // Dispose transports without revoking the encrypted login session. Only an
    // explicit sign-out or terminal SESSION_EXPIRED removes credentials.
    await closeSession({ close: auth?.dispose ? () => auth.dispose() : clearIdentity });
    accountId = null;
    devices = [];
    phase = 'signed_out';
  }
  async function mediaResponse(request) {
    if (phase !== 'ready' || !productSession) return new Response(null, { status: 403 });
    return productSession.mediaResponse(request);
  }

  if (mode === 'production' && typeof auth?.restore === 'function') {
    const gen = generation;
    publish('authenticating');
    void bounded(callAdapter(() => auth.restore()), gen).then((identity) => {
      if (!isCurrent(gen)) return;
      if (identity) {
        assert(plain(identity) && safeText(identity.accountId), 'CONTRACT_MISMATCH');
        accountId = identity.accountId;
        publish('selecting_device');
      } else {
        publish('signed_out');
      }
    }, (error) => {
      if (!isCurrent(gen)) return;
      accountId = null;
      devices = [];
      failure(error instanceof DesktopError ? error : new DesktopError('AUTHENTICATION_REQUIRED'), gen);
    });
  }
  return { invoke, snapshot, subscribe, dispose, mediaResponse };
}

module.exports = { createDesktopRuntime };
