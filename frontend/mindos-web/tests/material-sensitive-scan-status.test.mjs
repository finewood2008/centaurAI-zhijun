import assert from 'node:assert/strict'
import test from 'node:test'
import { materialSensitiveScanStatusMeta, materialStatusMeta } from '../src/shared/status.ts'

test('optional scan projection is independent of material completion and grants no authorization', () => {
  assert.equal(materialSensitiveScanStatusMeta(null), null)
  assert.equal(materialSensitiveScanStatusMeta(undefined), null)
  assert.equal(materialSensitiveScanStatusMeta({ state: 'processing', completedFields: 2, totalFields: 5 }).label, '敏感识别中 2/5')
  assert.equal(materialSensitiveScanStatusMeta({ state: 'queued', errorCode: 'retrying' }).tone, 'warning')
  assert.equal(materialSensitiveScanStatusMeta({ state: 'completed' }).label, '敏感识别已完成')
  assert.equal(materialSensitiveScanStatusMeta({ state: 'failed' }).label, '敏感识别失败', 'no retry action is promised without an available capability')
  assert.equal(materialSensitiveScanStatusMeta({ state: 'new-state' }).tone, 'neutral')
  assert.equal(materialStatusMeta('restoring').label, '恢复中')
})

test('invalid scan progress never prints nonsensical counts', () => {
  for (const counts of [{ completedFields: -1, totalFields: 4 }, { completedFields: 6, totalFields: 4 }, { completedFields: 1.5, totalFields: 4 }, { completedFields: 1, totalFields: 0 }]) {
    assert.equal(materialSensitiveScanStatusMeta({ state: 'processing', ...counts }).label, '敏感识别中')
  }
})
