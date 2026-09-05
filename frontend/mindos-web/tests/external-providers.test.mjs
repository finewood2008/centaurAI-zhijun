// Execute the real SFC setup with fake APIs, no DOM/network or saved credentials.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import { ApiError } from '../src/services/api.ts'

const source = await readFile(new URL('../src/components/conversation/ExternalProvidersPanel.vue', import.meta.url), 'utf8')
const compiled = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'external-test' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const copy = value => JSON.parse(JSON.stringify(value))
const defer = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
const flush = () => new Promise(resolve => setImmediate(resolve))
function setup(overrides = {}) {
  const store = { version: 4, providers: [
    { id: 'a', name: '服务 A', revision: 1, baseUrl: 'https://a.example.invalid/v1', model: 'a-one', apiKeyConfigured: true, active: true },
    { id: 'b', name: '服务 B', revision: 1, baseUrl: 'https://b.example.invalid/v1', model: 'b-one', apiKeyConfigured: true, active: false },
  ] }
  const calls = [], cleanup = [], emits = []
  const api = {
    getExternalProviders: async () => copy({ providers: store.providers, activeProviderId: store.providers.find(p => p.active)?.id || null, chatRevision: store.version }),
    getExternalProviderModels: async (id, revision) => ({ providerId: id, revision, models: [id + '-one', id + '-two'] }),
    createExternalProvider: async body => { calls.push(['create', copy(body)]); const p = { ...body, id: 'new', revision: 1, model: '', apiKeyConfigured: true, active: false }; delete p.apiKey; store.providers.push(p); return copy(p) },
    updateExternalProvider: async (id, body) => { calls.push(['update', copy(body)]); const p = store.providers.find(p => p.id === id); Object.assign(p, { name: body.name, baseUrl: body.baseUrl, revision: p.revision + 1 }); return copy(p) },
    activateExternalProvider: async (id, body) => { calls.push(['activate', copy(body)]); if (body.chatRevision !== store.version) throw new ApiError('冲突', 409, 'conflict'); const p = store.providers.find(p => p.id === id); store.providers.forEach(p => { p.active = p.id === id }); p.model = body.model; p.revision += 1; store.version += 1; return { provider: copy(p), chat: { revision: store.version, externalEnabled: true } } },
    deleteExternalProvider: async (id, revision) => { calls.push(['delete', id, revision]); store.providers = store.providers.filter(p => p.id !== id); return { deleted: true } },
    ...overrides,
  }
  const exports = {}
  new Function('require', 'exports', compiled)(id => id === 'vue' ? { ...Vue, onMounted: () => {}, onUnmounted: fn => cleanup.push(fn) } : { api, ApiError }, exports)
  const scope = Vue.effectScope()
  const props = Vue.reactive({ chatRevision: 4 })
  const ui = scope.run(() => exports.default.setup(props, { expose: () => {}, emit: (...args) => emits.push(args) }))
  return { ui, api, store, calls, emits, props, close() { cleanup.forEach(fn => fn()); scope.stop() } }
}

// Initial read does not query suppliers; save explicitly fetches available models, not activate.
{
  const h = setup(); let modelCalls = 0
  h.api.getExternalProviderModels = async (id, revision) => { modelCalls++; return { providerId: id, revision, models: ['new-one', 'new-two'] } }
  await h.ui.refresh(); assert.equal(modelCalls, 0)
  h.ui.choose(''); Object.assign(h.ui.draft, { name: '新服务', baseUrl: 'https://new.example.invalid/v1', apiKey: 'synthetic-token' })
  await h.ui.save()
  assert.equal(modelCalls, 1)
  assert.equal(h.ui.activeId.value, 'a')
  assert.equal(h.ui.draft.apiKey, '')
  assert.equal(h.ui.manual.value, false)
  assert.deepEqual(h.ui.models.value, ['new-one', 'new-two'])
  h.ui.model.value = 'new-two'; await h.ui.activate()
  assert.equal(h.ui.activeId.value, 'new')
  assert.equal(h.store.providers.find(p => p.active).model, 'new-two')
  assert.equal(h.ui.cache.get('new').revision, h.ui.selected.value.revision)
  h.close()
}

// Blank token keeps existing credentials; a changed endpoint requires a new token.
{
  const h = setup(); await h.ui.refresh(); h.ui.edit()
  h.ui.draft.name = '服务 A 改名'; await h.ui.save()
  assert.equal('apiKey' in h.calls[0][1], false)
  h.ui.edit(); h.ui.draft.baseUrl = 'https://other.example.invalid/v1'; await h.ui.save()
  assert.match(h.ui.error.value, /重新输入对应的 Token/)
  assert.equal(h.calls.length, 1)
  assert.equal(h.ui.draft.baseUrl, 'https://other.example.invalid/v1')
  h.close()
}

