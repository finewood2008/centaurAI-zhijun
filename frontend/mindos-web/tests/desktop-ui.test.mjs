import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import { DesktopController } from '../src/desktop/controller.ts'
import { connectedDeviceLabel } from '../src/desktop/deviceDisplay.ts'

const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const tick = () => new Promise(resolve => setImmediate(resolve))
const snapshot = (phase, generation, sequence, accountId = 'synthetic-account', deviceId = 'synthetic-box', provisioning = false) => ({
  protocolVersion: 1, environment: 'simulation', phase, generation, sequence,
  subject: phase === 'signed_out' ? null : phase === 'ready' ? { accountId, deviceId } : { accountId },
  capabilities: { materialsRead: phase === 'ready', streamChat: false, uploads: false, matters: false, provisioning },
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
    beginSignIn: invoke('beginSignIn'), signInWithPassword: invoke('signInWithPassword'), signInWithSavedPassword: invoke('signInWithSavedPassword'),
    sendRegistrationCode: invoke('sendRegistrationCode'), resetPassword: invoke('resetPassword'),
    registerWithPassword: invoke('registerWithPassword'), claimDevice: invoke('claimDevice'),
    openProvisioning: invoke('openProvisioning'),
    getRememberedLogin: async context => ok(context.expectedGeneration, null),
    connect: invoke('connect'), disconnect: invoke('disconnect'), signOut: invoke('signOut'),
    listDevices: context => { const result = deferred(); devices.push({ context, ...result }); return result.promise },
    materials: { list: (context, query) => { const result = deferred(); reads.push({ context, query, ...result }); return result.promise } },
    cancelRead: async (context, targetCallId) => { cancellations.push({ context, targetCallId }); return ok(context.expectedGeneration, { delivery: 'suppressed', remoteCancellation: 'not_supported' }) },
  }
  const controller = new DesktopController(bridge)
  return { controller, bridge, reads, devices, controls, cancellations, emit: next => listener?.(next), get unsubscribed() { return unsubscribed } }
}

async function connectionComponentFixture(savedLoginError, resetError) {
  const source = await readFile(new URL('../src/desktop/DesktopConnection.vue', import.meta.url), 'utf8')
  const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'desktop-connection-login-test' }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const signedOut = { ...snapshot('signed_out', 0, 0), environment: 'production', subject: null }
  const viewState = Vue.shallowRef({ snapshot: signedOut, hostAvailable: true, devices: [], devicesLoading: false,
    page: null, query: { limit: 20, offset: 0 }, loading: false, controlPending: false,
    pendingOperation: null, error: null, notice: '' })
  const controls = []
  const controller = {
    async control(operation, input) {
      controls.push({ operation, input: input && typeof input === 'object' ? { ...input } : input })
      if (operation === 'signInWithSavedPassword') {
        viewState.value = { ...viewState.value, snapshot: { ...signedOut, phase: 'authenticating', generation: 1, sequence: 1 }, error: null }
        await Vue.nextTick()
        const error = { code: savedLoginError, message: '合成公开错误', recovery: 'user_sign_in' }
        viewState.value = { ...viewState.value,
          snapshot: { ...signedOut, phase: 'failed', generation: 1, sequence: 2, error }, error }
        await Vue.nextTick()
      } else if (operation === 'signInWithPassword') {
        viewState.value = { ...viewState.value,
          snapshot: { ...snapshot('selecting_device', 2, 3), environment: 'production' }, error: null }
        await Vue.nextTick()
      }
    },
    getRememberedLogin: async () => ({ phone: '13800000000', passwordSaved: true }),
    sendRegistrationCode: async phone => { controls.push({ operation: 'sendRegistrationCode', input: phone }); return 300 },
    resetPassword: async credentials => {
      controls.push({ operation: 'resetPassword', input: { ...credentials } })
      if (resetError) {
        viewState.value = { ...viewState.value, error: resetError }
        return false
      }
      viewState.value = { ...viewState.value, notice: '密码重置请求已处理，请使用新密码登录' }
      return true
    },
    claimDevice: async () => false,
    openProvisioning: async () => false,
    loadDevices: async () => {},
  }
  const cleanups = []
  const exports = {}
  new Function('require', 'exports', code)(id => {
    if (id === 'vue') return { ...Vue, onBeforeUnmount: callback => cleanups.push(callback) }
    if (id === 'vue-router') return { useRoute: () => ({ meta: {} }) }
    if (id.endsWith('/claimToken') || id === './claimToken') {
      return { isValidClaimToken: value => /^\d{6}$/.test(value), normalizeClaimToken: value => value.trim() }
    }
    if (id.endsWith('/workspace') || id === './workspace') return { useDesktopWorkspace: () => ({ controller, state: viewState }) }
    if (id === './SecureConnectionProgress.vue') return { default: {} }
    throw new Error(`unexpected DesktopConnection import: ${id}`)
  }, exports)
  const scope = Vue.effectScope()
  const ui = scope.run(() => exports.default.setup({ embedded: false }, { expose() {} }))
  await tick()
  await Vue.nextTick()
  return { source, ui, controls, viewState, close() { cleanups.forEach(callback => callback()); scope.stop() } }
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

