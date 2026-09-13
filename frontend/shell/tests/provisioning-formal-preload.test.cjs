'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const source = fs.readFileSync(path.join(__dirname, '..', 'provisioning', 'preload-formal.mjs'), 'utf8')
const productionBuilder = fs.readFileSync(path.join(__dirname, '..', 'electron-builder.yml'), 'utf8')
const testBuilder = fs.readFileSync(path.join(__dirname, '..', 'electron-builder.test.yml'), 'utf8')

test('formal preload owns Web Bluetooth only and delegates claim state to Main', () => {
  assert.match(source, /@nexusaos\/device-discovery-electron\/main/)
  assert.match(source, /DISCOVERY_PICKER_CHANNELS/)
  assert.match(source, /DISCOVERY_PICKER_CHANNELS\.settled/)
  assert.match(source, /createElectronBluetoothSelectionEndpoint/)
  assert.match(source, /createElectronGattTransportFactory/)
  assert.match(source, /createElectronGattTransportEndpoint/)
  assert.match(source, /CLAIM_INVOKE_CHANNEL/)
  assert.match(source, /TRANSPORT_COMMAND_CHANNEL/)
  assert.match(source, /contextBridge\.exposeInMainWorld\(['"]desktopProvisioning['"]/)
  assert.doesNotMatch(source, /@nexusaos\/device-provisioning-electron|BLUETOOTH_PICKER_CHANNELS/)
  assert.doesNotMatch(source, /createClaimCoordinator|createNodeProvisioningCryptoProvider|consumerBaseUrl|fetch\(/)
  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB|console\./)
})

test('formal preload exposes no v1 provisioning operation and scrubs password bytes', () => {
  assert.match(source, /testBuild:\s*false/)
  assert.match(source, /legacyAllowed:\s*false/)
  assert.match(source, /encoded\?\.fill\(0\)/)
  assert.doesNotMatch(source, /createProvisioningSession|provisionWifi|scanWifiNetworks|allowLegacyPlaintextProvisioning/)
})

test('production package excludes v1 SDKs while the marked test flavor retains them', () => {
  assert.match(productionBuilder, /provisioning-broker\.cjs/)
  assert.match(productionBuilder, /!provisioning\/preload\.mjs/)
  assert.match(productionBuilder, /!node_modules\/@nexusaos\/device-provisioning-electron/)
  assert.match(productionBuilder, /!node_modules\/@nexusaos\/device-provisioning-uni/)
  assert.match(testBuilder, /provisioning-broker\.cjs/)
  assert.doesNotMatch(testBuilder, /!provisioning\/preload\.mjs/)
  assert.match(testBuilder, /zhijunProvisioningTestBuild:\s*true/)
})
