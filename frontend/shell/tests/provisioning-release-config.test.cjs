'use strict'

const assert = require('node:assert/strict')
const fs = require('node:fs')
const crypto = require('node:crypto')
const os = require('node:os')
const path = require('node:path')
const { execFileSync } = require('node:child_process')
const test = require('node:test')
const { TEST_ONLY_SPKI_PINS, loadProvisioningReleaseConfig } = require('../scripts/provisioning-release-config.cjs')

test('formal provisioning release stays fail-closed unless explicitly enabled', () => {
  assert.deepEqual(loadProvisioningReleaseConfig({}), {
    contractVersion: '2.0.0', electronWebBluetoothDiscoveryV1: false,
    electronBleProvisioningV2: false, trustedRootSpkiPins: [], trustedRootCertificatesPem: [],
  })
  assert.throws(() => loadProvisioningReleaseConfig({ ZHIJUN_PROVISIONING_V2_ENABLED: 'true' }),
    /PROVISIONING_RELEASE_GATE_INVALID/)
  assert.throws(() => loadProvisioningReleaseConfig({ ZHIJUN_PROVISIONING_V2_ENABLED: '1' }),
    /PROVISIONING_TRUST_CONFIG_REQUIRED/)
})

test('formal provisioning release accepts only a locked exact trust bundle', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'zhijun-provisioning-roots-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const filename = path.join(directory, 'roots.json')
  const certificateFile = path.join(directory, 'ephemeral-root.pem')
  const keyFile = path.join(directory, 'ephemeral-root.key')
  execFileSync('/usr/bin/openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-sha256', '-nodes', '-days', '1',
    '-subj', '/CN=Ephemeral-Zhijun-Root', '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0',
    '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-keyout', keyFile, '-out', certificateFile],
  { stdio: 'ignore', timeout: 15000 })
  const certificate = fs.readFileSync(certificateFile, 'utf8')
  const parsed = new crypto.X509Certificate(certificate)
  const pin = `sha256/${crypto.createHash('sha256').update(parsed.publicKey.export({ type: 'spki', format: 'der' })).digest('base64')}`
  fs.writeFileSync(filename, JSON.stringify({ trustedRootSpkiPins: [pin],
    trustedRootCertificatesPem: [certificate] }), { mode: 0o600 })
  assert.deepEqual(loadProvisioningReleaseConfig({ ZHIJUN_PROVISIONING_V2_ENABLED: '1',
    ZHIJUN_PROVISIONING_TRUST_CONFIG: filename }), {
    contractVersion: '2.0.0', electronWebBluetoothDiscoveryV1: true,
    electronBleProvisioningV2: true, trustedRootSpkiPins: [pin],
    trustedRootCertificatesPem: [certificate],
  })
  fs.chmodSync(filename, 0o622)
  assert.throws(() => loadProvisioningReleaseConfig({ ZHIJUN_PROVISIONING_V2_ENABLED: '1',
    ZHIJUN_PROVISIONING_TRUST_CONFIG: filename }), /PROVISIONING_TRUST_CONFIG_INVALID/)
  assert.equal(TEST_ONLY_SPKI_PINS.has('sha256/wV85x0lyNLyUqfPgCBYYdtvPzOrJ+yI5dCmYwhSP2Y4='), true)
  assert.equal(TEST_ONLY_SPKI_PINS.has('sha256/vohmhweJ/e8NmJ/h9y+pEtqtWOHNMx+MMgpCWbJwHy0='), true)
})
