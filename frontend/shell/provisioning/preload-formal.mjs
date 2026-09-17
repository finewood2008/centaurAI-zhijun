import { contextBridge, ipcRenderer } from 'electron'
import { DISCOVERY_PICKER_CHANNELS } from '@nexusaos/device-discovery-electron/main'
import {
  createElectronBluetoothSelectionEndpoint,
  createElectronGattTransportEndpoint,
  createElectronGattTransportFactory,
} from '@nexusaos/local-provisioning-electron-ble'

const CLAIM_INVOKE_CHANNEL = 'zhijun:provisioning:claim:v1'
const CLAIM_SNAPSHOT_CHANNEL = 'zhijun:provisioning:snapshot:v1'
const TRANSPORT_COMMAND_CHANNEL = 'zhijun:provisioning:transport-command:v1'
const TRANSPORT_RESULT_CHANNEL = 'zhijun:provisioning:transport-result:v1'
const IPC_VERSION = 1
const FLOW_ARGUMENT = '--zhijun-provisioning-flow='
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const TRANSPORT_ERRORS = new Set(['LOCAL_CONNECT_TIMEOUT', 'LOCAL_CONNECT_UNSUPPORTED_BOND_FLOW',
  'LOCAL_SELECTION_LEASE_INVALID', 'LOCAL_DEVICE_INFO_INVALID', 'LOCAL_DEVICE_INCOMPATIBLE',
  'GATT_SERVICE_NOT_FOUND', 'GATT_CHARACTERISTIC_MISMATCH', 'GATT_FRAGMENT_PROTOCOL_ERROR',
  'GATT_FRAGMENT_INCOMPLETE', 'LOCAL_TRANSPORT_DISCONNECTED', 'LOCAL_USER_CANCELLED'])
const CLAIM_ERRORS = new Set(['CLAIM_INVALID_STATE', 'CLAIM_OPERATION_IN_PROGRESS', 'CLAIM_INPUT_MISMATCH',
  'AUTH_REQUIRED', 'AUTH_SESSION_EXPIRED', 'CLIENT_REVOKED', 'CLIENT_KEY_INVALID', 'CLIENT_KEY_MISMATCH',
  'ACCOUNT_NOT_ACTIVE', 'CLIENT_UPGRADE_REQUIRED', 'REQUEST_SIGNATURE_INVALID', 'REQUEST_EXPIRED',
  'NONCE_REPLAYED', 'IDEMPOTENCY_CONFLICT', 'DEVICE_IDENTITY_MISMATCH', 'DEVICE_NOT_ENROLLED',
  'DEVICE_DISABLED', 'DEVICE_NOT_CLAIMABLE', 'DEVICE_ALREADY_OWNED', 'PAIRING_NOT_FOUND',
  'PAIRING_EXPIRED', 'PAIRING_CANCELLED', 'PAIRING_MISMATCH', 'PAIRING_SUPERSEDED',
  'SETUP_WINDOW_CLOSED', 'ACCESS_PROJECTION_PENDING', 'ACCESS_PROJECTION_FAILED', 'DEVICE_ACK_TIMEOUT',
  'DEVICE_OFFLINE', 'FEATURE_NOT_AVAILABLE', 'SERVICE_TEMPORARILY_UNAVAILABLE',
  'VERIFICATION_CODE_MISMATCH', 'UNSUPPORTED_NETWORK_SECURITY', 'HELLO_CONTEXT_EXPIRED',
  'REQUESTED_OPS_MISMATCH', 'PROTOCOL_CHANGED', 'CLAIM_UNKNOWN_ERROR'])
const LOCAL_UI_ERRORS = new Set(['PROVISIONING_USER_GESTURE_REQUIRED', 'PROVISIONING_BLUETOOTH_UNAVAILABLE',
  'PROVISIONING_SCAN_IN_PROGRESS', 'PROVISIONING_PHYSICAL_CODE_INVALID', 'PROVISIONING_NOT_STARTED',
  'DISCOVERY_RUNTIME_FAILURE', 'PROVISIONING_SCAN_TIMEOUT', 'PROVISIONING_SCAN_CANCELLED',
  'NotFoundError', 'NotAllowedError', 'SecurityError'])
