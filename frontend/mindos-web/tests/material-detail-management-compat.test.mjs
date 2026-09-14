// Run the real page setup with synthetic capability responses; no network.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const source = await readFile(new URL('../src/pages/MaterialDetailPage.vue', import.meta.url), 'utf8')
const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'material-detail-compat' }).content,
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const flush = async () => { await Vue.nextTick(); await new Promise(resolve => setImmediate(resolve)) }

function mount(desktop, overrides = {}) {
  const calls = [], polls = [], cleanups = [], exports = {}
  const record = { materialId: 'm1', fileName: 'test.pdf', fileType: 'document', status: 'available',
    text: '受控正文', previewUrl: '/controlled/file', tags: [],
    draftCard: { title: '草稿标题', content: '草稿正文', revision: 'r1', status: 'ok', confirmed: false }, ...overrides }
  const api = new Proxy({
    getMaterialDetail: async () => record,
    listMaterialVersions: async () => ({ items: [{ materialId: 'm1', versionNumber: 1 }] }),
    getMaterialRelated: async () => ({ items: [], note: '' }),
    saveMaterialDraftCard: async (_id, payload) => ({ ...record.draftCard, ...payload, revision: 'r2' }),
    getMaterialAnalysis: async () => ({ summary: { text: '', status: 'ok' },
      tagSuggestions: { items: [], status: 'ok' }, entities: { items: [], status: 'ok' }, relations: { items: [], status: 'ok' } }),
  }, { get(target, name) { return async (...args) => {
    calls.push([name, ...args]); if (!(name in target)) throw new Error(`Unexpected API: ${name}`)
    return target[name](...args)
  } } })
  const poller = name => () => ({ start: id => polls.push([name, id]), stop() {} })
  const require = id => {
    if (id === 'vue') return { ...Vue, onMounted() {}, onBeforeUnmount: fn => cleanups.push(fn) }
    if (id === 'vue-router') return { useRoute: () => ({ params: { materialId: 'm1' }, query: {} }), useRouter: () => ({}), onBeforeRouteLeave() {} }
    if (id.endsWith('/api')) return { api }
    if (id.endsWith('/productScope')) return { isDesktopProduct: () => desktop }
    if (id.endsWith('/productFiles')) return { productPreview: async url => { calls.push(['preview', url]); return 'blob:safe' }, releaseProductPreview() {} }
    if (id.endsWith('/useToast')) return { useToast: () => () => {} }
    if (id.endsWith('/useSummaryPolling')) return { createSummaryPoller: poller('summary') }
    if (id.endsWith('/useAnalysisPolling')) return { createAnalysisPoller: poller('analysis') }
    if (id.endsWith('/useGeneratedDraftRefresh')) return { createGeneratedDraftPoller: poller('draft') }
    if (id.endsWith('/sessionGate')) return { createSessionGate: () => { let ticket = 0; return { next: () => ++ticket, invalidate: () => ++ticket, isCurrent: n => n === ticket } } }
    if (id.endsWith('/useEntityTagAdd')) return { createEntityTagAdder: () => () => {} }
    return {}
  }
  new Function('require', 'exports', compiled)(require, exports)
  const scope = Vue.effectScope()
  const ui = scope.run(() => exports.default.setup({}, { expose() {} }))
  return { ui, calls, polls, close() { scope.stop() } }
}

// New desktop responses may omit legacy summary; that must not hide the page.
{
  const h = mount(true)
  await h.ui.loadDetail('m1'); await flush()
  assert.equal(h.ui.error.value, '')
  assert.equal(h.ui.detail.value.text, '受控正文')
  assert.equal(h.ui.draftContent.value, '草稿正文')
  assert.equal(h.ui.versions.value.length, 1)
  assert.equal(h.ui.derivedGenerationPending.value, false)
  h.ui.draftContent.value = '用户更新草稿'
  assert.equal(await h.ui.saveDraft(), true)
  assert.equal(h.ui.draft.value.revision, 'r2')
  await h.ui.openMainPreview()
  assert.equal(h.ui.mainPreviewUrl.value, 'blob:safe')
  assert.ok(!h.calls.some(([name]) => /Analysis|Summary|reparse|Redaction/.test(name)))
  h.close()
}

// Pending legacy summary never starts a denied desktop poll; draft polling stays.
{
  const h = mount(true, { summary: { status: 'pending' }, draftCard: { title: '', content: '', status: 'pending' } })
  await h.ui.loadDetail('m1'); await flush()
  await h.ui.loadAnalysis(); await h.ui.reparseMaterial()
  assert.deepEqual(h.polls, [['draft', 'm1']])
  assert.ok(!h.calls.some(([name]) => /Analysis|Summary|reparse|regenerate/.test(name)))
  h.close()
}

// Browser Data Engine management retains its existing legacy analysis behavior.
{
  const h = mount(false, { summary: { status: 'pending' } })
  await h.ui.loadDetail('m1'); await flush()
  assert.ok(h.calls.some(([name]) => name === 'getMaterialAnalysis'))
  assert.ok(h.polls.some(([name]) => name === 'summary'))
  assert.equal(h.ui.error.value, '')
  h.close()
}

// UI advertises the boundary and never mounts the retired redaction component.
assert.match(source, /v-if="!desktopManagement && \(detail\.privacyRequired/)
assert.match(source, /v-if="!desktopManagement" class="detail-panel entity-panel"/)
assert.match(source, /当前盒端不再提供旧版智能分析、重新解析和隐私复核操作/)
assert.match(source, /确认本轮材料及脱敏方式/)
console.log('material detail management compatibility: 4 cases passed')
