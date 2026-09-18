<script setup lang="ts">
import { createProductSessionStorage, isDesktopProduct } from '@/shared/productScope'
const productStorage = createProductSessionStorage()
// 输入区：Enter 发送、Shift+Enter 换行（提示只出现一次）；「深入」「我在考虑…」是两枚开关 chip；
// 麦克风在下方工具栏；字数只在快到上限时才出现。语音只填入输入框，永远不自动发送。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { appendReply, replaceReply, mergeReplyDrafts, undoReply, type ReplyAssistanceInput, type ReplyInputDraft } from '@/shared/replyAssistance'
import { replyNeedsRecovery } from '@/composables/useReplyRecovery'
import { Mic, MicOff, Send, Square, Plus } from 'lucide-vue-next'
import { DOC_EXTENSIONS, IMAGE_EXTENSIONS, AUDIO_EXTENSIONS } from '@/features/import/validation'
import BaseButton from '@/components/ui/BaseButton.vue'
import { useToast } from '@/composables/useToast'
import { intentHint } from '@/shared/decisionDraft'
import { createVoiceRecording, type VoiceState } from '@/services/voiceRecording'
import { createRecognizer, mergeTranscript, speechSupported, splitResults } from '@/shared/speech'

const props = defineProps<{
  streaming: boolean
  disabled?: boolean
  placeholder?: string
  allowDeliberate?: boolean
  // 模型没配置 / 不可用时的提示；给了就禁用输入，并在输入区上方显示同一句话（带去设置的链接）
  notice?: string
  noticeTo?: string
  hasAttachments?: boolean
  uploading?: boolean
  retrievalOnly?: boolean
  conversationId?: string | null
  // 沉浸壳：不显示两枚常驻 chip，「整理成判断」「展开分析」收进「+」菜单；意图提示行照旧
  quiet?: boolean
}>()

const emit = defineEmits<{
  (e: 'send', content: string, depth: 'brief' | 'deep', mode: 'chat' | 'deliberate', origin?: ReplyAssistanceInput): void
  (e: 'stop'): void
  (e: 'files', files: FileList): void
  (e: 'pick-materials'): void
}>()

