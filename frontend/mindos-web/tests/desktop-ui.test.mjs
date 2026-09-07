import assert from 'node:assert/strict'
import test from 'node:test'
import { DesktopController } from '../src/desktop/controller.ts'
import { connectedDeviceLabel } from '../src/desktop/deviceDisplay.ts'

const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const tick = () => new Promise(resolve => setImmediate(resolve))
const snapshot = (phase, generation, sequence, accountId = 'synthetic-account', deviceId = 'synthetic-box') => ({
  protocolVersion: 1, environment: 'simulation', phase, generation, sequence,
  subject: phase === 'signed_out' ? null : phase === 'ready' ? { accountId, deviceId } : { accountId },
  capabilities: { materialsRead: phase === 'ready', streamChat: false, uploads: false, matters: false, provisioning: false },
})
const ok = (generation, data) => ({ ok: true, generation, data })
const page = (query, fileName = 'synthetic-document.txt') => ({
  items: [{ materialId: 'synthetic-material', fileName, fileType: 'document', status: 'queued', createdAt: '2026-09-06T00:00:00Z' }],
  total: 41, limit: query.limit, offset: query.offset, hasMore: query.offset + 1 < 41,
})
function fixture(initial = snapshot('signed_out', 0, 0)) {
  let listener
  let unsubscribed = false
  const reads = [], devices = [], controls = [], cancellations = []
  const invoke = operation => (context, deviceId) => {
    const result = deferred(); controls.push({ operation, context, deviceId, ...result }); return result.promise
  }
  const bridge = {
    protocolVersion: 1,
    getSnapshot: async () => ok(initial.generation, initial),
    subscribe: callback => { listener = callback; return () => { unsubscribed = true; listener = undefined } },
    beginSignIn: invoke('beginSignIn'), signInWithPassword: invoke('signInWithPassword'), connect: invoke('connect'), disconnect: invoke('disconnect'), signOut: invoke('signOut'),
    listDevices: context => { const result = deferred(); devices.push({ context, ...result }); return result.promise },
    materials: { list: (context, query) => { const result = deferred(); reads.push({ context, query, ...result }); return result.promise } },
    cancelRead: async (context, targetCallId) => { cancellations.push({ context, targetCallId }); return ok(context.expectedGeneration, { delivery: 'suppressed', remoteCancellation: 'not_supported' }) },
  }
  const controller = new DesktopController(bridge)
  return { controller, bridge, reads, devices, controls, cancellations, emit: next => listener?.(next), get unsubscribed() { return unsubscribed } }
}

test('connected device label prefers a non-empty name and otherwise uses the device id', () => {
  assert.equal(connectedDeviceLabel({ accountId: 'account', deviceId: 'box-id', deviceName: '  公司 AMD 盒子  ' }), '公司 AMD 盒子')
  assert.equal(connectedDeviceLabel({ accountId: 'account', deviceId: 'box-id', deviceName: '   ' }), 'box-id')
  assert.equal(connectedDeviceLabel({ accountId: 'account', deviceId: 'box-id' }), 'box-id')
  assert.equal(connectedDeviceLabel(null), '')
})

test('without a desktop bridge no network fallback or simulated session is started', async () => {
  const controller = new DesktopController(undefined)
  await controller.start()
  assert.equal(controller.state.hostAvailable, false)
  assert.equal(controller.state.snapshot, null)
})

test('subscription wins over a late initial snapshot', async () => {
  const f = fixture()
  const pending = deferred()
  f.bridge.getSnapshot = () => pending.promise
  const starting = f.controller.start()
  f.emit(snapshot('selecting_device', 1, 2))
  pending.resolve(ok(0, snapshot('signed_out', 0, 0)))
  await starting
  assert.equal(f.controller.state.snapshot.generation, 1)
  assert.equal(f.controller.state.snapshot.phase, 'selecting_device')
  assert.equal(f.devices.length, 1)
  f.controller.dispose()
})

test('switching device clears data immediately and discards old reads and errors', async () => {
  const f = fixture(snapshot('ready', 1, 1))
  await f.controller.start()
  assert.equal(f.reads.length, 1)
  f.emit(snapshot('connecting', 2, 2, 'synthetic-account', 'synthetic-box-b'))
  f.emit(snapshot('ready', 2, 3, 'synthetic-account', 'synthetic-box-b'))
  const old = f.reads[0], current = f.reads[1]
  current.resolve(ok(2, page(current.query, 'box-b.txt')))
  await tick()
  old.resolve(ok(1, page(old.query, 'private-box-a.txt')))
  await tick()
  assert.equal(f.controller.state.page.items[0].fileName, 'box-b.txt')
  assert.equal(f.controller.state.error, null)
  f.controller.dispose()
})

test('initial snapshot failure cannot contaminate a newer subscribed connection state', async () => {
  const f = fixture()
  const pending = deferred()
  f.bridge.getSnapshot = () => pending.promise
  const starting = f.controller.start()
  f.emit(snapshot('ready', 1, 2))
  pending.reject(new Error('raw native failure must not enter the newer view'))
  await starting
  assert.equal(f.controller.state.snapshot.phase, 'ready')
  assert.equal(f.controller.state.error, null)
  f.controller.dispose()
})