const flowId = process.argv.find(value => value.startsWith(FLOW_ARGUMENT))?.slice(FLOW_ARGUMENT.length)
if (!UUID_V4.test(flowId || '')) throw new Error('PROVISIONING_CONFIGURATION_INVALID')

const clock = Object.freeze({ monotonicMs: () => performance.now() })
const selection = createElectronBluetoothSelectionEndpoint({ bluetooth: navigator.bluetooth, clock })
const factory = createElectronGattTransportFactory({ consumeSelectionLease: candidate => selection.consumeSelectionLease(candidate), clock })
const transport = createElectronGattTransportEndpoint({ factory })
const candidates = new Map()
const candidateWaiters = new Set()
const operations = new Map()
const subscriptions = new Map()
const snapshotListeners = new Set()
const discoveryListeners = new Set()
const retiredDiscoverySessions = new Set()
let selectedCandidate
let requestDeviceFlight
let scanActive = false
let discoverySessionId
let discoverySequence = 0
let discoveryGeneration = 0
let discoveryTimer
let discoveryState = 'idle'
let discoveryErrorCode
let selecting = false

function uuid() { return crypto.randomUUID() }
function safeCode(error, fallback = 'CLAIM_UNKNOWN_ERROR') {
  for (const value of [error?.code, error?.name, error?.message]) {
    if (typeof value !== 'string') continue
    for (const code of [...TRANSPORT_ERRORS, ...CLAIM_ERRORS, ...LOCAL_UI_ERRORS]) {
      if (value === code || value.endsWith(`: ${code}`)) return code
    }
  }
  return fallback
}
function failure(code) { const error = new Error(code); error.name = code; error.code = code; return error }
function safeSnapshot(value) {
  const states = new Set(['idle', 'authenticating', 'awaitingWifi', 'awaitingOwnershipConfirmation', 'waitingCloud',
    'attentionRequired', 'completed', 'cancelled', 'restartRequired', 'terminalError'])
  if (!value || typeof value !== 'object' || !states.has(value.state)) return { state: 'attentionRequired' }
  return Object.freeze({ state: value.state,
    ...(typeof value.attentionCode === 'string' && /^[A-Z][A-Z0-9_]{1,127}$/.test(value.attentionCode)
      ? { attentionCode: value.attentionCode } : {}) })
}
function candidateSnapshot() {
  return Object.freeze([...candidates.values()].map(candidate => Object.freeze({ ...candidate })))
}
function discoverySnapshot() {
  return Object.freeze({ state: discoveryState, candidates: candidateSnapshot(),
    ...(discoveryErrorCode ? { errorCode: discoveryErrorCode } : {}) })
}
function notifyDiscovery() {
  const snapshot = discoverySnapshot()
  for (const listener of [...discoveryListeners]) {
    try { listener(snapshot) } catch {}
  }
}
function notifyCandidates(error) {
  const snapshot = candidateSnapshot()
  for (const waiter of [...candidateWaiters]) {
    if (error) waiter.reject(error)
    else waiter.resolve(snapshot)
  }
  candidateWaiters.clear()
}
function finishDiscovery(state, errorCode, cancelPicker = false) {
  clearTimeout(discoveryTimer)
  discoveryTimer = undefined
  scanActive = false
  selecting = false
  discoveryGeneration += 1
  const sessionId = discoverySessionId
  if (sessionId) retiredDiscoverySessions.add(sessionId)
  discoverySessionId = undefined
  discoverySequence = 0
  discoveryState = state
  discoveryErrorCode = errorCode
  if (cancelPicker) {
    selection.cancelSelection()
    if (sessionId) ipcRenderer.send(DISCOVERY_PICKER_CHANNELS.cancel, { sessionId })
  }
  notifyCandidates(errorCode ? failure(errorCode) : undefined)
  notifyDiscovery()
}

