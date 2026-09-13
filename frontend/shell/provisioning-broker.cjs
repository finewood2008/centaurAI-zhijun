'use strict'

const { randomUUID } = require('node:crypto')
const { performance } = require('node:perf_hooks')

const CLAIM_INVOKE_CHANNEL = 'zhijun:provisioning:claim:v1'
const CLAIM_SNAPSHOT_CHANNEL = 'zhijun:provisioning:snapshot:v1'
const TRANSPORT_COMMAND_CHANNEL = 'zhijun:provisioning:transport-command:v1'
const TRANSPORT_RESULT_CHANNEL = 'zhijun:provisioning:transport-result:v1'
const IPC_VERSION = 1
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const SAFE_ID = /^[A-Za-z0-9._:-]{1,128}$/
const TRANSPORT_ERRORS = new Set(['LOCAL_CONNECT_TIMEOUT', 'LOCAL_CONNECT_UNSUPPORTED_BOND_FLOW',
  'LOCAL_SELECTION_LEASE_INVALID', 'LOCAL_DEVICE_INFO_INVALID', 'LOCAL_DEVICE_INCOMPATIBLE',
  'GATT_SERVICE_NOT_FOUND', 'GATT_CHARACTERISTIC_MISMATCH', 'GATT_FRAGMENT_PROTOCOL_ERROR',
  'GATT_FRAGMENT_INCOMPLETE', 'LOCAL_TRANSPORT_DISCONNECTED', 'LOCAL_USER_CANCELLED'])
const SNAPSHOT_STATES = new Set(['idle', 'authenticating', 'awaitingWifi', 'awaitingOwnershipConfirmation',
  'waitingCloud', 'attentionRequired', 'completed', 'cancelled', 'restartRequired', 'terminalError'])

const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value)
function exact(value, required, optional = []) {
  return plain(value) && required.every(key => Object.hasOwn(value, key))
    && Object.keys(value).every(key => required.includes(key) || optional.includes(key))
}
function abortError() { return new DOMException('Aborted', 'AbortError') }

function projectPreview(value) {
  if (!plain(value)) return undefined
  const name = value.display_name === 'CentaurAI Box' ? value.display_name : undefined
  const shortCode = typeof value.verification_code === 'string' && /^\d{6}$/.test(value.verification_code)
    ? value.verification_code : undefined
  const publicKeyFingerprint = typeof value.identity_public_key_sha256 === 'string'
    && /^[a-f0-9]{64}$/.test(value.identity_public_key_sha256)
    ? value.identity_public_key_sha256 : undefined
  if (!name || !shortCode || !publicKeyFingerprint) return undefined
  return Object.freeze({ name, shortCode, publicKeyFingerprint,
    macSuffix: typeof value.serial_suffix === 'string' ? value.serial_suffix.slice(0, 16) : '',
    hardwareProfile: typeof value.model === 'string' ? value.model.slice(0, 64) : '',
    state: value.network_state === 'connected' ? 'connected' : 'unprovisioned' })
}

function projectSnapshot(value) {
  const rawState = plain(value) && typeof value.state === 'string' ? value.state : 'terminalError'
  const state = ['connecting', 'readingDeviceInfo', 'previewReady', 'verifyingLocalDevice',
    'localDeviceVerified', 'establishingSecureChannel'].includes(rawState) ? 'authenticating'
    : ['submittingAppProof', 'provisioningWifi', 'ownershipCommitted', 'projectionPending',
        'deviceAckPending'].includes(rawState) ? 'waitingCloud'
      : SNAPSHOT_STATES.has(rawState) ? rawState : 'terminalError'
  const preview = projectPreview(value?.compatiblePreview || value?.preview)
  const attentionCode = typeof value?.errorCode === 'string' && /^[A-Z][A-Z0-9_]{1,127}$/.test(value.errorCode)
    ? value.errorCode : undefined
  return Object.freeze({ state, ...(preview ? { preview } : {}),
    ...(attentionCode ? { attentionCode } : {}), retryable: value?.retryable === true })
}