test('filter changes cancel previous delivery and queued filter is forwarded with default pagination', async () => {
  const f = fixture(snapshot('ready', 1, 1))
  await f.controller.start()
  const filtering = f.controller.setFilters({ keyword: '  notes  ', type: 'document', status: 'queued' })
  assert.deepEqual(f.reads[1].query, { limit: 20, offset: 0, keyword: 'notes', type: 'document', status: 'queued' })
  assert.equal(f.cancellations[0].targetCallId, f.reads[0].context.callId)
  f.reads[1].resolve(ok(1, page(f.reads[1].query, 'filtered.txt')))
  await filtering
  f.reads[0].resolve({ ok: false, generation: 1, error: { code: 'REMOTE_ERROR', message: 'old failure', recovery: 'none' } })
  await tick()
  assert.equal(f.controller.state.page.items[0].fileName, 'filtered.txt')
  assert.equal(f.controller.state.error, null)
  const paging = f.controller.changePage(1)
  assert.equal(f.reads[2].query.offset, 20)
  assert.equal(f.reads[2].query.status, 'queued')
  f.reads[2].resolve(ok(1, page(f.reads[2].query)))
  await paging
  assert.equal(f.controller.state.query.offset, 20)
  f.controller.dispose()
})

test('manual cancellation releases UI pending state without claiming remote cancellation', async () => {
  const f = fixture(snapshot('ready', 1, 1))
  await f.controller.start()
  f.controller.cancelRead()
  assert.equal(f.controller.state.loading, false)
  assert.match(f.controller.state.notice, /远端请求可能仍在执行/)
  assert.equal(f.controls.length, 0)
  f.reads[0].resolve(ok(1, page(f.reads[0].query)))
  await tick()
  assert.equal(f.controller.state.page, null)
  f.controller.dispose()
})

test('late device list after sign out cannot restore device names', async () => {
  const f = fixture(snapshot('selecting_device', 1, 1))
  await f.controller.start()
  f.emit(snapshot('signed_out', 2, 2))
  f.devices[0].resolve(ok(1, [{ deviceId: 'private-old-box', displayName: 'private old box', availability: 'online' }]))
  await tick()
  assert.deepEqual(f.controller.state.devices, [])
  assert.equal(f.controller.state.devicesLoading, false)
  f.controller.dispose()
})

test('sign out supersedes pending login and its late result cannot restore the account', async () => {
  const f = fixture()
  await f.controller.start()
  const signingIn = f.controller.control('beginSignIn')
  f.emit(snapshot('authenticating', 1, 1))
  const signingOut = f.controller.control('signOut')
  f.emit(snapshot('signed_out', 2, 2))
  f.controls[1].resolve(ok(2, snapshot('signed_out', 2, 2)))
  await signingOut
  f.controls[0].resolve(ok(1, snapshot('selecting_device', 1, 3)))
  await signingIn
  assert.equal(f.controller.state.snapshot.phase, 'signed_out')
  assert.equal(f.controller.state.snapshot.subject, null)
  assert.equal(f.controller.state.controlPending, false)
  assert.equal(f.devices.length, 0)
  f.controller.dispose()
})

test('unconfigured login displays its structured rejection', async () => {
  const f = fixture({ ...snapshot('signed_out', 0, 0), environment: 'unconfigured' })
  await f.controller.start()
  const login = f.controller.control('beginSignIn')
  f.controls[0].resolve({ ok: false, generation: 0, error: { code: 'CONFIGURATION_REQUIRED', message: '正式登录尚未配置', recovery: 'none' } })
  await login
  assert.equal(f.controller.state.error.code, 'CONFIGURATION_REQUIRED')
  assert.equal(f.controller.state.snapshot.environment, 'unconfigured')
  assert.equal(f.controller.state.snapshot.phase, 'signed_out')
  f.controller.dispose()
})

test('unmount suppresses a read and unsubscribes without disconnecting the session', async () => {
  const f = fixture(snapshot('ready', 1, 1))
  await f.controller.start()
  let updates = 0
  f.controller.observe(() => { updates++ })
  f.controller.dispose()
  assert.equal(f.unsubscribed, true)
  assert.equal(f.cancellations.length, 1)
  assert.equal(f.controls.length, 0)
  f.reads[0].resolve(ok(1, page(f.reads[0].query)))
  await tick()
  assert.equal(updates, 1)
})

test('password input only goes to the narrow action, never controller state, and logout wins', async () => {
  const f = fixture({ ...snapshot('signed_out', 0, 0), environment: 'production' })
  await f.controller.start()
  const credentials = { phone: '13800000000', password: 'synthetic-password' }
  const signingIn = f.controller.control('signInWithPassword', credentials)
  assert.equal(f.controls[0].operation, 'signInWithPassword')
  assert.deepEqual(f.controls[0].deviceId, credentials)
  assert.equal(JSON.stringify(f.controller.state).includes(credentials.password), false)
  f.emit({ ...snapshot('authenticating', 1, 1), environment: 'production', subject: null })
  const signingOut = f.controller.control('signOut')
  f.controls[1].resolve(ok(2, { ...snapshot('signed_out', 2, 3), environment: 'production' }))
  await signingOut
  f.controls[0].resolve(ok(1, snapshot('selecting_device', 1, 2)))
  await signingIn
  assert.equal(f.controller.state.snapshot.subject, null)
  assert.equal(f.controller.state.snapshot.generation, 2)
  f.controller.dispose()
})