for (const phase of ['connecting', 'authorizing']) {
  test(`cancel interrupts pending connect during ${phase} and ignores its late success`, async () => {
    const f = fixture(snapshot('selecting_device', 1, 1))
    await f.controller.start()
    const connecting = f.controller.control('connect', 'synthetic-box')
    f.emit(snapshot(phase, 2, 2))
    assert.equal(f.controller.state.controlPending, true)
    const cancelling = f.controller.control('disconnect')
    assert.equal(f.controls.length, 2)
    assert.equal(f.controls[1].operation, 'disconnect')
    assert.equal(f.controls[1].context.expectedGeneration, 2)
    await f.controller.control('disconnect')
    assert.equal(f.controls.length, 2, 'duplicate cancellation is suppressed')
    f.emit(snapshot('selecting_device', 3, 3))
    f.controls[1].resolve(ok(3, snapshot('selecting_device', 3, 3)))
    await cancelling
    f.emit(snapshot('ready', 2, 9))
    assert.equal(f.controller.state.snapshot.phase, 'selecting_device', 'late subscription cannot restore an old generation')
    f.controls[0].resolve(ok(2, snapshot('ready', 2, 4)))
    await connecting
    assert.equal(f.controller.state.snapshot.phase, 'selecting_device')
    assert.equal(f.controller.state.snapshot.generation, 3)
    assert.equal(f.controller.state.controlPending, false)
    assert.equal(f.reads.length, 0, 'late connection cannot load business data')
    f.controller.dispose()
  })
}

test('cancelled connection failures cannot replace the new selection state', async () => {
  for (const rejected of [false, true]) {
    const f = fixture(snapshot('selecting_device', 1, 1))
    await f.controller.start()
    const connecting = f.controller.control('connect', 'synthetic-box')
    f.emit(snapshot('connecting', 2, 2))
    const cancelling = f.controller.control('disconnect')
    f.emit(snapshot('selecting_device', 3, 3))
    f.controls[1].resolve(ok(3, snapshot('selecting_device', 3, 3)))
    await cancelling
    if (rejected) f.controls[0].reject(new Error('synthetic late transport failure'))
    else f.controls[0].resolve({ ok: false, generation: 2, error: { code: 'REMOTE_ERROR', message: 'synthetic error', recovery: 'user_reconnect' } })
    await connecting
    assert.equal(f.controller.state.error, null)
    assert.equal(f.controller.state.snapshot.phase, 'selecting_device')
    assert.equal(f.controller.state.controlPending, false)
    f.controller.dispose()
  }
})

