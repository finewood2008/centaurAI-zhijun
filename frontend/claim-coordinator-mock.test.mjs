// 阶段 3 Mock 流程状态机合同测试（WP L deterministic fake）。
//
// 覆盖：正常全流程（两候选→认证→Wi-Fi→App Proof→Owner/ACK→done）、与
// ClaimCoordinator 接口 bind 对接、非法转换拒绝、蓝牙关闭/权限拒绝/GATT 断线/
// ACK 丢失故障注入、取消与权威恢复、snapshot 与 deriveElectronViewState 对接。
//
// 运行：node --test claim-coordinator-mock.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const { MockClaimCoordinator, DEFAULT_CANDIDATES } = require('./claim-coordinator-mock.js')
const { ClaimCoordinator } = require('./claim-coordinator.js')
const { SETUP_STAGES, deriveElectronViewState } = require('./setup-view-state.js')

async function runHappyPath(coordinator) {
  const deviceInfo = { serviceUuid: '5d5e1a02-0002-4000-8000-000000000000', fw: 'mock-1.0' }
  const afterStart = await coordinator.start({ deviceInfo })
  assert.equal(afterStart.stage, SETUP_STAGES.candidate_selection)
  assert.equal(afterStart.candidates.length, 2)
  const afterSelect = await coordinator.selectCandidate('mock-ai-box-0002')
  assert.equal(afterSelect.stage, SETUP_STAGES.authentication)
  assert.equal(afterSelect.selectedCandidate.id, 'mock-ai-box-0002')
  const afterAuth = await coordinator.authenticate()
  assert.equal(afterAuth.stage, SETUP_STAGES.wifi)
  const afterWifi = await coordinator.provisionWifi('Centaur-5G', 'supersecret')
  assert.equal(afterWifi.stage, SETUP_STAGES.app_proof)
  assert.equal(afterWifi.wifiConfigured, true)
  const afterProof = await coordinator.appProof()
  assert.equal(afterProof.stage, SETUP_STAGES.owner_ack)
  const afterAck = await coordinator.acknowledge()
  assert.equal(afterAck.stage, SETUP_STAGES.done)
  return afterAck
}

test('Mock 全流程：预检→两候选→认证→Wi-Fi→App Proof→Owner/ACK→done', async () => {
  const coordinator = new MockClaimCoordinator()
  await runHappyPath(coordinator)
})

test('Mock 可作为 ClaimCoordinator 接口实现绑定', async () => {
  const coordinator = new ClaimCoordinator()
  coordinator.bind(new MockClaimCoordinator())
  assert.equal(coordinator.isBound, true)
  const snapshot = await runHappyPath(coordinator)
  assert.equal(snapshot.stage, SETUP_STAGES.done)
})

test('非法转换一律拒绝', async () => {
  const coordinator = new MockClaimCoordinator()
  await assert.rejects(coordinator.selectCandidate('mock-ai-box-0001'), /非法状态转换/)
  await assert.rejects(coordinator.resume(), /仅取消\/失败状态可恢复/)
  await runHappyPath(coordinator)
  await assert.rejects(coordinator.cancel('too-late'), /已完成认领，不可取消/)
})

test('蓝牙关闭：start 预检失败，resume 权威恢复', async () => {
  const coordinator = new MockClaimCoordinator({ bluetoothAvailable: false })
  const snap = await coordinator.start({ deviceInfo: { serviceUuid: 'x' } })
  assert.equal(snap.stage, SETUP_STAGES.failed)
  assert.equal(snap.error, 'bluetooth_unavailable')
  const resumed = await coordinator.resume()
  assert.equal(resumed.stage, SETUP_STAGES.candidate_selection)
  assert.equal(resumed.error, null)
  assert.equal(resumed.candidates.length, 2)
})

test('deviceInfo 缺失：预检失败', async () => {
  const coordinator = new MockClaimCoordinator()
  const snap = await coordinator.start({})
  assert.equal(snap.stage, SETUP_STAGES.failed)
  assert.equal(snap.error, 'device_info_missing')
})

test('chooser 取消/权限拒绝：selectCandidate 失败', async () => {
  const coordinator = new MockClaimCoordinator({ permissionDenied: true })
  await coordinator.start({ deviceInfo: { serviceUuid: 'x' } })
  const snap = await coordinator.selectCandidate('mock-ai-box-0001')
  assert.equal(snap.stage, SETUP_STAGES.failed)
  assert.equal(snap.error, 'permission_denied')
})

test('候选不存在：失败且不落入认证', async () => {
  const coordinator = new MockClaimCoordinator()
  await coordinator.start({ deviceInfo: { serviceUuid: 'x' } })
  const snap = await coordinator.selectCandidate('no-such-device')
  assert.equal(snap.stage, SETUP_STAGES.failed)
  assert.equal(snap.error, 'candidate_not_found')
})

test('GATT 断线：认证阶段断开后失败，可恢复', async () => {
  const coordinator = new MockClaimCoordinator({ gattDisconnectAt: SETUP_STAGES.authentication })
  await coordinator.start({ deviceInfo: { serviceUuid: 'x' } })
  await coordinator.selectCandidate('mock-ai-box-0001')
  const snap = await coordinator.authenticate()
  assert.equal(snap.stage, SETUP_STAGES.failed)
  assert.equal(snap.error, 'gatt_disconnected')
  const resumed = await coordinator.resume()
  assert.equal(resumed.stage, SETUP_STAGES.candidate_selection)
})

test('Wi-Fi 凭证缺失：失败', async () => {
  const coordinator = new MockClaimCoordinator()
  await coordinator.start({ deviceInfo: { serviceUuid: 'x' } })
  await coordinator.selectCandidate('mock-ai-box-0001')
  await coordinator.authenticate()
  const snap = await coordinator.provisionWifi('', '')
  assert.equal(snap.stage, SETUP_STAGES.failed)
  assert.equal(snap.error, 'wifi_credentials_missing')
})

test('ACK 丢失：owner_ack 超时失败', async () => {
  const coordinator = new MockClaimCoordinator({ ackLost: true })
  await coordinator.start({ deviceInfo: { serviceUuid: 'x' } })
  await coordinator.selectCandidate('mock-ai-box-0001')
  await coordinator.authenticate()
  await coordinator.provisionWifi('ssid', 'pw')
  await coordinator.appProof()
  const snap = await coordinator.acknowledge()
  assert.equal(snap.stage, SETUP_STAGES.failed)
  assert.equal(snap.error, 'ack_timeout')
})

test('取消与恢复：任意阶段取消后 resume 回到候选选择', async () => {
  const coordinator = new MockClaimCoordinator()
  await coordinator.start({ deviceInfo: { serviceUuid: 'x' } })
  await coordinator.selectCandidate('mock-ai-box-0001')
  const cancelled = await coordinator.cancel('window-closed')
  assert.equal(cancelled.stage, SETUP_STAGES.cancelled)
  assert.equal(cancelled.cancelledReason, 'window-closed')
  const resumed = await coordinator.resume()
  assert.equal(resumed.stage, SETUP_STAGES.candidate_selection)
})

test('snapshot 可被 deriveElectronViewState 消费', async () => {
  const coordinator = new MockClaimCoordinator()
  const snap = await coordinator.start({ deviceInfo: { serviceUuid: 'x' } })
  const view = deriveElectronViewState(snap)
  assert.equal(view.stage, SETUP_STAGES.candidate_selection)
  assert.equal(view.canSelectCandidate, true)
  assert.equal(view.candidateCount, DEFAULT_CANDIDATES.length)
  assert.equal(view.inProgress, true)
})
