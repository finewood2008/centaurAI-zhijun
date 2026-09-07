'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const { DesktopError } = require('../runtime/public-error.cjs');

function createCredentialStore({ directory, safeStorage, consumerBaseUrl }) {
  const scope = crypto.createHash('sha256').update(consumerBaseUrl).digest('hex');
  const root = path.join(directory, 'consumer', scope);
  let queue = Promise.resolve();
  const identityFlights = new Map();
  function secure() {
    if (!safeStorage?.isEncryptionAvailable() || safeStorage.getSelectedStorageBackend?.() === 'basic_text') {
      throw new DesktopError('SECURE_STORAGE_UNAVAILABLE');
    }
  }
  function serial(fn) {
    const next = queue.then(fn);
    queue = next.catch(() => {});
    return next.catch(() => { throw new DesktopError('SECURE_STORAGE_UNAVAILABLE'); });
  }
  async function ensureRoot() {
    await fs.mkdir(root, { recursive: true, mode: 0o700 });
    if ((await fs.lstat(root)).isSymbolicLink()) throw new Error('Invalid storage directory');
  }
  async function read(name) {
    secure();
    let file;
    try { file = await fs.open(path.join(root, name), require('node:fs').constants.O_RDONLY | require('node:fs').constants.O_NOFOLLOW); }
    catch (error) { if (error.code === 'ENOENT') return undefined; throw error; }
    try {
      const stat = await file.stat();
      if (!stat.isFile() || stat.size > 65536) throw new Error('Invalid encrypted record');
      const bytes = Buffer.alloc(65537);
      const { bytesRead } = await file.read(bytes, 0, bytes.length, 0);
      if (bytesRead > 65536) throw new Error('Invalid encrypted record');
      return JSON.parse(safeStorage.decryptString(bytes.subarray(0, bytesRead)));
    } finally { await file.close(); }
  }
  async function save(name, value) {
    secure(); await ensureRoot();
    const bytes = safeStorage.encryptString(JSON.stringify(value));
    if (bytes.length > 65536) throw new Error('Invalid encrypted record');
    const temporary = path.join(root, `.${crypto.randomUUID()}.tmp`);
    try {
      const file = await fs.open(temporary, 'wx', 0o600);
      try { await file.writeFile(bytes); await file.sync(); } finally { await file.close(); }
      await fs.rename(temporary, path.join(root, name));
    } finally { await fs.rm(temporary, { force: true }); }
  }
  return Object.freeze({
    identity(accountHint = 'default') {
      if (typeof accountHint !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(accountHint)) return Promise.reject(new DesktopError('INVALID_REQUEST'));
      if (!identityFlights.has(accountHint)) {
        if (identityFlights.size >= 32) return Promise.reject(new DesktopError('RESOURCE_EXHAUSTED'));
        const flight = serial(async () => {
          const index = await read('identities.enc') || { version: 1, identities: [] };
          if (index.version !== 1 || !Array.isArray(index.identities) || index.identities.length > 32
            || index.identities.some(item => !item || typeof item.accountHint !== 'string')
            || new Set(index.identities.map(item => item.accountHint)).size !== index.identities.length) throw new Error('Invalid identity index');
          let value = index.identities.find(item => item.accountHint === accountHint);
          if (!value) {
            if (index.identities.length >= 32) throw new Error('Identity limit reached');
            const keys = await new Promise((resolve, reject) => crypto.generateKeyPair('ec', { namedCurve: 'prime256v1',
              publicKeyEncoding: { type: 'spki', format: 'pem' }, privateKeyEncoding: { type: 'pkcs8', format: 'pem' } },
            (error, publicKey, privateKey) => error ? reject(error) : resolve({ publicKey, privateKey })));
            value = { accountHint, clientId: crypto.randomUUID(), ...keys };
            index.identities.push(value);
            await save('identities.enc', index);
          }
          if (!value || typeof value.clientId !== 'string' || !/^[A-Za-z0-9_-]{8,64}$/.test(value.clientId) || typeof value.privateKey !== 'string' || typeof value.publicKey !== 'string') throw new Error('Invalid identity');
          const privateKey = crypto.createPrivateKey(value.privateKey);
          if (privateKey.asymmetricKeyType !== 'ec' || privateKey.asymmetricKeyDetails?.namedCurve !== 'prime256v1'
            || crypto.createPublicKey(privateKey).export({ type: 'spki', format: 'pem' }) !== value.publicKey) throw new Error('Invalid identity');
          return Object.freeze({ clientId: value.clientId, publicKey: value.publicKey,
            sign: bytes => crypto.sign('sha256', bytes, privateKey).toString('base64') });
        });
        identityFlights.set(accountHint, flight);
        flight.catch(() => { if (identityFlights.get(accountHint) === flight) identityFlights.delete(accountHint); });
      }
      return identityFlights.get(accountHint);
    },
    load: () => serial(() => read('session.enc')),
    save: value => serial(() => save('session.enc', value)),
    remove: () => serial(() => fs.rm(path.join(root, 'session.enc'), { force: true })),
  });
}
module.exports = { createCredentialStore };
