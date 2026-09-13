'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const { randomUUID } = require('node:crypto')
const { CLAIM_INVOKE_CHANNEL, CLAIM_SNAPSHOT_CHANNEL, TRANSPORT_COMMAND_CHANNEL,
  TRANSPORT_RESULT_CHANNEL, createProvisioningBroker, projectSnapshot } = require('../provisioning-broker.cjs')

function fixture() {
  const ipcMain = new EventEmitter()
  const handlers = new Map()
  ipcMain.handle = (channel, handler) => handlers.set(channel, handler)
  ipcMain.removeHandler = channel => handlers.delete(channel)
  const contents = new EventEmitter()
  contents.mainFrame = {}
  contents.destroyed = false
  contents.isDestroyed = () => contents.destroyed
  contents.sent = []
  contents.send = (channel, message) => contents.sent.push({ channel, message })
  const window = { webContents: contents, isDestroyed: () => false }
  const calls = []
  let listener = () => {}
  const coordinator = {
    snapshot: { flowId: randomUUID(), state: 'idle' },
    getSnapshot() { return this.snapshot },
    subscribe(value) { listener = value; value(this.snapshot); return () => { listener = () => {} } },
    async begin(input) { calls.push(['begin', input]); this.snapshot = { state: 'previewReady',
      compatiblePreview: { display_name: 'CentaurAI Box', verification_code: '233449',
        identity_public_key_sha256: 'a'.repeat(64), model: 'M1', serial_suffix: 'E2334B',
        network_state: 'unprovisioned' } }; listener(this.snapshot) },
    async confirmPhysicalDevice(input) { calls.push(['confirmPhysicalDevice', input]); this.snapshot.state = 'awaitingWifi' },
    async provideWifi(input) { calls.push(['provideWifi', input]); this.snapshot.state = 'awaitingOwnershipConfirmation' },
    async confirmOwnership() { calls.push(['confirmOwnership']); this.snapshot.state = 'completed' },
    async refreshAuthority() { calls.push(['refresh']) },
    async cancel() { calls.push(['cancel']); this.snapshot.state = 'cancelled' },
    async suspend(reason) { calls.push(['suspend', reason]) },
    async dispose() { calls.push(['dispose']) },
  }
  const importCore = async () => ({
    createNodeProvisioningCryptoProvider: () => ({ selfTest: async () => {} }),
    createClaimCoordinator(deps) { calls.push(['deps', deps]); return coordinator },
  })
  const provisioningContext = { accountId: 'account-1', clientId: 'client-1', consumerApi: {}, resumeStore: {} }
  const provisioningConfig = { trustedRootSpkiPins: [`sha256/${Buffer.alloc(32).toString('base64')}`],
    trustedRootCertificatesPem: ['-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----'] }
  const event = { sender: contents, senderFrame: contents.mainFrame }
  return { ipcMain, handlers, contents, window, calls, coordinator, importCore, provisioningContext,
    provisioningConfig, event }
}

test('snapshot projection exposes only the allowlisted UI model', () => {
  assert.deepEqual(projectSnapshot({ state: 'establishingSecureChannel', pairingToken: 'secret',
    errorCode: 'DEVICE_IDENTITY_MISMATCH', preview: { display_name: 'CentaurAI Box', verification_code: '123456',
      identity_public_key_sha256: 'b'.repeat(64), model: 'M1', serial_suffix: 'ABC123', network_state: 'connected',
      device_id: 'must-not-leak' } }), {
    state: 'authenticating', attentionCode: 'DEVICE_IDENTITY_MISMATCH', retryable: false,
    preview: { name: 'CentaurAI Box', shortCode: '123456', publicKeyFingerprint: 'b'.repeat(64),
      hardwareProfile: 'M1', macSuffix: 'ABC123', state: 'connected' },
  })
})

