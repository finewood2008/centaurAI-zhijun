import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { contextItems, normalizeContextPlan, normalizeProvenance, provenanceMemorySummary } from '../src/shared/provenanceGraph.ts'
import { contextNeedsReview, contextRetryBody, isContextReviewError } from '../src/shared/contextRecovery.ts'

const item = (citationId, extra = {}) => ({ citationId, kind: 'claim', id: 'claim-' + citationId, version: 'v1', title: '一项理解', text: '当时提供的原文', ref: { kind: 'claim', id: 'claim-' + citationId, version: 'v1' }, category: 'evidence', ...extra })
const receipt = { revision: 'r1', stage: 'initial', background: [item('p1')], evidence: [item('p2')], providedRefs: ['p1', 'p2'], citedRefs: ['p2'], excluded: [] }
const plan = normalizeContextPlan(receipt)
assert.deepEqual(contextItems(plan, 'providedRefs').map(x => x.citationId), ['p1', 'p2'])
assert.deepEqual(contextItems(plan, 'citedRefs').map(x => x.citationId), ['p2'])
assert.equal(provenanceMemorySummary({ contextPlan: plan, confirmedClaims: [{ id: 'unprovided' }], routing: { sources: [{ kind: 'claim' }] }, memoryContext: { inheritedCount: 100 } }), '提供给模型 2 项信息 · 回答明确引用 1 项')

const invalid = normalizeContextPlan({ ...receipt, providedRefs: ['p1', 'p1', 'p99'], citedRefs: ['p2', 'p99', 'p1', 'p1'], citationAudit: { invalidRefs: ['p99', 'p99', 4] } })
assert.deepEqual(invalid.providedRefs, ['p1'])
assert.deepEqual(invalid.citedRefs, ['p1'], 'cited must also be provided, not merely in evidence candidates')
assert.deepEqual(invalid.citationAudit.invalidRefs, ['p99'])
const duplicate = normalizeContextPlan({ ...receipt, evidence: [item('p1', { version: 'v2' }), item('p2')] })
assert.deepEqual(duplicate.providedRefs, ['p2'], 'ambiguous citation IDs have no verified target')
assert.equal(normalizeContextPlan({ ...receipt, providedRefs: undefined }), undefined)
assert.equal(normalizeContextPlan({ ...receipt, stage: 'unknown' }), undefined)
assert.deepEqual(normalizeContextPlan({ ...receipt, background: [null, {}, item('p0'), item('p1', { version: null })], evidence: false }).providedRefs, [])
const frozen = JSON.stringify(receipt)
normalizeProvenance({ contextPlan: receipt })
assert.equal(JSON.stringify(receipt), frozen, 'normalization never rewrites persisted history')
assert.equal(provenanceMemorySummary({ contextPlan: { ...receipt, citedRefs: [] } }), '提供给模型 2 项信息 · 回答明确引用 0 项')
assert.equal(provenanceMemorySummary({ contextPlan: { ...receipt, stage: 'supplemented' } }), '提供给模型 2 项信息 · 回答明确引用 1 项 · 已补查一次')

const attachment = item('m1', { kind: 'material', id: 'mat1', title: '计划.txt', category: 'attachment', material: { materialId: 'mat1', version: 1, title: '计划.txt' } })
const withAttachment = normalizeContextPlan({ ...receipt, evidence: [item('p2'), attachment], providedRefs: ['p1', 'p2', 'm1'], citedRefs: ['p2', 'm1'] })
assert.deepEqual(contextItems(withAttachment, 'providedRefs').map(x => x.citationId), ['p1', 'p2', 'm1'])
assert.equal(provenanceMemorySummary({ contextPlan: withAttachment }), '提供给模型 3 项信息 · 回答明确引用 2 项')

