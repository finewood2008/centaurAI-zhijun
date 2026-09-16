'use strict';
const fs = require('node:fs');
const path = require('node:path');
const asar = require('@electron/asar');
const normalizeAsarPath = name => name.replaceAll('\\', '/').replace(/^\//, '');

// Release verification is performed against the checked-out, tested source and lockfile.
const dependencies = [
  '@nexusaos/connectivity-contracts', '@nexusaos/connectivity-electron', '@nexusaos/consumer-contracts',
  '@nexusaos/device-discovery-contracts', '@nexusaos/device-discovery-electron',
  '@nexusaos/local-provisioning-contracts', '@nexusaos/local-provisioning-core', '@nexusaos/local-provisioning-electron-ble',
  '@noble/hashes', '@peculiar/asn1-cms', '@peculiar/asn1-csr', '@peculiar/asn1-ecc', '@peculiar/asn1-pfx',
  '@peculiar/asn1-pkcs8', '@peculiar/asn1-pkcs9', '@peculiar/asn1-rsa', '@peculiar/asn1-schema',
  '@peculiar/asn1-x509', '@peculiar/asn1-x509-attr', '@peculiar/utils', '@peculiar/x509',
  'asn1js', 'pvtsutils', 'pvutils', 'reflect-metadata', 'tslib', 'tsyringe', 'tsyringe/node_modules/tslib',
];
function verifyPackagedContent(filename, shell = path.resolve(__dirname, '..')) {
  const lock = JSON.parse(fs.readFileSync(path.join(shell, 'package-lock.json')));
  const metadata = JSON.parse(fs.readFileSync(path.join(shell, 'package.json')));
  const files = ['app-icon.cjs', 'assets/centaur.png', 'main.js', 'preload.cjs', 'security.cjs',
    'provisioning-window.cjs', 'provisioning-broker.cjs', 'provisioning/preload.cjs',
    'provisioning/renderer.js', 'provisioning/picker.cjs', 'provisioning/setup.css', 'provisioning/setup.html'];
  for (const dir of ['production', 'runtime']) {
    files.push(...fs.readdirSync(path.join(shell, dir)).filter(name => name.endsWith('.cjs')).map(name => `${dir}/${name}`));
  }
  const entries = new Set(asar.listPackage(filename).map(normalizeAsarPath));
  const permitted = new Set(['package.json', ...files]);
  for (const name of [...permitted]) {
    let parent = path.posix.dirname(name);
    while (parent !== '.') { permitted.add(parent); parent = path.posix.dirname(parent); }
  }
  permitted.add('node_modules');
  for (const name of dependencies) {
    let parent = `node_modules/${name}`;
    while (parent !== '.') { permitted.add(parent); parent = path.posix.dirname(parent); }
    const expected = lock.packages[`node_modules/${name}`]?.version;
    const actual = JSON.parse(asar.extractFile(filename, `node_modules/${name}/package.json`));
    if (!expected || actual.version !== expected) throw new Error('ASAR_DEPENDENCY_VERSION_INVALID');
  }
  for (const entry of entries) {
    if (entry.startsWith('node_modules/') && /\/(?:test|tests|fixtures|vectors)(?:\/|$)|\.(?:pem|key|crt|cer|der|p12|pfx)$/i.test(entry)) {
      throw new Error('ASAR_TEST_OR_KEY_MATERIAL');
    }
    if (!permitted.has(entry) && !dependencies.some(name => entry.startsWith(`node_modules/${name}/`))) throw new Error('ASAR_UNEXPECTED_ENTRY');
    if (entry.startsWith('node_modules/') && entry.endsWith('/package.json')
        && !dependencies.some(name => entry === `node_modules/${name}/package.json`)) throw new Error('ASAR_UNDECLARED_DEPENDENCY');
  }
  for (const name of files) {
    if (!asar.extractFile(filename, name).equals(fs.readFileSync(path.join(shell, name)))) throw new Error('ASAR_SOURCE_MISMATCH');
  }
  const packaged = JSON.parse(asar.extractFile(filename, 'package.json'));
  if (packaged.version !== metadata.version || packaged.name !== metadata.name || packaged.main !== 'main.js'
      || packaged.zhijunProvisioningTestBuild === true) throw new Error('ASAR_METADATA_INVALID');
  return { asarEntries: entries.size, version: packaged.version };
}
module.exports = { verifyPackagedContent, normalizeAsarPath, dependencies: Object.freeze(dependencies) };
