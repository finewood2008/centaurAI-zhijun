'use strict';
const { DesktopError } = require('../runtime/public-error.cjs');
const { createCredentialStore } = require('./credential-store.cjs');
const { createConsumerClient } = require('./consumer-client.cjs');
const { createSdkRuntime, mapConnectionError } = require('./sdk-runtime.cjs');

async function createProductionAdapter({ config, directory, safeStorage, consumer, credentialStore,
  bridge, runtimeFactory = createSdkRuntime }) {
  const store = credentialStore || (directory && config.consumerBaseUrl
    ? createCredentialStore({ directory, safeStorage, consumerBaseUrl: config.consumerBaseUrl }) : null);
  const client = consumer || await createConsumerClient({ config, store });
  let epoch = 0;
  const closers = new Set();
  async function closeAll() {
    epoch++;
    await Promise.allSettled([...closers].map(close => close()));
    closers.clear();
  }
  return Object.freeze({
    restore: () => typeof client.restore === 'function' ? client.restore() : Promise.resolve(null),
    async getRememberedLogin() {
      if (!store) return null;
      const value = await store.loadRememberedLogin();
      if (value === undefined) return null;
      const keys = value && !Array.isArray(value) ? Object.keys(value).sort() : [];
      if (!value || typeof value.phone !== 'string' || !/^1\d{10}$/.test(value.phone)
          || (!(keys.length === 1 && keys[0] === 'phone') && !(keys.length === 2 && keys[0] === 'password' && keys[1] === 'phone'))
          || (Object.hasOwn(value, 'password') && (typeof value.password !== 'string' || /[\r\n\0]/u.test(value.password)
            || Buffer.byteLength(value.password) < 8 || Buffer.byteLength(value.password) > 72))) {
        throw new DesktopError('SECURE_STORAGE_UNAVAILABLE');
      }
      return { phone: value.phone, passwordSaved: Object.hasOwn(value, 'password') };
    },
    async signIn(input, guard, rememberPassword = false) {
      const identity = await client.signIn(input, guard);
      if (!guard()) throw new DesktopError('STALE_GENERATION');
      if (store) await store.saveRememberedLogin({ phone: input.phone, ...(rememberPassword ? { password: input.password } : {}) }, guard);
      if (!guard()) throw new DesktopError('STALE_GENERATION');
      return identity;
    },
    sendRegistrationCode: phone => client.sendRegistrationCode(phone),
    async register(input, guard, rememberPassword = false) {
      const identity = await client.register(input, guard);
      if (!guard()) throw new DesktopError('STALE_GENERATION');
      if (store) await store.saveRememberedLogin({ phone: input.phone, ...(rememberPassword ? { password: input.password } : {}) }, guard);
      if (!guard()) throw new DesktopError('STALE_GENERATION');
      return identity;
    },
    async signInSaved(guard, rememberPassword = true) {
      if (!store) throw new DesktopError('AUTHENTICATION_REQUIRED');
      const value = await store.loadRememberedLogin();
      const keys = value && !Array.isArray(value) ? Object.keys(value).sort() : [];
      if (!value || keys.length !== 2 || keys[0] !== 'password' || keys[1] !== 'phone'
          || typeof value.phone !== 'string' || !/^1\d{10}$/.test(value.phone) || typeof value.password !== 'string'
          || /[\r\n\0]/u.test(value.password) || Buffer.byteLength(value.password) < 8
          || Buffer.byteLength(value.password) > 72) throw new DesktopError('AUTHENTICATION_REQUIRED');
      const identity = await client.signIn({ phone: value.phone, password: value.password }, guard);
      if (!guard()) throw new DesktopError('STALE_GENERATION');
      await store.saveRememberedLogin({ phone: value.phone, ...(rememberPassword ? { password: value.password } : {}) }, guard);
      if (!guard()) throw new DesktopError('STALE_GENERATION');
      return identity;
    },
    listDevices: () => client.listDevices(),
    claimDevice: claimToken => client.claimDevice(claimToken),
    async connect(binding) {
      const expected = epoch;
      // D03 is not a URL/config toggle. Only an implemented, trusted main adapter
      // may authorize a business session. Never pass a P2P ticket as a JWT.
      if (!bridge || typeof bridge.authorize !== 'function') throw new DesktopError('BUSINESS_BRIDGE_REQUIRED');
      if (!config.connectivity) throw new DesktopError('CONFIGURATION_REQUIRED');
      const current = await client.current();
      if (expected !== epoch) throw new DesktopError('STALE_GENERATION');
      if (current.accountId !== binding.accountId) throw new DesktopError('AUTHENTICATION_REQUIRED');
      let runtime;
      try { runtime = await runtimeFactory({ config: config.connectivity, consumer: client }); }
      catch (error) { throw mapConnectionError(error); }
      if (expected !== epoch) {
        try { await runtime.close(); } catch { /* Retain the session invalidation reason. */ }
        throw new DesktopError('STALE_GENERATION');
      }
      let session; let authorization; let closed = false; let failureListener; let pendingFailure;
      const lifetime = new AbortController();
      const close = async () => {
        if (closed) return;
        closed = true;
        lifetime.abort();
        try { await authorization?.close?.(); } finally {
          try { await runtime.close(); } finally { closers.delete(close); }
        }
      };
      closers.add(close);
      try {
        session = await runtime.connect(binding.deviceId);
        if (expected !== epoch) throw new DesktopError('STALE_GENERATION');
      } catch (error) {
        // A failed cleanup must not replace a precise, already-safe connection
        // rejection with an unrelated native transport error.
        try { await close(); } catch { /* The primary connection failure wins. */ }
        throw mapConnectionError(error);
      }
      return Object.freeze({
        onFailure(listener) { failureListener = listener; if (pendingFailure && !closed) listener(pendingFailure); },
        async authorize() {
          if (closed || expected !== epoch) throw new DesktopError('SESSION_NOT_READY');
          const value = await bridge.authorize({ session, signal: lifetime.signal, subject: { accountId: current.accountId, clientId: current.clientId,
            deviceId: binding.deviceId }, applicationId: config.connectivity.applicationId,
            onFailure(error) { if (!closed && expected === epoch) { pendingFailure = error; failureListener?.(error); } },
          });
          if (closed || expected !== epoch) { await value?.close?.(); throw new DesktopError('STALE_GENERATION'); }
          authorization = value;
          if (!value || value.accountId !== binding.accountId || value.deviceId !== binding.deviceId
            || typeof value.request !== 'function') throw new DesktopError('ACCESS_DENIED');
          return { accountId: value.accountId, deviceId: value.deviceId, ...(value.product === true ? { product: true, workspaceId: value.workspaceId } : {}) };
        },
        get managesRequestQueue() { return authorization?.managesRequestQueue === true; },
        reserveTransfer(value) { if (closed || !authorization || expected !== epoch) throw new DesktopError('SESSION_NOT_READY'); return authorization.reserveTransfer?.(value); },
        releaseTransfer(value) { authorization?.releaseTransfer?.(value); },
        async request(request, options) {
          if (closed || !authorization || expected !== epoch) throw new DesktopError('SESSION_NOT_READY');
          // The bridge owns business credentials. Renderer only chooses an approved operation.
          return authorization.request({ method: request.method, relative_path: request.path,
            headers: Object.fromEntries(Object.entries(request.headers).map(([key, value]) => [key, [value]])),
            ...(request.body === undefined ? {} : { body: request.body }) }, options);
        },
        close,
      });
    },
    async signOut() {
      const clearing = client.signOut(); // Start credential invalidation before waiting for native close.
      await Promise.all([clearing, closeAll()]);
    },
    async dispose() { await Promise.all([client.dispose(), closeAll()]); },
  });
}
module.exports = { createProductionAdapter };