ipcRenderer.on(DISCOVERY_PICKER_CHANNELS.candidates, (_event, message) => {
  if (!message || typeof message !== 'object' || !UUID_V4.test(message.sessionId || '')
      || !Number.isSafeInteger(message.sequence) || message.sequence < 1 || !Array.isArray(message.candidates)) return
  if (retiredDiscoverySessions.has(message.sessionId)) return
  if (!scanActive) {
    // A cancelled native request can create its picker after local cancellation.
    retiredDiscoverySessions.add(message.sessionId)
    ipcRenderer.send(DISCOVERY_PICKER_CHANNELS.cancel, { sessionId: message.sessionId })
    return
  }
  if (discoverySessionId !== message.sessionId) {
    if (discoverySessionId) return
    discoverySessionId = message.sessionId
    discoverySequence = 0
    candidates.clear()
  }
  if (message.sequence <= discoverySequence) return
  discoverySequence = message.sequence
  const next = new Set()
  for (const device of message.candidates.slice(0, 20)) {
    if (!UUID_V4.test(device?.candidateId || '') || device.transport !== 'ble-gatt'
        || typeof device?.advertisedName !== 'string') continue
    const candidateId = device.candidateId
    const name = device.advertisedName.replace(/[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u206f]/gu, '')
      .trim().slice(0, 64) || '附近的 AI 盒子'
    const suffix = /^CentaurOS-Setup-([A-Za-z0-9]{6})$/.exec(name)?.[1]
    candidates.set(candidateId, { candidateId, name, ...(suffix ? { shortCode: suffix } : {}) })
    next.add(candidateId)
  }
  for (const id of [...candidates.keys()]) if (!next.has(id)) candidates.delete(id)
  notifyDiscovery()
  if (candidates.size) notifyCandidates()
})

ipcRenderer.on(DISCOVERY_PICKER_CHANNELS.settled, (_event, message) => {
  if (!message || message.sessionId !== discoverySessionId || message.cancelled !== true) return
  finishDiscovery('completed', candidates.size ? undefined : 'PROVISIONING_SCAN_TIMEOUT', true)
})

ipcRenderer.on(CLAIM_SNAPSHOT_CHANNEL, (_event, message) => {
  if (!message || message.ipcVersion !== IPC_VERSION || message.flowId !== flowId) return
  const snapshot = safeSnapshot(message.snapshot)
  for (const listener of [...snapshotListeners]) listener(snapshot)
})

function sendTransport(message) {
  ipcRenderer.send(TRANSPORT_RESULT_CHANNEL, { ipcVersion: IPC_VERSION, flowId, ...message })
}
function bytes(value) {
  if (!(value instanceof ArrayBuffer) && !ArrayBuffer.isView(value)) throw failure('GATT_FRAGMENT_PROTOCOL_ERROR')
  const source = value instanceof ArrayBuffer ? new Uint8Array(value)
    : new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
  if (source.byteLength < 2 || source.byteLength > 2048) throw failure('GATT_FRAGMENT_PROTOCOL_ERROR')
  return source.slice()
}