test('disconnect does not interrupt sign-out or password sign-in', async () => {
  for (const operation of ['signOut', 'signInWithPassword']) {
    const f = fixture(snapshot('signed_out', 0, 0))
    await f.controller.start()
    const pending = f.controller.control(operation, { phone: '13800000000', password: 'synthetic', rememberPassword: false })
    await f.controller.control('disconnect')
    assert.equal(f.controls.length, 1)
    f.controls[0].resolve(ok(1, snapshot('signed_out', 1, 1)))
    await pending
    f.controller.dispose()
  }
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
  const credentials = { phone: '13800000000', password: 'synthetic-password', rememberPassword: true }
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

test('registration and device claim use narrow IPC calls and refresh claimed devices', async () => {
  const f = fixture({ ...snapshot('signed_out', 0, 0), environment: 'production' })
  await f.controller.start()
  const sending = f.controller.sendRegistrationCode('13800000000')
  assert.equal(f.controls[0].operation, 'sendRegistrationCode')
  f.controls[0].resolve(ok(0, { expiresIn: 300 }))
  assert.equal(await sending, 300)
  const credentials = { phone: '13800000000', password: 'Synthetic-password-1', code: '123456', rememberPassword: true }
  const registering = f.controller.control('registerWithPassword', credentials)
  assert.equal(f.controls[1].operation, 'registerWithPassword')
  assert.equal(JSON.stringify(f.controller.state).includes(credentials.password), false)
  f.controls[1].resolve(ok(1, { ...snapshot('selecting_device', 1, 2), environment: 'production' }))
  await registering
  assert.equal(f.devices.length, 1)
  f.devices[0].resolve(ok(1, []))
  await tick()
  const claiming = f.controller.claimDevice('123456')
  assert.equal(f.controls[2].operation, 'claimDevice')
  f.controls[2].resolve(ok(1, { deviceId: 'device-claimed-1', displayName: '新盒子', availability: 'unknown' }))
  await tick()
  assert.equal(f.devices.length, 2)
  f.devices[1].resolve(ok(1, [{ deviceId: 'device-claimed-1', displayName: '新盒子', availability: 'online' }]))
  assert.equal(await claiming, true)
  assert.equal(f.controller.state.devices[0].displayName, '新盒子')
  assert.match(f.controller.state.notice, /已认领盒子/)
  f.controller.dispose()
})

test('password reset uses an account-opaque narrow call and never authenticates automatically', async () => {
  const f = fixture({ ...snapshot('signed_out', 0, 0), environment: 'production' })
  await f.controller.start()
  const credentials = { phone: '13800000000', code: '123456', password: 'Synthetic-reset-password-1' }
  const resetting = f.controller.resetPassword(credentials)
  assert.equal(f.controls.length, 1)
  assert.equal(f.controls[0].operation, 'resetPassword')
  assert.deepEqual(f.controls[0].deviceId, credentials)
  assert.equal(f.controller.state.pendingOperation, 'resetPassword')
  assert.equal(JSON.stringify(f.controller.state).includes(credentials.password), false)
  f.controls[0].resolve(ok(0, { processed: true }))
  assert.equal(await resetting, true)
  assert.equal(f.controller.state.snapshot.phase, 'signed_out')
  assert.equal(f.controller.state.controlPending, false)
  assert.equal(f.controller.state.notice, '密码重置请求已处理，请使用新密码登录')
  assert.equal(f.controls.some(call => call.operation.startsWith('signIn')), false)
  f.controller.dispose()
})

test('password reset UI reuses SMS countdown, clears secrets and returns to manual login', async () => {
  const f = await connectionComponentFixture('ACCOUNT_SERVICE_UNAVAILABLE')
  f.ui.setAuthMode('reset')
  f.ui.phone.value = '13800000000'
  await f.ui.sendRegistrationCode()
  assert.deepEqual(f.controls[0], { operation: 'sendRegistrationCode', input: '13800000000' })
  assert.equal(f.ui.codeSeconds.value, 60)
  f.ui.registrationCode.value = '123456'
  f.ui.password.value = 'Synthetic-reset-password-2'
  f.ui.confirmPassword.value = 'Synthetic-reset-password-2'
  await f.ui.resetPassword()
  assert.deepEqual(f.controls[1], { operation: 'resetPassword', input: {
    phone: '13800000000', code: '123456', password: 'Synthetic-reset-password-2',
  } })
  assert.equal(f.ui.authMode.value, 'login')
  assert.equal(f.ui.registrationCode.value, '')
  assert.equal(f.ui.password.value, '')
  assert.equal(f.ui.confirmPassword.value, '')
  assert.equal(f.ui.savedPasswordUsable.value, false)
  assert.equal(f.controls.some(call => call.operation.startsWith('signIn')), false)
  assert.equal(f.viewState.value.notice, '密码重置请求已处理，请使用新密码登录')
  f.close()
})

test('password reset UI preserves a usable code on service failure and clears an invalid code', async () => {
  for (const [errorCode, expectedCode] of [
    ['ACCOUNT_SERVICE_UNAVAILABLE', '123456'],
    ['VERIFICATION_CODE_INVALID', ''],
  ]) {
    const error = { code: errorCode, message: '合成公开错误', recovery: 'user_read' }
    const f = await connectionComponentFixture('AUTHENTICATION_FAILED', error)
    f.ui.setAuthMode('reset')
    f.ui.phone.value = '13800000000'
    f.ui.registrationCode.value = '123456'
    f.ui.password.value = 'Synthetic-reset-password-2'
    f.ui.confirmPassword.value = 'Synthetic-reset-password-2'

    await f.ui.resetPassword()

    assert.equal(f.ui.authMode.value, 'reset')
    assert.equal(f.ui.registrationCode.value, expectedCode)
    assert.equal(f.ui.password.value, '')
    assert.equal(f.ui.confirmPassword.value, '')
    assert.equal(f.viewState.value.error.code, errorCode)
    f.close()
  }
})

test('password reset UI has explicit fields and an account-opaque success contract', async () => {
  const source = await readFile(new URL('../src/desktop/DesktopConnection.vue', import.meta.url), 'utf8')
  for (const testId of ['show-reset-password', 'password-reset', 'reset-phone', 'reset-code',
    'send-reset-code', 'reset-password', 'reset-confirm-password', 'reset-password-submit']) {
    assert.match(source, new RegExp(`data-testid="${testId}"`))
  }
  assert.match(source, /提交后的提示不会透露该手机号是否已注册/)
})

test('provisioning entry is capability gated and opens only the isolated window with a context', async () => {
  const disabled = fixture(snapshot('selecting_device', 1, 1))
  await disabled.controller.start()
  assert.equal(await disabled.controller.openProvisioning(), false)
  assert.equal(disabled.controls.length, 0)
  disabled.controller.dispose()

  const enabled = fixture(snapshot('selecting_device', 2, 1, 'synthetic-account', 'synthetic-box', true))
  await enabled.controller.start()
  const opening = enabled.controller.openProvisioning()
  assert.equal(enabled.controls.length, 1)
  assert.equal(enabled.controls[0].operation, 'openProvisioning')
  assert.deepEqual(Object.keys(enabled.controls[0].context).sort(), ['callId', 'expectedGeneration'])
  assert.equal(enabled.controls[0].deviceId, undefined)
  assert.equal(enabled.controller.state.pendingOperation, 'openProvisioning')
  assert.equal(JSON.stringify(enabled.controller.state).includes('password'), false)
  enabled.controls[0].resolve(ok(2, { opened: true }))
  assert.equal(await opening, true)
  assert.equal(enabled.controller.state.controlPending, false)
  assert.match(enabled.controller.state.notice, /已打开盒子配网窗口/)
  enabled.controller.dispose()
})

test('main connection UI exposes a capability-gated provisioning action and no Wi-Fi password field', async () => {
  const source = await readFile(new URL('../src/desktop/DesktopConnection.vue', import.meta.url), 'utf8')
  assert.match(source, /snapshot\.capabilities\.provisioning/)
  assert.match(source, /data-testid="open-provisioning"/)
  assert.match(source, /controller\.openProvisioning\(\)/)
  assert.doesNotMatch(source, /type="password"[^>]*(?:wifi|ssid)|(?:wifi|ssid)[^>]*type="password"/i)
})

test('connection status renders only the safe Direct or relay path labels', async () => {
  const topbar = await readFile(new URL('../src/desktop/DesktopTopbar.vue', import.meta.url), 'utf8')
  const connection = await readFile(new URL('../src/desktop/DesktopConnection.vue', import.meta.url), 'utf8')
  assert.match(topbar, /selectedPath === 'DIRECT' \? '直连'/)
  assert.match(topbar, /selectedPath === 'RELAY' \? '安全中继'/)
  assert.match(topbar, /data-testid="connection-path"/)
  assert.match(connection, /selectedPath === 'DIRECT' \? '直连'/)
  assert.match(connection, /selectedPath === 'RELAY' \? '安全中继'/)
  for (const source of [topbar, connection]) {
    assert.doesNotMatch(source, /iceServers|candidate|failedSessionId|fallback_from_session_id/)
  }
})

test('remembered login returns only metadata and never silently signs in or stores credentials in controller state', async () => {
  const f = fixture({ ...snapshot('signed_out', 0, 0), environment: 'production' })
  const remembered = { phone: '13800000000', passwordSaved: true }
  const contexts = []
  f.bridge.getRememberedLogin = async context => { contexts.push(context); return ok(context.expectedGeneration, remembered) }
  await f.controller.start()
  assert.deepEqual(await f.controller.getRememberedLogin(), remembered)
  assert.ok(contexts.every(context => context.expectedGeneration === 0))
  assert.equal(JSON.stringify(f.controller.state).includes(remembered.phone), false)
  assert.equal(f.controls.length, 0)
  f.emit({ ...snapshot('failed', 1, 1), environment: 'production', subject: null,
    error: { code: 'SESSION_EXPIRED', message: '登录会话已过期，请重新登录。', recovery: 'user_sign_in' } })
  await tick()
  assert.equal(f.controls.length, 0, 'expiry must not use the saved password without a login action')
  assert.equal(f.controller.state.snapshot.subject, null)
  f.controller.dispose()
})

test('connection failure keeps account context and never starts password login or logout in the renderer', async () => {
  const f = fixture({ ...snapshot('ready', 1, 1), environment: 'production' })
  await f.controller.start()
  f.emit({ ...snapshot('failed', 2, 2), environment: 'production',
    error: { code: 'CONNECTIVITY_SESSION_EXPIRED', message: '盒子连接已失效，请重新连接盒子。', recovery: 'user_reconnect' } })
  await tick()
  assert.equal(f.controller.state.snapshot.subject.accountId, 'synthetic-account')
  assert.equal(f.controller.state.error.recovery, 'user_reconnect')
  assert.equal(f.controls.length, 0, 'only the trusted runtime manages connection recovery; no renderer login or logout')
  assert.equal(f.controller.state.page, null, 'stale business data must be removed while disconnected')
  f.controller.dispose()
})

test('remembered login metadata cannot return after a scope change or disposal', async () => {
  for (const dispose of [false, true]) {
    const f = fixture({ ...snapshot('signed_out', 0, 0), environment: 'production' })
    await f.controller.start()
    const pending = deferred()
    f.bridge.getRememberedLogin = () => pending.promise
    const reading = f.controller.getRememberedLogin()
    if (dispose) f.controller.dispose()
    else f.emit(snapshot('selecting_device', 1, 1, 'other-synthetic-account'))
    pending.resolve(ok(0, { phone: '13800000000', passwordSaved: true }))
    assert.equal(await reading, null)
    assert.equal(JSON.stringify(f.controller.state).includes('13800000000'), false)
    f.controller.dispose()
  }
})

test('saved-password login forwards only the remember flag and sign-out wins over its late result', async () => {
  const f = fixture({ ...snapshot('signed_out', 0, 0), environment: 'production' })
  await f.controller.start()
  const signingIn = f.controller.control('signInWithSavedPassword', false)
  assert.equal(f.controls[0].operation, 'signInWithSavedPassword')
  assert.equal(f.controls[0].deviceId, false)
  assert.equal(f.controller.state.pendingOperation, 'signInWithSavedPassword')
  f.emit({ ...snapshot('authenticating', 1, 1), environment: 'production', subject: null })
  const signingOut = f.controller.control('signOut')
  f.controls[1].resolve(ok(2, { ...snapshot('signed_out', 2, 3), environment: 'production' }))
  await signingOut
  f.controls[0].resolve(ok(1, snapshot('selecting_device', 1, 2)))
  await signingIn
  assert.equal(f.controller.state.snapshot.subject, null)
  assert.equal(f.controller.state.controlPending, false)
  assert.equal(f.devices.length, 0)
  f.controller.dispose()
})

test('rejected saved credentials disable blank login and require a manually entered password', async () => {
  for (const code of ['SAVED_CREDENTIAL_REJECTED', 'AUTHENTICATION_FAILED', 'AUTHENTICATION_REQUIRED']) {
    const f = await connectionComponentFixture(code)
    assert.equal(f.ui.phone.value, '13800000000')
    assert.equal(f.ui.savedPasswordUsable.value, true)
    assert.equal(f.ui.passwordRequired.value, false)
    assert.match(f.ui.passwordPlaceholder.value, /留空即可登录/)

    await f.ui.signIn()
    assert.deepEqual(f.controls[0], { operation: 'signInWithSavedPassword', input: true })
    assert.equal(f.ui.savedPasswordUsable.value, false)
    assert.equal(f.ui.passwordRequired.value, true)
    assert.equal(f.ui.passwordPlaceholder.value, '')
    assert.match(f.ui.savedCredentialMessage.value, /重新输入当前密码/)

    const replacement = `manual-password-${code}`
    f.ui.password.value = replacement
    await f.ui.signIn()
    assert.deepEqual(f.controls[1], { operation: 'signInWithPassword', input: {
      phone: '13800000000', password: replacement, rememberPassword: true,
    } })
    assert.equal(f.ui.password.value, '')
    assert.equal(JSON.stringify({ password: f.ui.password.value, notice: f.ui.savedCredentialMessage.value }).includes(replacement), false)
    f.close()
  }
})

test('a transient saved-login failure does not incorrectly discard a still-valid saved password', async () => {
  const f = await connectionComponentFixture('ACCOUNT_SERVICE_UNAVAILABLE')
  await f.ui.signIn()
  assert.equal(f.ui.savedPasswordUsable.value, true)
  assert.equal(f.ui.passwordRequired.value, false)
  assert.match(f.ui.passwordPlaceholder.value, /留空即可登录/)
  assert.equal(f.ui.savedCredentialMessage.value, '')
  f.close()
})