const text = ref('')
const expression = ref<ReplyAssistanceInput>()
type InputUndo = NonNullable<ReplyInputDraft['undo']>
type InputDraft = ReplyInputDraft
const undo = ref<InputUndo>()
let lastSubmission: { conversationId?: string | null; text: string; origin?: ReplyAssistanceInput; undo?: typeof undo.value } | undefined
const recovery = computed(() => replyNeedsRecovery(props.conversationId, expression.value))
const inputDrafts = new Map<string, InputDraft>()
const LANDING_DRAFT = '__new_conversation__'
let draftLoaded = false
const draftKey = (id: string) => `zhijun.reply-input.${id}`
const failedKey = (id: string) => `zhijun.reply-failed.${id}`
const failedDrafts = ref<InputDraft[]>([])
const failedDraftCache = new Map<string, InputDraft[]>()
function readFailed(id: string): InputDraft[] {
  if (failedDraftCache.has(id)) return failedDraftCache.get(id)!
  try { const saved = JSON.parse(productStorage.getItem(failedKey(id)) || '[]'); return Array.isArray(saved) ? saved.filter(d => d && typeof d.text === 'string') : [] }
  catch { return [] }
}
function saveFailed(id: string, values: InputDraft[]) {
  failedDraftCache.set(id, values)
  if (id === (props.conversationId || LANDING_DRAFT)) failedDrafts.value = values
  try { productStorage.setItem(failedKey(id), JSON.stringify(values)) } catch { /* The current mounted draft remains readable. */ }
}
function storedDraft(id: string) {
  if (inputDrafts.has(id)) return inputDrafts.get(id)
  try {
    const saved = JSON.parse(productStorage.getItem(draftKey(id)) || 'null')
    if (saved && typeof saved.text === 'string' && saved.text.length <= 4000) return saved as InputDraft
  } catch { /* Storage may be unavailable; typing still works. */ }
}
watch(() => props.conversationId, (next, previous) => {
  if (draftLoaded) inputDrafts.set(previous || LANDING_DRAFT, { text: text.value, origin: expression.value, undo: undo.value })
  const saved = storedDraft(next || LANDING_DRAFT)
  text.value = saved?.text || ''; expression.value = saved?.origin; undo.value = saved?.undo
  failedDrafts.value = readFailed(next || LANDING_DRAFT)
  draftLoaded = true
}, { immediate: true })
watch([text, expression, undo], () => {
  const id = props.conversationId || LANDING_DRAFT
  try {
    if (text.value) productStorage.setItem(draftKey(id), JSON.stringify({ text: text.value, origin: expression.value, undo: undo.value }))
    else productStorage.removeItem(draftKey(id))
  } catch { /* Do not block the composer if local storage is full. */ }
}, { deep: true })
watch(text, value => { if (!value.trim()) { expression.value = undefined; undo.value = undefined } })
function insertReply(extra: string, origin: ReplyAssistanceInput) {
  try {
    const replacing = origin.control === 'rephrase' || origin.control === 'pause'
    if (!replacing && recovery.value) throw new Error('请先撤销旧辅助句，再选择新的回答。已改写的文字会保留，不能自动移除其来源。')
    const result = replacing ? replaceReply(text.value, origin) : appendReply(text.value, extra, expression.value, origin)
    if (replacing && text.value === result.text && JSON.stringify(expression.value) === JSON.stringify(result.origin)) {
      textareaRef.value?.focus({ preventScroll: true })
      return
    }
    undo.value = { inserted: result.inserted, offset: result.offset, ...(replacing ? { replaced: text.value } : {}), origin: expression.value }
    text.value = result.text; expression.value = result.origin
    textareaRef.value?.focus({ preventScroll: true })
  } catch (e) { toast({ type: 'info', message: e instanceof Error ? e.message : '原文未改变' }) }
}
function undoInsertion() {
  if (!undo.value) return
  const result = undoReply(text.value, undo.value)
  if (result === null) { toast({ type: 'info', message: '你已修改填入的文字，为保留修改，请手动调整或删除。' }); return }
  text.value = result; expression.value = undo.value.origin; undo.value = undefined
}
function applyDraft(id: string, draft: InputDraft) {
  inputDrafts.set(id, draft)
  try { productStorage.setItem(draftKey(id), JSON.stringify(draft)) } catch { /* Preserve the in-memory copy if storage is unavailable. */ }
  if (id === (props.conversationId || LANDING_DRAFT)) { text.value = draft.text; expression.value = draft.origin; undo.value = draft.undo }
}
function restoreSubmission(value: string, origin: ReplyAssistanceInput | undefined, conversationId: string | null) {
  const id = conversationId || LANDING_DRAFT
  const previous = lastSubmission && (lastSubmission.conversationId || LANDING_DRAFT) === id && lastSubmission.text.trim() === value && JSON.stringify(lastSubmission.origin) === JSON.stringify(origin) ? lastSubmission : undefined
  const failed: InputDraft = { text: previous?.text ?? value, origin, undo: previous?.undo }
  const here = id === (props.conversationId || LANDING_DRAFT)
  const current = here ? { text: text.value, origin: expression.value, undo: undo.value } : storedDraft(id) || { text: '' }
  const merged = mergeReplyDrafts(current, failed)
  if (merged) { applyDraft(id, merged); return }
  const pending = here ? failedDrafts.value : readFailed(id)
  if (!pending.some(d => d.text === failed.text && JSON.stringify(d.origin) === JSON.stringify(failed.origin))) saveFailed(id, [...pending, failed])
}
function switchFailedDraft() {
  const [next, ...rest] = failedDrafts.value
  if (!next) return
  if (text.value) rest.push({ text: text.value, origin: expression.value, undo: undo.value })
  const id = props.conversationId || LANDING_DRAFT
  saveFailed(id, rest); applyDraft(id, next)
  textareaRef.value?.focus({ preventScroll: true })
}
const filesInput = ref<HTMLInputElement | null>(null)
const addOpen = ref(false)
const addRef = ref<HTMLElement | null>(null)
function closeAddOutside(event: PointerEvent) {
  if (addOpen.value && !event.composedPath().includes(addRef.value as EventTarget)) addOpen.value = false
}
function closeAddOnEscape(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !addOpen.value) return
  const focusInMenu = addRef.value?.querySelector('.zj-composer__add-menu')?.contains(document.activeElement)
  addOpen.value = false
  if (focusInMenu) addRef.value?.querySelector<HTMLButtonElement>('.zj-composer__chip')?.focus({ preventScroll: true })
}
function closeAddOnBlur(event: FocusEvent) {
  if (event.relatedTarget && !addRef.value?.contains(event.relatedTarget as Node)) addOpen.value = false
}
onMounted(() => {
  document.addEventListener('pointerdown', closeAddOutside, true)
  document.addEventListener('keydown', closeAddOnEscape)
})
onBeforeUnmount(() => {
  if (typeof document === 'undefined') return
  document.removeEventListener('pointerdown', closeAddOutside, true)
  document.removeEventListener('keydown', closeAddOnEscape)
})
const audioInput = ref<HTMLInputElement | null>(null)
const acceptFiles = [...DOC_EXTENSIONS, ...IMAGE_EXTENSIONS, ...AUDIO_EXTENSIONS].join(',')
function onFiles(e: Event) {
  const input = e.target as HTMLInputElement
  if (!props.retrievalOnly && input.files?.length) emit('files', input.files)
  input.value = ''
  addOpen.value = false
}
const deep = ref(false)
const deliberate = ref(false)
const MAX = 4000
const COUNT_FROM = 3000
const HINT_KEY = 'zhijun.composer.hintSeen'

