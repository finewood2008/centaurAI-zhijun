'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const { DesktopError } = require('../runtime/public-error.cjs');
const { assertSafe, protectPrivate } = require('./file-security.cjs');
const RESUME_STATES = new Set(['waitingAppProof', 'waitingDeviceProof', 'ownershipCommitted', 'projectionPending',
  'deviceAckPending', 'completed', 'cancelled', 'expired', 'attentionRequired', 'failed']);

function validateResume(value) {
  const required = ['accountId', 'deviceId', 'pairingSessionId', 'claimState', 'expiresAt', 'updatedAt'];
  const optional = ['taskId', 'cancelIntent'];
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || required.some(key => !Object.hasOwn(value, key))
      || Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))
      || typeof value.accountId !== 'string' || !/^[A-Za-z0-9._:-]{1,128}$/.test(value.accountId)
      || typeof value.deviceId !== 'string' || !/^[A-Za-z0-9._-]{1,36}$/.test(value.deviceId)
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value.pairingSessionId)
      || !RESUME_STATES.has(value.claimState)
      || ![value.expiresAt, value.updatedAt].every(item => typeof item === 'string' && Number.isFinite(Date.parse(item)))
      || (value.taskId !== undefined && (typeof value.taskId !== 'string' || !/^[A-Za-z0-9._:-]{1,128}$/.test(value.taskId)))) {
    throw new Error('Invalid pairing resume record');
  }
  if (value.cancelIntent !== undefined) {
    const intent = value.cancelIntent;
    if (!intent || typeof intent !== 'object' || Array.isArray(intent)
        || Object.keys(intent).sort().join(',') !== 'bodySha256,createdAt,idempotencyKey'
        || !/^[0-9a-f-]{36}$/.test(intent.idempotencyKey) || !/^[a-f0-9]{64}$/.test(intent.bodySha256)
        || typeof intent.createdAt !== 'string' || !Number.isFinite(Date.parse(intent.createdAt))) {
      throw new Error('Invalid pairing cancel intent');
    }
  }
  return { ...value, ...(value.cancelIntent ? { cancelIntent: { ...value.cancelIntent } } : {}) };
}

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
    return next.catch((error) => {
      if (error instanceof DesktopError) throw error;
      throw new DesktopError('SECURE_STORAGE_UNAVAILABLE');
    });
  }
  async function ensureRoot() {
    await fs.mkdir(root, { recursive: true, mode: 0o700 });
    if ((await fs.lstat(root)).isSymbolicLink()) throw new Error('Invalid storage directory');
    await protectPrivate(root, 0o700);
  }
  async function read(name) {
    secure();
    const filename = path.join(root, name);
    try {
      const stat = await fs.lstat(filename);
      if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('Invalid encrypted record');
      await assertSafe(filename, stat);
    } catch (error) { if (error.code === 'ENOENT') return undefined; throw error; }
    let file;
    try { file = await fs.open(filename, require('node:fs').constants.O_RDONLY | require('node:fs').constants.O_NOFOLLOW); }
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
  async function save(name, value, guard = () => true) {
    secure(); await ensureRoot();
    if (!guard()) throw new DesktopError('STALE_GENERATION');
    const bytes = safeStorage.encryptString(JSON.stringify(value));
    if (bytes.length > 65536) throw new Error('Invalid encrypted record');
    if (!guard()) throw new DesktopError('STALE_GENERATION');
    const temporary = path.join(root, `.${crypto.randomUUID()}.tmp`);
    try {
      const file = await fs.open(temporary, 'wx', 0o600);
      try { await protectPrivate(temporary, 0o600); await file.writeFile(bytes); await file.sync(); } finally { await file.close(); }
      if (!guard()) throw new DesktopError('STALE_GENERATION');
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
    loadRememberedLogin: () => serial(() => read('remembered-login.enc')),
    saveRememberedLogin: (value, guard) => serial(() => save('remembered-login.enc', value, guard)),
    loadPairingResumes: accountId => serial(async () => {
      if (typeof accountId !== 'string' || !/^[A-Za-z0-9._:-]{1,128}$/.test(accountId)) throw new DesktopError('INVALID_REQUEST');
      const index = await read('pairing-resumes.enc') || { version: 1, records: [] };
      if (index.version !== 1 || !Array.isArray(index.records) || index.records.length > 32) throw new Error('Invalid pairing resume index');
      return index.records.map(validateResume).filter(record => record.accountId === accountId);
    }),
    savePairingResume: value => serial(async () => {
      const record = validateResume(value);
      const index = await read('pairing-resumes.enc') || { version: 1, records: [] };
      if (index.version !== 1 || !Array.isArray(index.records) || index.records.length > 32) throw new Error('Invalid pairing resume index');
      const records = index.records.map(validateResume);
      const at = records.findIndex(item => item.accountId === record.accountId && item.pairingSessionId === record.pairingSessionId);
      if (at >= 0) records[at] = record;
      else {
        if (records.length >= 32) throw new Error('Pairing resume limit reached');
        records.push(record);
      }
      await save('pairing-resumes.enc', { version: 1, records });
    }),
    deletePairingResume: (accountId, pairingSessionId) => serial(async () => {
      if (typeof accountId !== 'string' || !/^[A-Za-z0-9._:-]{1,128}$/.test(accountId)
          || !/^[0-9a-f-]{36}$/.test(pairingSessionId)) throw new DesktopError('INVALID_REQUEST');
      const index = await read('pairing-resumes.enc');
      if (!index) return;
      if (index.version !== 1 || !Array.isArray(index.records) || index.records.length > 32) throw new Error('Invalid pairing resume index');
      const records = index.records.map(validateResume)
        .filter(item => item.accountId !== accountId || item.pairingSessionId !== pairingSessionId);
      await save('pairing-resumes.enc', { version: 1, records });
    }),
  });
}
module.exports = { createCredentialStore };
