'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')

const source = fs.readFileSync(path.join(__dirname, '..', 'provisioning', 'renderer.js'), 'utf8')
const flush = () => new Promise(resolve => setImmediate(resolve))
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
class FakeElement {
  constructor() {
    this.disabled = false; this.hidden = false; this.textContent = ''; this.value = ''; this.checked = false; this.tabIndex = 0
    this.listeners = new Map(); this.children = []
    const classes = new Set()
    this.classList = {
      toggle(name, force) {
        if (force === true) classes.add(name)
        else if (force === false) classes.delete(name)
        else if (classes.has(name)) classes.delete(name)
        else classes.add(name)
      },
      contains: name => classes.has(name),
    }
  }
  addEventListener(name, listener) { this.listeners.set(name, listener) }
  replaceChildren(...children) { this.children = children }
  append(child) { this.children.push(child) }
  emit(name) { this.listeners.get(name)?.({ preventDefault() {} }) }
  click() { if (!this.disabled) this.emit('click') }
}
function fixture(overrides = {}) {
  const ids = ['result', 'scan', 'cancel-scan', 'disconnect', 'read-status', 'scan-networks',
    'submit-wifi', 'security', 'password', 'availability', 'legacy-row', 'test-build-warning',
    'secure-notice', 'candidates', 'device-actions', 'device-info', 'networks', 'wifi', 'ssid', 'legacy',
    'mode-description', 'physical-confirmation-panel', 'physical-confirmation', 'physical-code',
    'confirm-physical', 'wifi-panel', 'ownership-panel', 'confirm-ownership', 'v2-progress',
    'v2-state', 'v2-guidance', 'refresh-state']
  const elements = Object.fromEntries(ids.map(id => [id, new FakeElement()]))
  elements.security.value = 'wpa-personal'
  let snapshotListener = () => {}
  const api = {
    available: true, legacyAllowed: true, testBuild: true,
    onCandidates() {}, scan: async () => [], cancelScan: async () => {}, connect: async () => ({}),
    disconnect: async () => {}, status: async () => ({ state: 'idle' }), networks: async () => [],
    provision: async () => ({ state: 'connected' }), select: async () => {},
    begin: async () => ({}), refresh: async () => ({ state: 'waitingCloud' }),
    confirmPhysicalDevice: async () => ({ state: 'awaitingWifi' }),
    provideWifi: async () => ({ state: 'waitingCloud' }),
    confirmOwnership: async () => ({ state: 'completed' }), cancel: async () => {},
    onSnapshot(listener) { snapshotListener = listener }, ...overrides,
  }
  vm.runInNewContext(source, {
    window: { desktopProvisioning: api, confirm: () => true },
    document: { getElementById: id => elements[id], createElement: () => new FakeElement() },
    Promise, setTimeout, clearTimeout,
  })
  return { api, elements, emitSnapshot: snapshot => snapshotListener(snapshot) }
}

test('cancel scan preempts the busy scan and stale rejection cannot overwrite the result', async () => {
  const scan = deferred()
  let cancelled = 0
  const f = fixture({
    scan: () => scan.promise,
    cancelScan: async () => {
      cancelled++
      const error = new Error('PROVISIONING_SCAN_CANCELLED'); error.code = 'PROVISIONING_SCAN_CANCELLED'
      scan.reject(error)
      await scan.promise.catch(() => {})
    },
  })
  f.elements.scan.click()
  assert.equal(f.elements['cancel-scan'].disabled, false)
  assert.equal(f.elements.disconnect.disabled, false)
  f.elements['cancel-scan'].click()
  await flush(); await flush()
  assert.equal(cancelled, 1)
  assert.equal(f.elements.result.textContent, '已取消扫描。')
  assert.equal(f.elements.scan.disabled, false)
  assert.equal(f.elements['cancel-scan'].disabled, true)
})

test('disconnect preempts a pending GATT connection instead of being swallowed by busy', async () => {
  const connecting = deferred()
  let disconnected = 0
  const f = fixture({
    scan: async () => [{ transportId: 'device-1' }],
    connect: () => connecting.promise,
    disconnect: async () => {
      disconnected++
      connecting.reject(new Error('PROVISIONING_DISCONNECTED'))
      await connecting.promise.catch(() => {})
    },
  })
  f.elements.scan.click()
  await flush()
  assert.equal(f.elements['cancel-scan'].disabled, true, 'scan cancellation ends before GATT connect')
  assert.equal(f.elements.disconnect.disabled, false, 'disconnect remains available while connecting')
  f.elements.disconnect.click()
  await flush(); await flush()
  assert.equal(disconnected, 1)
  assert.equal(f.elements.result.textContent, '已断开蓝牙连接。')
  assert.equal(f.elements['device-actions'].hidden, true)
  assert.equal(f.elements['device-info'].hidden, true)
  assert.equal(f.elements.scan.disabled, false)
})

test('unavailable Web Bluetooth keeps scan disabled after control initialization', () => {
  const f = fixture({ available: false })
  assert.equal(f.elements.scan.disabled, true)
  assert.match(f.elements.availability.textContent, /没有可用的 Web Bluetooth/)
})

