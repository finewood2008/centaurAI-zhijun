'use strict';
const { DesktopError } = require('../runtime/public-error.cjs');
const { createCredentialStore } = require('./credential-store.cjs');
const { createConsumerClient } = require('./consumer-client.cjs');
const { createSdkRuntime } = require('./sdk-runtime.cjs');

async function createProductionAdapter({ config, directory, safeStorage, consumer,
  bridge, runtimeFactory = createSdkRuntime }) {
  const client = consumer || await createConsumerClient({ config,
    store: createCredentialStore({ directory, safeStorage, consumerBaseUrl: config.consumerBaseUrl }) });
  let epoch = 0;
  const closers = new Set();
  async function closeAll() {
    epoch++;
    await Promise.allSettled([...closers].map(close => close()));
    closers.clear();
  }
  return Object.freeze({
    signIn: (input, guard) => client.signIn(input, guard),
    listDevices: () => client.listDevices(),
    async connect(binding) {
      // D03 is not a URL/config toggle. Only an implemented, trusted main adapter
      // may authorize a business session. Never pass a P2P ticket as a JWT.
      if (!bridge || typeof bridge.authorize !== 'function') throw new DesktopError('BUSINESS_BRIDGE_REQUIRED');
      if (!config.connectivity) throw new DesktopError('CONFIGURATION_REQUIRED');
      const current = await client.current();
      if (current.accountId !== binding.accountId) throw new DesktopError('AUTHENTICATION_REQUIRED');
      const expected = epoch;
      const runtime = await runtimeFactory({ config: config.connectivity, consumer: client });
      if (expected !== epoch) { await runtime.close(); throw new DesktopError('STALE_GENERATION'); }
      let session; let authorization; let closed = false;
      const close = async () => {
        if (closed) return;
        closed = true;
        try { await authorization?.close?.(); } finally {
          try { await runtime.close(); } finally { closers.delete(close); }
        }
      };
      closers.add(close);
      try {
        session = await runtime.connect(binding.deviceId);
        if (expected !== epoch) throw new DesktopError('STALE_GENERATION');
      } catch (error) { await close(); throw error instanceof DesktopError ? error : new DesktopError('TRANSPORT_UNAVAILABLE'); }
      return Object.freeze({
        async authorize() {
          if (closed || expected !== epoch) throw new DesktopError('SESSION_NOT_READY');
          const value = await bridge.authorize({ session, subject: { accountId: current.accountId, clientId: current.clientId,
            deviceId: binding.deviceId }, applicationId: config.connectivity.applicationId });
          if (closed || expected !== epoch) { await value?.close?.(); throw new DesktopError('STALE_GENERATION'); }
          authorization = value;
          if (!value || value.accountId !== binding.accountId || value.deviceId !== binding.deviceId
            || typeof value.request !== 'function') throw new DesktopError('ACCESS_DENIED');
          return { accountId: value.accountId, deviceId: value.deviceId };
        },
        async request(request) {
          if (closed || !authorization || expected !== epoch) throw new DesktopError('SESSION_NOT_READY');
          // The bridge owns business credentials. Renderer only chooses an approved operation.
          return authorization.request({ method: request.method, relative_path: request.path,
            headers: Object.fromEntries(Object.entries(request.headers).map(([key, value]) => [key, [value]])) });
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
