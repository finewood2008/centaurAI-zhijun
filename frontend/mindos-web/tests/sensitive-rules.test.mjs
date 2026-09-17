import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import { ApiError, throwApiError } from '../src/services/api.ts'

const source = await readFile(new URL('../src/components/settings/SensitiveRulesPanel.vue', import.meta.url), 'utf8')
const apiSource = await readFile(new URL('../src/services/api.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'sensitive-rules-test' }).content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const copy = value => JSON.parse(JSON.stringify(value))

function rule(overrides = {}) {
  return {
    ruleId: 'custom-contact', source: 'custom', immutable: false, revision: 1,
    name: '联系方式', description: '保护个人联系方式', examples: ['手机号 13800000000'],
    counterExamples: ['公司总机'], enabled: true, deliveryMode: 'confirm',
    allowOriginalAfterConfirm: false, masking: { kind: 'server-owned' }, ...overrides,
  }
}

function setup(overrides = {}) {
  const store = {
    rules: [
      rule({ ruleId: 'builtin-id', source: 'built_in', immutable: true, name: '身份信息', revision: 4 }),
      rule(),
    ],
    maxCustomRules: 3,
    epoch: 7,
  }
  const calls = []
  const cleanup = []
  const response = () => ({
    items: copy(store.rules), total: store.rules.length,
    builtinCount: store.rules.filter(item => item.source === 'built_in').length,
    customCount: store.rules.filter(item => item.source === 'custom').length,
    maxCustomRules: store.maxCustomRules,
    enabledRuleCount: store.rules.filter(item => item.enabled).length,
    detectorPromptTokens: 120, detectorPromptTokenLimit: 1000,
    detectorPromptWithinLimit: true, detectorPromptTokensRemaining: 880,
    epoch: store.epoch, detectorRevision: `detector-${store.epoch}`,
  })
  const api = {
    getSensitiveRules: async () => response(),
    getSensitiveRule: async id => copy(store.rules.find(item => item.ruleId === id)),
    createSensitiveRule: async body => {
      calls.push(['create', copy(body)])
      const saved = rule({ ...body, ruleId: 'custom-new', revision: 1, source: 'custom', immutable: false, masking: { kind: 'server-owned' } })
      delete saved.requestId; delete saved.acknowledgeSimilarRuleId
      store.rules.push(saved); store.epoch += 1
      return copy(saved)
    },
    updateSensitiveRule: async (id, body) => {
      calls.push(['update', id, copy(body)])
      const index = store.rules.findIndex(item => item.ruleId === id)
      const saved = { ...store.rules[index], ...body, revision: store.rules[index].revision + 1 }
      delete saved.requestId; delete saved.expectedRevision; delete saved.acknowledgeSimilarRuleId
      store.rules[index] = saved; store.epoch += 1
      return copy(saved)
    },
    deleteSensitiveRule: async (id, expectedRevision) => {
      calls.push(['delete', id, expectedRevision])
      store.rules = store.rules.filter(item => item.ruleId !== id); store.epoch += 1
      return { deleted: true, ruleId: id }
    },
    ...overrides,
  }
  const exports = {}
  new Function('require', 'exports', compiled)(id => id === 'vue'
    ? { ...Vue, onMounted: () => {}, onUnmounted: fn => cleanup.push(fn) }
    : { api, ApiError }, exports)
  const scope = Vue.effectScope()
  const exposed = {}
  const ui = scope.run(() => exports.default.setup({}, { expose: value => Object.assign(exposed, value) }))
  return { ui, exposed, api, store, calls, close() { cleanup.forEach(fn => fn()); scope.stop() } }
}

// Capacity and built-in/custom capabilities come from the server response.
{
  const h = setup()
  await h.ui.refresh()
  assert.equal(h.ui.customRemaining.value, 2)
  assert.equal(h.ui.canCreate.value, true)
  h.ui.choose('builtin-id')
  h.ui.startEdit()
  assert.equal(h.ui.editorMode.value, null, 'immutable built-in rules cannot be edited')
  await h.ui.toggle(h.ui.selected.value)
  assert.equal(h.calls.length, 0, 'immutable built-in rules cannot be toggled')
  h.store.maxCustomRules = 1
  await h.ui.refresh()
  assert.equal(h.ui.canCreate.value, false, 'create uses dynamic server capacity')
  h.close()
}

// Create/edit/toggle/delete write only the public rule input and immediately explain effect.
{
  const h = setup()
  await h.ui.refresh()
  h.ui.startCreate()
  Object.assign(h.ui.draft, {
    name: '家庭住址', description: '保护具体家庭住址和门牌信息', examplesText: '我住在幸福路 1 号',
    counterExamplesText: '城市名称', deliveryMode: 'always_mask', allowOriginalAfterConfirm: true,
  })
  await h.ui.save()
  const createBody = h.calls[0][1]
  assert.match(createBody.requestId, /^[0-9a-f-]{36}$/)
  assert.equal(createBody.allowOriginalAfterConfirm, false, 'non-confirm delivery never allows original')
  assert.equal('masking' in createBody, false)
  assert.match(h.ui.notice.value, /立即生效/)

  h.ui.startEdit(); h.ui.draft.description = '保护精确到门牌的家庭地址'
  await h.ui.save()
  const updateBody = h.calls.at(-1)[2]
  assert.equal(updateBody.expectedRevision, 1)
  assert.equal('masking' in updateBody, false)
  await h.ui.toggle(h.ui.selected.value)
  assert.equal(h.calls.at(-1)[2].enabled, false)
  assert.match(h.ui.notice.value, /立即生效/)
  const selected = h.ui.selected.value
  h.ui.deleteConfirmId.value = selected.ruleId
  await h.ui.remove(selected)
  assert.deepEqual(h.calls.at(-1), ['delete', selected.ruleId, selected.revision])
  assert.match(h.ui.notice.value, /立即生效/)
  h.close()
}

// Similarity is a two-step user decision: load the referenced rule, then issue a fresh request id with acknowledgement.
{
  let phase = 0
  const h = setup({
    createSensitiveRule: async body => {
      h.calls.push(['create', copy(body)])
      if (phase++ === 0) {
        throw new ApiError('发现相似规则', 409, 'CUSTOM_RULE_SIMILAR', undefined, undefined, undefined, 'builtin-id')
      }
      if (phase === 2) throw new TypeError('network result unknown')
      return rule({ ...body, ruleId: 'confirmed-confirmed', revision: 1, source: 'custom', immutable: false })
    },
  })
  await h.ui.refresh(); h.ui.startCreate()
  Object.assign(h.ui.draft, { name: '证件号码', description: '保护个人证件号码和签发信息', examplesText: '证件号 1234567890' })
  await h.ui.save()
  assert.equal(h.ui.similarRule.value.ruleId, 'builtin-id')
  assert.equal(h.calls.length, 1)
  const firstRequestId = h.calls[0][1].requestId
  await h.ui.confirmSimilar()
  assert.equal(h.calls.length, 2)
  assert.notEqual(h.calls[1][1].requestId, firstRequestId)
  assert.equal(h.calls[1][1].acknowledgeSimilarRuleId, 'builtin-id')
  const confirmationRequestId = h.calls[1][1].requestId
  await h.ui.confirmSimilar()
  assert.equal(h.calls.length, 3)
  assert.equal(h.calls[2][1].requestId, confirmationRequestId, 'uncertain confirmation retry keeps its idempotency identity')
  assert.equal(h.ui.similarRule.value, null)
  h.close()
}

// An ordinary create freezes its request id for a same-draft retry after an unknown network result.
{
  const h = setup()
  let attempts = 0
  h.api.createSensitiveRule = async body => {
    h.calls.push(['create', copy(body)])
    if (attempts++ === 0) throw new TypeError('network result unknown')
    return rule({ ...body, ruleId: 'retried-create', revision: 1, source: 'custom', immutable: false })
  }
  await h.ui.refresh(); h.ui.startCreate()
  Object.assign(h.ui.draft, { name: '工作编号', description: '保护内部员工工作编号信息', examplesText: '工号 A-1001' })
  await h.ui.save()
  assert.match(h.ui.error.value, /network result unknown/)
  const firstRequestId = h.calls[0][1].requestId
  await h.ui.save()
  assert.equal(h.calls[1][1].requestId, firstRequestId)
  h.close()
}

// Error parsing retains only the safe similar rule identifier needed for the confirmation read.
{
  const response = new Response(JSON.stringify({ detail: {
    code: 'CUSTOM_RULE_SIMILAR', message: '需要确认', similarRuleId: 'builtin-id',
  } }), { status: 409, headers: { 'content-type': 'application/json' } })
  await assert.rejects(throwApiError(response), error => error instanceof ApiError
    && error.code === 'CUSTOM_RULE_SIMILAR' && error.similarRuleId === 'builtin-id')

  const unsafe = new Response(JSON.stringify({ detail: {
    code: 'CUSTOM_RULE_SIMILAR', message: '需要确认', similarRuleId: 'bad\r\nid',
  } }), { status: 409, headers: { 'content-type': 'application/json' } })
  await assert.rejects(throwApiError(unsafe), error => error instanceof ApiError
    && error.code === 'CUSTOM_RULE_SIMILAR' && error.similarRuleId === undefined)
}

assert.match(apiSource, /\/mindos\/settings\/sensitive-rules/)
assert.match(apiSource, /JSON\.stringify\(\{ expectedRevision \}\)/)
assert.doesNotMatch(source, /\bpattern\b|localStorage|sessionStorage|v-html/i)
assert.doesNotMatch(source, /maskingText|JSON\.parse/)
assert.doesNotMatch(source, /secret|etag/i)
console.log('sensitive rules: dynamic capacity, immutable built-ins, CRUD, immediate effect and explicit similar-rule acknowledgement passed')