function validateSelected(value) {
  if (!exact(value, ['candidateId', 'transport', 'transportHandle', 'advertisedName', 'rssi',
    'firstSeenMonotonicMs', 'lastSeenMonotonicMs', 'selectionLeaseId'])
      || !UUID_V4.test(value.candidateId) || value.transport !== 'ble-gatt'
      || !UUID_V4.test(value.transportHandle) || !UUID_V4.test(value.selectionLeaseId)
      || typeof value.advertisedName !== 'string' || Buffer.byteLength(value.advertisedName) > 64
      || (value.rssi !== null && (!Number.isInteger(value.rssi) || value.rssi < -127 || value.rssi > 20))
      || !Number.isFinite(value.firstSeenMonotonicMs) || !Number.isFinite(value.lastSeenMonotonicMs)
      || value.firstSeenMonotonicMs > value.lastSeenMonotonicMs) throw new Error('LOCAL_SELECTION_LEASE_INVALID')
  return Object.freeze({ ...value })
}

function validateWifi(value) {
  if (!exact(value, ['ssid', 'security', 'hidden'], ['passwordUtf8']) || typeof value.ssid !== 'string'
      || Buffer.byteLength(value.ssid) < 1 || Buffer.byteLength(value.ssid) > 32
      || /[\u0000-\u001f\u007f-\u009f]/u.test(value.ssid) || typeof value.hidden !== 'boolean'
      || !['open', 'wpa_personal'].includes(value.security)) throw new Error('CLAIM_INPUT_MISMATCH')
  let passwordUtf8
  if (value.security === 'open') {
    if (Object.hasOwn(value, 'passwordUtf8')) throw new Error('CLAIM_INPUT_MISMATCH')
  } else {
    const source = value.passwordUtf8
    if (!(source instanceof ArrayBuffer) && !ArrayBuffer.isView(source)) throw new Error('CLAIM_INPUT_MISMATCH')
    passwordUtf8 = new Uint8Array(source instanceof ArrayBuffer
      ? source.slice(0) : source.buffer.slice(source.byteOffset, source.byteOffset + source.byteLength))
    const password = new TextDecoder('utf-8', { fatal: true }).decode(passwordUtf8)
    if (!(/^[ -~]{8,63}$/.test(password) || /^[0-9A-Fa-f]{64}$/.test(password))) {
      passwordUtf8.fill(0); throw new Error('CLAIM_INPUT_MISMATCH')
    }
  }
  return { ssid: value.ssid, security: value.security, hidden: value.hidden,
    ...(passwordUtf8 ? { passwordUtf8 } : {}) }
}

