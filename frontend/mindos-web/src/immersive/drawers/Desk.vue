<script setup lang="ts">
// 「案头」：此刻相关的对象。判断草稿（LiveObjectPanel，读当前轮桥接）；对话页投进 #zj-desk-host 的
// 事情与成果 / 待核对 / 本轮资料 / 章程；回访时经桥接打开既有的「观察与复盘」工作台。
// 宿主就在这个抽屉里：抽屉常驻挂载，所以对话页渲染前宿主已经存在。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { DecisionDraftConfirmPayload } from '@/services/api'
import LiveObjectPanel from '@/components/conversation/LiveObjectPanel.vue'
import SideDrawer from '@/components/ui/SideDrawer.vue'
import { useActiveTurn } from '../activeTurn'
import { DESK_HOST } from '../shellContext'
import { observeDeskHost, useDeskObjects } from '../composables/useDeskObjects'
import { useSheetPlacement } from '../composables/useSheetPlacement'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()
const placement = useSheetPlacement()
const turn = useActiveTurn()
const { draftPresent, hostChildren, reviewAvailable } = useDeskObjects()
const host = ref<HTMLElement | null>(null)
const hostId = DESK_HOST.slice(1)
let stop: (() => void) | null = null
onMounted(() => { if (host.value) stop = observeDeskHost(host.value) })
onBeforeUnmount(() => { stop?.(); stop = null })

const draftView = computed(() => {
  const t = turn.value
  if (!t) return null
  const draft = t.draft.value
  return {
    key: draft?.id || t.conversationId.value || 'pending-draft',
    draft: draft && draft.status !== 'discarded' ? draft : null,
    changed: t.draftChanged.value,
    busy: t.draftBusy.value,
    error: t.draftError.value,
    pending: t.draftPending.value,
    timedOut: t.draftTimedOut.value,
  }
})
const empty = computed(() => !draftPresent.value && hostChildren.value === 0 && !reviewAvailable.value)

const confirmDraft = (payload: DecisionDraftConfirmPayload) => { void turn.value?.onConfirmDraft(payload) }
const discardDraft = () => { void turn.value?.onDiscardDraft() }
const retryDraft = () => { turn.value?.retryDraft() }
const openReview = () => { turn.value?.openWorkspace('review') }
</script>

<template>
  <SideDrawer :open="open" title="案头" :placement="placement" @close="emit('close')">
    <div class="zj-drawer zj-desk" data-testid="desk">
      <p v-if="empty" class="zj-drawer__quiet">此刻没有相关的东西。聊到判断、事情或资料时，它们会出现在这里。</p>

      <section v-if="draftView && draftPresent" class="zj-desk__section" aria-labelledby="zj-desk-draft-title">
        <h3 id="zj-desk-draft-title">判断草稿</h3>
        <p class="zj-desk__hint">我们把它记成一个判断？</p>
        <LiveObjectPanel
          :key="draftView.key"
          :draft="draftView.draft"
          :changed-fields="draftView.changed"
          :busy="draftView.busy"
          :error="draftView.error"
          :pending="draftView.pending"
          :timed-out="draftView.timedOut"
          @confirm="confirmDraft"
          @discard="discardDraft"
          @retry="retryDraft"
        />
      </section>

      <section class="zj-desk__section" :hidden="hostChildren === 0" aria-labelledby="zj-desk-objects-title">
        <h3 id="zj-desk-objects-title">事情、待核对、资料与章程</h3>
        <p class="zj-desk__hint">这段对话正在推进的事、还没核对的理解、这一轮的资料。看完合上。</p>
        <div :id="hostId" ref="host" class="zj-desk__host"></div>
      </section>

      <section v-if="reviewAvailable" class="zj-desk__section" aria-labelledby="zj-desk-review-title">
        <h3 id="zj-desk-review-title">回访</h3>
        <p class="zj-desk__hint">这段对话围绕一个判断：观察与复盘在工作台里。</p>
        <button type="button" class="zj-drawer__link" aria-haspopup="dialog" @click="openReview">打开观察与复盘</button>
      </section>
    </div>
  </SideDrawer>
</template>