ipcRenderer.on(TRANSPORT_COMMAND_CHANNEL, (_event, message) => {
  if (!message || message.ipcVersion !== IPC_VERSION || message.flowId !== flowId
      || !UUID_V4.test(message.operationId || '') || typeof message.kind !== 'string') return
  if (message.kind === 'transport.abort') {
    operations.get(message.targetOperationId)?.abort()
    sendTransport({ kind: 'transport.aborted', operationId: message.operationId,
      targetOperationId: message.targetOperationId })
    return
  }
  const controller = new AbortController()
  operations.set(message.operationId, controller)
  void (async () => {
    if (message.kind === 'transport.connect') {
      if (!selectedCandidate || message.selectionLeaseId !== selectedCandidate.selectionLeaseId) {
        throw failure('LOCAL_SELECTION_LEASE_INVALID')
      }
      const candidate = selectedCandidate
      selectedCandidate = undefined
      const connectionId = await transport.connect(candidate, controller.signal)
      sendTransport({ kind: 'transport.connected', operationId: message.operationId, connectionId })
    } else if (message.kind === 'transport.readDeviceInfo') {
      const value = await transport.readDeviceInfo(message.connectionId, controller.signal)
      sendTransport({ kind: 'transport.deviceInfo', operationId: message.operationId,
        connectionId: message.connectionId, bytes: value.slice().buffer })
    } else if (message.kind === 'transport.request') {
      const value = await transport.request(message.connectionId, bytes(message.logicalFrame),
        { requestId: message.requestId, responseMessageType: message.responseMessageType }, controller.signal)
      sendTransport({ kind: 'transport.response', operationId: message.operationId,
        connectionId: message.connectionId, logicalFrame: value.slice().buffer })
    } else if (message.kind === 'transport.subscribeStatus') {
      let sequence = 0
      const subscriptionId = uuid()
      const unsubscribe = await transport.subscribeStatus(message.connectionId, logicalFrame => {
        sendTransport({ kind: 'transport.status', connectionId: message.connectionId, subscriptionId,
          sequence: ++sequence, logicalFrame: logicalFrame.slice().buffer })
      }, controller.signal)
      subscriptions.set(`${message.connectionId}:${subscriptionId}`, unsubscribe)
      sendTransport({ kind: 'transport.subscribed', operationId: message.operationId,
        connectionId: message.connectionId, subscriptionId })
    } else if (message.kind === 'transport.unsubscribeStatus') {
      const key = `${message.connectionId}:${message.subscriptionId}`
      const unsubscribe = subscriptions.get(key)
      subscriptions.delete(key)
      await unsubscribe?.()
      sendTransport({ kind: 'transport.unsubscribed', operationId: message.operationId,
        connectionId: message.connectionId, subscriptionId: message.subscriptionId })
    } else if (message.kind === 'transport.close') {
      for (const [key, unsubscribe] of [...subscriptions]) {
        if (key.startsWith(`${message.connectionId}:`)) { subscriptions.delete(key); await unsubscribe().catch(() => {}) }
      }
      await transport.close(message.connectionId)
      sendTransport({ kind: 'transport.closed', operationId: message.operationId,
        connectionId: message.connectionId, reason: 'requested' })
    } else throw failure('GATT_FRAGMENT_PROTOCOL_ERROR')
  })().catch(error => sendTransport({ kind: 'transport.failure', operationId: message.operationId,
    code: safeCode(error, 'LOCAL_TRANSPORT_DISCONNECTED'), retryable: error?.retryable === true }))
    .finally(() => operations.delete(message.operationId))
})

async function claim(action, input) {
  try { return await ipcRenderer.invoke(CLAIM_INVOKE_CHANNEL, { ipcVersion: IPC_VERSION, flowId,
    operationId: uuid(), kind: `claim.${action}`, ...(input === undefined ? {} : { input }) }) }
  catch (error) { throw failure(safeCode(error)) }
}

