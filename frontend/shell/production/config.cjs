'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const { DesktopError } = require('../runtime/public-error.cjs');
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const identifier = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
function requireConfig(condition) { if (!condition) throw new DesktopError('CONFIGURATION_REQUIRED'); }

function validateConfig(value) {
  requireConfig(plain(value) && value.version === 1 && Object.keys(value).every(key => ['version', 'consumerBaseUrl', 'connectivity'].includes(key)));
  let base;
  try { base = new URL(value.consumerBaseUrl); } catch { requireConfig(false); }
  requireConfig(typeof value.consumerBaseUrl === 'string' && base.protocol === 'https:' && !base.username && !base.password
    && !base.search && !base.hash && !/%|\\|\/\//.test(base.pathname) && /^\/[A-Za-z0-9/_-]*$/.test(base.pathname));
  const result = { version: 1, consumerBaseUrl: base.origin + base.pathname.replace(/\/+$/, '') };
  if (value.connectivity !== undefined) {
    const c = value.connectivity;
    requireConfig(plain(c) && Object.keys(c).length === 8 && ['applicationId', 'purpose', 'requestedScopes', 'gatewayHost', 'iceHost', 'sidecarPath', 'sidecarSha256', 'profile'].every(key => Object.hasOwn(c, key)));
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
  return Object.freeze(result);
}
async function loadConfig(filename) {
  if (!filename) return null;
  try {
    requireConfig(path.isAbsolute(filename));
    const file = await fs.open(filename, require('node:fs').constants.O_RDONLY | require('node:fs').constants.O_NOFOLLOW);
    try {
      const stat = await file.stat();
      requireConfig(stat.isFile() && stat.size <= 16384 && !(stat.mode & 0o022));
      const buffer = Buffer.alloc(16385);
      const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
      requireConfig(bytesRead <= 16384);
      return validateConfig(JSON.parse(buffer.subarray(0, bytesRead).toString('utf8')));
    } finally { await file.close(); }
  } catch { throw new DesktopError('CONFIGURATION_REQUIRED'); }
}
module.exports = { validateConfig, loadConfig };
