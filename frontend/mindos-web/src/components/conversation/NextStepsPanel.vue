<script setup lang="ts">
// 空白态「下一步」：起手卡上方最多三行（到期回访的判断 / 可以复盘的判断 / 到期的承诺 / 等你点头的理解）。
// 点一行：判断回访 → 开（或复用）回访会话；复盘 → 判断页；承诺 → 把话头放进输入框；待确认 → 我的本体 · 待确认。
// 没有内容时由父组件不渲染。措辞不催：「到了回访的时候」，不是「逾期未处理」。
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ChevronRight } from 'lucide-vue-next'
import { createConversation } from '@/services/api'
import type { NextStep } from '@/shared/labels'
import { useToast } from '@/composables/useToast'

defineProps<{ items: NextStep[] }>()
const emit = defineEmits<{ (e: 'say', text: string): void }>()

const router = useRouter()
const toast = useToast()
const busy = ref<Record<string, boolean>>({})

function kindLabel(kind: NextStep['kind']): string {
  if (kind === 'review') return '回访'
  if (kind === 'reflect') return '复盘'
  if (kind === 'commitment') return '承诺'
  return '待确认'
}

async function pick(step: NextStep) {
  if (busy.value[step.key]) return
  busy.value[step.key] = true
  try {
    if (step.kind === 'review' && step.decisionId) {
      // 后端已存在同一判断的回访会话时会直接返回它（reused），不会重复开场
      const conv = await createConversation({ mode: 'review', decisionId: step.decisionId })
      router.push(`/c/${encodeURIComponent(conv.id)}`)
      return
    }
    if (step.kind === 'reflect' && step.decisionId) {
      router.push({ path: '/judgments', query: { decisionId: step.decisionId } })
      return
    }
    if (step.kind === 'commitment') {
      emit('say', step.say || step.text)
      return
    }
    router.push('/me/inbox')
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error ? err.message : '无法打开' })
  } finally {
    delete busy.value[step.key]
  }
}
</script>

<template>
  <section v-if="items.length" class="zj-next" data-testid="next-steps" aria-label="下一步">
    <h3 class="zj-next__title">下一步</h3>
    <ul class="zj-next__list">
      <li v-for="(s, index) in items" :key="s.key">
        <button type="button" class="zj-next__row" :class="{ 'is-primary': index === 0 }" :disabled="busy[s.key]" @click="pick(s)">
          <span class="zj-seal zj-seal--muted zj-next__seal">{{ kindLabel(s.kind) }}</span>
          <span class="zj-next__text">{{ s.text }}</span>
          <ChevronRight class="zj-next__arrow" :size="15" aria-hidden="true" />
        </button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.zj-next {
  margin: 18px 0 0;
  text-align: left;
}
.zj-next__title {
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 500;
  letter-spacing: 0.06em;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-next__list {
  display: grid;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.zj-next__row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.6;
  text-align: left;
  cursor: pointer;
}
.zj-next__row:hover:not(:disabled) {
  border-color: var(--ws-primary-color, #a6452e);
}
.zj-next__row.is-primary {
  border-color: rgba(166, 69, 46, 0.45);
  border-left-width: 3px;
  background: linear-gradient(90deg, rgba(166, 69, 46, 0.08), var(--ws-card-bg, #fff) 48%);
  color: var(--ws-text-primary-color, #1d211f);
  font-weight: 600;
}
.zj-next__row:focus-visible {
  outline: 2px solid var(--ws-primary-color, #a6452e);
  outline-offset: 2px;
}
.zj-next__row:disabled {
  opacity: 0.6;
  cursor: default;
}
.zj-next__seal {
  flex: none;
}
.zj-next__text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.zj-next__arrow {
  flex: none;
  color: var(--ws-primary-color, #a6452e);
}
</style>