function createProvisioningBroker({ ipcMain, window, provisioningContext, provisioningConfig,
  flowId: requestedFlowId,
  importCore = async () => ({ ...(await import('@nexusaos/local-provisioning-core')),
    createNodeProvisioningCryptoProvider: (await import('@nexusaos/local-provisioning-core/node')).createNodeProvisioningCryptoProvider }),
  operationTimeoutMs = 30000 } = {}) {
  if (!ipcMain || !window || !plain(provisioningContext) || !plain(provisioningConfig)
      || !SAFE_ID.test(provisioningContext.accountId) || !SAFE_ID.test(provisioningContext.clientId)
      || !Array.isArray(provisioningConfig.trustedRootSpkiPins)
      || !Array.isArray(provisioningConfig.trustedRootCertificatesPem)) throw new Error('PROVISIONING_CONFIGURATION_INVALID')
  const contents = window.webContents
  const flowId = requestedFlowId === undefined ? randomUUID() : requestedFlowId
  if (!UUID_V4.test(flowId)) throw new Error('PROVISIONING_CONFIGURATION_INVALID')
  const pending = new Map()
  const claimOperations = new Set()
  const statusSubscriptions = new Map()
  let selected
  let coordinator
  let transportFailure = code => Object.assign(new Error(code), { code, retryable: code === 'LOCAL_CONNECT_TIMEOUT'
    || code === 'GATT_FRAGMENT_INCOMPLETE' || code === 'LOCAL_TRANSPORT_DISCONNECTED' })
  let unsubscribe = () => {}
  let disposed = false

  const trusted = event => !disposed && !window.isDestroyed() && event?.sender === contents
    && (!event.senderFrame || event.senderFrame === contents.mainFrame)
  const send = message => {
    if (disposed || window.isDestroyed() || contents.isDestroyed()) throw new Error('LOCAL_TRANSPORT_DISCONNECTED')
    contents.send(TRANSPORT_COMMAND_CHANNEL, Object.freeze({ ipcVersion: IPC_VERSION, flowId, ...message }))
  }
  function settlePending(operationId, failure, value) {
    const entry = pending.get(operationId)
    if (!entry) return
    pending.delete(operationId); clearTimeout(entry.timer); entry.signal?.removeEventListener('abort', entry.abort)
    failure ? entry.reject(failure) : entry.resolve(value)
  }
  function callTransport(kind, fields, expectedKind, signal, timeoutMs = operationTimeoutMs) {
    if (signal?.aborted) return Promise.reject(abortError())
    const operationId = randomUUID()
    return new Promise((resolve, reject) => {
      const abort = () => {
        try { send({ kind: 'transport.abort', operationId: randomUUID(), targetOperationId: operationId }) } catch {}
        settlePending(operationId, abortError())
      }
      const timer = setTimeout(() => {
        try { send({ kind: 'transport.abort', operationId: randomUUID(), targetOperationId: operationId }) } catch {}
        settlePending(operationId, transportFailure('LOCAL_CONNECT_TIMEOUT'))
      }, timeoutMs)
      pending.set(operationId, { expectedKind, resolve, reject, timer, signal, abort })
      signal?.addEventListener('abort', abort, { once: true })
      try { send({ kind, operationId, ...fields }) } catch (error) { settlePending(operationId, error) }
    })
  }
  function frameBytes(value) {
    if (!(value instanceof ArrayBuffer) && !ArrayBuffer.isView(value)) throw transportFailure('GATT_FRAGMENT_PROTOCOL_ERROR')
    const bytes = new Uint8Array(value instanceof ArrayBuffer ? value : value.buffer, value.byteOffset || 0,
      value.byteLength === undefined ? value.byteLength : value.byteLength)
    if (bytes.byteLength < 2 || bytes.byteLength > 2048) throw transportFailure('GATT_FRAGMENT_PROTOCOL_ERROR')
    return bytes.slice()
  }
  function transportFactory(core) {
    return Object.freeze({ async connect(candidate, signal) {
      const bound = validateSelected(candidate)
      const connected = await callTransport('transport.connect', { selectionLeaseId: bound.selectionLeaseId },
        'transport.connected', signal, 15000)
      if (!UUID_V4.test(connected.connectionId)) throw new Error('LOCAL_TRANSPORT_DISCONNECTED')
      const connectionId = connected.connectionId
      let closed = false
      return Object.freeze({ kind: 'ble-gatt', capabilities: Object.freeze({ statusNotifications: true }),
        async readDeviceInfo(readSignal) {
          if (closed) throw new Error('LOCAL_TRANSPORT_DISCONNECTED')
          const result = await callTransport('transport.readDeviceInfo', { connectionId, timeoutMs: 5000 },
            'transport.deviceInfo', readSignal, 5000)
          if (result.connectionId !== connectionId) throw new Error('LOCAL_TRANSPORT_DISCONNECTED')
          return frameBytes(result.bytes)
        },
        async request(logicalFrame, match, requestSignal) {
          if (closed || !(logicalFrame instanceof Uint8Array) || logicalFrame.byteLength > 2048
              || !UUID_V4.test(match?.requestId) || typeof match?.responseMessageType !== 'string') {
            throw new Error('GATT_FRAGMENT_PROTOCOL_ERROR')
          }
          const bytes = logicalFrame.slice().buffer
          const result = await callTransport('transport.request', { connectionId, logicalFrame: bytes,
            requestId: match.requestId, responseMessageType: match.responseMessageType, timeoutMs: operationTimeoutMs },
          'transport.response', requestSignal)
          if (result.connectionId !== connectionId) throw new Error('LOCAL_TRANSPORT_DISCONNECTED')
          return frameBytes(result.logicalFrame)
        },
        async subscribeStatus(listener, subscribeSignal) {
          if (closed || typeof listener !== 'function') throw new Error('LOCAL_TRANSPORT_DISCONNECTED')
          const result = await callTransport('transport.subscribeStatus', { connectionId, timeoutMs: 5000 },
            'transport.subscribed', subscribeSignal, 5000)
          if (result.connectionId !== connectionId || !UUID_V4.test(result.subscriptionId)) throw new Error('LOCAL_TRANSPORT_DISCONNECTED')
          const key = `${connectionId}:${result.subscriptionId}`
          statusSubscriptions.set(key, { listener, sequence: 0 })
          let flight
          return () => flight || (flight = callTransport('transport.unsubscribeStatus',
            { connectionId, subscriptionId: result.subscriptionId }, 'transport.unsubscribed', undefined, 1000)
            .finally(() => statusSubscriptions.delete(key)))
        },
        async close() {
          if (closed) return
          closed = true
          for (const key of statusSubscriptions.keys()) if (key.startsWith(`${connectionId}:`)) statusSubscriptions.delete(key)
          await callTransport('transport.close', { connectionId }, 'transport.closed', undefined, 1000).catch(() => {})
        } })
    } })
  }

  const ready = (async () => {
    const core = await importCore()
    transportFailure = code => new core.LocalProvisioningFailure(code)
    const crypto = core.createNodeProvisioningCryptoProvider({
      trustedRootCertificatesPem: provisioningConfig.trustedRootCertificatesPem })
    await crypto.selfTest()
    coordinator = core.createClaimCoordinator({ transportFactory: transportFactory(core), crypto,
      consumerApi: provisioningContext.consumerApi, resumeStore: provisioningContext.resumeStore,
      trustedRootSpkiPins: [...provisioningConfig.trustedRootSpkiPins], clientVersion: 'zhijun-desktop/2',
      clock: Object.freeze({ monotonicMs: () => performance.now(), nowUtc: () => new Date().toISOString() }) })
    unsubscribe = coordinator.subscribe(snapshot => {
      if (!disposed && !window.isDestroyed() && !contents.isDestroyed()) {
        contents.send(CLAIM_SNAPSHOT_CHANNEL, { ipcVersion: IPC_VERSION, flowId, snapshot: projectSnapshot(snapshot) })
      }
    })
    return core
  })()

  const onTransportResult = (event, message) => {
    if (!trusted(event) || !exact(message, ['ipcVersion', 'flowId', 'kind'], ['operationId', 'connectionId',
      'subscriptionId', 'sequence', 'bytes', 'logicalFrame', 'code', 'retryable'])) return
    if (message.ipcVersion !== IPC_VERSION || message.flowId !== flowId) return
    if (message.kind === 'transport.status') {
      if (!UUID_V4.test(message.connectionId) || !UUID_V4.test(message.subscriptionId)
          || !Number.isSafeInteger(message.sequence)) return
      const entry = statusSubscriptions.get(`${message.connectionId}:${message.subscriptionId}`)
      if (!entry) return
      if (message.sequence !== entry.sequence + 1) { statusSubscriptions.delete(`${message.connectionId}:${message.subscriptionId}`); return }
      entry.sequence = message.sequence
      try { entry.listener(frameBytes(message.logicalFrame)) } catch {}
      return
    }
    if (!UUID_V4.test(message.operationId)) return
    const entry = pending.get(message.operationId)
    if (!entry) return
    if (message.kind === 'transport.failure') {
      if (!TRANSPORT_ERRORS.has(message.code) || typeof message.retryable !== 'boolean') return
      const error = transportFailure(message.code)
      settlePending(message.operationId, error); return
    }
    if (message.kind !== entry.expectedKind) return
    settlePending(message.operationId, null, message)
  }
  ipcMain.on(TRANSPORT_RESULT_CHANNEL, onTransportResult)
  ipcMain.handle(CLAIM_INVOKE_CHANNEL, async (event, message) => {
    if (!trusted(event) || !exact(message, ['ipcVersion', 'flowId', 'operationId', 'kind'], ['input'])
        || message.ipcVersion !== IPC_VERSION || message.flowId !== flowId || !UUID_V4.test(message.operationId)
        || typeof message.kind !== 'string' || !message.kind.startsWith('claim.')) throw new Error('PROVISIONING_ACCESS_DENIED')
    if (claimOperations.has(message.operationId)) throw new Error('CLAIM_OPERATION_IN_PROGRESS')
    if (claimOperations.size >= 128) claimOperations.delete(claimOperations.values().next().value)
    claimOperations.add(message.operationId)
    const action = message.kind.slice('claim.'.length)
    const input = message.input
    await ready
    if (action === 'bindSelected') { selected = validateSelected(input); return projectSnapshot(coordinator.getSnapshot()) }
    let beginResponse = false
    if (action === 'begin') {
      if (!selected) throw new Error('LOCAL_SELECTION_LEASE_INVALID')
      await coordinator.begin({ selected, accountId: provisioningContext.accountId,
        clientId: provisioningContext.clientId, clientPlatform: 'electron' }, new AbortController().signal)
      beginResponse = true
    } else if (action === 'confirmPhysicalDevice') {
      if (!exact(input, ['verificationCode']) || !/^\d{6}$/.test(input.verificationCode)) throw new Error('VERIFICATION_CODE_MISMATCH')
      await coordinator.confirmPhysicalDevice(input, new AbortController().signal)
    } else if (action === 'provideWifi') {
      const wifi = validateWifi(input)
      try { await coordinator.provideWifi(wifi) } catch (error) { wifi.passwordUtf8?.fill(0); throw error }
    } else if (action === 'confirmOwnership') {
      await coordinator.confirmOwnership(new AbortController().signal)
    } else if (action === 'refresh') {
      await coordinator.refreshAuthority(new AbortController().signal)
    } else if (action === 'cancel') {
      await coordinator.cancel('user_cancelled')
    } else if (action !== 'snapshot') throw new Error('PROVISIONING_OPERATION_DENIED')
    const raw = coordinator.getSnapshot()
    const snapshot = projectSnapshot(raw)
    return beginResponse ? { preview: projectPreview(raw.compatiblePreview || raw.preview), snapshot } : snapshot
  })

  async function dispose(reason = 'renderer_crash') {
    if (disposed) return
    disposed = true
    ipcMain.removeHandler(CLAIM_INVOKE_CHANNEL)
    ipcMain.removeListener(TRANSPORT_RESULT_CHANNEL, onTransportResult)
    for (const operationId of [...pending.keys()]) settlePending(operationId,
      Object.assign(new Error('LOCAL_TRANSPORT_DISCONNECTED'), { code: 'LOCAL_TRANSPORT_DISCONNECTED', retryable: true }))
    statusSubscriptions.clear(); claimOperations.clear(); unsubscribe()
    try { await ready; await coordinator.suspend(reason) } catch {}
    try { await coordinator?.dispose() } catch {}
  }
  return Object.freeze({ flowId, ready, dispose })
}

module.exports = { CLAIM_INVOKE_CHANNEL, CLAIM_SNAPSHOT_CHANNEL, TRANSPORT_COMMAND_CHANNEL,
  TRANSPORT_RESULT_CHANNEL, IPC_VERSION, createProvisioningBroker, projectSnapshot, validateSelected }
