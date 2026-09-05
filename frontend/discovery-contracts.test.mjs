// 阶段 1（WP J）Discovery Contracts 骨架合同测试。
//
// 覆盖：候选生命周期转换表（非法转换拒绝）、服务过滤、过期判定、规范化
// （不支持服务/缺失信息 → 拒绝，不产生权威 device_id 语义）、conformance fixtures。
//
// 运行：node --test discovery-contracts.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const {
  CANDIDATE_STATES,
  DISCOVERY_ERRORS,
  candidateSupportsService,
  isCandidateStale,
  transitionCandidate,
  normalizeCandidate,
  CONFORMANCE_FIXTURES,
} = require('./discovery-contracts.js')
const { GATT_V2_SERVICE_UUID } = require('./ble-contracts.js')

const freshCandidate = () => ({
  id: 'local-device-0001',
  name: 'AI Box',
  rssi: -52,
  serviceUuids: [GATT_V2_SERVICE_UUID],
  state: CANDIDATE_STATES.discovered,
  lastSeenAt: Date.now(),
})

test('生命周期：合法推进全链', () => {
  const chain = [
    CANDIDATE_STATES.discovered,
    CANDIDATE_STATES.selected,
    CANDIDATE_STATES.connecting,
    CANDIDATE_STATES.verified,
    CANDIDATE_STATES.claimed,
  ]
  let candidate = freshCandidate()
  for (const state of chain.slice(1)) {
    candidate = transitionCandidate(candidate, state)
    assert.equal(candidate.state, state)
  }
})

test('生命周期：非法转换一律拒绝', () => {
  const candidate = freshCandidate()
  assert.throws(() => transitionCandidate(candidate, CANDIDATE_STATES.claimed), /非法候选状态转换/)
  assert.throws(() => transitionCandidate(candidate, CANDIDATE_STATES.verified), /非法候选状态转换/)
  const failed = transitionCandidate(
    transitionCandidate(transitionCandidate(freshCandidate(), CANDIDATE_STATES.selected), CANDIDATE_STATES.connecting),
    CANDIDATE_STATES.failed,
  )
  assert.throws(() => transitionCandidate(failed, CANDIDATE_STATES.selected), /非法候选状态转换/)
  const claimed = transitionCandidate(transitionCandidate(transitionCandidate(transitionCandidate(freshCandidate(), CANDIDATE_STATES.selected), CANDIDATE_STATES.connecting), CANDIDATE_STATES.verified), CANDIDATE_STATES.claimed)
  assert.throws(() => transitionCandidate(claimed, CANDIDATE_STATES.failed), /非法候选状态转换/)
})

test('服务过滤：仅匹配 GATT v2 服务', () => {
  assert.equal(candidateSupportsService(CONFORMANCE_FIXTURES.supported[0]), true)
  assert.equal(candidateSupportsService(CONFORMANCE_FIXTURES.unsupported), false)
  assert.equal(candidateSupportsService(null), false)
  assert.equal(candidateSupportsService({}), false)
})

test('过期判定：超阈值标记 stale', () => {
  const now = 100000
  assert.equal(isCandidateStale({ ...freshCandidate(), lastSeenAt: now - 20000 }, now), true)
  assert.equal(isCandidateStale({ ...freshCandidate(), lastSeenAt: now - 1000 }, now), false)
  assert.equal(isCandidateStale(null, now), false)
})

test('规范化：不支持服务与缺失信息拒绝，且不产出权威 device_id', () => {
  const ok = normalizeCandidate({ id: 'bluetooth-dev-9', name: 'Box', rssi: -50, serviceUuids: [GATT_V2_SERVICE_UUID] })
  assert.equal(ok.id, 'bluetooth-dev-9')
  assert.equal(ok.state, CANDIDATE_STATES.discovered)
  assert.equal('device_id' in ok, false, '候选对象不得含权威 device_id')
  assert.equal(normalizeCandidate({ id: 'x', serviceUuids: ['0000ffff-0000-1000-8000-00805f9b34fb'] }), null)
  assert.equal(normalizeCandidate(null), null)
  assert.equal(normalizeCandidate({ id: 'y', serviceUuids: [GATT_V2_SERVICE_UUID], deviceId: 'authoritative-device-id' }).id, 'y')
})

test('conformance fixtures：确定性且覆盖双候选/不支持/过期/错误', () => {
  assert.equal(CONFORMANCE_FIXTURES.supported.length, 2)
  assert.equal(CONFORMANCE_FIXTURES.supported[0].id, 'local-device-0001')
  assert.equal(CONFORMANCE_FIXTURES.supported[0].state, undefined)
  assert.equal(candidateSupportsService(CONFORMANCE_FIXTURES.supported[1]), true)
  assert.equal(candidateSupportsService(CONFORMANCE_FIXTURES.unsupported), false)
  assert.equal(isCandidateStale(CONFORMANCE_FIXTURES.stale, 0), true)
  for (const code of Object.values(CONFORMANCE_FIXTURES.errorSamples)) {
    assert.equal(typeof code, 'string')
  }
  assert.equal(DISCOVERY_ERRORS.bluetooth_unavailable, 'bluetooth_unavailable')
  assert.equal(DISCOVERY_ERRORS.canceled, 'canceled')
})
