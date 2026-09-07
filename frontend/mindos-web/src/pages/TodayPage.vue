<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight } from 'lucide-vue-next'
import {
  createConversation,
  getZhijunHome,
  updateOnboarding,
  type HomeMapNode,
  type HomeSourceRef,
  type ZhijunHomeOverview,
} from '@/services/api'
import { useToast } from '@/composables/useToast'
import { greetingLine } from '@/shared/labels'
import RelationshipMap from '@/components/today/RelationshipMap.vue'
import RelationshipTimeline from '@/components/today/RelationshipTimeline.vue'
import HomeNodePanel from '@/components/today/HomeNodePanel.vue'
import MattersHome from '@/components/matters/MattersHome.vue'
import ErrorState from '@/components/ui/ErrorState.vue'

const router = useRouter()
const toast = useToast()

const overview = ref<ZhijunHomeOverview | null>(null)
const loading = ref(true)
const error = ref('')
const actionBusy = ref(false)
const selectedId = ref<string | null>(null)
const panelAnchor = ref<HTMLElement | null>(null)

let alive = true
let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollCount = 0

const dateLine = computed(() => greetingLine(''))
const selectedNode = computed(() => overview.value?.map.nodes.find((node) => node.id === selectedId.value) ?? null)

function clearPoll() {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
}

function scheduleRefresh() {
  clearPoll()
  if (!alive || overview.value?.brief.status !== 'refreshing' || pollCount >= 7) return
  pollTimer = setTimeout(async () => {
    pollCount += 1
    await loadHome(false)
  }, 1500)
}

async function loadHome(showLoading = true) {
  if (showLoading) loading.value = true
  error.value = ''
  try {
    const result = await getZhijunHome()
    if (!alive) return
    overview.value = result
    if (selectedId.value && !result.map.nodes.some((node) => node.id === selectedId.value)) {
      selectedId.value = null
    }
    scheduleRefresh()
  } catch (err) {
    if (!alive) return
    error.value = err instanceof Error ? err.message : '共同地图暂时没有打开'
    if (overview.value) scheduleRefresh()
  } finally {
    if (alive) loading.value = false
  }
}

async function selectNode(node: HomeMapNode) {
  selectedId.value = node.id
  await nextTick()
  panelAnchor.value?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
}

function openSource(source: HomeSourceRef) {
  const node = overview.value?.map.nodes.find((item) => item.id === source.id)
  if (node) {
    void selectNode(node)
    return
  }
  if (source.sourceType === 'decision') {
    router.push({ path: '/judgments', query: { decisionId: source.id.replace(/^decision:/, '') } })
    return
  }
  router.push({ path: '/me', query: { claim: source.id.replace(/^claim:/, '') } })
}

async function runPrimaryAction() {
  const action = overview.value?.nextAction
  if (!action || actionBusy.value) return
  if (action.kind === 'onboarding') {
    actionBusy.value = true
    try {
      await updateOnboarding('restart')
      await router.push('/onboarding/chat')
    } catch (err) {
      toast({ type: 'error', message: err instanceof Error ? err.message : '暂时无法开始第一次认识' })
    } finally {
      actionBusy.value = false
    }
    return
  }
  if (action.kind === 'resume_onboarding' && action.targetId) {
    router.push(`/onboarding/c/${encodeURIComponent(action.targetId)}`)
    return
  }
  if (action.kind === 'confirm') {
    const node = overview.value?.map.nodes.find((item) => item.claim?.id === action.targetId)
    if (node) await selectNode(node)
    else router.push('/me/inbox')
    return
  }
  if ((action.kind === 'review' || action.kind === 'reflect') && action.targetId) {
    actionBusy.value = true
    try {
      const conversation = await createConversation({ mode: 'review', decisionId: action.targetId })
      router.push(`/c/${encodeURIComponent(conversation.id)}`)
    } catch (err) {
      toast({ type: 'error', message: err instanceof Error ? err.message : '无法开始回访' })
      actionBusy.value = false
    }
    return
  }
  // System-generated prompts may quote protected decisions, summaries or claims.
  router.push({ path: '/chat', query: action.say ? { say: action.say, localOnly: '1' } : undefined })
}

async function handleNodeChanged() {
  selectedId.value = null
  pollCount = 0
  await loadHome(false)
}

onMounted(() => void loadHome())
onBeforeUnmount(() => {
  alive = false
  clearPoll()
})
</script>

