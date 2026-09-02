<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { X } from 'lucide-vue-next'
import { createConversation, reviewClaim, type HomeMapNode, type ReviewAction } from '@/services/api'
import { useToast } from '@/composables/useToast'
import { formatDate } from '@/shared/format'
import ClaimCard from '@/components/ontology/ClaimCard.vue'
import BaseButton from '@/components/ui/BaseButton.vue'

const props = defineProps<{ node: HomeMapNode }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'changed'): void }>()

const router = useRouter()
const toast = useToast()
const busy = ref(false)
const decision = computed(() => props.node.decision)
const due = computed(() => {
  const reviewAt = decision.value?.reviewAt
  return !!reviewAt && new Date(reviewAt).valueOf() <= Date.now()
})

async function onReview(action: ReviewAction, editedContent?: string) {
  if (!props.node.claim || busy.value) return
  busy.value = true
  try {
    await reviewClaim(props.node.claim.id, { action, editedContent, surface: 'today' })
    toast({ type: 'success', message: action === 'partial' ? '已保存修正' : action === 'confirm' ? '已确认，我会记住' : '已经更新' })
    emit('changed')
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error ? err.message : '操作失败' })
  } finally {
    busy.value = false
  }
}

async function openDecision() {
  const item = decision.value
  if (!item || busy.value) return
  if ((item.status === 'open' && due.value) || item.status === 'outcome_recorded') {
    busy.value = true
    try {
      const conversation = await createConversation({ mode: 'review', decisionId: item.id })
      router.push(`/c/${encodeURIComponent(conversation.id)}`)
    } catch (err) {
      toast({ type: 'error', message: err instanceof Error ? err.message : '无法开始回访' })
      busy.value = false
    }
    return
  }
  router.push({ path: '/judgments', query: { decisionId: item.id } })
}
</script>

<template>
  <aside class="zj-home-panel" aria-label="共同地图详情">
    <header class="zj-home-panel__head">
      <div>
        <span>{{ node.ring === 'remembered' ? '我记得' : node.ring === 'tracking' ? '我们在跟进' : '我还不确定' }}</span>
        <strong>{{ node.summary }}</strong>
      </div>
      <button type="button" aria-label="关闭详情" @click="$emit('close')"><X :size="17" aria-hidden="true" /></button>
    </header>

    <ClaimCard v-if="node.claim" :claim="node.claim" :busy="busy" show-section @review="onReview" />

    <article v-else-if="decision" class="zj-home-decision">
      <p class="zj-home-decision__status">{{ decision.status === 'open' ? (due ? '到了回访的时候' : '等待结果') : decision.status === 'outcome_recorded' ? '结果已回来' : '已复盘' }}</p>
      <h3>{{ decision.title }}</h3>
      <dl>
        <div><dt>当时选择</dt><dd>{{ decision.choice }}</dd></div>
        <div><dt>当时把握</dt><dd>{{ decision.confidence }}%</dd></div>
        <div><dt>预期结果</dt><dd>{{ decision.expectedOutcome }}</dd></div>
        <div><dt>约定回访</dt><dd>{{ decision.reviewAt ? formatDate(decision.reviewAt) : '尚未约定' }}</dd></div>
      </dl>
      <BaseButton variant="primary" size="sm" :loading="busy" @click="openDecision">
        {{ (decision.status === 'open' && due) || decision.status === 'outcome_recorded' ? '进入回访对话' : '查看这个判断' }}
      </BaseButton>
    </article>
  </aside>
</template>

<style scoped>
.zj-home-panel { display: grid; gap: 14px; padding: 18px; border: 1px solid rgba(166, 69, 46, 0.28); border-radius: 14px; background: rgba(255, 253, 248, 0.96); box-shadow: 0 14px 36px rgba(55, 45, 35, 0.08); }
.zj-home-panel__head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.zj-home-panel__head > div { display: grid; gap: 3px; }
.zj-home-panel__head span { color: var(--ws-primary-color, #a6452e); font-size: 11px; font-weight: 600; letter-spacing: .08em; }
.zj-home-panel__head strong { color: var(--ws-text-secondary-color, #686b66); font-size: 11px; font-weight: 400; line-height: 1.5; }
.zj-home-panel__head button { display: grid; flex: none; width: 28px; height: 28px; place-items: center; border: 0; border-radius: 50%; background: transparent; color: var(--ws-text-secondary-color, #686b66); cursor: pointer; }
.zj-home-panel__head button:hover { background: var(--ws-surface-2, #f7f2e8); color: var(--ws-primary-color, #a6452e); }
.zj-home-decision { display: grid; gap: 12px; }
.zj-home-decision__status { margin: 0; color: var(--ws-primary-color, #a6452e); font-size: 11px; }
.zj-home-decision h3 { margin: 0; font-family: var(--ws-font-display, serif); font-size: 19px; line-height: 1.4; color: var(--ws-text-primary-color, #1d211f); }
.zj-home-decision dl { display: grid; gap: 8px; margin: 0; padding: 12px; border-radius: 8px; background: var(--ws-surface-2, #f8f4eb); }
.zj-home-decision dl div { display: grid; grid-template-columns: 68px minmax(0, 1fr); gap: 8px; font-size: 11px; line-height: 1.55; }
.zj-home-decision dt { color: var(--ws-text-placeholder-color, #92958f); }
.zj-home-decision dd { margin: 0; color: var(--ws-text-primary-color, #1d211f); }
</style>