function readHintSeen(): boolean {
  try {
    return localStorage.getItem(HINT_KEY) === '1'
  } catch {
    return true
  }
}
const hintSeen = ref(readHintSeen())
function markHintSeen() {
  hintSeen.value = true
  try {
    localStorage.setItem(HINT_KEY, '1')
  } catch {
    // 无法持久化时忽略
  }
}

const showHint = computed(() => props.allowDeliberate !== false && !deliberate.value && intentHint(text.value))
const blocked = computed(() => !!props.disabled || !!props.notice)

const effectivePlaceholder = computed(() => {
  if (deliberate.value) return '说说你在纠结什么、有哪几个选项、你倾向哪个、把握有几成'
  return props.placeholder || '最近有什么事，想一起想一想？'
})

function send() {
  const content = text.value.trim()
  if ((!content && !props.hasAttachments) || props.streaming || props.uploading || blocked.value || voiceState.value !== 'idle') return
  if (content.length > MAX) return
  lastSubmission = { conversationId: props.conversationId, text: text.value, origin: expression.value, undo: undo.value }
  emit('send', content, deep.value ? 'deep' : 'brief', deliberate.value ? 'deliberate' : 'chat', expression.value)
  text.value = ''
  expression.value = undefined; undo.value = undefined
  if (!hintSeen.value) markHintSeen()
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    send()
  }
}

// ---- 语音输入（浏览器 Web Speech；只填入输入框，永远不自动发送）
const toast = useToast()
const desktopAudioUpload = isDesktopProduct()
const voiceAvailable = desktopAudioUpload || speechSupported()
const voiceState = ref<VoiceState>('idle')
const listening = ref(false)
let recognizer: any = null
let baseText = ''
let finalText = ''
const boxVoice = desktopAudioUpload ? createVoiceRecording({
  onState(value) { voiceState.value = value; listening.value = value === 'recording' },
  onText(value) {
    text.value = text.value.trim() ? text.value + '\n' + value : value
    textareaRef.value?.focus({ preventScroll: true })
  },
  onError(message) { toast({ type: 'error', message }) },
}) : null
watch(() => props.conversationId, () => boxVoice?.cancel())

function stopVoice() {
  boxVoice?.cancel()
  if (recognizer) {
    try {
      recognizer.stop()
    } catch {
      /* 已停止 */
    }
  }
  listening.value = false
}

function startVoice() {
  recognizer = createRecognizer()
  if (!recognizer) {
    toast({ type: 'error', message: '这个浏览器不支持语音输入' })
    return
  }
  baseText = text.value
  finalText = ''
  recognizer.onresult = (event: any) => {
    const { finalText: fin, interimText } = splitResults(event.results)
    finalText = fin
    text.value = mergeTranscript(baseText, finalText, interimText)
  }
  recognizer.onerror = (event: any) => {
    listening.value = false
    const code = event?.error || 'unknown'
    if (code === 'no-speech') return
    toast({ type: 'error', message: code === 'not-allowed' ? '麦克风权限被拒绝' : `语音识别出错：${code}` })
  }
  recognizer.onend = () => {
    listening.value = false
    text.value = mergeTranscript(baseText, finalText, '')
  }
  try {
    recognizer.start()
    listening.value = true
  } catch (err) {
    listening.value = false
    toast({ type: 'error', message: err instanceof Error ? err.message : '无法开始语音输入' })
  }
}