<template>
  <main class="zj-today">
    <header class="zj-today__greeting">
      <div>
        <p>从眼下重要的事开始</p>
        <span>继续推进，也留一点空间回看自己。</span>
      </div>
      <time>{{ dateLine }}</time>
    </header>

    <div v-if="loading" class="zj-today__skeleton" aria-label="正在打开共同地图">
      <span />
      <span />
    </div>

    <ErrorState v-else-if="!overview" :message="error || '共同地图暂时没有打开'" recover-on-reconnect @retry="loadHome()" />

    <template v-else>
      <MattersHome class="zj-today__matters" />
      <div class="zj-today__grid">
        <article class="zj-letter" aria-label="知君写给你的今日来信">
          <header class="zj-letter__identity">
            <span class="zj-letter__seal" aria-hidden="true">知</span>
            <div>
              <strong>知君写给你的今日来信</strong>
              <small>{{ dateLine }} · 今日已送达</small>
            </div>
            <i v-if="overview.brief.status === 'refreshing'">我在重新整理</i>
          </header>
          <h1>{{ overview.brief.headline }}</h1>
          <p>{{ overview.brief.message }}</p>

          <section v-if="overview.brief.sourceRefs.length" class="zj-letter__sources" aria-label="这封来信的依据">
            <p>为什么今天写给你</p>
            <div>
              <button
                v-for="source in overview.brief.sourceRefs"
                :key="`${source.sourceType}:${source.id}`"
                type="button"
                @click="openSource(source)"
              >
                <span>{{ source.label }}</span>
                {{ source.title }}
              </button>
            </div>
          </section>

          <button class="zj-letter__action" type="button" :disabled="actionBusy" @click="runPrimaryAction">
            <span>
              <small>读完这封信，可以从这里继续</small>
              <strong>{{ overview.nextAction.title }}</strong>
            </span>
            <ArrowRight :size="18" aria-hidden="true" />
          </button>
        </article>

        <RelationshipMap
          class="zj-today__map"
          :nodes="overview.map.nodes"
          :relationship-days="overview.map.relationshipDays"
          :selected-id="selectedId"
          :empty="overview.state === 'first_meet'"
          @select="selectNode"
        />

        <div v-if="selectedNode" ref="panelAnchor" class="zj-today__panel">
          <HomeNodePanel :node="selectedNode" @close="selectedId = null" @changed="handleNodeChanged" />
        </div>
      </div>

      <RelationshipTimeline :items="overview.timeline" @open="openSource" />

      <p v-if="overview.state === 'first_meet'" class="zj-today__first-note">
        你说过的原则、做过的选择和后来发生的结果，都会在这里留下位置。
      </p>
    </template>
  </main>
</template>

