<script setup lang="ts">
// 今日提醒条：安静。默认只露一条，其余折在「还有 N 条」里；每条都说明「为何现在」。
// 动作：去回访 / 去看看 / 一起看看 · 稍后 · 不再提醒。没有「检查」按钮——扫描由后台每小时做。
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { createConversation, dismissNudge, getNudgesToday, scanNudges, silenceNudge, type Nudge } from '@/services/api'
import { nudgeKindLabel } from '@/shared/labels'
import { useToast } from '@/composables/useToast'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'

const router = useRouter()
const toast = useToast()

const items = ref<Nudge[]>([])
const busy = ref<Record<string, boolean>>({})
const expanded = ref(false)
const silenceTarget = ref<Nudge | null>(null)

const visible = computed(() => (expanded.value ? items.value : items.value.slice(0, 1)))
const hiddenCount = computed(() => Math.max(0, items.value.length - visible.value.length))

async function load() {
  try {
    const res = await getNudgesToday()
    items.value = res.items.slice(0, 3)
  } catch {
    items.value = []
  }
}

async function scan() {
  try {
    await scanNudges()
    await load()
  } catch {
    // 手动扫描失败不打扰
  }
}

function remove(id: string) {
  items.value = items.value.filter((i) => i.id !== id)
}

function primaryLabel(n: Nudge): string {
  if (n.kind === 'review_due') return '去回访'
  if (n.kind === 'weekly_review') return '一起看看'
  return '去看看'
}

async function primary(n: Nudge) {
  busy.value[n.id] = true
  try {
    if (n.kind === 'review_due') {
      const decisionId = n.triggerRef?.decisionId
      if (!decisionId) return
      const conv = await createConversation({ mode: 'review', decisionId })
      remove(n.id)
      router.push(`/c/${encodeURIComponent(conv.id)}`)
      return
    }
    // 其余三种：标记已处理后跳到对应的地方；标记失败不阻塞跳转
    try {
      await dismissNudge(n.id)
    } catch {
      /* 忽略 */
    }
    remove(n.id)
    if (n.kind === 'principle_tension') {
      router.push({ path: '/me', query: { section: 'principles' } })
    } else if (n.kind === 'commitment_due') {
      const claimId = n.triggerRef?.claimId
      router.push({ path: '/me', query: { section: n.triggerRef?.section || 'matters', ...(claimId ? { claim: claimId } : {}) } })
    } else if (n.kind === 'weekly_review') {
      router.push({ path: '/', query: { say: `我们一起回顾一下这周吧。${n.triggerRef?.summary || ''}` } })
    }
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error ? err.message : '无法打开' })
  } finally {
    delete busy.value[n.id]
  }
}

async function later(n: Nudge) {
  busy.value[n.id] = true
  try {
    await dismissNudge(n.id)
    remove(n.id)
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error ? err.message : '操作失败' })
  } finally {
    delete busy.value[n.id]
  }
}

async function confirmSilence() {
  const n = silenceTarget.value
  if (!n) return
  busy.value[n.id] = true
  try {
    await silenceNudge(n.id)
    remove(n.id)
    toast({ type: 'success', message: '这件事不会再提醒了' })
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error ? err.message : '操作失败' })
  } finally {
    delete busy.value[n.id]
    silenceTarget.value = null
  }
}

function silenceMessage(n: Nudge | null): string {
  if (!n) return ''
  if (n.kind === 'principle_tension') return '这条原则与做法的张力以后不会再提醒；你仍可以在「我的本体」里自己核对。'
  if (n.kind === 'weekly_review') return '以后周末不再邀请你回顾；想看的时候随时在对话里说一声。'
  if (n.kind === 'commitment_due') return '这件事到期后不会再提醒；它仍留在「我的本体 · 手头的事」里。'
  return `「${n.triggerRef?.title || '这个判断'}」以后不会再出现在提醒里，你仍可以在判断页手动回访。`
}

onMounted(() => {
  void load()
})

defineExpose({ reload: load, scan })
</script>

<template>
  <div v-if="items.length" class="zj-nudges" role="region" aria-label="今日提醒">
    <article v-for="n in visible" :key="n.id" class="zj-nudge" :class="`is-${n.kind}`">
      <span class="zj-nudge__mark" aria-hidden="true" />
      <div class="zj-nudge__body">
        <span class="zj-seal zj-seal--muted zj-nudge__kind">{{ nudgeKindLabel(n.kind) }}</span>
        <p class="zj-nudge__msg">{{ n.message }}</p>
        <p class="zj-nudge__why">{{ n.whyNow }}</p>
      </div>
      <div class="zj-nudge__actions">
        <button type="button" class="zj-nudge__btn is-primary" :disabled="busy[n.id]" @click="primary(n)">{{ primaryLabel(n) }}</button>
        <button type="button" class="zj-nudge__btn" :disabled="busy[n.id]" @click="later(n)">稍后</button>
        <button type="button" class="zj-nudge__btn is-quiet" :disabled="busy[n.id]" @click="silenceTarget = n">不再提醒</button>
      </div>
    </article>
    <button v-if="hiddenCount > 0" type="button" class="zj-nudges__more" @click="expanded = true">还有 {{ hiddenCount }} 条</button>
    <button v-else-if="expanded && items.length > 1" type="button" class="zj-nudges__more" @click="expanded = false">收起</button>

    <ConfirmDialog
      :open="!!silenceTarget"
      title="不再提醒这件事？"
      :message="silenceMessage(silenceTarget)"
      confirm-text="不再提醒"
      @confirm="confirmSilence"
      @cancel="silenceTarget = null"
    />
  </div>
</template>

<style scoped>
.zj-nudges {
  display: grid;
  gap: 6px;
}
.zj-nudge {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 12px;
  align-items: center;
  padding: 10px 14px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
}
.zj-nudge__mark {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ws-primary-color, #a6452e);
}
.zj-nudge__kind {
  margin-bottom: 4px;
}
.zj-nudge__msg {
  margin: 0;
  font-size: 14px;
  line-height: 1.6;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-nudge.is-principle_tension .zj-nudge__msg,
.zj-nudge.is-weekly_review .zj-nudge__msg {
  font-family: var(--ws-font-display, serif);
}
.zj-nudge__why {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-nudge__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.zj-nudge__btn {
  padding: 4px 12px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: 999px;
  background: transparent;
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.zj-nudge__btn.is-primary {
  border-color: var(--ws-primary-color, #a6452e);
  color: var(--ws-primary-color, #a6452e);
}
.zj-nudge__btn.is-quiet {
  border-color: transparent;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-nudge__btn:hover:not(:disabled) {
  border-color: var(--ws-primary-color, #a6452e);
}
.zj-nudge__btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.zj-nudges__more {
  justify-self: end;
  border: none;
  background: transparent;
  color: var(--ws-text-secondary-color, #686b66);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.zj-nudges__more:hover {
  color: var(--ws-primary-color, #a6452e);
}
@media (max-width: 767px) {
  .zj-nudge {
    grid-template-columns: auto minmax(0, 1fr);
  }
  .zj-nudge__actions {
    grid-column: 2;
  }
}
</style>
