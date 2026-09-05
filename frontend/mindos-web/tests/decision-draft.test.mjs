// 判断草稿纯逻辑回归：合并不覆盖已有值、缺失项、意图提示。
// 运行：node --experimental-strip-types tests/decision-draft.test.mjs
import assert from 'node:assert/strict'
import { draftMissingFields, intentHint, mergeDraftFields, reviewDateToIso, defaultReviewDate, directionPatch } from '../src/shared/decisionDraft.ts'

// 1) 合并：null/空数组不覆盖，已有值保留，新值覆盖
{
  const prev = { title: '要不要扩张', options: ['扩', '不扩'], choice: '先不扩', confidence: 70 }
  const merged = mergeDraftFields(prev, { title: '', options: [], choice: null, rationale: '因为现金流紧', confidence: null })
  assert.equal(merged.title, '要不要扩张')
  assert.deepEqual(merged.options, ['扩', '不扩'])
  assert.equal(merged.choice, '先不扩')
  assert.equal(merged.rationale, '因为现金流紧')
  assert.equal(merged.confidence, 70)
  const again = mergeDraftFields(merged, { options: ['A', 'B', 'C'], confidence: 40 })
  assert.deepEqual(again.options, ['A', 'B', 'C'])
  assert.equal(again.confidence, 40)
  assert.notEqual(again, merged)
  assert.deepEqual(mergeDraftFields(null, null).options, [])
}

// 2) 缺失项：四项用户必填；知君的看法不算
{
  assert.deepEqual(draftMissingFields({ zhijunView: '我建议不扩' }), ['choice', 'rationale', 'confidence', 'expectedOutcome'])
  assert.deepEqual(draftMissingFields({ choice: '不扩', rationale: '钱紧', confidence: 70, expectedOutcome: '半年内不裁员' }), [])
  assert.deepEqual(draftMissingFields({ choice: ' ', rationale: '钱紧', confidence: 120, expectedOutcome: 'x' }), ['choice', 'confidence'])
  assert.deepEqual(draftMissingFields(null), ['choice', 'rationale', 'confidence', 'expectedOutcome'])
}

// 3) 意图提示：只对拿主意的措辞为真
{
  assert.equal(intentHint('我在考虑要不要换城市'), true)
  assert.equal(intentHint('这个还是那个'), true)
  assert.equal(intentHint('最近有点纠结'), true)
  assert.equal(intentHint('今天天气不错'), false)
  assert.equal(intentHint(''), false)
  assert.equal(intentHint(null), false)
}

// 4) 回访日期工具
{
  const d = defaultReviewDate(new Date('2026-09-02T00:00:00'), 14)
  assert.equal(d, '2026-09-16')
  assert.equal(reviewDateToIso(''), undefined)
  const iso = reviewDateToIso('2026-09-16')
  assert.ok(iso && iso.endsWith('Z'))
}

// 5) Explicit candidate adoption: never overwrite without approval, never grade confidence.
{
  const current = { choice: '我已写的选择', rationale: '', expectedOutcome: '已有预期', confidence: 0 }
  const candidate = { choice: '候选选择', rationale: '候选理由', expectedOutcome: '候选预期', confidence: 99 }
  assert.deepEqual(directionPatch(current, candidate, true), { rationale: '候选理由' })
  assert.deepEqual(directionPatch(current, candidate, false), { choice: '候选选择', rationale: '候选理由', expectedOutcome: '候选预期' })
  assert.equal(current.choice, '我已写的选择')
  assert.equal(current.confidence, 0)
}

console.log('decision-draft: 5 groups OK')
