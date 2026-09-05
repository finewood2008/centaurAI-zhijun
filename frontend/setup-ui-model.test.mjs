// 阶段 3 Setup 窗口 UI 模型合同测试（WP M renderer 消费侧）。
//
// 覆盖：snapshot → 展示模型（阶段徽章/状态文案/按钮可用性/候选列表/错误文案），
// 失败/取消文案映射、进度步骤、Wi-Fi 完成标记；异常输入回退 idle 不抛错。
//
// 运行：node --test setup-ui-model.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const { buildSetupUiModel } = require('./setup-ui-model.js')
const { SETUP_STAGES } = require('./setup-view-state.js')

test('idle：待开始、无候选、可开始', () => {
  const model = buildSetupUiModel({ stage: SETUP_STAGES.idle, candidates: [] })
  assert.equal(model.stage, SETUP_STAGES.idle)
  assert.equal(model.badge, '待开始')
  assert.equal(model.canStart, true)
  assert.equal(model.canSelectCandidate, false)
  assert.equal(model.canCancel, false)
  assert.equal(model.canResume, false)
  assert.deepEqual(model.candidates, [])
  assert.equal(model.progress, '')
})

test('candidate_selection：两候选渲染 + 可选中', () => {
  const snapshot = {
    stage: SETUP_STAGES.candidate_selection,
    candidates: [
      { id: 'a', name: 'AI Box A', rssi: -52 },
      { id: 'b', name: 'AI Box B', rssi: -63 },
    ],
  }
  const model = buildSetupUiModel(snapshot)
  assert.equal(model.badge, '选择设备')
  assert.equal(model.canSelectCandidate, true)
  assert.equal(model.canCancel, true)
  assert.equal(model.candidates.length, 2)
  assert.equal(model.candidates[0].name, 'AI Box A')
  assert.equal(model.candidates[0].rssi, -52)
  assert.equal(model.candidates[0].selected, false)
  assert.equal(model.progress, '步骤 2/7')
})

test('wifi：显示阶段与进度', () => {
  const snapshot = { stage: SETUP_STAGES.wifi }
  const model = buildSetupUiModel(snapshot)
  assert.equal(model.badge, '配置 Wi-Fi')
  assert.equal(model.progress, '步骤 4/7')
})

test('done：成功文案', () => {
  const model = buildSetupUiModel({ stage: SETUP_STAGES.done, wifiConfigured: true })
  assert.equal(model.badge, '已完成')
  assert.match(model.statusText, /设备已添加/)
  assert.equal(model.wifiConfigured, true)
})

test('failed：蓝牙关闭 → 中文错误文案 + 可恢复', () => {
  const snapshot = { stage: SETUP_STAGES.failed, error: 'bluetooth_unavailable' }
  const model = buildSetupUiModel(snapshot)
  assert.equal(model.badge, '失败')
  assert.match(model.errorText, /蓝牙未开启/)
  assert.equal(model.canResume, true)
})

test('failed：未知错误码原样透出', () => {
  const model = buildSetupUiModel({ stage: SETUP_STAGES.failed, error: 'some_unknown_reason' })
  assert.equal(model.errorText, 'some_unknown_reason')
})

test('cancelled：显示取消原因', () => {
  const model = buildSetupUiModel({ stage: SETUP_STAGES.cancelled, cancelledReason: 'user_cancelled' })
  assert.equal(model.badge, '已取消')
  assert.match(model.cancelledText, /user_cancelled/)
  assert.equal(model.canResume, true)
})

test('异常输入回退 idle 不抛错', () => {
  const model = buildSetupUiModel({ stage: 'weird', candidates: 'not-array' })
  assert.equal(model.stage, SETUP_STAGES.idle)
  assert.deepEqual(model.candidates, [])
})