// Stale models cannot replace another supplier or a user's manually typed model.
{
  const h = setup(); await h.ui.refresh()
  const waiting = defer()
  h.api.getExternalProviderModels = async (id, revision) => id === 'a' ? waiting.promise : { providerId: id, revision, models: ['b-one', 'b-two'] }
  const old = h.ui.fetchModels(); h.ui.choose('b'); await flush()
  waiting.resolve({ providerId: 'a', revision: 1, models: ['a-wrong'] }); await old
  assert.equal(h.ui.selectedId.value, 'b')
  assert.deepEqual(h.ui.models.value, ['b-one', 'b-two'])
  const typing = defer(); h.api.getExternalProviderModels = async () => typing.promise
  const req = h.ui.fetchModels(); h.ui.manual.value = true; h.ui.model.value = 'typed-model'; h.ui.modelEditRevision += 1
  typing.resolve({ providerId: 'b', revision: 1, models: ['b-one'] }); await req
  assert.equal(h.ui.model.value, 'typed-model'); assert.equal(h.ui.manual.value, true)
  h.close()
}

// A slower read cannot revert a completed activation; 409 can recover via a refreshed chat revision.
{
  const h = setup(); await h.ui.refresh()
  const stale = await h.api.getExternalProviders(); const waiting = defer()
  const originalRead = h.api.getExternalProviders
  h.api.getExternalProviders = async () => waiting.promise
  const read = h.ui.refresh(); h.ui.model.value = 'a-two'; await h.ui.activate()
  waiting.resolve(stale); await read
  assert.equal(h.ui.selected.value.model, 'a-two'); assert.equal(h.ui.serverChatRevision.value, 5)
  h.store.version = 8; await h.ui.activate(); assert.match(h.ui.error.value, /在别处更新/)
  h.api.getExternalProviders = originalRead; await h.ui.refresh(); await h.ui.activate()
  assert.equal(h.calls.at(-1)[1].chatRevision, 8)
  assert.equal(h.ui.serverChatRevision.value, 9)
  h.props.chatRevision = 6; await Vue.nextTick()
  assert.equal(h.ui.serverChatRevision.value, 9, 'late parent config cannot lower known revision')
  h.close()
}

// Editing during an outstanding save and refresh does not overwrite the ongoing draft.
{
  const h = setup(); await h.ui.refresh(); h.ui.edit(); h.ui.draft.name = '提交的名称'
  const saved = defer(); h.api.updateExternalProvider = async () => saved.promise
  const operation = h.ui.save(); h.ui.draft.name = '继续输入的名称'
  saved.resolve({ ...h.store.providers[0], name: '提交的名称', revision: 2 }); await operation
  assert.equal(h.ui.draft.name, '继续输入的名称'); assert.equal(h.ui.editing.value, true)
  h.ui.selectProvider('b'); assert.equal(h.ui.selectedId.value, 'a'); assert.equal(h.ui.pendingSelection.value, 'b')
  h.ui.pendingSelection.value = null
  h.store.providers[0].revision = 3; h.store.providers[0].baseUrl = 'https://changed.example.invalid/v1'
  h.ui.model.value = 'keep-my-model'; await h.ui.refresh()
  assert.deepEqual(h.ui.models.value, []); assert.equal(h.ui.model.value, 'keep-my-model'); assert.equal(h.ui.manual.value, true)
  assert.equal(h.ui.draft.name, '继续输入的名称')
  assert.equal(h.ui.outdatedDraft.value, true)
  await h.ui.save(); assert.match(h.ui.error.value, /请先核对新版/)
  h.close()
}

// List failure supports manual fallback, and removing an inactive provider never changes the default.
{
  const h = setup(); await h.ui.refresh()
  h.api.getExternalProviderModels = async () => { throw new ApiError('模型列表不可用', 503) }
  await h.ui.fetchModels(); assert.equal(h.ui.model.value, 'a-one'); assert.equal(h.ui.manual.value, true)
  assert.match(h.ui.modelError.value, /手动填写/)
  await h.ui.remove(); assert.equal(h.calls.length, 0, 'active provider cannot be deleted')
  h.ui.choose('b'); await flush(); await h.ui.remove()
  assert.equal(h.ui.activeId.value, 'a'); assert.equal(h.ui.providers.value.length, 1)
  h.close()
}

assert.doesNotMatch(source, /localStorage|sessionStorage|v-html/)
assert.match(source, /type="password"/)
assert.match(source, /已启用在线理解的对话/)
assert.match(source, /仅本地的对话保持不变/)
console.log('external providers: real SFC save/discovery/default, token boundary, stale reads/models, editing, conflict refresh, fallback and inactive deletion passed')