test('test-only warning and plaintext control stay hidden in a production build', () => {
  const production = fixture({ legacyAllowed: false, testBuild: false })
  assert.equal(production.elements['test-build-warning'].hidden, true)
  assert.equal(production.elements['legacy-row'].hidden, true)
  assert.equal(production.elements.legacy.disabled, true)
  assert.equal(production.elements.legacy.tabIndex, -1)
  assert.equal(production.elements['scan-networks'].hidden, true)
  assert.equal(production.elements['scan-networks'].disabled, true)
  assert.equal(production.elements['scan-networks'].tabIndex, -1)
  assert.equal(production.elements['cancel-scan'].hidden, true)
  assert.equal(production.elements['v2-progress'].hidden, false)

  const testBuild = fixture({ legacyAllowed: true, testBuild: true })
  assert.equal(testBuild.elements['test-build-warning'].hidden, false)
  assert.equal(testBuild.elements['legacy-row'].hidden, false)
  assert.equal(testBuild.elements.legacy.disabled, false)
  assert.equal(testBuild.elements['scan-networks'].hidden, false)
  assert.equal(testBuild.elements['v2-progress'].hidden, true)
  assert.match(testBuild.elements['secure-notice'].textContent, /Wi-Fi 密码不会被端到端加密/)
})

test('formal flow confirms the physical code and renders only allowlisted preview fields', async () => {
  let confirmed
  let selected
  const f = fixture({
    testBuild: false, legacyAllowed: false,
    scan: async () => [{ candidateId: 'opaque-candidate-1', name: 'CentaurOS-Setup-123456', shortCode: '123456' }],
    select: async candidateId => { selected = candidateId },
    begin: async () => ({ preview: {
      name: 'CentaurOS-Setup-123456', shortCode: '123456', publicKeyFingerprint: 'safe-fingerprint',
      reason: 'RAW_DEVICE_REASON_MUST_NOT_RENDER', secret: 'RAW_SECRET_MUST_NOT_RENDER',
    } }),
    confirmPhysicalDevice: async code => { confirmed = code; return { state: 'awaitingWifi' } },
  })

  f.elements.scan.click()
  await flush(); await flush()
  assert.equal(f.elements.candidates.children.length, 1)
  assert.doesNotMatch(f.elements.candidates.children[0].children[0].textContent, /opaque-candidate-1/)
  f.elements.candidates.children[0].children[0].click()
  await flush(); await flush()
  assert.equal(selected, 'opaque-candidate-1')
  assert.equal(f.elements['physical-confirmation-panel'].hidden, false)
  assert.equal(f.elements['wifi-panel'].hidden, true)
  assert.match(f.elements['device-info'].textContent, /CentaurOS-Setup-123456/)
  assert.doesNotMatch(f.elements['device-info'].textContent, /RAW_DEVICE_REASON|RAW_SECRET/)
  assert.equal(f.elements['confirm-physical'].disabled, true)

  f.elements['physical-code'].value = '123456'
  f.elements['physical-code'].emit('input')
  assert.equal(f.elements['confirm-physical'].disabled, false)
  f.elements['physical-confirmation'].emit('submit')
  assert.equal(f.elements['physical-code'].value, '', 'physical confirmation code clears before awaiting IPC')
  await flush(); await flush()
  assert.equal(confirmed, '123456')
  assert.equal(f.elements['physical-confirmation-panel'].hidden, true)
  assert.equal(f.elements['wifi-panel'].hidden, false)
  assert.equal(f.elements['v2-state'].textContent, '可以设置 Wi-Fi')
})

test('formal Wi-Fi password clears synchronously and snapshots drive ownership through completion', async () => {
  const sent = deferred()
  let credentials
  const f = fixture({
    testBuild: false, legacyAllowed: false,
    provideWifi: value => { credentials = value; return sent.promise },
  })
  f.emitSnapshot({ state: 'awaitingWifi', reason: 'RAW_WIFI_REASON' })
  f.elements.ssid.value = 'Private Wi-Fi'
  f.elements.password.value = 'Synthetic-password-1'
  f.elements.wifi.emit('submit')
  assert.equal(f.elements.password.value, '', 'password must clear before provideWifi settles')
  assert.equal(credentials.password, 'Synthetic-password-1')
  sent.resolve({ state: 'awaitingOwnershipConfirmation', reason: 'RAW_OWNERSHIP_REASON' })
  await flush(); await flush()
  assert.equal(credentials.password, '', 'temporary credentials are scrubbed after IPC settles')
  assert.equal(f.elements['wifi-panel'].hidden, true)
  assert.equal(f.elements['ownership-panel'].hidden, false)
  assert.doesNotMatch(f.elements.result.textContent + f.elements['v2-guidance'].textContent, /RAW_WIFI_REASON|RAW_OWNERSHIP_REASON/)

  f.elements['confirm-ownership'].click()
  await flush(); await flush()
  assert.equal(f.elements['ownership-panel'].hidden, true)
  assert.equal(f.elements['v2-state'].textContent, '盒子已添加')
  assert.equal(f.elements['v2-progress'].classList.contains('completed'), true)
})