function toggleVoice() {
  if (boxVoice) {
    if (voiceState.value === 'recording') { void boxVoice.finish(); return }
    if (voiceState.value !== 'idle') return
    if (!props.streaming && !blocked.value) void boxVoice.start()
    return
  }
  if (props.streaming || blocked.value) return
  if (listening.value) stopVoice()
  else startVoice()
}

onBeforeUnmount(() => { stopVoice(); boxVoice?.dispose() })

const textareaRef = ref<HTMLTextAreaElement | null>(null)
defineExpose({
  insertReply,
  restoreSubmission,
  adoptLandingDraft: (conversationId: string) => {
    if (props.conversationId || !conversationId || storedDraft(conversationId)) return
    applyDraft(conversationId, { text: text.value, origin: expression.value, undo: undo.value })
  },
  replaceText: (value: string) => {
    text.value = value
    expression.value = undefined
    undo.value = undefined
    textareaRef.value?.focus({ preventScroll: true })
  },
  appendText: (value: string) => {
    text.value = text.value.trim() ? text.value + '\n\n' + value : value
    textareaRef.value?.focus()
  },
  focus: () => textareaRef.value?.focus(),
  setDeliberate: (on: boolean) => {
    deliberate.value = on
  },
  setDeep: (on: boolean) => {
    deep.value = on
  },
  setText: (value: string, origin?: ReplyAssistanceInput) => {
    const failed = lastSubmission && lastSubmission.conversationId === props.conversationId && lastSubmission.text.trim() === value && JSON.stringify(lastSubmission.origin) === JSON.stringify(origin) ? lastSubmission : undefined
    text.value = failed?.text ?? value
    expression.value = origin
    undo.value = failed?.undo
    textareaRef.value?.focus()
  },
})
</script>

