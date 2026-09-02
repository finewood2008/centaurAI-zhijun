<script setup lang="ts">
// 输入区：Enter 发送、Shift+Enter 换行（提示只出现一次）；「深入」「我在考虑…」是两枚开关 chip；
// 麦克风在输入框里；字数只在快到上限时才出现。语音只填入输入框，永远不自动发送。
import { computed, onBeforeUnmount, ref } from 'vue'
import { Mic, MicOff, Send, Square } from 'lucide-vue-next'
import BaseButton from '@/components/ui/BaseButton.vue'
import { useToast } from '@/composables/useToast'
import { intentHint } from '@/shared/decisionDraft'
import { createRecognizer, mergeTranscript, speechSupported, splitResults } from '@/shared/speech'

const props = defineProps<{
  streaming: boolean
  disabled?: boolean
  placeholder?: string
  allowDeliberate?: boolean
  // 模型没配置 / 不可用时的提示；给了就禁用输入，并在输入区上方显示同一句话（带去偏好的链接）
  notice?: string
  noticeTo?: string
}>()

const emit = defineEmits<{
  (e: 'send', content: string, depth: 'brief' | 'deep', mode: 'chat' | 'deliberate'): void
  (e: 'stop'): void
}>()

const text = ref('')
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
  return props.placeholder || '跟知君说点什么…'
})

function send() {
  const content = text.value.trim()
  if (!content || props.streaming || blocked.value) return
  if (content.length > MAX) return
  emit('send', content, deep.value ? 'deep' : 'brief', deliberate.value ? 'deliberate' : 'chat')
  text.value = ''
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
const voiceAvailable = speechSupported()
const listening = ref(false)
let recognizer: any = null
let baseText = ''
let finalText = ''

function stopVoice() {
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
  if (props.streaming || blocked.value) return
  if (listening.value) stopVoice()
  else startVoice()
}

onBeforeUnmount(stopVoice)

const textareaRef = ref<HTMLTextAreaElement | null>(null)
defineExpose({
  focus: () => textareaRef.value?.focus(),
  setDeliberate: (on: boolean) => {
    deliberate.value = on
  },
  setText: (value: string) => {
    text.value = value
    textareaRef.value?.focus()
  },
})
</script>

<template>
  <div class="zj-composer" :class="{ 'is-blocked': !!notice }">
    <p v-if="notice" class="zj-composer__notice" role="status">
      <span>{{ notice }}</span>
      <RouterLink v-if="noticeTo" :to="noticeTo" class="zj-composer__notice-link">去偏好</RouterLink>
    </p>
    <p v-else-if="showHint" class="zj-composer__intent" role="status">
      像是在拿主意？切到「我在考虑…」，知君会帮你整理成判断草稿。
      <button type="button" class="zj-composer__intent-btn" @click="deliberate = true">切换</button>
    </p>
    <div class="zj-composer__wrap">
      <textarea
        ref="textareaRef"
        v-model="text"
        class="zj-composer__field"
        :class="{ 'has-voice': voiceAvailable }"
        :placeholder="effectivePlaceholder"
        rows="2"
        :maxlength="MAX"
        :disabled="blocked"
        aria-label="输入消息"
        @keydown="onKeydown"
      />
      <button
        v-if="voiceAvailable"
        type="button"
        class="zj-composer__voice"
        :class="{ 'is-on': listening }"
        :aria-pressed="listening"
        :aria-label="listening ? '停止语音输入' : '用说的（不会自动发送）'"
        :disabled="streaming || blocked"
        :title="listening ? '停止语音输入' : '用说的（不会自动发送）'"
        @click="toggleVoice"
      >
        <component :is="listening ? MicOff : Mic" :size="16" aria-hidden="true" />
      </button>
    </div>
    <div class="zj-composer__bar">
      <button
        v-if="allowDeliberate !== false"
        type="button"
        class="zj-composer__chip"
        :class="{ 'is-on': deliberate }"
        :aria-pressed="deliberate"
        :disabled="streaming || blocked"
        title="把这件事整理成一条判断：选项、倾向、把握、预期"
        @click="deliberate = !deliberate"
      >
        我在考虑…
      </button>
      <button
        type="button"
        class="zj-composer__chip"
        :class="{ 'is-on': deep }"
        :aria-pressed="deep"
        :disabled="streaming || blocked"
        title="让知君展开说：观察、依据、其他解释、想确认什么、可以试什么"
        @click="deep = !deep"
      >
        深入
      </button>
      <span v-if="!hintSeen" class="zj-composer__tip">Enter 发送 · Shift+Enter 换行</span>
      <span v-if="text.length >= COUNT_FROM" class="zj-composer__count" aria-live="polite">{{ text.length }}/{{ MAX }}</span>
      <BaseButton v-if="streaming" variant="secondary" size="sm" class="zj-composer__send" @click="emit('stop')">
        <Square :size="14" aria-hidden="true" />停止
      </BaseButton>
      <BaseButton v-else variant="primary" size="sm" class="zj-composer__send" :disabled="blocked || !text.trim()" @click="send">
        <Send :size="14" aria-hidden="true" />发送
      </BaseButton>
    </div>
  </div>
</template>

<style scoped>
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
.zj-composer__field.has-voice {
  padding-right: 40px;
}
.zj-composer__field:focus {
  outline: none;
}
.zj-composer__field:disabled {
  opacity: 0.55;
}
.zj-composer__voice {
  position: absolute;
  right: 0;
  bottom: 6px;
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
.zj-composer__send {
  margin-left: auto;
}
@media (max-width: 600px) {
  .zj-composer__tip {
    display: none;
  }
}
</style>
