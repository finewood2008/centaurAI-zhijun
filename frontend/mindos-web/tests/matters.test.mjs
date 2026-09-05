// Run real helpers and component setup against isolated in-memory stores; no HTTP/model calls.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import * as helpers from '../src/shared/matters.ts'

const original = { id:'synthetic-matter', title:'合伙人职责沟通', goal:'说清责任', context:'预算有限', nextStep:'准备提纲', outcome:'', status:'active', revision:4 }
const draft = helpers.matterDraft(original)
assert.deepEqual(Object.keys(draft), ['title','goal','context','nextStep','outcome','status'])
draft.context = '手动修改'; assert.equal(original.context, '预算有限')
assert.equal(helpers.cleanFilename('  '), '知君文稿.md')
assert.equal(helpers.cleanFilename('合伙人/职责:沟通?'), '合伙人-职责-沟通-.md')
assert.doesNotMatch(helpers.cleanFilename('a\u0000b\\c|d'), /[\\/:*?"<>|\u0000-\u001f]/)
assert.ok(helpers.cleanFilename('字'.repeat(120)).length <= 83)
const routes = await readFile(new URL('../../../backend/mindos/matters_routes.py', import.meta.url), 'utf8')
const kinds = [...routes.match(/ArtifactKind = Literal\[([^\]]+)\]/)[1].matchAll(/"([^"]+)"/g)].map(m => m[1])
assert.deepEqual([...Object.keys(helpers.artifactPrompts), 'freeform'].sort(), kinds.sort(), 'preparation types match backend contract')
for (const prompt of Object.values(helpers.artifactPrompts)) {
  assert.ok(prompt.startsWith('请'), 'preparation is a request, not a stated user belief')
  assert.match(prompt, /未知|待补充|待确认|待我确认|留空|不要编造|不替我编造/)
}

const clone = value => JSON.parse(JSON.stringify(value))
const tick = async () => { await Vue.nextTick(); await new Promise(resolve => setImmediate(resolve)) }
const defer = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }
const record = (id, cid = 'conversation-a') => ({ ...original, id, revision:1, conversationId:cid, decisionId:null, createdAt:'2026-09-05T00:00:00Z', updatedAt:'2026-09-05T00:00:00Z' })
const reply = '# 沟通提纲\n\n' + '先确认目标、约束和双方责任。\n'.repeat(30) + '\nEND-OF-COMPLETE-REPLY'
async function componentCode(name) {
  const source = await readFile(new URL('../src/components/matters/' + name + '.vue', import.meta.url), 'utf8')
  return ts.transpileModule(compileScript(parse(source).descriptor, { id:'matters-' + name }).content, { compilerOptions:{ module:ts.ModuleKind.CommonJS, target:ts.ScriptTarget.ES2022 } }).outputText
}
const code = await componentCode('MatterWorkspace')
function setup() {
  const props = Vue.reactive({ conversationId:'conversation-a', disabled:false })
  const state = { bindings:{ 'conversation-a':{ matter:null, bindingRevision:0 }, 'conversation-b':{ matter:record('matter-b','conversation-b'), bindingRevision:1 } }, artifacts:[], wait:null,writeWait:null }
  const calls = [], emits = [], cleanups = [], exports = {}
  const api = {
    artifactLabels:Object.fromEntries(kinds.map(k => [k, k])),
    async getMatterBinding(cid) { calls.push(['binding',cid]); const value = clone(state.bindings[cid]); if (state.wait?.cid === cid) { const wait = state.wait; state.wait = null; await wait.promise } return value },
    async listMatters() { calls.push(['list']); return { items:Object.values(state.bindings).map(b => b.matter).filter(Boolean) } },
    async listArtifacts(id) { calls.push(['artifacts',id]); return { items:clone(state.artifacts.filter(a => a.matterId === id)) } },
    async createMatter(data) { calls.push(['create',clone(data)]); if (state.writeWait) await state.writeWait.promise; const matter = { ...record('matter-a',data.conversationId), title:data.title }; state.bindings[data.conversationId] = { matter,bindingRevision:1 }; return clone(matter) },
    async bindMatter(cid,id,revision,requestId) { calls.push(['bind',cid,id,revision,requestId]); assert.equal(revision,state.bindings[cid].bindingRevision); state.bindings[cid] = { matter:id ? record(id,cid) : null,bindingRevision:revision+1 }; return clone(state.bindings[cid]) },
    async updateMatter(id,data) { calls.push(['edit',id,clone(data)]); const binding = Object.values(state.bindings).find(b => b.matter?.id === id); assert.equal(data.expectedRevision,binding.matter.revision); binding.matter = { ...binding.matter,...data,revision:data.expectedRevision+1 }; return clone(binding.matter) },
    async saveArtifact(id,data) { calls.push(['save-reply',id,clone(data)]); assert.equal(data.messageId,'assistant-complete'); assert.equal('markdown' in data,false,'server must fetch the complete source message'); const artifact = { id:'artifact-a',matterId:id,title:'完整沟通提纲',kind:data.kind,markdown:reply,revision:1,userEdited:false,sourceMessageId:data.messageId,sourceConversationId:data.conversationId }; state.artifacts = [artifact]; return clone(artifact) },
    async updateArtifact(id,data) { calls.push(['save-document',id,clone(data)]); const artifact = state.artifacts.find(a => a.id === id); assert.equal(data.expectedRevision,artifact.revision); Object.assign(artifact,{ title:data.title,markdown:data.markdown,revision:artifact.revision+1,userEdited:true }); return clone(artifact) },
  }
  new Function('require','exports',code)(id => {
    if (id === 'vue') return { ...Vue,onBeforeUnmount:fn => cleanups.push(fn) }
    if (id.includes('services/matters')) return api
    if (id.includes('shared/matters')) return helpers
    if (id.includes('useToast')) return { useToast:() => () => {} }
    if (id.endsWith('.vue')) return { default:{} }
    throw new Error('Unmocked import: ' + id)
  }, exports)
  const scope = Vue.effectScope()
  const ui = scope.run(() => exports.default.setup(props,{ expose() {},emit:(...args) => emits.push(args) }))
  return { ui,props,state,calls,emits,close() { cleanups.forEach(fn => fn()); scope.stop() } }
}
{
  const h = setup(); await tick()
  await h.ui.show()
  assert.equal(h.ui.open.value,true)
  assert.ok(h.calls.every(c => ['binding','artifacts','list'].includes(c[0])), 'opening only reads local stores')
  h.ui.newTitle.value = '  与合伙人说明分工  '; await h.ui.connect(true)
  assert.equal(h.ui.matter.value.title,'与合伙人说明分工')
  assert.equal(h.calls.find(c => c[0] === 'create')[1].conversationId,'conversation-a')
  const before = h.calls.length
  h.ui.prepare('communication')
  assert.deepEqual(h.emits,[['prepare',helpers.artifactPrompts.communication]])
  assert.equal(h.calls.length,before,'prepare does not call a model or write a message')
  h.ui.saveFromReply({ id:'assistant-complete',content:reply }); await tick(); await h.ui.saveReply()
  assert.equal(h.ui.markdown.value,reply,'saved content includes the full reply beyond its 180-character preview')
  h.ui.markdown.value += '\n\n我补充的具体限制。'; h.ui.documentTitle.value = '我的沟通提纲'; await h.ui.saveDocument()
  assert.equal(h.state.artifacts[0].userEdited,true)
  h.ui.selectedArtifact.value = null; await h.ui.load(); h.ui.editDocument(h.ui.artifacts.value[0])
  assert.equal(h.ui.documentTitle.value,'我的沟通提纲'); assert.ok(h.ui.markdown.value.endsWith('我补充的具体限制。'))
  h.close()
}
{
  const h = setup(); await tick()
  h.state.bindings['conversation-a'] = { matter:record('matter-a'),bindingRevision:1 }
  const wait = defer(); h.state.wait = { cid:'conversation-a',...wait }
  const oldLoad = h.ui.load()
  h.props.conversationId = 'conversation-b'; await tick()
  assert.equal(h.ui.matter.value.id,'matter-b')
  wait.resolve(); await oldLoad; await tick()
  assert.equal(h.ui.matter.value.id,'matter-b','late old-conversation data cannot replace current workspace')
  assert.equal(h.ui.open.value,false); assert.equal(h.emits.length,0)
  h.close()
}
{
  const h = setup(); await tick()
  h.state.writeWait = defer(); h.ui.newTitle.value = '保存还在途中'
  const pending = h.ui.connect(true)
  h.props.conversationId = 'conversation-b'; await tick()
  h.state.writeWait.resolve(); await pending; await tick()
  assert.equal(h.state.bindings['conversation-a'].matter.title,'保存还在途中','the already-requested write remains bound to its original conversation')
  assert.equal(h.ui.matter.value.id,'matter-b','late writes cannot replace the newly selected workspace')
  assert.equal(h.ui.newTitle.value,''); assert.equal(h.emits.length,0)
  h.props.conversationId = 'conversation-a'; await tick()
  h.ui.fields.goal = '还没保存的用户原文'
  h.props.conversationId = 'conversation-b'; await tick()
  h.props.conversationId = 'conversation-a'; await tick()
  assert.equal(h.ui.fields.goal,'还没保存的用户原文','switching away and back preserves pending manual edits')
  assert.equal(h.ui.matter.value.revision,1,'restoring a manual draft does not silently rebase its revision')
  h.close()
}
{
  const exports = {}, mounts = [], cleanups = [], calls = [], pushed = []
  const homeCode = await componentCode('MattersHome')
  new Function('require','exports',homeCode)(id => {
    if (id === 'vue') return { ...Vue,onMounted:fn => mounts.push(fn),onBeforeUnmount:fn => cleanups.push(fn) }
    if (id === 'vue-router') return { useRouter:() => ({ push:path => pushed.push(path) }) }
    if (id.includes('services/matters')) return { listMatters:async status => { calls.push(['list',status]); return { items:[record('matter-a')] } },continueMatter:async value => { calls.push(['continue',value.id]); return value.conversationId } }
    throw new Error('Unmocked home import: ' + id)
  },exports)
  const scope = Vue.effectScope(); const ui = scope.run(() => exports.default.setup({}, { expose() {} }))
  await Promise.all(mounts.map(fn => fn()))
  assert.deepEqual(calls,[['list','active']]); assert.equal(pushed.length,0,'home read does not create or enter a conversation')
  await ui.resume(ui.items.value[0]); assert.deepEqual(pushed,['/c/conversation-a'])
  cleanups.forEach(fn => fn()); scope.stop()
}
{
  // Exercise the real service as well: stale homepage data and a failed bind cannot create duplicates.
  const source = await readFile(new URL('../src/services/matters.ts', import.meta.url), 'utf8')
  const serviceCode = ts.transpileModule(source, { compilerOptions:{ module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022 } }).outputText
  const exports = {}, calls = []
  let fresh = record('matter-service','fresh-conversation'), failBind = true
  new Function('require','exports',serviceCode)(id => {
    if (id === './api') return { createConversation:async data => { calls.push(['create-conversation',data]); return { id:'new-conversation' } } }
    if (id === './taskRouting') return { routingRequest:async (path,method = 'GET',data) => {
      calls.push([method,path,data])
      if (path === '/mindos/matters/matter-service') return clone(fresh)
      if (path === '/mindos/conversations/new-conversation/matter' && method === 'GET') return { matter:null,bindingRevision:2 }
      if (path === '/mindos/conversations/new-conversation/matter' && method === 'PUT') {
        assert.equal(data.expectedRevision,2)
        if (failBind) { failBind = false; throw new Error('synthetic bind network failure') }
        fresh.conversationId = 'new-conversation'
        return { matter:clone(fresh),bindingRevision:3 }
      }
      throw new Error('Unexpected service request: ' + method + ' ' + path)
    } }
    throw new Error('Unmocked service import: ' + id)
  },exports)
  assert.equal(await exports.continueMatter(record('matter-service','stale-conversation'),'request-fresh'),'fresh-conversation')
  assert.equal(calls.filter(c => c[0] === 'create-conversation').length,0,'re-read the current binding before creating anything')
  fresh = { ...fresh,conversationId:null }
  await assert.rejects(exports.continueMatter(fresh,'request-retry'),/synthetic bind network failure/)
  assert.equal(await exports.continueMatter(fresh,'request-retry'),'new-conversation')
  assert.equal(calls.filter(c => c[0] === 'create-conversation').length,1,'reuse the created conversation after bind failure')
  assert.equal(calls.filter(c => c[0] === 'PUT').length,2)
}
console.log('matters: helpers/kind contract, read-only opening, explicit create/bind, preparation without calls, full reply/edit/reload, stale conversation response, home resume and failed-bind retry passed')
