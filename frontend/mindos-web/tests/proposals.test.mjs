// 裁决 / 导出纯逻辑回归：矛盾标题、合并文案、导出文件名、删除确认词。
// 运行：node --experimental-strip-types tests/proposals.test.mjs
import assert from 'node:assert/strict'
import { conflictTitle, exportFileName, mergeLabel, purgeConfirmed, PURGE_PHRASE } from '../src/shared/proposals.ts'

// 1) 矛盾标题按 kind 区分，未知 kind 回落到矛盾
{
  assert.equal(conflictTitle('tension'), '原则与做法有张力')
  assert.equal(conflictTitle('contradiction'), '两条理解看起来矛盾')
  assert.equal(conflictTitle('whatever'), '两条理解看起来矛盾')
}

// 2) 合并文案：带理由 / 不带理由 / 缺名
{
  assert.equal(mergeLabel({ fromName: '岚姐', intoName: '林岚', reason: '别名相同' }), '「岚姐」和「林岚」可能是同一个（别名相同）')
  assert.equal(mergeLabel({ fromName: '岚姐', intoName: '林岚' }), '「岚姐」和「林岚」可能是同一个')
  assert.equal(mergeLabel({ fromName: null, intoName: '林岚', reason: '' }), '「（未命名）」和「林岚」可能是同一个')
}

// 3) 导出文件名按日期，且不含非法字符
{
  assert.equal(exportFileName(new Date(2026, 8, 2)), 'zhijun-ontology-20260902.json')
  assert.match(exportFileName(), /^zhijun-ontology-\d{8}\.json$/)
}

// 4) 删除确认词必须逐字相同（允许首尾空白）
{
  assert.equal(purgeConfirmed(PURGE_PHRASE), true)
  assert.equal(purgeConfirmed(` ${PURGE_PHRASE} `), true)
  assert.equal(purgeConfirmed('删除全部'), false)
  assert.equal(purgeConfirmed(''), false)
  assert.equal(purgeConfirmed(null), false)
}

console.log('proposals: 4 groups OK')
