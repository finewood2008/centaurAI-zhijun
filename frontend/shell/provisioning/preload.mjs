import { contextBridge, ipcRenderer } from 'electron'
import { createElectronGattProvisioningAdapter, createProvisioningSession } from '@nexusaos/device-provisioning-electron'
import { BLUETOOTH_PICKER_CHANNELS } from '@nexusaos/device-provisioning-electron/main'

const legacyAllowed = process.argv.includes('--zhijun-legacy-plaintext-provisioning')
const testBuild = process.argv.includes('--zhijun-provisioning-test-build')
const candidatesListeners = new Set()
let adapter
let provisioningSession
let scanPending = false
let activeScan
let legacyArmed = false

ipcRenderer.on(BLUETOOTH_PICKER_CHANNELS.candidates, (_event, devices) => {
  const projected = Array.isArray(devices) ? devices.map(device => ({
    transportId: typeof device?.transportId === 'string' ? device.transportId : '',
    name: typeof device?.name === 'string' ? device.name : '未命名 AI 盒子',
  })).filter(device => device.transportId) : []
  for (const listener of [...candidatesListeners]) listener(projected)
})

function requireSession() {
  if (!provisioningSession) throw new Error('PROVISIONING_NOT_STARTED')
  return provisioningSession
}

const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))
async function waitForNetworkTerminal(session) {
  const deadline = Date.now() + 90000
  for (;;) {
    const status = await session.refreshStatus()
    if (['connected', 'failed'].includes(status.state)) return status
    if (Date.now() >= deadline) throw new Error('PROVISIONING_STATUS_TIMEOUT')
    await wait(1000)
  }
}

async function release() {
  const currentSession = provisioningSession
  const currentAdapter = adapter
  const currentScan = activeScan
  provisioningSession = undefined
  adapter = undefined
  activeScan = undefined
  legacyArmed = false
  try { await currentSession?.disconnect() } finally {
    await currentAdapter?.dispose()
    await currentScan?.catch(() => {})
  }
}

const api = Object.freeze({
  available: Boolean(globalThis.navigator?.bluetooth?.requestDevice),
  legacyAllowed,
  testBuild,
  async scan(requestLegacy = false) {
    if (scanPending) throw new Error('PROVISIONING_SCAN_IN_PROGRESS')
    if (!globalThis.navigator?.userActivation?.isActive) throw new Error('PROVISIONING_USER_GESTURE_REQUIRED')
    if (!globalThis.navigator?.bluetooth?.requestDevice) throw new Error('PROVISIONING_BLUETOOTH_UNAVAILABLE')
    legacyArmed = legacyAllowed && requestLegacy === true
    const previousSession = provisioningSession
    const previousAdapter = adapter
    adapter = createElectronGattProvisioningAdapter({
      bluetooth: globalThis.navigator.bluetooth,
      cancelSelection: () => ipcRenderer.invoke(BLUETOOTH_PICKER_CHANNELS.cancel),
      allowLegacyPlaintextProvisioning: legacyArmed,
    })
    provisioningSession = createProvisioningSession(adapter, {
      // The legacy test path must identify itself as schema v1. A v2 schema
      // number and checksum alone would not make plaintext credentials secure.
      protocolVersion: legacyArmed ? 1 : 2,
      scanTimeoutMs: 30000,
    })
    void previousSession?.disconnect().catch(() => {})
    void previousAdapter?.dispose().catch(() => {})
    scanPending = true
    const pending = provisioningSession.scan()
    activeScan = pending
    try { return await pending } finally {
      scanPending = false
      if (activeScan === pending) activeScan = undefined
    }
  },
  select(transportId) {
    if (typeof transportId !== 'string' || transportId.length < 1 || transportId.length > 256) {
      throw new Error('PROVISIONING_INVALID_DEVICE')
    }
    return ipcRenderer.invoke(BLUETOOTH_PICKER_CHANNELS.select, transportId)
  },
  async cancelScan() {
    const pending = activeScan
    await adapter?.stopScan()
    await pending?.catch(() => {})
  },
  onCandidates(listener) {
    if (typeof listener !== 'function') throw new TypeError('Expected a candidate listener')
    candidatesListeners.add(listener)
    let active = true
    return () => { if (active) { active = false; candidatesListeners.delete(listener) } }
  },
  connect(transportId) {
    if (typeof transportId !== 'string' || transportId.length < 1 || transportId.length > 256) {
      throw new Error('PROVISIONING_INVALID_DEVICE')
    }
    return requireSession().connect(transportId)
  },
  status() { return requireSession().refreshStatus() },
  networks() { return requireSession().scanWifiNetworks() },
  async provision(credentials, legacyConfirmation = '') {
    const value = credentials && typeof credentials === 'object' && !Array.isArray(credentials) ? credentials : null
    if (!value || !['open', 'wpa-personal'].includes(value.security)
      || typeof value.ssid !== 'string' || value.ssid.length < 1 || new TextEncoder().encode(value.ssid).byteLength > 32
      || typeof value.password !== 'string' || value.password.length > 128
      || (value.security === 'wpa-personal' && (value.password.length < 8 || value.password.length > 63))
      || (value.security === 'open' && value.password !== '')) throw new Error('PROVISIONING_INVALID_WIFI_CREDENTIALS')
    if (legacyArmed && (!legacyAllowed || legacyConfirmation !== 'CONFIRM_LEGACY_PLAINTEXT_WIFI')) {
      throw new Error('PROVISIONING_LEGACY_CONFIRMATION_REQUIRED')
    }
    const session = requireSession()
    const transient = { ssid: value.ssid, password: value.password, security: value.security }
    try {
      await session.startSession()
      await session.provisionWifi(transient)
      return await waitForNetworkTerminal(session)
    } finally {
      transient.password = ''
    }
  },
  disconnect: release,
})

globalThis.addEventListener('beforeunload', () => { void release() })
contextBridge.exposeInMainWorld('desktopProvisioning', api)
