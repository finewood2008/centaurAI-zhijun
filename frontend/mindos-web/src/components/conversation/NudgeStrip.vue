<script setup lang="ts">
// 今日提醒条：安静、最多 3 条、每条都说明「为何现在」。去回访 / 稍后 / 不再提醒。
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { BellRing } from 'lucide-vue-next'
import { createConversation, dismissNudge, getNudgesToday, scanNudges, silenceNudge, type Nudge } from '@/services/api'
import { useToast } from '@/composables/useToast'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'

const router = useRouter()
const toast = useToast()

const items = ref<Nudge[]>([])
const busy = ref<Record<string, boolean>>({})
const scanning = ref(false)
const silenceTarget = ref<Nudge | null>(null)

async function load() {
  try {
    const res = await getNudgesToday()
    items.value = res.items.slice(0, 3)
  } catch {
    items.value = []
  }
}

async function scan() {
  scanning.value = true
  try {
    const res = await scanNudges()
    await load()
    toast({ type: 'info', message: res.created ? `新增 ${res.created} 条提醒` : '暂时没有到期的回访' })
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error ? err.message : '检查提醒失败' })
  } finally {
    scanning.value = false
  }
}

async function goReview(n: Nudge) {
  const decisionId = n.triggerRef?.decisionId
  if (!decisionId) return
  busy.value[n.id] = true
  try {
    const conv = await createConversation({ mode: 'review', decisionId })
    items.value = items.value.filter((i) => i.id !== n.id)
    router.push(`/c/${encodeURIComponent(conv.id)}`)
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error ? err.message : '无法开始回访' })
  } finally {
    delete busy.value[n.id]
  }
}

async function later(n: Nudge) {
  busy.value[n.id] = true
  try {
    await dismissNudge(n.id)
    items.value = items.value.filter((i) => i.id !== n.id)
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
    items.value = items.value.filter((i) => i.id !== n.id)
    toast({ type: 'success', message: '这件事不会再提醒了' })
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error ? err.message : '操作失败' })
  } finally {
    delete busy.value[n.id]
    silenceTarget.value = null
  }
}

onMounted(() => {
  void load()
})

defineExpose({ reload: load })
</script>

<template>
  <div class="zj-nudges">
    <div v-if="items.length" class="zj-nudges__list" role="region" aria-label="今日提醒">
      <article v-for="n in items" :key="n.id" class="zj-nudge">
        <BellRing :size="16" aria-hidden="true" class="zj-nudge__icon" />
        <div class="zj-nudge__body">
          <p class="zj-nudge__msg">{{ n.message }}</p>
          <p class="zj-nudge__why">为何现在：{{ n.whyNow }}</p>
        </div>
        <div class="zj-nudge__actions">
          <button type="button" class="zj-nudge__btn is-primary" :disabled="busy[n.id]" @click="goReview(n)">去回访</button>
          <button type="button" class="zj-nudge__btn" :disabled="busy[n.id]" @click="later(n)">稍后</button>
          <button type="button" class="zj-nudge__btn" :disabled="busy[n.id]" @click="silenceTarget = n">不再提醒</button>
        </div>
      </article>
    </div>
    <button type="button" class="zj-nudges__scan" :disabled="scanning" @click="scan">{{ scanning ? '检查中…' : '检查提醒' }}</button>

    <ConfirmDialog
      :open="!!silenceTarget"
      title="不再提醒这件事？"
      :message="`「${silenceTarget?.triggerRef?.title || '这个判断'}」以后不会再出现在提醒里，你仍可以在判断页手动回访。`"
      confirm-text="不再提醒"
      @confirm="confirmSilence"
      @cancel="silenceTarget = null"
    />
  </div>
</template>

<style scoped>
.zj-nudges {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}
.zj-nudges__list {
  display: grid;
  gap: 8px;
  width: 100%;
}
.zj-nudge {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  padding: 10px 14px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-left: 3px solid var(--ws-primary-color, #a6452e);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #f3efe6);
}
.zj-nudge__icon {
  color: var(--ws-primary-color, #a6452e);
}
.zj-nudge__msg {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-nudge__why {
  margin: 2px 0 0;
  font-size: 11px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-nudge__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.zj-nudge__btn {
  padding: 4px 10px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: 999px;
  background: var(--ws-body-bg, #fffcf6);
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.zj-nudge__btn.is-primary {
  border-color: var(--ws-primary-color, #a6452e);
  color: var(--ws-primary-color, #a6452e);
}
.zj-nudge__btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.zj-nudges__scan {
  border: none;
  background: transparent;
  color: var(--ws-text-placeholder-color, #a3a69f);
  font-family: inherit;
  font-size: 11px;
  cursor: pointer;
}
.zj-nudges__scan:hover {
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