<template>
  <div class="zj-composer" :class="{ 'is-blocked': !!notice }">
    <slot name="attachments" />
    <div v-if="expression" class="zj-composer__assisted" role="status">
      <span>{{ expression.selections.length ? 'AI 辅助起草，可修改后发送' : '对话操作，发送后生效' }}</span>
      <button v-if="undo" type="button" @click="undoInsertion">撤销填入</button>
    </div>
    <p v-if="recovery" class="zj-composer__recovery" role="status">{{ recovery.reason }}<span v-if="!undo"> 已修改的辅助文字不会自动删除；请保留草稿，再整理要表达的内容。</span></p>
    <div v-if="failedDrafts.length" class="zj-composer__assisted" role="status"><span>另有 {{ failedDrafts.length }} 份未发送草稿已保留，切换不会丢失当前输入。</span><button type="button" @click="switchFailedDraft">切换到未发送草稿</button></div>
    <p v-if="notice" class="zj-composer__notice" role="status">
      <span>{{ notice }}</span>
      <RouterLink v-if="noticeTo" :to="noticeTo" class="zj-composer__notice-link">去设置</RouterLink>
    </p>
    <p v-else-if="showHint" class="zj-composer__intent" role="status">
      像是在拿主意？切到「我在考虑…」，知君会帮你整理成判断草稿。
      <button type="button" class="zj-composer__intent-btn" @click="deliberate = true">切换</button>
    </p>
    <p v-if="quiet && (deliberate || deep)" class="zj-composer__intent" role="status" data-testid="composer-quiet-modes">
      这一轮{{ deliberate ? '会整理成判断' : '' }}{{ deliberate && deep ? '，也' : '' }}{{ deep ? '会展开分析' : '' }}。
      <button type="button" class="zj-composer__intent-btn" @click="deliberate = false; deep = false">先不用</button>
    </p>
    <div class="zj-composer__wrap">
      <textarea
        ref="textareaRef"
        v-model="text"
        class="zj-composer__field"
        :placeholder="effectivePlaceholder"
        rows="2"
        :maxlength="MAX"
        :disabled="blocked"
        aria-label="输入消息"
        @keydown="onKeydown"
      />
    </div>
    <p v-if="desktopAudioUpload && voiceState !== 'idle'" class="zj-composer__voice-status" role="status">
      {{ voiceState === 'requesting' ? '正在请求麦克风权限…' : voiceState === 'recording' ? '正在录音，最长 120 秒。停止后由盒子转写，仅填入草稿。' : '正在由盒子转写…' }}
      <button v-if="voiceState === 'recording'" type="button" @click="boxVoice?.finish()">停止并转写</button>
      <button type="button" @click="boxVoice?.cancel()">取消</button>
    </p>
    <div class="zj-composer__bar">
      <div ref="addRef" class="zj-composer__add" @pointerleave="addOpen = false" @focusout="closeAddOnBlur">
        <button type="button" class="zj-composer__chip" :aria-label="retrievalOnly ? '使用资料' : '添加文件'" :aria-expanded="addOpen" :disabled="disabled || uploading" @click="addOpen = !addOpen"><Plus :size="17" /></button>
        <div v-if="addOpen" class="zj-composer__add-menu">
          <template v-if="retrievalOnly">
            <span>资料由 Data Engine 管理。在对话中说明要检索的主题，再确认使用哪些片段。</span>
          </template>
          <template v-else>
          <button type="button" @click="filesInput?.click()">上传文件</button>
          <button v-if="desktopAudioUpload" type="button" @click="audioInput?.click()">上传音频到盒子处理</button>
          <span v-if="desktopAudioUpload">可点麦克风录音交给盒子转写，也可上传已有录音。</span>
          <button type="button" @click="emit('pick-materials'); addOpen = false">选择已有资料</button>
          <span>也可以拖入文件或粘贴截图</span>
          </template>
          <template v-if="quiet">
          <button v-if="allowDeliberate !== false" type="button" :aria-pressed="deliberate" @click="deliberate = !deliberate; addOpen = false">{{ deliberate ? '不整理成判断' : '整理成判断' }}</button>
          <button type="button" :aria-pressed="deep" @click="deep = !deep; addOpen = false">{{ deep ? '不展开分析' : '展开分析' }}</button>
          </template>
        </div>
        <input v-if="!retrievalOnly && desktopAudioUpload" ref="audioInput" type="file" multiple hidden :accept="AUDIO_EXTENSIONS.join(',')" aria-label="上传音频到盒子处理" @change="onFiles" />
        <input v-if="!retrievalOnly" ref="filesInput" type="file" multiple hidden :accept="acceptFiles" aria-label="上传聊天文件" @change="onFiles" />
      </div>
      <button
        v-if="allowDeliberate !== false && !quiet"
        type="button"
        class="zj-composer__chip"
        :class="{ 'is-on': deliberate }"
        :aria-pressed="deliberate"
        :disabled="streaming || blocked"
        title="把这件事整理成一条判断：选项、倾向、把握、预期"
        @click="deliberate = !deliberate"
      >
        整理成判断
      </button>
      <button
        v-if="!quiet"
        type="button"
        class="zj-composer__chip"
        :class="{ 'is-on': deep }"
        :aria-pressed="deep"
        :disabled="streaming || blocked"
        title="结合依据展开分析，按当前问题组织内容；不要求每次回答一串问题"
        @click="deep = !deep"
      >
        展开分析
      </button>
      <span v-if="!hintSeen" class="zj-composer__tip">Enter 发送 · Shift+Enter 换行</span>
      <span v-if="text.length >= COUNT_FROM" class="zj-composer__count" aria-live="polite">{{ text.length }}/{{ MAX }}</span>
      <div class="zj-composer__actions">
      <button
        v-if="voiceAvailable"
        type="button"
        class="zj-composer__voice"
        :class="{ 'is-on': listening }"
        :aria-pressed="listening"
        :aria-label="desktopAudioUpload ? listening ? '停止录音并由盒子转写' : '录音并由盒子转写' : listening ? '停止语音输入' : '用说的（不会自动发送）'"
        :disabled="streaming || blocked"
        :title="desktopAudioUpload ? '盒子转写：最长 120 秒，只填入草稿，不自动发送' : listening ? '停止语音输入' : '用说的（不会自动发送）'"
        @click="toggleVoice"
      >
        <component :is="listening ? MicOff : Mic" :size="16" aria-hidden="true" />
      </button>
      <BaseButton v-if="streaming" variant="secondary" size="sm" class="zj-composer__send" @click="emit('stop')">
        <Square :size="14" aria-hidden="true" />停止
      </BaseButton>
      <BaseButton v-else variant="primary" size="sm" class="zj-composer__send" :disabled="blocked || uploading || voiceState !== 'idle' || (!text.trim() && !hasAttachments)" @click="send">
        <Send :size="14" aria-hidden="true" />发送
      </BaseButton>
      </div>
    </div>
  </div>
