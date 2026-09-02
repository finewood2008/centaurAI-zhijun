<script setup lang="ts">
// 输入区：Enter 发送、Shift+Enter 换行；「深入」切换 depth=deep；
// 「我在考虑…」切换 mode=deliberate（只提示、不自动切换）；生成中显示停止按钮。
import { computed, ref } from 'vue'
import { Send, Square } from 'lucide-vue-next'
import BaseButton from '@/components/ui/BaseButton.vue'
import { intentHint } from '@/shared/decisionDraft'

const props = defineProps<{
  streaming: boolean
  disabled?: boolean
  placeholder?: string
  allowDeliberate?: boolean
}>()

const emit = defineEmits<{
  (e: 'send', content: string, depth: 'brief' | 'deep', mode: 'chat' | 'deliberate'): void
  (e: 'stop'): void
}>()

const text = ref('')
const deep = ref(false)
const deliberate = ref(false)
const MAX = 4000

const showHint = computed(() => props.allowDeliberate !== false && !deliberate.value && intentHint(text.value))

const effectivePlaceholder = computed(() => {
  if (deliberate.value) return '说说你在纠结什么、有哪几个选项、你倾向哪个、把握有几成'
  return props.placeholder || '跟知君说点什么…（Enter 发送，Shift+Enter 换行）'
})

function send() {
  const content = text.value.trim()
  if (!content || props.streaming || props.disabled) return
  if (content.length > MAX) return
  emit('send', content, deep.value ? 'deep' : 'brief', deliberate.value ? 'deliberate' : 'chat')
  text.value = ''
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    send()
  }
}

const textareaRef = ref<HTMLTextAreaElement | null>(null)
defineExpose({
  focus: () => textareaRef.value?.focus(),
  setDeliberate: (on: boolean) => {
    deliberate.value = on
  },
})
</script>

<template>
  <div class="zj-composer">
    <p v-if="showHint" class="zj-composer__intent" role="status">
      像是在拿主意？切到「我在考虑…」知君会帮你整理成判断草稿。
      <button type="button" class="zj-composer__intent-btn" @click="deliberate = true">切换</button>
    </p>
    <textarea
      ref="textareaRef"
      v-model="text"
      class="zj-composer__field"
      :placeholder="effectivePlaceholder"
      rows="2"
      :maxlength="MAX"
      :disabled="disabled"
      aria-label="输入消息"
      @keydown="onKeydown"
    />
    <div class="zj-composer__bar">
      <button
        v-if="allowDeliberate !== false"
        type="button"
        class="zj-composer__mode"
        :class="{ 'is-on': deliberate }"
        :aria-pressed="deliberate"
        :disabled="streaming || disabled"
        @click="deliberate = !deliberate"
      >
        我在考虑…
      </button>
      <label class="zj-composer__deep">
        <input v-model="deep" type="checkbox" :disabled="streaming || disabled" />
        <span>深入</span>
        <span class="zj-composer__hint">观察 · 依据 · 其他解释 · 想确认什么 · 可尝试什么</span>
      </label>
      <span class="zj-composer__count" aria-live="polite">{{ text.length }}/{{ MAX }}</span>
      <BaseButton v-if="streaming" variant="secondary" size="sm" @click="emit('stop')">
        <Square :size="14" aria-hidden="true" />停止
      </BaseButton>
      <BaseButton v-else variant="primary" size="sm" :disabled="disabled || !text.trim()" @click="send">
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
  background: var(--ws-body-bg, #fffcf6);
}
.zj-composer:focus-within {
  border-color: var(--ws-input-focus-border-color, #a6452e);
}
.zj-composer__intent {
  margin: 0;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
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
.zj-composer__field {
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
.zj-composer__field:focus {
  outline: none;
}
.zj-composer__field:disabled {
  opacity: 0.55;
}
.zj-composer__bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.zj-composer__mode {
  padding: 3px 10px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: 999px;
  background: transparent;
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  font-size: 13px;
  cursor: pointer;
}
.zj-composer__mode.is-on {
  border-color: var(--ws-primary-color, #a6452e);
  background: var(--ws-edit-color, rgba(166, 69, 46, 0.06));
  color: var(--ws-primary-color, #a6452e);
  font-weight: 600;
}
.zj-composer__mode:disabled {
  opacity: 0.5;
  cursor: default;
}
.zj-composer__deep {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--ws-text-color, #3c403d);
  cursor: pointer;
}
.zj-composer__hint {
  font-size: 11px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-composer__count {
  margin-left: auto;
  font-size: 11px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
@media (max-width: 600px) {
  .zj-composer__hint {
    display: none;
  }
}
</style>