test('claim IPC binds one opaque lease and transfers Wi-Fi bytes only to coordinator', async t => {
  const f = fixture()
  const broker = createProvisioningBroker({ ipcMain: f.ipcMain, window: f.window,
    provisioningContext: f.provisioningContext, provisioningConfig: f.provisioningConfig, importCore: f.importCore })
  t.after(() => broker.dispose())
  await broker.ready
  const invoke = (action, input) => f.handlers.get(CLAIM_INVOKE_CHANNEL)(f.event, { ipcVersion: 1,
    flowId: broker.flowId, operationId: randomUUID(), kind: `claim.${action}`,
    ...(input === undefined ? {} : { input }) })
  const now = performance.now()
  const selected = { candidateId: randomUUID(), transport: 'ble-gatt', transportHandle: randomUUID(),
    advertisedName: 'CentaurOS-Setup-E2334B', rssi: -40, firstSeenMonotonicMs: now,
    lastSeenMonotonicMs: now, selectionLeaseId: randomUUID() }
  await invoke('bindSelected', selected)
  await invoke('begin')
  await invoke('confirmPhysicalDevice', { verificationCode: '233449' })
  const password = new TextEncoder().encode('correct horse')
  await invoke('provideWifi', { ssid: 'Office', security: 'wpa_personal', hidden: false,
    passwordUtf8: password.buffer })
  assert.deepEqual(f.calls.find(call => call[0] === 'provideWifi')[1].passwordUtf8,
    new TextEncoder().encode('correct horse'))
  assert.equal(JSON.stringify(f.contents.sent).includes('correct horse'), false)
  assert.ok(f.contents.sent.some(item => item.channel === CLAIM_SNAPSHOT_CHANNEL))
  await invoke('confirmOwnership')
  assert.equal((await invoke('snapshot')).state, 'completed')
})

test('transport proxy matches operation IDs and rejects cross-window results', async t => {
  const f = fixture()
  const broker = createProvisioningBroker({ ipcMain: f.ipcMain, window: f.window,
    provisioningContext: f.provisioningContext, provisioningConfig: f.provisioningConfig, importCore: f.importCore })
  t.after(() => broker.dispose())
  await broker.ready
  const deps = f.calls.find(call => call[0] === 'deps')[1]
  const selected = { candidateId: randomUUID(), transport: 'ble-gatt', transportHandle: randomUUID(),
    advertisedName: 'CentaurOS-Setup-E2334B', rssi: null, firstSeenMonotonicMs: 1,
    lastSeenMonotonicMs: 2, selectionLeaseId: randomUUID() }
  const connecting = deps.transportFactory.connect(selected, new AbortController().signal)
  const command = f.contents.sent.find(item => item.channel === TRANSPORT_COMMAND_CHANNEL).message
  f.ipcMain.emit(TRANSPORT_RESULT_CHANNEL, { sender: {}, senderFrame: f.contents.mainFrame },
    { ipcVersion: 1, flowId: broker.flowId, kind: 'transport.connected', operationId: command.operationId,
      connectionId: randomUUID() })
  let settled = false
  connecting.then(() => { settled = true })
  await Promise.resolve()
  assert.equal(settled, false)
  const connectionId = randomUUID()
  f.ipcMain.emit(TRANSPORT_RESULT_CHANNEL, f.event,
    { ipcVersion: 1, flowId: broker.flowId, kind: 'transport.connected', operationId: command.operationId,
      connectionId })
  const transport = await connecting
  const reading = transport.readDeviceInfo(new AbortController().signal)
  const readCommand = f.contents.sent.at(-1).message
  f.ipcMain.emit(TRANSPORT_RESULT_CHANNEL, f.event,
    { ipcVersion: 1, flowId: broker.flowId, kind: 'transport.deviceInfo', operationId: readCommand.operationId,
      connectionId, bytes: new Uint8Array([123, 10]).buffer })
  assert.deepEqual(await reading, new Uint8Array([123, 10]))
})
