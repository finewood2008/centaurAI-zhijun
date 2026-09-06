'use strict';
// Invoke with Node. It starts this same probe under the pinned Electron runtime.
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const prefix = 'ZHIJUN_OS_ACCEPTANCE ';
async function probe() {
  const { app, safeStorage } = require('electron');
  const { createCredentialStore } = require('../production/credential-store.cjs');
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-os-acceptance-'));
  app.setPath('userData', directory);
  app.setName('知君');
  let report = { checkedAt: new Date().toISOString(), platform: process.platform, arch: process.arch,
    os: os.release(), electron: process.versions.electron, result: 'failed', scope: 'real-os-synthetic-records',
    realLoginValidated: false, realDeviceValidated: false, checks: [] };
  try {
    await app.whenReady();
    const options = { directory, safeStorage, consumerBaseUrl: 'https://storage-probe.invalid' };
    const store = createCredentialStore(options);
    const identity = await store.identity('acceptance');
    const message = crypto.randomBytes(32);
    if (!crypto.verify('sha256', message, identity.publicKey, Buffer.from(identity.sign(message), 'base64'))) throw new Error('SIGNATURE_ROUNDTRIP_FAILED');
    report.checks.push('real-safeStorage-encrypt-and-sign');
    const restoredStore = createCredentialStore(options);
    const restored = await restoredStore.identity('acceptance');
    if (restored.clientId !== identity.clientId || restored.publicKey !== identity.publicKey) throw new Error('IDENTITY_RELOAD_FAILED');
    report.checks.push('encrypted-identity-reload');
    const record = { accessToken: `acceptance-${crypto.randomUUID()}`, refreshToken: `acceptance-${crypto.randomUUID()}` };
    await store.save(record);
    if (JSON.stringify(await restoredStore.load()) !== JSON.stringify(record)) throw new Error('SESSION_RELOAD_FAILED');
    const root = path.join(directory, 'consumer', crypto.createHash('sha256').update(options.consumerBaseUrl).digest('hex'));
    for (const filename of ['identities.enc', 'session.enc']) {
      const file = path.join(root, filename);
      const bytes = await fs.readFile(file);
      if (bytes.includes(Buffer.from(record.accessToken)) || bytes.includes(Buffer.from(record.refreshToken))
        || bytes.includes(Buffer.from('PRIVATE KEY')) || bytes.includes(Buffer.from('acceptance'))) throw new Error('PLAINTEXT_FOUND');
      if (process.platform !== 'win32' && ((await fs.stat(file)).mode & 0o777) !== 0o600) throw new Error('FILE_PERMISSION_INVALID');
    }
    report.checks.push('encrypted-session-reload-no-plaintext');
    await store.remove();
    if (await restoredStore.load() !== undefined) throw new Error('SESSION_REMOVE_FAILED');
    report.checks.push('local-session-removal');
    report.result = 'passed';
  } catch (error) {
    report.code = /^[A-Z_]+$/.test(error.code || error.message) ? error.code || error.message : 'OS_STORAGE_CHECK_FAILED';
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
    console.log(prefix + JSON.stringify(report));
    app.exit(report.result === 'passed' ? 0 : 1);
  }
}
if (process.versions.electron) {
  probe().catch(() => { console.error(prefix + JSON.stringify({ result: 'failed', code: 'OS_PROBE_SETUP_FAILED' })); process.exit(1); });
} else {
  const { spawn } = require('node:child_process');
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  const child = spawn(require('electron'), [__filename], { env, stdio: ['ignore', 'pipe', 'ignore'] });
  let output = ''; let valid = false;
  const deadline = setTimeout(() => child.kill('SIGKILL'), 30000);
  child.stdout.on('data', data => {
    output += data.toString();
    if (output.length > 16384) child.kill('SIGKILL');
  });
  child.on('error', () => { clearTimeout(deadline); process.exitCode = 1; });
  child.on('close', code => {
    clearTimeout(deadline);
    for (const line of output.split('\n')) {
      if (line.startsWith(prefix)) { console.log(line.slice(prefix.length)); valid = true; }
    }
    if (!valid) console.log(JSON.stringify({ result: 'failed', code: 'OS_PROBE_TIMED_OUT_OR_EXITED' }));
    process.exitCode = valid && code === 0 ? 0 : 1;
  });
}
