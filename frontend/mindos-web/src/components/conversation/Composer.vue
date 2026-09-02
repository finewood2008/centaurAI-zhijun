<script setup lang="ts">
// 输入区：Enter 发送、Shift+Enter 换行；「深入」切换 depth=deep；生成中显示停止按钮。
import { ref } from 'vue'
import { Send, Square } from 'lucide-vue-next'
import BaseButton from '@/components/ui/BaseButton.vue'

const props = defineProps<{
  streaming: boolean
  disabled?: boolean
  placeholder?: string
}>()

const emit = defineEmits<{
  (e: 'send', content: string, depth: 'brief' | 'deep'): void
  (e: 'stop'): void
}>()

const text = ref('')
const deep = ref(false)
const MAX = 4000

function send() {
  const content = text.value.trim()
  if (!content || props.streaming || props.disabled) return
  if (content.length > MAX) return
  emit('send', content, deep.value ? 'deep' : 'brief')
  text.value = ''
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    send()
  }
}

defineExpose({ focus: () => textareaRef.value?.focus() })
const textareaRef = ref<HTMLTextAreaElement | null>(null)
</script>

<template>
  <div class="zj-composer">
    <textarea
      ref="textareaRef"
      v-model="text"
      class="zj-composer__field"
      :placeholder="placeholder || '跟知君说点什么…（Enter 发送，Shift+Enter 换行）'"
      rows="2"
      :maxlength="MAX"
      :disabled="disabled"
      aria-label="输入消息"
      @keydown="onKeydown"
    />
    <div class="zj-composer__bar">
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
