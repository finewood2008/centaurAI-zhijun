<script setup lang="ts">
// 单条消息：user 为纯文本气泡；assistant 用 markdown-it（html:false）渲染后再把
// 认识论标记替换成文字徽章；system 为居中的系统备注（如「你确认了：…」）。
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import type { MessageRole, MessageStatus } from '@/services/api'
import { decorateLabels } from '@/shared/labels'

const props = defineProps<{
  role: MessageRole
  content: string
  status?: MessageStatus
  streaming?: boolean
}>()

const emit = defineEmits<{ (e: 'cite', index: number): void }>()

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const html = computed(() => {
  if (props.role !== 'assistant') return ''
  return decorateLabels(md.render(props.content || ''))
})

function onClick(e: MouseEvent) {
  const target = (e.target as HTMLElement | null)?.closest?.('a.cite-chip') as HTMLElement | null
  if (!target) return
  e.preventDefault()
  const n = Number(target.dataset.cite)
  if (Number.isFinite(n) && n > 0) emit('cite', n)
}
</script>

<template>
  <div v-if="role === 'system'" class="zj-msg zj-msg--system" role="status">
    <span class="zj-msg__system-text">{{ content }}</span>
  </div>
  <div v-else class="zj-msg" :class="`zj-msg--${role}`">
    <div class="zj-msg__meta">
      <span class="zj-msg__who">{{ role === 'user' ? '你' : '知君' }}</span>
      <span v-if="streaming" class="zj-msg__state">正在回复…</span>
      <span v-else-if="status === 'aborted'" class="zj-msg__state">已停止</span>
      <span v-else-if="status === 'error'" class="zj-msg__state is-error">出错了</span>
    </div>
    <div v-if="role === 'user'" class="zj-msg__body zj-msg__body--plain">{{ content }}</div>
    <!-- eslint-disable-next-line vue/no-v-html -->
    <div v-else class="zj-msg__body zj-prose" @click="onClick" v-html="html" />
    <span v-if="role === 'assistant' && streaming" class="zj-msg__cursor" aria-hidden="true" />
  </div>
</template>

<style scoped>
.zj-msg {
  position: relative;
  max-width: 760px;
  padding: 14px 18px;
  border-radius: var(--ws-radius-lg, 8px);
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  background: var(--ws-body-bg, #fffcf6);
}
.zj-msg--user {
  margin-left: auto;
  background: var(--ws-card-bg, #f3efe6);
  border-color: var(--ws-border-color-2, #e2ded4);
}
.zj-msg--assistant {
  margin-right: auto;
}
.zj-msg--system {
  max-width: none;
  padding: 4px 0;
  border: none;
  background: transparent;
  text-align: center;
}
.zj-msg__system-text {
  display: inline-block;
  padding: 3px 12px;
  border-radius: 999px;
  background: var(--ws-success-color-bd, rgba(74, 124, 89, 0.08));
  color: var(--ws-success-color, #4a7c59);
  font-size: 12px;
}
.zj-msg__meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-msg__who {
  font-family: var(--ws-font-display, serif);
  font-weight: 600;
  color: var(--ws-text-color, #3c403d);
}
.zj-msg__state.is-error {
  color: var(--ws-danger-color, #a6452e);
}
.zj-msg__body {
  font-size: 15px;
  line-height: 1.75;
  color: var(--ws-text-primary-color, #1d211f);
  word-break: break-word;
}
.zj-msg__body--plain {
  white-space: pre-wrap;
}
.zj-msg__cursor {
  display: inline-block;
  width: 8px;
  height: 16px;
  margin-left: 2px;
  vertical-align: -2px;
  background: var(--ws-primary-color, #a6452e);
  animation: zj-blink 1s steps(2, start) infinite;
}
@keyframes zj-blink {
  to {
    visibility: hidden;
  }
}
</style>

<style>
/* v-html 内容不受 scoped 影响：正文排版与徽章样式放全局，前缀 zj- 避免冲突 */
.zj-prose p {
  margin: 0 0 0.6em;
}
.zj-prose p:last-child {
  margin-bottom: 0;
}
.zj-prose ul,
.zj-prose ol {
  margin: 0 0 0.6em 1.4em;
}
.zj-prose code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--ws-card-bg, #f3efe6);
  font-size: 0.92em;
}
.zj-prose pre {
  overflow-x: auto;
  padding: 10px 12px;
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-card-bg, #f3efe6);
}
.zj-prose blockquote {
  margin: 0 0 0.6em;
  padding-left: 12px;
  border-left: 3px solid var(--ws-border-color, #d8d3c8);
  color: var(--ws-text-color, #3c403d);
}
.layer-badge {
  display: inline-block;
  margin: 0 3px 0 0;
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid transparent;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.5;
  vertical-align: 1px;
  white-space: nowrap;
}
.layer-badge--told {
  background: var(--ws-success-color-bd, rgba(74, 124, 89, 0.08));
  color: var(--ws-success-color, #4a7c59);
  border-color: rgba(74, 124, 89, 0.25);
}
.layer-badge--material {
  background: var(--ws-edit-color, rgba(166, 69, 46, 0.06));
  color: var(--ws-text-color, #3c403d);
  border-color: var(--ws-border-color, #d8d3c8);
}
.layer-badge--guess {
  background: var(--ws-warning-color-bd, rgba(184, 134, 43, 0.08));
  color: var(--ws-warning-color, #b8862b);
  border-color: rgba(184, 134, 43, 0.3);
}
.layer-badge--view {
  background: transparent;
  color: var(--ws-primary-color, #a6452e);
  border-color: var(--ws-primary-color, #a6452e);
}
.cite-chip {
  display: inline-block;
  margin: 0 2px;
  padding: 0 6px;
  border-radius: 999px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  background: var(--ws-card-bg, #f3efe6);
  color: var(--ws-text-color, #3c403d);
  font-size: 12px;
  line-height: 1.5;
  text-decoration: none;
  vertical-align: 1px;
}
.cite-chip:hover {
  border-color: var(--ws-primary-color, #a6452e);
  color: var(--ws-primary-color, #a6452e);
}
</style>
