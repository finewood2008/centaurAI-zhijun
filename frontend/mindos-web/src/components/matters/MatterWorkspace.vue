<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import SideDrawer from '@/components/ui/SideDrawer.vue'
import MessageBubble from '@/components/conversation/MessageBubble.vue'
import { useToast } from '@/composables/useToast'
import { artifactLabels, bindMatter, createMatter, getMatterBinding, listArtifacts, listMatters, saveArtifact, updateArtifact, updateMatter,
  type ArtifactKind, type Matter, type MatterArtifact, type MatterStatus } from '@/services/matters'
import { artifactPrompts, cleanFilename, matterDraft } from '@/shared/matters'

const props = defineProps<{ conversationId: string; disabled?: boolean; suspension?: { matterId: string; revision: number } | null }>()
const emit = defineEmits<{ (e: 'prepare', text: string): void }>()
const toast = useToast()
const open = ref(false), busy = ref(false), loading = ref(false), error = ref(''), notice = ref('')
const matter = ref<Matter | null>(null), all = ref<Matter[]>([]), artifacts = ref<MatterArtifact[]>([])
const bindingRevision = ref(0), selectedId = ref(''), newTitle = ref('')
const suspended = computed(() => props.suspension?.matterId === matter.value?.id && props.suspension?.revision === bindingRevision.value)
const fields = reactive({ title: '', goal: '', context: '', nextStep: '', outcome: '', status: 'active' as MatterStatus })
const dirty = computed(() => matter.value && JSON.stringify(fields) !== JSON.stringify(matterDraft(matter.value)))
const selectedArtifact = ref<MatterArtifact | null>(null), documentTitle = ref(''), markdown = ref(''), preview = ref(false)
const documentDirty = computed(() => selectedArtifact.value && (documentTitle.value !== selectedArtifact.value.title || markdown.value !== selectedArtifact.value.markdown))
const pendingMessage = ref<{ id: string; content: string } | null>(null), kind = ref<ArtifactKind>('freeform')
let epoch = 0, loadSequence = 0, alive = true
const operationKeys = new Map<string, string>()
function key(operation: string, value: unknown) {
  const signature = props.conversationId + ':' + operation + ':' + JSON.stringify(value)
  if (!operationKeys.has(signature)) operationKeys.set(signature, crypto.randomUUID())
  return operationKeys.get(signature)!
}
function assign(value: Matter | null) { matter.value = value; if (value) Object.assign(fields, matterDraft(value)) }
function editDocument(value: MatterArtifact) { selectedArtifact.value = value; documentTitle.value = value.title; markdown.value = value.markdown; preview.value = false }
const drafts = new Map<string, { base: Matter; fields: typeof fields; artifact: MatterArtifact | null; title: string; markdown: string }>()
function rememberDraft() {
  if (matter.value && (dirty.value || documentDirty.value)) drafts.set(matter.value.id, { base: matter.value, fields: { ...fields }, artifact: selectedArtifact.value, title: documentTitle.value, markdown: markdown.value })
}
async function load() {
  const ticket = epoch, sequence = ++loadSequence, cid = props.conversationId
  loading.value = true; error.value = ''
  try {
    const binding = await getMatterBinding(cid)
    const result = binding.matter ? await listArtifacts(binding.matter.id) : { items: [] }
    if (!alive || ticket !== epoch || sequence !== loadSequence) return
    assign(binding.matter); bindingRevision.value = binding.bindingRevision; artifacts.value = result.items
    const saved = binding.matter && drafts.get(binding.matter.id)
    if (saved) {
      matter.value = saved.base // Retain the edit's original revision; do not silently rebase over newer edits.
      Object.assign(fields, saved.fields); selectedArtifact.value = saved.artifact; documentTitle.value = saved.title; markdown.value = saved.markdown
      notice.value = '保留了上次尚未保存的编辑，请核对后保存。'
    }
  } catch (e) { if (ticket === epoch && sequence === loadSequence) error.value = e instanceof Error ? e.message : '事情读取失败' }
  finally { if (ticket === epoch && sequence === loadSequence) loading.value = false }
}
watch(() => props.conversationId, () => {
  rememberDraft(); epoch++; busy.value = false; open.value = false; assign(null); artifacts.value = []; all.value = []
  selectedArtifact.value = null; pendingMessage.value = null; notice.value = ''; selectedId.value = ''; newTitle.value = ''; operationKeys.clear()
  void load()
}, { immediate: true })
onBeforeUnmount(() => { alive = false; epoch++ })
async function show() {
  open.value = true
  const ticket = epoch
  try { const result = await listMatters('all'); if (ticket === epoch) all.value = result.items }
  catch (e) { if (ticket === epoch) error.value = e instanceof Error ? e.message : '事情列表读取失败' }
}
async function run(operation: (valid: () => boolean) => Promise<void>) {
  if (busy.value || props.disabled) return
  const ticket = epoch
  busy.value = true; error.value = ''; notice.value = ''
  try { await operation(() => alive && ticket === epoch) }
  catch (e) { if (ticket === epoch) error.value = e instanceof Error ? e.message : '未能保存，编辑仍保留' }
  finally { if (ticket === epoch) busy.value = false }
}
async function connect(create = false) {
  if (dirty.value || documentDirty.value) { error.value = '请先保存编辑，或明确放弃编辑后再更换事情。'; return }
  if (create && matter.value) { error.value = '请先解除本段关联，再新建另一件事；原来的事情和文稿会保留。'; return }
  await run(async valid => {
    const cid = props.conversationId
    if (create) {
      const title = newTitle.value.trim()
      if (!title) throw new Error('请给这件事起一个简短名称')
      await createMatter({ requestId: key('create', title), title, conversationId: cid })
    } else {
      await bindMatter(cid, selectedId.value || null, bindingRevision.value, key('bind', [selectedId.value, bindingRevision.value]))
    }
    if (!valid()) return
    selectedArtifact.value = null; drafts.delete(matter.value?.id || '')
    await load()
    if (!valid()) return
    newTitle.value = ''; notice.value = matter.value ? '已关联到这段对话；没有发送消息、修改本体或增加在线授权。' : '已解除本段关联；事情、文稿与原对话仍然保留。'
  })
}
async function saveDetails() {
  if (!matter.value) return
  const record = matter.value, data = { ...fields }
  await run(async valid => {
    const result = await updateMatter(record.id, { ...data, expectedRevision: record.revision, requestId: key('edit', [record.id, record.revision, data]) })
    if (!valid()) return
    assign(result); drafts.delete(record.id); notice.value = '事情记录已保存；不会自动改变本体或人生章程。'
  })
}
async function saveReply() {
  if (!matter.value || !pendingMessage.value) return
  const id = matter.value.id, messageId = pendingMessage.value.id, selectedKind = kind.value
  await run(async valid => {
    const result = await saveArtifact(id, { conversationId: props.conversationId, messageId, kind: selectedKind, requestId: key('reply', [id, messageId, selectedKind]) })
    if (!valid()) return
    artifacts.value = [result, ...artifacts.value.filter(a => a.id !== result.id)]
    editDocument(result); pendingMessage.value = null; notice.value = '已留下本地文稿，可编辑、复制或下载；原有来源限制继续保留。'
  })
}
async function saveDocument() {
  if (!selectedArtifact.value) return
  const record = selectedArtifact.value, title = documentTitle.value, content = markdown.value
  await run(async valid => {
    const result = await updateArtifact(record.id, { title, markdown: content, expectedRevision: record.revision, requestId: key('document', [record.id, record.revision, title, content]) })
    if (!valid()) return
    editDocument(result); artifacts.value = artifacts.value.map(a => a.id === result.id ? result : a)
    drafts.delete(record.matterId)
    notice.value = '文稿已保存。修改不会解除原内容的来源和授权限制。'
  })
}
async function discardAndReload() {
  if (busy.value) return
  drafts.delete(matter.value?.id || ''); selectedArtifact.value = null; notice.value = ''
  await load()
}
function prepare(value: Exclude<ArtifactKind, 'freeform'>) {
  if (dirty.value) { error.value = '事情有未保存的修改，请先保存，再让知君参考。'; return }
  if (suspended.value || matter.value?.status !== 'active') { error.value = '这件事暂未用于日常对话。请先重新参考，或选择一起回顾结果。'; return }
  emit('prepare', artifactPrompts[value]); open.value = false
}
async function resumeReference() {
  if (!matter.value || dirty.value || documentDirty.value) { error.value = '请先保存尚未保存的编辑。'; return }
  const id = matter.value.id
  await run(async valid => {
    const result = await bindMatter(props.conversationId, id, bindingRevision.value, key('resume', [id, bindingRevision.value]))
    if (!valid()) return
    bindingRevision.value = result.bindingRevision
    notice.value = '下一轮将重新参考这件事，在线使用仍会检查授权。'
  })
}
function review() {
  if (dirty.value || documentDirty.value) { error.value = '请先保存结果或文稿，再开始回顾。'; return }
  emit('prepare', '我想回顾这件事：请结合已记录的目标、决定和实际结果，看看哪些与预期不同，哪些经验值得保留。未知结果请先说明，不替我把一次经历认定为长期原则。')
  open.value = false
}
async function copyDocument() {
  try { await navigator.clipboard.writeText(markdown.value); toast({ type: 'success', message: '文稿已复制' }) }
  catch { error.value = '复制未完成，可以在正文中选中复制，或下载 Markdown。' }
}
function download() {
  const url = URL.createObjectURL(new Blob([markdown.value], { type: 'text/markdown;charset=utf-8' }))
  const link = document.createElement('a'); link.href = url; link.download = cleanFilename(documentTitle.value); link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
function saveFromReply(message: { id: string; content: string }) { pendingMessage.value = message; kind.value = 'freeform'; void show() }
defineExpose({ saveFromReply, show })
</script>

<template>
  <button type="button" class="matter-trigger" aria-haspopup="dialog" :aria-expanded="open" @click="show"><span>{{ matter ? '这件事 · ' + matter.title : '事情与成果' }}</span></button>
  <SideDrawer :open="open" title="事情与成果" @close="open = false">
    <div class="matter-workspace" :aria-busy="busy || loading">
      <p class="muted">把同一件事跨对话接着推进。你选择保存什么；事情、文稿与结果不自动变成长期个人理解。</p>
      <p v-if="loading" role="status">正在读取…</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <p v-if="notice" class="notice" role="status">{{ notice }}</p>
      <button v-if="error && !matter" type="button" @click="load">重新读取</button>
      <template v-if="matter">
        <div class="matter-title"><h3>{{ matter.title }}</h3><span>{{ matter.status === 'active' ? '正在推进' : matter.status === 'paused' ? '暂时放下' : '已有结果' }}</span></div>
        <p v-if="suspended" class="muted">换题后已暂不参考这件事。<button :disabled="busy || disabled" @click="resumeReference">重新参考这件事</button></p>
        <p v-else-if="matter.status !== 'active'" class="muted">不自动带入普通聊天。需要时可一起回顾，或将状态改回“正在推进”。</p>
        <details>
          <summary>背景与进展<span v-if="dirty"> · 有未保存修改</span></summary>
          <form @submit.prevent="saveDetails">
            <label>事情名称<input v-model="fields.title" maxlength="120" required :disabled="busy" /></label>
            <label>希望达成什么<textarea v-model="fields.goal" rows="2" maxlength="2000" :disabled="busy" placeholder="可以暂时留空" /></label>
            <label>重要背景与约束<textarea v-model="fields.context" rows="4" maxlength="6000" :disabled="busy" placeholder="人物、时间、资源、已经明确的条件……" /></label>
            <label>下一步<textarea v-model="fields.nextStep" rows="2" maxlength="2000" :disabled="busy" placeholder="没有确定也没关系" /></label>
            <label>实际结果与新的发现<textarea v-model="fields.outcome" rows="3" maxlength="6000" :disabled="busy" placeholder="后来怎样了？哪些与预想不同？" /></label>
            <label>状态<select v-model="fields.status" :disabled="busy"><option value="active">正在推进</option><option value="paused">暂时放下</option><option value="completed">已有结果</option></select></label>
            <div class="actions"><button class="primary" :disabled="busy || disabled || !dirty || !fields.title.trim()">保存事情记录</button><button v-if="dirty || error" type="button" :disabled="busy" @click="discardAndReload">放弃本地编辑，载入最新版</button></div>
          </form>
        </details>
        <section class="prepare"><h3>一起准备一份可用的成果</h3><p class="muted">只填入对话请求，可修改再发送。不会现在就调用模型。</p><div class="actions"><button v-for="(prompt, id) in artifactPrompts" :key="id" type="button" :disabled="busy || disabled" @click="prepare(id)">{{ artifactLabels[id] }}</button></div></section>
        <div class="actions"><button :disabled="busy || disabled" @click="review">一起回顾这件事</button><RouterLink v-if="matter.decisionId" :to="{ path: '/judgments', query: { decisionId: matter.decisionId } }">查看关联判断</RouterLink></div>
      </template>
      <section v-if="pendingMessage" class="pending"><h3>将这条回复留下来</h3><p>{{ pendingMessage.content.slice(0, 180) }}{{ pendingMessage.content.length > 180 ? '…' : '' }}</p><p v-if="!matter" class="muted">先在下方新建或选择一件事，随后保存这份文稿。</p><label>文稿类型<select v-model="kind" :disabled="busy"><option v-for="(label, id) in artifactLabels" :key="id" :value="id">{{ label }}</option></select></label><div class="actions"><button class="primary" :disabled="!matter || busy || disabled" @click="saveReply">保存为可编辑文稿</button><button :disabled="busy" @click="pendingMessage = null">暂不保存</button></div></section>
      <section v-if="matter"><h3>留下的文稿 <small>{{ artifacts.length }}</small></h3><p v-if="!artifacts.length" class="muted">在知君完整回复下点“留下文稿”，即可在这里继续修改。</p><div v-else class="artifact-list"><button v-for="item in artifacts" :key="item.id" :aria-pressed="selectedArtifact?.id === item.id" :disabled="busy || !!documentDirty" @click="editDocument(item)">{{ item.title }}<small>{{ artifactLabels[item.kind] }} · 第 {{ item.revision }} 版</small></button></div></section>
      <section v-if="selectedArtifact" class="document"><label>文稿名称<input v-model="documentTitle" maxlength="120" :disabled="busy" /></label><div class="actions"><button :aria-pressed="!preview" @click="preview = false">编辑 Markdown</button><button :aria-pressed="preview" @click="preview = true">阅读</button></div><MessageBubble v-if="preview" role="assistant" :content="markdown" /><label v-else>完整正文<textarea v-model="markdown" class="document-text" rows="16" maxlength="50000" :disabled="busy" /></label><div class="actions"><button class="primary" :disabled="busy || disabled || !documentDirty || !markdown.trim() || !documentTitle.trim()" @click="saveDocument">保存文稿</button><button @click="copyDocument">复制</button><button @click="download">下载 .md</button></div><p class="muted">{{ documentDirty ? '有尚未保存的编辑；复制和下载使用当前正文。' : '本地已保存。' }} {{ selectedArtifact.userEdited ? '你修改过这份文稿。' : '来自知君回复，仍需你核对。' }}</p><details><summary>来源与版本</summary><p>从原回复保存后，编辑仍保留原始来源限制。</p><RouterLink :to="'/c/' + selectedArtifact.sourceConversationId">查看来源对话</RouterLink><p>文稿修订 {{ selectedArtifact.revision }} · {{ selectedArtifact.updatedAt }}</p></details><button v-if="documentDirty" :disabled="busy" @click="discardAndReload">放弃本地编辑，载入最新版</button></section>
      <details :open="!matter" class="connect"><summary>{{ matter ? '更换或解除本段关联' : '从一件事开始' }}</summary><p v-if="matter" class="muted">若要新建另一件事，请先解除本段关联。原来的事情与文稿会保留。</p><label>新事情的名称<input v-model="newTitle" maxlength="120" :disabled="busy || disabled || !!matter" placeholder="例如：准备与合伙人的职责沟通" @keyup.enter="connect(true)" /></label><button :disabled="busy || disabled || !!matter || !newTitle.trim() || !!dirty || !!documentDirty" @click="connect(true)">新建并关联本段对话</button><label>或选择已有事情<select v-model="selectedId" :disabled="busy || disabled"><option value="">不关联事情</option><option v-for="item in all" :key="item.id" :value="item.id">{{ item.title }}{{ item.status === 'completed' ? '（已有结果）' : item.status === 'paused' ? '（暂时放下）' : '' }}</option></select></label><button :disabled="busy || disabled || !!dirty || !!documentDirty" @click="connect(false)">{{ selectedId ? '关联所选事情' : '解除本段关联' }}</button><p class="muted">只改变本段对话的参考范围，不删除事情、文稿或聊天；外发仍需对应授权。</p></details>
    </div>
  </SideDrawer>
</template>
<style scoped>
.matter-trigger{display:inline-flex;max-width:220px;min-width:0;border:1px solid var(--ws-border-color-3,#ebe7de);border-radius:20px;background:transparent;color:var(--ws-text-color,#3c403d);padding:6px 10px;cursor:pointer;font:inherit;font-size:13px}.matter-trigger span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.matter-workspace{font-size:15px;line-height:1.75}.matter-workspace h3{font-size:18px;margin:0 0 10px}.matter-workspace p{margin:10px 0}.muted,small{color:var(--ws-text-secondary-color,#686b66);font-size:13px}.matter-workspace section,.matter-workspace details{border-top:1px solid var(--ws-border-color-3,#ebe7de);padding-top:18px;margin-top:22px}.matter-workspace summary{cursor:pointer;color:var(--ws-text-primary-color,#1d211f)}.matter-workspace label{display:grid;gap:6px;margin:14px 0;font-size:14px}.matter-workspace input,.matter-workspace textarea,.matter-workspace select{width:100%;min-width:0;box-sizing:border-box;border:1px solid var(--ws-border-color,#d8d3c8);border-radius:7px;padding:10px;background:var(--ws-card-bg,#fff);color:inherit;font:inherit}.matter-workspace textarea{resize:vertical;line-height:1.75}.matter-workspace button{font:inherit;font-size:14px;border:1px solid var(--ws-border-color,#d8d3c8);border-radius:8px;padding:7px 12px;background:transparent;color:inherit;cursor:pointer}.matter-workspace button:disabled{opacity:.45;cursor:not-allowed}.matter-workspace .primary{background:var(--ws-primary-color,#a6452e);border-color:transparent;color:white}.actions{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.notice{background:#4a7c5910;padding:12px;border-radius:8px}.error{color:var(--ws-danger-color,#a6452e)}.matter-title{display:flex;justify-content:space-between;gap:12px;align-items:baseline}.matter-title span{flex-shrink:0;font-size:12px}.artifact-list{display:grid;gap:8px}.artifact-list button{text-align:left;overflow-wrap:anywhere}.artifact-list small{display:block}.artifact-list button[aria-pressed=true]{border-color:var(--ws-primary-color,#a6452e)}.document-text{font-family:ui-monospace,"PingFang SC",monospace!important;min-height:300px}.pending{border-left:2px solid var(--ws-primary-color,#a6452e);padding-left:16px}
@media(max-width:600px){.matter-trigger{max-width:165px}.matter-title{display:block}.document-text{min-height:240px}}
</style>
