// 阶段 3（WP M）Electron 安全窗口骨架合同测试。
//
// 覆盖：Feature Gate 默认全关（fail-closed）、ProvisioningCryptoProvider 未绑定
// 调用即抛错、setup ViewState 派生与可恢复性、typed IPC 通道常量、SecureStore
// 加密不可用时拒绝生成 Client Key（无明文回退）与加密落盘/读回一致。
//
// 运行：node --test provisioning-skeleton.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'
import os from 'node:os'
import path from 'node:path'
import fs from 'node:fs/promises'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const { FEATURE_GATES, isEnabled, enabledGates } = require('./feature-gates.js')
const { ProvisioningCryptoProvider } = require('./provisioning-crypto-provider.js')
const { SETUP_STAGES, deriveElectronViewState, canResume } = require('./setup-view-state.js')
const { IPC } = require('./ipc-channels.js')
const { SecureStore } = require('./secure-store.js')

test('Feature Gate 默认全关，未知开关 fail-closed', () => {
  assert.equal(isEnabled('electronWebBluetoothDiscoveryV1'), false)
  assert.equal(isEnabled('electronBleProvisioningV2'), false)
  assert.equal(isEnabled('localDeviceAddSkeleton'), false)
  assert.equal(isEnabled('unknown-gate'), false)
  assert.deepEqual(enabledGates(), [])
  assert.equal(Object.keys(FEATURE_GATES).length, 3)
})

test('ProvisioningCryptoProvider 未绑定调用一律抛错', async () => {
  const provider = new ProvisioningCryptoProvider()
  assert.equal(provider.isBound, false)
  await assert.rejects(provider.createClientKeyPair(), /未接入/)
  await assert.rejects(provider.signChallenge('challenge', {}), /未接入/)
  await assert.rejects(provider.verifyDeviceProof('proof', {}), /未接入/)
  await assert.rejects(provider.deriveRootSecret('challenge', {}), /未接入/)
})

test('ProvisioningCryptoProvider 绑定后可用且禁止热替换', async () => {
  const provider = new ProvisioningCryptoProvider()
  const impl = {
    createClientKeyPair: async () => ({ publicKeyPem: 'pub', privateKeyPem: 'priv' }),
    signChallenge: async () => 'sig',
    verifyDeviceProof: async () => true,
    deriveRootSecret: async () => 'root-secret',
  }
  provider.bind(impl)
  assert.equal(provider.isBound, true)
  assert.deepEqual(await provider.createClientKeyPair(), { publicKeyPem: 'pub', privateKeyPem: 'priv' })
  assert.equal(await provider.signChallenge('c', {}), 'sig')
  assert.equal(await provider.verifyDeviceProof('p', {}), true)
  assert.equal(await provider.deriveRootSecret('c', {}), 'root-secret')
  assert.throws(() => provider.bind(impl), /禁止热替换/)
})

test('ProvisioningCryptoProvider 绑定缺方法即拒绝', () => {
  const provider = new ProvisioningCryptoProvider()
  assert.throws(() => provider.bind({}), /缺少方法/)
})

test('setup ViewState：无快照时全不可交互（idle）', () => {
  const view = deriveElectronViewState(null)
  assert.equal(view.stage, SETUP_STAGES.idle)
  assert.equal(view.canStart, false)
  assert.equal(view.canSelectCandidate, false)
  assert.equal(view.canCancel, false)
  assert.equal(view.inProgress, false)
  assert.equal(view.candidateCount, 0)
})

test('setup ViewState：候选选择阶段派生', () => {
  const view = deriveElectronViewState({
    stage: SETUP_STAGES.candidate_selection,
    candidates: [{ id: 'a' }, { id: 'b' }],
    selectedCandidate: { id: 'a' },
  })
  assert.equal(view.stage, SETUP_STAGES.candidate_selection)
  assert.equal(view.canStart, false)
  assert.equal(view.canSelectCandidate, true)
  assert.equal(view.canCancel, true)
  assert.equal(view.inProgress, true)
  assert.equal(view.candidateCount, 2)
  assert.equal(view.selectedCandidate.id, 'a')
})

test('setup ViewState：未知 stage 回退 idle', () => {
  const view = deriveElectronViewState({ stage: 'not-a-stage', candidates: 'x' })
  assert.equal(view.stage, SETUP_STAGES.idle)
  assert.equal(view.candidateCount, 0)
})

test('canResume 仅失败/取消可恢复', () => {
  assert.equal(canResume(SETUP_STAGES.failed), true)
  assert.equal(canResume(SETUP_STAGES.cancelled), true)
  assert.equal(canResume(SETUP_STAGES.done), false)
  assert.equal(canResume(SETUP_STAGES.precheck), false)
})

test('typed IPC 通道常量完整', () => {
  assert.ok(IPC.connectivityTicket.length > 0)
  assert.ok(IPC.rpc.length > 0)
  for (const key of ['open', 'close', 'state', 'start', 'cancel', 'resume']) {
    assert.equal(typeof IPC.setup[key], 'string')
    assert.ok(IPC.setup[key].length > 0)
  }
})

function fakeSafeStorage() {
  const store = new Map()
  return {
    isEncryptionAvailable: () => true,
    encryptString: (text) => {
      const buf = Buffer.from(text, 'utf8')
      store.set(`k${store.size}`, buf)
      return buf
    },
    decryptString: (buf) => {
      for (const value of store.values()) {
        if (value.equals(buf)) return value.toString('utf8')
      }
      return null
    },
  }
}

test('SecureStore：加密不可用时拒绝生成 Client Key（无明文回退）', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'ss-fail-'))
  const store = new SecureStore({ userDataDir: dir, safeStorage: { isEncryptionAvailable: () => false } })
  await assert.rejects(store.getOrCreateClientKey(), /拒绝/)
})

test('SecureStore：加密生成 Client Key 并持久化读回一致', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'ss-ok-'))
  const safe = fakeSafeStorage()
  const store = new SecureStore({ userDataDir: dir, safeStorage: safe })
  const key = await store.getOrCreateClientKey()
  assert.match(key.publicKeyPem, /BEGIN PUBLIC KEY/)
  assert.match(key.privateKeyPem, /BEGIN PRIVATE KEY/)
  const store2 = new SecureStore({ userDataDir: dir, safeStorage: safe })
  const key2 = await store2.getOrCreateClientKey()
  assert.equal(key2.privateKeyPem, key.privateKeyPem)
  assert.equal(key2.publicKeyPem, key.publicKeyPem)
  store.clearMemory()
  assert.equal(store._clientKey, null)
})
