<script setup lang="ts">
// 「偏好」：承载 /settings 的页面内容（关系设置 / 记忆整理 / 日常对话与理解 / 本地文件处理 / 运行监控与任务，
// 桌面端的账号与盒子、外部 Agent 由同一个槽带进来），顶部一排锚点；
// 「高级」= 对话页 RoutingPanel 投进来的整理状态（#zj-prefs-advanced-host）+ 界面与节奏偏好卡。
// 抽屉常驻挂载，所以宿主在对话页渲染前就存在。
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import SideDrawer from '@/components/ui/SideDrawer.vue'
import ShellPreferenceCard from '@/components/settings/ShellPreferenceCard.vue'
import { ADVANCED_HOST } from '../shellContext'
import { useSheetPlacement } from '../composables/useSheetPlacement'
import { PREFS_ADVANCED_ID, PREFS_ANCHORS, availableAnchorIds, findAnchorTarget, type PrefsAnchor } from './prefsAnchors'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()
const placement = useSheetPlacement()
const body = ref<HTMLElement | null>(null)
const advancedHostId = ADVANCED_HOST.slice(1)
const available = ref<Set<string>>(new Set())
let observer: MutationObserver | null = null
let frame = 0

function refreshAnchors() {
  cancelAnimationFrame(frame)
  frame = requestAnimationFrame(() => { if (body.value) available.value = availableAnchorIds(body.value) })
}

onMounted(() => {
  if (!body.value) return
  refreshAnchors()
  observer = new MutationObserver(refreshAnchors)
  observer.observe(body.value, { childList: true, subtree: true })
})
onBeforeUnmount(() => { observer?.disconnect(); cancelAnimationFrame(frame) })
watch(() => props.open, open => { if (open) void nextTick(refreshAnchors) })

function jump(anchor: PrefsAnchor) {
  const target = body.value ? findAnchorTarget(body.value, anchor) : null
  if (!(target instanceof HTMLElement)) return
  const reduced = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  target.scrollIntoView({ block: 'start', behavior: reduced ? 'auto' : 'smooth' })
  if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1')
  target.focus({ preventScroll: true })
}
</script>

<template>
  <SideDrawer :open="open" title="偏好" wide :placement="placement" @close="emit('close')">
    <template #navigation>
      <nav class="zj-prefs__anchors" aria-label="偏好分区">
        <button v-for="anchor in PREFS_ANCHORS" :key="anchor.id" type="button" :hidden="!available.has(anchor.id)" @click="jump(anchor)">{{ anchor.label }}</button>
      </nav>
    </template>
    <div ref="body" class="zj-drawer zj-prefs" data-testid="preferences">
      <div class="zj-prefs__page"><slot /></div>
      <section :id="PREFS_ADVANCED_ID" class="zj-prefs__advanced" aria-labelledby="zj-prefs-advanced-title">
        <h2 id="zj-prefs-advanced-title">高级</h2>
        <p class="zj-prefs__hint">这段对话的整理状态，以及界面与节奏。</p>
        <div :id="advancedHostId" class="zj-prefs__host"></div>
        <ShellPreferenceCard />
      </section>
    </div>
  </SideDrawer>
</template>
