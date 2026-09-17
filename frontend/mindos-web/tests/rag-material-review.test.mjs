import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const source = await readFile(new URL('../src/components/conversation/RagSensitiveDialog.vue', import.meta.url), 'utf8')
const componentCode = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'rag-review' }).content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const routingCode = ts.transpileModule(await readFile(new URL('../src/services/taskRouting.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const item = (previewId, extra = {}) => ({ previewId, materialId: 'material-1', materialVersion: 2,
  title: '项目备忘', preview: '本季度安排三次评审。', containsSensitive: false, verificationStatus: 'verified', ...extra })
const prompt = (extra = {}) => ({ interactionId: 'review-1', status: 'materials_confirmation_required',
  query: '本季度有哪些安排？', scopeLabel: '本次选中的附件', outcome: 'ok', items: [item('p1'), item('p2')],
  hits: [], passedCount: 2, canReadOriginal: false, riskAvailable: false, ...extra })
function mount(extra = {}) {
  const exports = {}, events = [], props = Vue.reactive({ ...prompt(), busy: false, ...extra })
  new Function('require', 'exports', componentCode)(id => id === 'vue' ? Vue : {}, exports)
  const scope = Vue.effectScope()
  const ui = scope.run(() => exports.default.setup(props, { expose() {}, emit: (...args) => events.push(args) }))
  return { ui, props, events, close: () => scope.stop() }
}
function routing(transport = async () => ({ ok: true, json: async () => ({}) })) {
  const exports = {}, calls = [], resets = []
  new Function('require', 'exports', routingCode)(id => {
    if (id === 'vue') return Vue
    if (id.includes('productScope')) return { isDesktopProduct: () => true, onProductScopeReset: fn => resets.push(fn) }
    if (id.includes('transport')) return { transportRequest: async (...args) => { calls.push(args); return transport(...args) } }
    if (id === './api') return { buildHeaders: options => options.headers, throwApiError: async response => { throw response.error } }
    if (id.includes('useReplyRecovery')) return { reportReplyFailure() {} }
    throw Error('Unexpected dependency ' + id)
  }, exports)
  return { ...exports, calls, reset: () => resets.forEach(fn => fn()) }
}
const settle = async () => { await Vue.nextTick(); await new Promise(resolve => setImmediate(resolve)) }

test('material review starts unselected and submits only current unique preview identifiers', () => {
  const h = mount({ items: [item('p1'), item('p2'), item('p1'), item('', { preview: 'invalid' })] })
  assert.deepEqual(h.ui.selectedPreviewIds.value, [])
  h.ui.useSelected()
  assert.deepEqual(h.events, [])
  h.ui.selectedPreviewIds.value = ['p2', 'p2', 'stale']
  h.ui.useSelected()
  assert.deepEqual(h.events, [['use-selected', ['p2']]])
  h.props.busy = true
  h.ui.useSelected(); h.ui.withoutMaterials(); h.ui.cancel()
  assert.equal(h.events.length, 1)
  h.props.busy = false
  h.props.items = [item('p3')]
  assert.deepEqual(h.ui.selectedPreviewIds.value, [])
  h.ui.useSelected()
  assert.equal(h.events.length, 1)
  h.ui.selectedPreviewIds.value = ['p3']
  h.props.interactionId = 'review-2'
  assert.deepEqual(h.ui.selectedPreviewIds.value, [])
  h.close()
})

test('material labels distinguish sensitivity from verification and preserve useful locators', () => {
  const h = mount()
  assert.equal(h.ui.materialStatus(item('p1')), '已完成检测 · 无敏感标记')
  assert.equal(h.ui.materialStatus(item('p2', { verificationStatus: 'unverified', containsSensitive: true })), '未完成检测 · 含敏感标记')
  assert.doesNotMatch(h.ui.materialStatus(item('p1')), /脱敏/)
  assert.equal(h.ui.materialLocation({ page: 3, paragraph: 2, table: '计划', cell: 'A1', startMs: 1500, endMs: 2500 }), '第 3 页 · 第 2 段 · 表格 计划 · A1 · 1.5 秒–2.5 秒')
  h.close()
})

test('delivery mode labels use the server decision and never infer desensitization from sensitivity flags', () => {
  const h = mount({ deliveryMode: '' })
  assert.equal(h.ui.deliveryLabel.value, '')
  assert.match(h.ui.materialStatus(h.props.items[0]), /无敏感标记/)
  for (const [mode, label] of [
    ['masked', '使用脱敏后片段'], ['original', '原文片段'],
    ['risk-release', '未经验证原文'], ['continue-passed', '仅使用已通过检测的片段'],
  ]) {
    h.ui.selectedPreviewIds.value = ['p1']
    h.props.deliveryMode = mode
    assert.equal(h.ui.deliveryLabel.value, label)
    assert.deepEqual(h.ui.selectedPreviewIds.value, [], 'changed delivery mode requires a fresh selection')
    assert.match(h.ui.materialStatus(h.props.items[0]), /无敏感标记/)
  }
  h.props.deliveryMode = 'unknown-mode'
  assert.equal(h.ui.deliveryLabel.value, '')
  h.close()
})

test('truncated previews disclose full-fragment authorization without preselecting the item', () => {
  const truncated = item('long', { preview: '字'.repeat(500), previewTruncated: true, textLength: 1240 })
  const h = mount({ items: [truncated] })
  assert.equal(h.ui.previewNotice(truncated), '仅展示前 500 字，完整检索片段共 1240 字。选中后允许使用整个检索片段，包括未展示部分。')
  assert.equal(h.ui.previewNotice(item('short', { previewTruncated: false, textLength: 12 })), '')
  assert.equal(h.ui.previewNotice(item('legacy')), '')
  assert.equal(h.ui.previewNotice(item('unicode', { preview: '𠮷'.repeat(500), previewTruncated: true })), '仅展示前 500 字。选中后允许使用整个检索片段，包括未展示部分。')
  h.ui.useSelected()
  assert.deepEqual(h.events, [])
  h.ui.selectedPreviewIds.value = ['long']
  h.ui.useSelected()
  assert.deepEqual(h.events, [['use-selected', ['long']]])
  assert.match(source, /选中的完整检索片段/)
  h.close()
})

test('empty results distinguish policy blocking and let the user continue without materials', () => {
  const h = mount({ items: [], outcome: 'no_results' })
  assert.match(h.ui.emptyMessage.value, /没有找到匹配/)
  assert.match(h.ui.emptyMessage.value, /当前授权且索引就绪/)
  assert.match(h.ui.emptyMessage.value, /不代表文件不存在/)
  assert.match(h.ui.emptyMessage.value, /不能判定.*未索引或索引失败/)
  h.ui.useSelected()
  assert.deepEqual(h.events, [])
  h.props.outcome = 'sensitive_content_blocked'
  assert.match(h.ui.emptyMessage.value, /交付策略限制/)
  for (const status of ['materials_confirmation_required', 'sensitive_confirmation_required', 'sensitive_check_unavailable']) {
    h.props.status = status
    h.ui.withoutMaterials(); h.ui.cancel()
  }
  assert.deepEqual(h.events, Array.from({ length: 3 }, () => [['without-materials'], ['cancel']]).flat())
  assert.doesNotMatch(source, /v-html|localStorage|sessionStorage/)
  h.close()
})

test('same-name material versions remain individually selectable and identified', () => {
  const h = mount({ items: [item('v1', { materialId: 'material-1', materialVersion: 1 }),
    item('v2', { materialId: 'material-2', materialVersion: 2 })] })
  assert.equal(h.ui.materialItems.value.length, 2)
  h.ui.selectedPreviewIds.value = ['v2']
  h.ui.useSelected()
  assert.deepEqual(h.events, [['use-selected', ['v2']]])
  assert.match(source, /资料编号 \{\{ item.materialId \}\}/)
  assert.match(source, /版本 \{\{ item.materialVersion \}\}/)
  h.close()
})

test('routing recognizes review errors and sends choices without copying preview content or credentials', async () => {
  const h = routing(), p = prompt({ confirmToken: 'never-send', evidenceRef: 'never-send' })
  assert.equal(h.ragPromptOf({ code: 'RAG_MATERIAL_REVIEW_REQUIRED', ragV2: p }), p)
  assert.equal(h.ragPromptOf({ code: 'UNRELATED', ragV2: p }), null)
  const controller = new AbortController()
  await h.submitRagDecision('c/1', p, { action: 'use-selected', selectedPreviewIds: ['p2'], unexpected: 'never-send' }, controller.signal)
  assert.equal(h.calls[0][0], '/api/mindos/conversations/c%2F1/rag-v2/decision')
  assert.equal(h.calls[0][1].signal, controller.signal)
  assert.deepEqual(JSON.parse(h.calls[0][1].body), { interactionId: 'review-1', action: 'use-selected', selectedPreviewIds: ['p2'] })
  await h.submitRagDecision('c1', p, 'without-materials')
  assert.deepEqual(JSON.parse(h.calls[1][1].body), { interactionId: 'review-1', action: 'without-materials' })
})

test('abort, replacement and product scope resets cancel questions without clearing newer ones', async () => {
  const h = routing(), controller = new AbortController()
  const first = h.askRag(prompt(), controller.signal)
  const stale = h.ragQuestion.value
  const second = h.askRag(prompt({ interactionId: 'review-2' }))
  assert.equal(await first, 'cancel')
  controller.abort(); stale.done('cancel')
  assert.equal(h.ragQuestion.value.prompt.interactionId, 'review-2')
  h.reset()
  assert.equal(await second, 'cancel')
  assert.equal(h.ragQuestion.value, null)
  assert.equal(await h.askRag(prompt(), controller.signal), 'cancel')
  const thirdController = new AbortController()
  const third = h.askRag(prompt(), thirdController.signal)
  thirdController.abort()
  assert.equal(await third, 'cancel')
})

test('multiple user confirmations have a separate budget from automatic preview refreshes', async () => {
  let previews = 0
  const h = routing(async path => {
    if (path.endsWith('/decision')) return { ok: true, json: async () => ({}) }
    previews++
    if (previews <= 7) return { ok: false, error: { code: 'RAG_MATERIAL_REVIEW_REQUIRED', ragV2: prompt({ interactionId: `review-${previews}` }) } }
    return { ok: true, json: async () => ({ revision: 'ready', service: { external: false }, missing: [] }) }
  })
  const request = h.prepareChatRoute('c1', { requestId: 'message-1' })
  for (let i = 0; i < 7; i++) {
    await settle()
    assert.equal(h.chatPreparation.value.stage, 'reviewing')
    h.ragQuestion.value.done({ action: 'use-selected', selectedPreviewIds: ['p1'] })
  }
  assert.equal((await request).routeRevision, 'ready')
  assert.equal(previews, 8)
  assert.equal(h.chatPreparation.value, null)
})

test('a completed older preparation cannot clear the current conversation progress', async () => {
  const pending = new Map()
  const h = routing(path => new Promise(resolve => pending.set(path, resolve)))
  const first = h.prepareChatRoute('c1', {})
  const second = h.prepareChatRoute('c2', {})
  const response = { ok: true, json: async () => ({ revision: 'ready', service: { external: false }, missing: [] }) }
  pending.get('/api/mindos/conversations/c1/routing/preview')(response)
  await first
  assert.equal(h.chatPreparation.value.conversationId, 'c2')
  pending.get('/api/mindos/conversations/c2/routing/preview')(response)
  await second
  assert.equal(h.chatPreparation.value, null)
})

test('uncertain one-shot risk release stops automatic work and requires a new user submission', async () => {
  const failure = { code: 'RAG_RISK_RESULT_UNKNOWN', status: 503, retryAfter: 120, retryable: false }
  const h = routing(async path => path.endsWith('/decision')
    ? { ok: false, error: failure }
    : { ok: false, error: { code: 'RAG_SENSITIVE_CHECK_INCOMPLETE', ragV2: prompt({ status: 'sensitive_check_unavailable' }) } })
  const request = h.prepareChatRoute('c1', { requestId: 'message-1' })
  const rejected = assert.rejects(request, error => error === failure)
  await settle()
  h.ragQuestion.value.done('risk-release')
  await rejected
  assert.equal(h.calls.length, 2, 'neither the confirmation nor Search is retried automatically')
  assert.equal(h.requiresFreshRagSearch(failure), false)
  assert.equal(h.chatPreparation.value, null)
})