<style scoped>
.zj-today {
  display: grid;
  gap: 22px;
  width: min(100%, 1120px);
  min-width: 0;
  margin: 0 auto;
  padding: 2px 0 40px;
}
.zj-today__greeting {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 18px;
  flex-wrap: wrap;
  padding: 0 4px;
}
.zj-today__greeting div { display: grid; gap: 4px; }
.zj-today__greeting p,
.zj-today__greeting span,
.zj-today__greeting time { margin: 0; }
.zj-today__greeting p {
  font-family: var(--ws-font-display, serif);
  color: var(--ws-text-primary-color, #1d211f);
  font-size: 21px;
  font-weight: 600;
}
.zj-today__greeting span,
.zj-today__greeting time {
  color: var(--ws-text-secondary-color, #686b66);
  font-size: 13px;
}
.zj-today__greeting time { white-space: nowrap; }
.zj-today__grid {
  display: grid;
  grid-template-areas: "letter map" "panel map";
  grid-template-columns: minmax(0, .86fr) minmax(0, 1.14fr);
  align-items: start;
  gap: 16px;
}
.zj-today__grid > *, .zj-today__matters { min-width: 0; max-width: 100%; }
.zj-today__map { grid-area: map; }
.zj-today__panel { grid-area: panel; }
.zj-letter {
  grid-area: letter;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  min-width: 0;
  min-height: 430px;
  padding: 24px 24px 22px;
  border: 1px solid rgba(166, 69, 46, .25);
  border-top: 3px solid var(--ws-primary-color, #a6452e);
  border-radius: 18px;
  background: linear-gradient(155deg, #fffdf8 0%, #fbf4e9 100%);
  box-shadow: 0 20px 56px rgba(55, 45, 35, .08);
}
.zj-letter > * { min-width: 0; overflow-wrap: anywhere; }
.zj-letter__identity {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}
.zj-letter__identity > div {
  display: grid;
  flex: 1;
  min-width: 0;
  gap: 2px;
}
.zj-letter__identity strong {
  color: var(--ws-primary-color, #a6452e);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: .08em;
}
.zj-letter__identity small {
  color: var(--ws-text-secondary-color, #686b66);
  font-size: 12px;
}
.zj-letter__identity i {
  color: var(--ws-text-secondary-color, #686b66);
  font-size: 12px;
  font-style: normal;
  font-weight: 400;
  white-space: nowrap;
}
.zj-letter__seal {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  border: 1px solid var(--ws-primary-color, #a6452e);
  border-radius: 8px;
  color: var(--ws-primary-color, #a6452e);
  font-family: var(--ws-font-display, serif);
  font-size: 17px;
}
.zj-letter h1 {
  margin: 0;
  font-family: var(--ws-font-display, serif);
  color: var(--ws-text-primary-color, #1d211f);
  font-size: clamp(25px, 2.4vw, 34px);
  font-weight: 600;
  line-height: 1.28;
  letter-spacing: -.02em;
}
.zj-letter > p {
  margin: -2px 0 0;
  color: var(--ws-text-secondary-color, #686b66);
  font-family: var(--ws-font-display, serif);
  font-size: 15px;
  line-height: 1.9;
}
.zj-letter__sources {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--ws-border-color-3, #e8e2d7);
}
.zj-letter__sources > p {
  margin: 0;
  color: var(--ws-text-secondary-color, #686b66);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: .08em;
}
.zj-letter__sources > div {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  gap: 7px;
}
.zj-letter__sources button {
  min-width: 0;
  max-width: 100%;
  padding: 5px 8px;
  border: 1px solid var(--ws-border-color-3, #e8e2d7);
  border-radius: 12px;
  background: transparent;
  color: var(--ws-text-secondary-color, #686b66);
  font: inherit;
  font-size: 12px;
  white-space: normal;
  overflow-wrap: anywhere;
  line-height: 1.55;
  text-align: left;
  cursor: pointer;
}
.zj-letter__sources button:hover,
.zj-letter__sources button:focus-visible {
  border-color: var(--ws-primary-color, #a6452e);
  outline: none;
}
.zj-letter__sources span {
  margin-right: 4px;
  color: var(--ws-primary-color, #a6452e);
  font-weight: 600;
}
.zj-letter__action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  width: 100%;
  margin-top: 3px;
  padding: 14px 15px;
  border: 0;
  border-radius: 10px;
  background: var(--ws-primary-color, #a6452e);
  color: #fffaf2;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
}
.zj-letter__action:disabled { cursor: wait; opacity: .7; }
.zj-letter__action span { display: grid; min-width: 0; gap: 3px; text-align: left; overflow-wrap: anywhere; }
.zj-letter__action svg { flex-shrink: 0; }
.zj-letter__action small { opacity: .9; font-size: 12px; font-weight: 400; }
.zj-letter__action strong { font-size: 14px; font-weight: 600; }
.zj-letter__action:hover:not(:disabled) { filter: brightness(.94); }
.zj-letter__action:focus-visible { outline: 3px solid rgba(166, 69, 46, .2); outline-offset: 3px; }
.zj-today__first-note {
  margin: -6px 0 0;
  text-align: center;
  color: var(--ws-text-secondary-color, #686b66);
  font-family: var(--ws-font-display, serif);
  font-size: 13px;
}
.zj-today__skeleton {
  display: grid;
  grid-template-columns: 1.42fr .78fr;
  gap: 16px;
}
.zj-today__skeleton span {
  min-height: 560px;
  border-radius: 18px;
  background: linear-gradient(110deg, #f4f0e8 25%, #faf7f0 45%, #f4f0e8 65%);
  background-size: 220% 100%;
  animation: zj-home-loading 1.4s ease infinite;
}
.zj-today__skeleton span:last-child { min-height: 330px; }
.zj-today__fallback {
  display: grid;
  justify-items: start;
  gap: 12px;
  padding: 26px;
  border: 1px solid var(--ws-border-color-3, #e8e2d7);
  border-radius: 14px;
}
.zj-today__fallback p { margin: 0; color: var(--ws-text-secondary-color, #686b66); }
.zj-today__fallback button { border: 0; background: none; color: var(--ws-primary-color, #a6452e); cursor: pointer; }
@keyframes zj-home-loading { from { background-position: 100% 0; } to { background-position: -100% 0; } }
@media (prefers-reduced-motion: reduce) {
  .zj-today *, .zj-today *::before, .zj-today *::after { scroll-behavior: auto !important; animation: none !important; transition: none !important; }
}
@media (max-width: 800px) {
  .zj-today { gap: 14px; padding-bottom: 26px; }
  .zj-today__greeting { display: grid; gap: 4px; padding: 0 2px; }
  .zj-today__greeting p { font-size: 18px; }
  .zj-today__greeting span { font-size: 13px; }
  .zj-today__grid {
    grid-template-areas: "letter" "map" "panel";
    grid-template-columns: minmax(0, 1fr);
  }
  .zj-letter { min-height: 0; padding: 20px 18px 18px; }
  .zj-letter h1 { font-size: 27px; }
  .zj-today__skeleton { grid-template-columns: 1fr; }
  .zj-today__skeleton span { min-height: 320px; }
  .zj-today__skeleton span:last-child { min-height: 420px; }
}
</style>
