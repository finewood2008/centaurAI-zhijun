'use strict'

const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const { assertSafeSync } = require('../production/file-security.cjs')
const { X509Certificate, BasicConstraintsExtension, KeyUsagesExtension,
  KeyUsageFlags } = require('@peculiar/x509')

const ROOT_PIN = /^sha256\/[A-Za-z0-9+/]{43}=$/
const CERTIFICATE_PEM = /^-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+\n-----END CERTIFICATE-----\n?$/
const TEST_ONLY_SPKI_PINS = new Set([
  'sha256/wV85x0lyNLyUqfPgCBYYdtvPzOrJ+yI5dCmYwhSP2Y4=',
  'sha256/QBekLwfFW3qqbXc53ZkSLx1QFsvXmSk8KzATx3ZWvqg=',
  'sha256/vohmhweJ/e8NmJ/h9y+pEtqtWOHNMx+MMgpCWbJwHy0=',
])
const disabled = () => Object.freeze({
  contractVersion: '2.0.0',
  electronWebBluetoothDiscoveryV1: false,
  electronBleProvisioningV2: false,
  trustedRootSpkiPins: Object.freeze([]),
  trustedRootCertificatesPem: Object.freeze([]),
})

function validPin(value) {
  if (typeof value !== 'string' || !ROOT_PIN.test(value)) return false
  const decoded = Buffer.from(value.slice(7), 'base64')
  return decoded.length === 32 && decoded.toString('base64') === value.slice(7)
}

function validateRootCertificate(pem, pin, now = new Date()) {
  let certificate
  let native
  try {
    certificate = new X509Certificate(pem)
    native = new crypto.X509Certificate(pem)
  } catch { throw new Error('PROVISIONING_TRUST_CONFIG_INVALID') }
  const actualPin = `sha256/${crypto.createHash('sha256')
    .update(Buffer.from(certificate.publicKey.rawData)).digest('base64')}`
  const basic = certificate.extensions.find(item => item instanceof BasicConstraintsExtension)
  const usage = certificate.extensions.find(item => item instanceof KeyUsagesExtension)
  if (actualPin !== pin || TEST_ONLY_SPKI_PINS.has(actualPin)
      || certificate.signatureAlgorithm?.name !== 'RSASSA-PKCS1-v1_5'
      || certificate.signatureAlgorithm?.hash?.name !== 'SHA-256'
      || certificate.publicKey.algorithm?.name !== 'RSASSA-PKCS1-v1_5'
      || !Number.isInteger(certificate.publicKey.algorithm?.modulusLength)
      || certificate.publicKey.algorithm.modulusLength < 2048
      || certificate.subject !== certificate.issuer
      || !(basic instanceof BasicConstraintsExtension) || basic.ca !== true || basic.critical !== true
      || !(usage instanceof KeyUsagesExtension) || usage.critical !== true
      || !(usage.usages & KeyUsageFlags.keyCertSign)
      || now < certificate.notBefore || now > certificate.notAfter
      || native.ca !== true || !native.checkIssued(native) || !native.verify(native.publicKey)) {
    throw new Error('PROVISIONING_TRUST_CONFIG_INVALID')
  }
}

function loadProvisioningReleaseConfig(env = process.env) {
  const gate = env.ZHIJUN_PROVISIONING_V2_ENABLED
  if (gate === undefined || gate === '' || gate === '0') return disabled()
  if (gate !== '1') throw new Error('PROVISIONING_RELEASE_GATE_INVALID')
  const filename = env.ZHIJUN_PROVISIONING_TRUST_CONFIG
  if (typeof filename !== 'string' || !path.isAbsolute(filename)) {
    throw new Error('PROVISIONING_TRUST_CONFIG_REQUIRED')
  }
  const linkStat = fs.lstatSync(filename)
  if (!linkStat.isFile() || linkStat.isSymbolicLink()) throw new Error('PROVISIONING_TRUST_CONFIG_INVALID')
  const handle = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW)
  try {
    const stat = fs.fstatSync(handle)
    if (!stat.isFile() || stat.size < 1 || stat.size > 65536 || stat.ino !== linkStat.ino || stat.dev !== linkStat.dev) {
      throw new Error('PROVISIONING_TRUST_CONFIG_INVALID')
    }
    try { assertSafeSync(filename, stat) } catch { throw new Error('PROVISIONING_TRUST_CONFIG_INVALID') }
    const input = Buffer.alloc(stat.size + 1)
    const bytesRead = fs.readSync(handle, input, 0, input.length, 0)
    if (bytesRead !== stat.size) throw new Error('PROVISIONING_TRUST_CONFIG_INVALID')
    const value = JSON.parse(input.subarray(0, bytesRead).toString('utf8'))
    if (!value || typeof value !== 'object' || Array.isArray(value)
        || Object.keys(value).length !== 2
        || !Object.hasOwn(value, 'trustedRootSpkiPins')
        || !Object.hasOwn(value, 'trustedRootCertificatesPem')
        || !Array.isArray(value.trustedRootSpkiPins)
        || value.trustedRootSpkiPins.length < 1 || value.trustedRootSpkiPins.length > 4
        || new Set(value.trustedRootSpkiPins).size !== value.trustedRootSpkiPins.length
        || !value.trustedRootSpkiPins.every(validPin)
        || !Array.isArray(value.trustedRootCertificatesPem)
        || value.trustedRootCertificatesPem.length !== value.trustedRootSpkiPins.length
        || !value.trustedRootCertificatesPem.every(item => typeof item === 'string'
          && item.length <= 16384 && CERTIFICATE_PEM.test(item))) {
      throw new Error('PROVISIONING_TRUST_CONFIG_INVALID')
    }
    for (let index = 0; index < value.trustedRootSpkiPins.length; index += 1) {
      validateRootCertificate(value.trustedRootCertificatesPem[index], value.trustedRootSpkiPins[index])
    }
    return Object.freeze({
      contractVersion: '2.0.0',
      electronWebBluetoothDiscoveryV1: true,
      electronBleProvisioningV2: true,
      trustedRootSpkiPins: Object.freeze([...value.trustedRootSpkiPins]),
      trustedRootCertificatesPem: Object.freeze([...value.trustedRootCertificatesPem]),
    })
  } finally {
    fs.closeSync(handle)
  }
}

module.exports = { TEST_ONLY_SPKI_PINS, loadProvisioningReleaseConfig, validateRootCertificate }