</template>

<style scoped>
.zj-composer__assisted { display:flex; flex-wrap:wrap; gap:8px; align-items:center; font-size:11px; color:var(--ws-text-secondary-color,#686b66); }
.zj-composer__assisted button { border:0; background:transparent; color:var(--ws-primary-color,#a6452e); font:inherit; cursor:pointer; text-decoration:underline; }
.zj-composer__recovery { margin:6px 0; font-size:12px; line-height:1.6; color:var(--ws-text-secondary-color,#686b66); overflow-wrap:anywhere; }
.zj-composer__add { position: relative; }
.zj-composer__add-menu { position: absolute; bottom: calc(100% + 8px); left: 0; width: 210px; z-index: 20; padding: 8px; border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 9px; background: var(--ws-card-bg, #fff); box-shadow: 0 5px 22px rgb(0 0 0 / 10%); }
.zj-composer__add-menu::after { content: ''; position: absolute; top: 100%; left: -1px; right: -1px; height: 9px; }
.zj-composer__add-menu button { display: block; width: 100%; padding: 10px; border: 0; background: none; color: inherit; text-align: left; cursor: pointer; font: inherit; }
.zj-composer__add-menu button:hover { background: var(--ws-surface-2, #fbf8f1); }
.zj-composer__add-menu span { display: block; padding: 8px 10px; font-size: 11px; color: var(--ws-text-secondary-color, #686b66); }
.zj-composer {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.zj-composer:focus-within {
  border-color: var(--ws-input-focus-border-color, #a6452e);
  box-shadow: 0 0 0 3px var(--accent-ring, rgba(166, 69, 46, 0.18));
}
.zj-composer__intent {
  margin: 0;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-composer.is-blocked {
  border-style: dashed;
}
.zj-composer__notice {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-size: 13px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-composer__notice-link {
  color: var(--ws-primary-color, #a6452e);
  text-decoration: underline;
}
.zj-composer__intent-btn {
  margin-left: 6px;
  border: none;
  background: transparent;
  color: var(--ws-primary-color, #a6452e);
  font-family: inherit;
  font-size: 12px;
  text-decoration: underline;
  cursor: pointer;
}
.zj-composer__wrap {
  position: relative;
}
.zj-composer__field {
  display: block;
  width: 100%;
  min-height: 56px;
  max-height: 240px;
  border: none;
  background: transparent;
  color: var(--ws-text-primary-color, #1d211f);
  font-family: inherit;
  font-size: 15px;
  line-height: 1.6;
  resize: vertical;
}
.zj-composer__voice-status { display:flex; flex-wrap:wrap; align-items:center; gap:8px; font-size:12px; color:var(--ws-text-secondary-color); margin:8px 0; }
.zj-composer__voice-status button { border:1px solid var(--ws-border-color); border-radius:6px; padding:4px 8px; background:transparent; color:inherit; cursor:pointer; }
.zj-composer__field:focus {
  outline: none;
}
.zj-composer__field:disabled {
  opacity: 0.55;
}
.zj-composer__voice {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--ws-text-secondary-color, #686b66);
  cursor: pointer;
}
.zj-composer__voice:hover:not(:disabled) {
  background: var(--ws-surface-2, #fbf8f1);
  color: var(--ws-primary-color, #a6452e);
}
.zj-composer__voice.is-on {
  color: var(--ws-danger-color, #a6452e);
  box-shadow: 0 0 0 3px var(--accent-ring, rgba(166, 69, 46, 0.18));
}
.zj-composer__voice:disabled {
  opacity: 0.4;
  cursor: default;
}
.zj-composer__bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.zj-composer__chip {
  padding: 3px 12px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: 999px;
  background: transparent;
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, color 0.15s;
}
.zj-composer__chip:hover:not(:disabled) {
  border-color: var(--ws-primary-color, #a6452e);
}
.zj-composer__chip.is-on {
  border-color: var(--ws-primary-color, #a6452e);
  background: var(--ws-edit-color, rgba(166, 69, 46, 0.06));
  color: var(--ws-primary-color, #a6452e);
  font-weight: 600;
}
.zj-composer__chip:disabled {
  opacity: 0.5;
  cursor: default;
}
.zj-composer__tip,
.zj-composer__count {
  font-size: 12px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-composer__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}
@media (max-width: 600px) {
  .zj-composer__tip {
    display: none;
  }
}
</style>