const api = Object.freeze({
  available: Boolean(navigator.bluetooth?.requestDevice),
  testBuild: false,
  legacyAllowed: false,
  async scan() {
    if (scanActive || requestDeviceFlight) throw failure('CLAIM_OPERATION_IN_PROGRESS')
    if (!navigator.userActivation?.isActive) throw failure('PROVISIONING_USER_GESTURE_REQUIRED')
    if (!navigator.bluetooth?.requestDevice) throw failure('PROVISIONING_BLUETOOTH_UNAVAILABLE')
    scanActive = true; candidates.clear(); selectedCandidate = undefined
    discoverySessionId = undefined; discoverySequence = 0
    discoveryState = 'scanning'; discoveryErrorCode = undefined
    const generation = ++discoveryGeneration
    const result = new Promise((resolve, reject) => candidateWaiters.add({ resolve, reject }))
    // Start requestDevice before any await, preserving the click's user activation.
    try {
      const pending = selection.requestDeviceFromUserGesture()
      requestDeviceFlight = pending
      // Keep the native flight occupied through cancellation: cancelSelection
      // only revokes leases and cannot abort requestDevice before a picker exists.
      pending.then(() => {
        if (requestDeviceFlight === pending) requestDeviceFlight = undefined
        if (generation !== discoveryGeneration) selection.cancelSelection()
      }, error => {
        if (requestDeviceFlight === pending) requestDeviceFlight = undefined
        if (generation !== discoveryGeneration) return
        const code = safeCode(error, 'DISCOVERY_RUNTIME_FAILURE')
        finishDiscovery(code === 'NotFoundError' ? 'completed' : 'error',
          code === 'NotFoundError' && candidates.size ? undefined : code, true)
      })
      // Main owns the discovery/selection deadlines (30s + up to 30s). This
      // watchdog only bounds startup before Chromium creates a picker; it must
      // not cancel a device just discovered near the end of the scan window.
      discoveryTimer = setTimeout(() => {
        if (generation === discoveryGeneration) {
          finishDiscovery('completed', candidates.size ? undefined : 'PROVISIONING_SCAN_TIMEOUT', true)
        }
      }, 90000)
      notifyDiscovery()
    } catch (error) {
      finishDiscovery('error', safeCode(error, 'DISCOVERY_RUNTIME_FAILURE'), true)
    }
    return result
  },
  async select(candidateId) {
    const candidate = candidates.get(candidateId)
    if (!candidate || !scanActive || selecting || !requestDeviceFlight || !discoverySessionId) {
      throw failure('LOCAL_SELECTION_LEASE_INVALID')
    }
    selecting = true
    const generation = discoveryGeneration
    const pending = requestDeviceFlight
    ipcRenderer.send(DISCOVERY_PICKER_CHANNELS.select,
      { sessionId: discoverySessionId, candidateId: candidate.candidateId })
    let bound
    try { bound = await pending }
    catch (error) { throw failure(safeCode(error, 'DISCOVERY_RUNTIME_FAILURE')) }
    if (generation !== discoveryGeneration) throw failure('LOCAL_SELECTION_LEASE_INVALID')
    selectedCandidate = bound
    finishDiscovery('selected')
    await claim('bindSelected', bound)
  },
  begin: () => claim('begin'),
  confirmPhysicalDevice(code) {
    if (typeof code !== 'string' || !/^\d{6}$/.test(code)) return Promise.reject(failure('VERIFICATION_CODE_MISMATCH'))
    return claim('confirmPhysicalDevice', { verificationCode: code })
  },
  async provideWifi(input) {
    if (!input || typeof input.ssid !== 'string' || !['open', 'wpa-personal'].includes(input.security)
        || typeof input.password !== 'string') throw failure('CLAIM_INPUT_MISMATCH')
    const security = input.security === 'wpa-personal' ? 'wpa_personal' : 'open'
    const encoded = security === 'wpa_personal' ? new TextEncoder().encode(input.password) : undefined
    try { return await claim('provideWifi', { ssid: input.ssid, security, hidden: false,
      ...(encoded ? { passwordUtf8: encoded.buffer } : {}) }) }
    finally { encoded?.fill(0) }
  },
  confirmOwnership: () => claim('confirmOwnership'),
  refresh: () => claim('refresh'),
  cancel: () => {
    if (scanActive) finishDiscovery('cancelled', 'PROVISIONING_SCAN_CANCELLED', true)
    return claim('cancel')
  },
  cancelScan: () => {
    if (scanActive) finishDiscovery('cancelled', 'PROVISIONING_SCAN_CANCELLED', true)
  },
  onDiscoverySnapshot(listener) {
    if (typeof listener !== 'function') throw new TypeError('Expected a discovery listener')
    discoveryListeners.add(listener)
    listener(discoverySnapshot())
    return () => discoveryListeners.delete(listener)
  },
  onSnapshot(listener) {
    if (typeof listener !== 'function') throw new TypeError('Expected a snapshot listener')
    snapshotListeners.add(listener)
    let active = true
    void claim('snapshot').then(snapshot => { if (active) listener(safeSnapshot(snapshot)) }).catch(() => {})
    return () => { active = false; snapshotListeners.delete(listener) }
  },
})

addEventListener('beforeunload', () => {
  if (scanActive) finishDiscovery('cancelled', 'PROVISIONING_SCAN_CANCELLED', true)
  else selection.cancelSelection()
  for (const operation of operations.values()) operation.abort()
  operations.clear(); subscriptions.clear(); snapshotListeners.clear(); discoveryListeners.clear()
  void transport.closeAll()
})

contextBridge.exposeInMainWorld('desktopProvisioning', api)
