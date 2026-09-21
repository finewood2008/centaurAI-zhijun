// 状态行：三类夹具（新回执 contextPlan / 旧回执计数 / 没有出处），不出现服务商与模型名，「· 在线 / 本机」后缀默认关。
// 运行：node --experimental-strip-types --test tests/status-line.test.mjs
import assert from 'node:assert/strict'
import test from 'node:test'
import { statusLineText } from '../src/immersive/statusLine.ts'

const item = (citationId, kind = 'claim') => ({ citationId, kind, id: `${kind}-${citationId}`, version: '1', title: `标题 ${citationId}`, text: '正文', ref: { kind, id: citationId, version: '1' }, category: 'memory' })
const routing = { revision: 'r1', service: { name: 'openai-fixture', model: 'gpt-fixture', external: true }, purposeLabel: '对话', excluded: [], reason: '' }
const planned = {
  routing,
  contextPlan: {
    revision: 'ctx-1', stage: 'initial', delivery: 'provided',
    background: [item('p1'), item('p2')], evidence: [item('p3'), item('m1', 'material')],
    providedRefs: ['p1', 'p2', 'p3', 'm1'], citedRefs: ['p1', 'm1'], excluded: [], citationAudit: { invalidRefs: [] },
  },
  confirmedClaims: [], workingClaims: [], materials: [], retractedNotices: 0, charterVersion: null, promptChars: 1200,
}
const legacy = {
  routing,
  confirmedClaims: [{ id: 'c1', section: 'who', content: '叫我阿远' }, { id: 'c2', section: 'matters', content: '在准备融资' }],
  workingClaims: [], materials: [{ materialId: 'm-1', title: '合同.pdf' }], retractedNotices: 1, charterVersion: null, promptChars: 800,
}
const meta = { provider: 'openai', model: 'gpt-fixture', external: true }
const forbidden = /openai|gpt|deepseek|ollama|synthetic|模型|provider/i

test('new receipts count what the answer explicitly cited', () => {
  assert.equal(statusLineText(planned, meta, 'complete', false), '参考了你记下的 2 条')
  const provided = { ...planned, contextPlan: { ...planned.contextPlan, citedRefs: [] } }
  assert.equal(statusLineText(provided, meta, 'complete', false), '看了你记下的 4 条，这次没有直接引用')
  const supplemented = { ...planned, contextPlan: { ...planned.contextPlan, stage: 'supplemented' } }
  assert.equal(statusLineText(supplemented, meta, 'complete', false), '参考了你记下的 2 条 · 补查了一次')
  const unavailable = { ...planned, contextPlan: { ...planned.contextPlan, stage: 'lookup_unavailable' } }
  assert.equal(statusLineText(unavailable, meta, 'complete', false), '参考了你记下的 2 条 · 补查没有完成')
})

test('legacy receipts fall back to the recorded count and mention avoided corrections', () => {
  assert.equal(statusLineText(legacy, meta, 'complete', false), '参考了你记下的 3 条 · 避开了 1 条你纠正过的')
  assert.equal(statusLineText({ ...legacy, confirmedClaims: [], materials: [], retractedNotices: 0 }, meta, 'complete', false), '没有参考记下的内容')
})

test('no provenance, initiated turns and non-complete statuses', () => {
  assert.equal(statusLineText(null, meta, 'complete', false), '没有参考记下的内容')
  assert.equal(statusLineText(null, meta, 'complete', true), '', '主动找你的那条：气泡自己带说明，状态行不再重复')
  assert.equal(statusLineText(planned, meta, 'complete', true), '参考了你记下的 2 条')
  assert.equal(statusLineText(planned, meta, 'streaming', false), '在想')
  assert.equal(statusLineText(planned, meta, 'aborted', false), '你按了停止，这段没有说完')
  assert.equal(statusLineText(null, meta, 'error', false), '这一轮没有完成')
})

test('channel suffix is off by default and never names a provider or model', () => {
  for (const fixture of [planned, legacy, null]) {
    for (const status of ['complete', 'aborted', 'error', 'streaming']) {
      const text = statusLineText(fixture, meta, status, false)
      assert.doesNotMatch(text, forbidden)
      assert.doesNotMatch(text, /在线|本机/)
    }
  }
  assert.equal(statusLineText(planned, meta, 'complete', false, { channel: true }), '参考了你记下的 2 条 · 在线')
  assert.equal(statusLineText(planned, { external: false }, 'complete', false, { channel: true }), '参考了你记下的 2 条 · 本机')
  assert.equal(statusLineText(planned, null, 'complete', false, { channel: true }), '参考了你记下的 2 条')
  assert.doesNotMatch(statusLineText(planned, meta, 'complete', false, { channel: true }), forbidden)
})

// PRD V2 5.5：危机那一轮，气泡下不出现任何与记忆有关的字样。
test('危机轮次整行留空，不显示「参考了你记下的 N 条」', () => {
  const crisis = { ...planned, disclosure: { kind: 'crisis', extractionDeferred: true } }
  assert.equal(statusLineText(crisis, null, 'done'), '', '危机轮次状态行必须为空')

  // 重话只是推迟抽取，状态行照常——它说的是这次回答参考了什么，不是记了什么。
  const heavy = { ...planned, disclosure: { kind: 'heavy', extractionDeferred: true } }
  assert.notEqual(statusLineText(heavy, null, 'done'), '')

  // 没有 disclosure 字段的旧回执不受影响。
  assert.notEqual(statusLineText(planned, null, 'done'), '')
})