test('formal snapshots and rejected operations never render raw reasons or unknown error messages', async () => {
  const f = fixture({
    testBuild: false, legacyAllowed: false,
    scan: async () => [{ candidateId: 'opaque-candidate-1', name: 'CentaurOS-Setup' }],
    begin: async () => { throw Object.assign(new Error('RAW_ERROR_MESSAGE'), { code: 'UNRECOGNIZED_RAW_CODE', reason: 'RAW_REASON' }) },
  })
  f.emitSnapshot({ state: 'attentionRequired', attentionCode: 'UNKNOWN_ATTENTION', reason: 'RAW_SNAPSHOT_REASON' })
  assert.equal(f.elements['v2-state'].textContent, '需要处理后继续')
  assert.doesNotMatch(f.elements['v2-guidance'].textContent, /UNKNOWN_ATTENTION|RAW_SNAPSHOT_REASON/)

  f.elements.scan.click()
  await flush(); await flush()
  f.elements.candidates.children[0].children[0].click()
  await flush(); await flush()
  assert.equal(f.elements.result.textContent, '配网操作未完成，请重试。')
  assert.doesNotMatch(f.elements.result.textContent, /RAW_ERROR_MESSAGE|UNRECOGNIZED_RAW_CODE|RAW_REASON/)
})

test('formal refresh consumes only the safe snapshot state and attention code', async () => {
  let refreshed = 0
  const f = fixture({
    testBuild: false, legacyAllowed: false,
    refresh: async () => { refreshed++; return { state: 'waitingCloud', reason: 'RAW_REFRESH_REASON' } },
  })
  f.emitSnapshot({ state: 'authenticating' })
  assert.equal(f.elements['v2-state'].textContent, '正在认证盒子')
  f.emitSnapshot({ state: 'awaitingOwnershipConfirmation' })
  assert.equal(f.elements['v2-state'].textContent, '等待确认绑定')
  f.elements['refresh-state'].click()
  await flush(); await flush()
  assert.equal(refreshed, 1)
  assert.equal(f.elements['v2-state'].textContent, '正在等待盒子上线')
  assert.doesNotMatch(f.elements['v2-guidance'].textContent, /RAW_REFRESH_REASON/)

  f.emitSnapshot({ state: 'attentionRequired', attentionCode: 'PROVISIONING_WIFI_AUTHENTICATION_FAILED', reason: 'RAW_REASON' })
  assert.match(f.elements['v2-guidance'].textContent, /检查网络名称和密码/)
  assert.doesNotMatch(f.elements['v2-guidance'].textContent, /PROVISIONING_WIFI_AUTHENTICATION_FAILED|RAW_REASON/)
})

test('formal cancel preempts a pending coordinator begin without leaking its late failure', async () => {
  const beginning = deferred()
  let cancelled = 0
  const f = fixture({
    testBuild: false, legacyAllowed: false,
    scan: async () => [{ candidateId: 'opaque-candidate-1', name: 'CentaurOS-Setup' }],
    begin: () => beginning.promise,
    cancel: async () => {
      cancelled++
      beginning.reject(Object.assign(new Error('RAW_LATE_BEGIN_FAILURE'), { code: 'UNKNOWN_LATE_CODE' }))
      await beginning.promise.catch(() => {})
    },
  })
  f.elements.scan.click()
  await flush(); await flush()
  f.elements.candidates.children[0].children[0].click()
  await flush()
  assert.equal(f.elements.disconnect.disabled, false)
  f.elements.disconnect.click()
  await flush(); await flush()
  assert.equal(cancelled, 1)
  assert.equal(f.elements.result.textContent, '已取消本次配网。')
  assert.doesNotMatch(f.elements.result.textContent, /RAW_LATE_BEGIN_FAILURE|UNKNOWN_LATE_CODE/)
  assert.equal(f.elements['v2-state'].textContent, '准备开始')
})

test('legacy status and provisioning failures do not expose device reasons', async () => {
  const f = fixture({
    status: async () => ({ state: 'unknown-state', reason: 'RAW_STATUS_REASON' }),
    scan: async () => [{ transportId: 'device-1' }],
    connect: async () => ({ name: 'CentaurOS-Setup-123456' }),
    provision: async () => ({ state: 'failed', reason: 'RAW_PROVISION_REASON', code: 'UNKNOWN_DEVICE_CODE' }),
  })
  f.elements.scan.click()
  await flush(); await flush()
  f.elements['read-status'].click()
  await flush(); await flush()
  assert.equal(f.elements.result.textContent, '盒子状态：状态待确认')
  assert.doesNotMatch(f.elements.result.textContent, /RAW_STATUS_REASON/)

  f.elements.ssid.value = 'Test Wi-Fi'
  f.elements.password.value = 'Synthetic-password-1'
  f.elements.wifi.emit('submit')
  assert.equal(f.elements.password.value, '')
  await flush(); await flush()
  assert.equal(f.elements.result.textContent, '盒子未能完成操作，请检查状态后重试。')
  assert.doesNotMatch(f.elements.result.textContent, /RAW_PROVISION_REASON|UNKNOWN_DEVICE_CODE/)
})
