'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const { DesktopError } = require('../runtime/public-error.cjs');
const { assertSafe } = require('./file-security.cjs');
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const identifier = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
const rootPin = value => {
  if (typeof value !== 'string' || !/^sha256\/[A-Za-z0-9+/]{43}=$/.test(value)) return false;
  try { const bytes = Buffer.from(value.slice(7), 'base64'); return bytes.length === 32 && bytes.toString('base64') === value.slice(7); } catch { return false; }
};
const certificatePem = value => typeof value === 'string' && value.length <= 16384
  && /^-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+\n-----END CERTIFICATE-----\n?$/.test(value);
const testOnlyProvisioningPins = new Set([
  'sha256/wV85x0lyNLyUqfPgCBYYdtvPzOrJ+yI5dCmYwhSP2Y4=',
  'sha256/QBekLwfFW3qqbXc53ZkSLx1QFsvXmSk8KzATx3ZWvqg=',
  'sha256/vohmhweJ/e8NmJ/h9y+pEtqtWOHNMx+MMgpCWbJwHy0=',
]);
function requireConfig(condition) { if (!condition) throw new DesktopError('CONFIGURATION_REQUIRED'); }

function validateConfig(value) {
  requireConfig(plain(value) && value.version === 1 && Object.keys(value).every(key => ['version', 'consumerBaseUrl', 'connectivity', 'provisioning'].includes(key)));
  let base;
  try { base = new URL(value.consumerBaseUrl); } catch { requireConfig(false); }
  requireConfig(typeof value.consumerBaseUrl === 'string' && base.protocol === 'https:' && !base.username && !base.password
    && !base.search && !base.hash && !/%|\\|\/\//.test(base.pathname) && /^\/[A-Za-z0-9/_-]*$/.test(base.pathname));
  const result = { version: 1, consumerBaseUrl: base.origin + base.pathname.replace(/\/+$/, '') };
  if (value.connectivity !== undefined) {
    const c = value.connectivity;
    const required = ['applicationId', 'purpose', 'requestedScopes', 'gatewayHost', 'iceHost', 'sidecarPath', 'sidecarSha256', 'profile'];
    requireConfig(plain(c) && required.every(key => Object.hasOwn(c, key))
      && Object.keys(c).every(key => required.includes(key) || key === 'directConnectTimeoutMs'));
    if (Object.hasOwn(c, 'directConnectTimeoutMs')) requireConfig(Number.isInteger(c.directConnectTimeoutMs)
      && c.directConnectTimeoutMs >= 2000 && c.directConnectTimeoutMs <= 8000);
    requireConfig(identifier(c.applicationId) && identifier(c.purpose) && c.profile === 'SOVEREIGN_DIRECT_ONLY');
    const purposes = { 'mindos-person-data-pc': 'person-data.read', 'zhijun-desktop': 'zhijun.workspace' };
    requireConfig(Object.hasOwn(purposes, c.applicationId) && c.purpose === purposes[c.applicationId]);
    requireConfig(Array.isArray(c.requestedScopes) && c.requestedScopes.length === 1 && c.requestedScopes[0] === 'remote.p2p' && c.requestedScopes.length <= 32
      && c.requestedScopes.every(identifier) && new Set(c.requestedScopes).size === c.requestedScopes.length);
    for (const key of ['gatewayHost', 'iceHost']) requireConfig(typeof c[key] === 'string' && /^(?=.{1,253}$)[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*$/.test(c[key]));
    requireConfig(typeof c.sidecarPath === 'string' && path.isAbsolute(c.sidecarPath) && !/[\r\n\0]/.test(c.sidecarPath)
      && typeof c.sidecarSha256 === 'string' && /^[a-f0-9]{64}$/.test(c.sidecarSha256));
    result.connectivity = Object.freeze({ ...c, requestedScopes: Object.freeze([...c.requestedScopes]) });
  }
  if (value.provisioning !== undefined) {
    const p = value.provisioning;
    requireConfig(plain(p) && Object.keys(p).length === 5
      && ['contractVersion', 'electronWebBluetoothDiscoveryV1', 'electronBleProvisioningV2',
        'trustedRootSpkiPins', 'trustedRootCertificatesPem'].every(key => Object.hasOwn(p, key)));
    requireConfig(p.contractVersion === '2.0.0'
      && typeof p.electronWebBluetoothDiscoveryV1 === 'boolean'
      && typeof p.electronBleProvisioningV2 === 'boolean'
      && Array.isArray(p.trustedRootSpkiPins) && p.trustedRootSpkiPins.length <= 4
      && p.trustedRootSpkiPins.every(pin => rootPin(pin) && !testOnlyProvisioningPins.has(pin))
      && new Set(p.trustedRootSpkiPins).size === p.trustedRootSpkiPins.length
      && Array.isArray(p.trustedRootCertificatesPem) && p.trustedRootCertificatesPem.length <= 4
      && p.trustedRootCertificatesPem.every(certificatePem)
      && (!p.electronBleProvisioningV2 || (p.electronWebBluetoothDiscoveryV1
        && p.trustedRootSpkiPins.length > 0 && p.trustedRootCertificatesPem.length === p.trustedRootSpkiPins.length)));
    result.provisioning = Object.freeze({ ...p,
      trustedRootSpkiPins: Object.freeze([...p.trustedRootSpkiPins]),
      trustedRootCertificatesPem: Object.freeze([...p.trustedRootCertificatesPem]) });
  }
  return Object.freeze(result);
}
function resolvePackagedSidecar(value, resourceRoot) {
  if (resourceRoot === undefined) return value;
  requireConfig(typeof resourceRoot === 'string' && path.isAbsolute(resourceRoot));
  const sidecar = value?.connectivity?.sidecarPath;
  requireConfig(typeof sidecar === 'string' && sidecar.length <= 512 && !path.isAbsolute(sidecar)
    && !/[\\\r\n\0]/.test(sidecar));
  const parts = sidecar.split('/');
  requireConfig(parts.length > 1 && parts.every(part => part && part !== '.' && part !== '..'));
  const resolved = path.resolve(resourceRoot, ...parts);
  requireConfig(resolved.startsWith(`${path.resolve(resourceRoot)}${path.sep}`));
  return { ...value, connectivity: { ...value.connectivity, sidecarPath: resolved } };
}

async function loadConfig(filename, options = undefined) {
  if (!filename) return null;
  try {
    requireConfig(path.isAbsolute(filename));
    const linkStat = await fs.lstat(filename);
    requireConfig(linkStat.isFile() && !linkStat.isSymbolicLink());
    const file = await fs.open(filename, require('node:fs').constants.O_RDONLY | require('node:fs').constants.O_NOFOLLOW);
    try {
      const stat = await file.stat();
      requireConfig(stat.isFile() && stat.size <= 16384);
      requireConfig(stat.ino === linkStat.ino && stat.dev === linkStat.dev);
      await assertSafe(filename, stat);
      const buffer = Buffer.alloc(16385);
      const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
      requireConfig(bytesRead <= 16384);
      const value = JSON.parse(buffer.subarray(0, bytesRead).toString('utf8'));
      return validateConfig(resolvePackagedSidecar(value, options?.resourceRoot));
    } finally { await file.close(); }
  } catch { throw new DesktopError('CONFIGURATION_REQUIRED'); }
}
module.exports = { validateConfig, loadConfig, resolvePackagedSidecar };