const lookupNotice = '额外补查暂未完成，本轮使用已读取且已授权的信息回答。'
const unavailable = { ...receipt, stage: 'lookup_unavailable', delivery: 'provided', lookupNotice, lookupAttempts: 2 }
const restored = normalizeProvenance(JSON.parse(JSON.stringify({ contextPlan: unavailable }))).contextPlan
assert.equal(restored.stage, 'lookup_unavailable', 'optional lookup failure remains a current-format receipt after refresh')
assert.equal(restored.lookupNotice, lookupNotice)
assert.equal(restored.lookupAttempts, 2)
assert.equal(restored.delivery, 'provided')
assert.deepEqual(restored.providedRefs, receipt.providedRefs)
assert.deepEqual(restored.citedRefs, receipt.citedRefs, 'lookup failure does not change which information was provided or cited')
assert.equal(provenanceMemorySummary({ contextPlan: restored }), '提供给模型 2 项信息 · 回答明确引用 1 项 · 补查暂未完成')
assert.equal(normalizeContextPlan({ ...unavailable, lookupNotice: undefined }).lookupNotice, lookupNotice)
assert.equal(normalizeContextPlan({ ...unavailable, lookupNotice: {}, lookupAttempts: '2' }).lookupNotice, lookupNotice)
assert.equal(normalizeContextPlan({ ...unavailable, lookupAttempts: -1 }).lookupAttempts, undefined)
assert.equal(normalizeContextPlan({ ...receipt, lookupNotice }).lookupNotice, undefined, 'a stale notice does not appear on ordinary or successful lookup receipts')
for (const [delivery, expected] of [
  ['prepared', '额外补查暂未完成，正在继续处理本轮回答。'],
  ['awaiting_authorization', '额外补查暂未完成，核对授权后可继续回答。'],
  ['paused', '额外补查暂未完成。'],
  [undefined, '额外补查暂未完成。'],
  ['unknown', '额外补查暂未完成。'],
]) {
  const pending = normalizeContextPlan({ ...unavailable, delivery, providedRefs: [], citedRefs: [] })
  assert.equal(pending.lookupNotice, expected, 'an undelivered or legacy receipt cannot claim a completed answer, even if the backend includes the full notice')
  assert.deepEqual(pending.providedRefs, [])
  assert.equal(normalizeContextPlan(pending).lookupNotice, expected, 'repeated normalization preserves the safe delivery-specific notice')
}

const user = { id: 'user-1', content: '继续比较原来的选择', meta: { materialRefs: ['material-1'] } }
const assistant = { meta: { requestId: 'same-operation', replyTo: 'user-1', depth: 'deep', turnMode: 'deliberate', contextPending: { code: 'ROUTE_CONSENT_REQUIRED', stage: 'supplemented' } } }
const body = contextRetryBody(user, assistant, false)
assert.equal(body.requestId, 'same-operation')
assert.equal(body.retryUserMessageId, user.id)
assert.equal(body.content, user.content)
assert.equal(body.depth, 'deep')
assert.equal(body.mode, 'deliberate')
assert.deepEqual(body.materialRefs, ['material-1'])
assert.equal(body.localOnly, false)
assert.equal(contextRetryBody(user, assistant, true).localOnly, true, 'only explicit local choice switches processing')
assert.equal(contextRetryBody(user, {}, false).requestId, undefined, 'old messages do not fabricate a persisted nonce')
assert.equal(contextNeedsReview(assistant), true)
assert.equal(contextNeedsReview({ meta: { contextPending: { code: 'TIMEOUT' } } }), false)
assert.equal(isContextReviewError({ code: 'ROUTE_CONSENT_REQUIRED', stage: 'supplemented' }), true)
assert.equal(isContextReviewError({ code: 'ROUTE_CHANGED', preview: {} }), true)
assert.equal(isContextReviewError({ code: 'TIMEOUT', stage: 'supplemented' }), false)
assert.equal(contextNeedsReview({ meta: { contextStage: 'lookup_unavailable' } }), false, 'optional lookup failure is not an authorization or failed-message card')

const strip = await readFile(new URL('../src/components/conversation/ProvenanceStrip.vue', import.meta.url), 'utf8')
assert.match(strip, /<p v-if="lookupNotice" class="zj-prov__line zj-prov__lookup-notice" data-testid="context-lookup-notice">\{\{ lookupNotice \}\}<\/p>/)
assert.ok(strip.indexOf('data-testid="context-lookup-notice"') < strip.indexOf('<div v-if="open"'), 'the small notice stays visible while the provenance detail is collapsed')
assert.doesNotMatch(strip, /role="alert"|<dialog|<Modal|<Banner/)
assert.match(strip, /\.zj-prov__lookup-notice\s*\{[^}]*overflow-wrap: anywhere/s)

const graph = await readFile(new URL('../src/components/conversation/ProvenanceGraph.vue', import.meta.url), 'utf8')
for (const label of ['遵循的约定', '提供给模型的信息', '回答明确引用的信息', '不表示完整阅读原文件', '它不证明结论被证据支持']) assert.ok(graph.includes(label))
assert.doesNotMatch(graph, /<svg|lineWidth|影响权重.*\{\{/)
assert.doesNotMatch(graph, /\[\{\{ item\.citationId \}\}\]/, 'user-facing source detail uses titles, not temporary pN IDs')
const bubble = await readFile(new URL('../src/components/conversation/MessageBubble.vue', import.meta.url), 'utf8')
assert.match(bubble, /renderer\.rules\.text[\s\S]*stripContextCitations/, 'assistant prose hides pN only at the Markdown text-token display boundary')
const conversation = await readFile(new URL('../src/pages/ConversationPage.vue', import.meta.url), 'utf8')
assert.match(conversation, /contextRetryBody\(user, message, localOnly\)/)
assert.match(conversation, /provenance: d => \{ message.provenance = d as ProvenanceEvent \}/)
assert.match(conversation, /核对补充资料并继续/)
console.log('context-plan: provided/cited/lineage separation, conservative legacy records, stable consent recovery and non-blocking lookup notice OK')
