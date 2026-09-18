<script setup lang="ts">
// 沉浸壳：一张米纸，上方一行在场（知印 + 印坞），中间一条流，下方输入区宿主。
// 没有导航：流路由（/chat、/c/:id）在舞台上渲染 #content 槽（RouterView → 嵌入模式的 ConversationPage）；
// 其余路由在宽抽屉里渲染同一个槽，合上抽屉回到 /chat。桌面端未就绪时，槽由 DesktopApp 提供连接 / 不可用界面。
// 不引入 src/desktop/*：桌面状态只经 App.vue 以 props 传入。
import { computed, provide, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ErrorBoundary from '@/components/ErrorBoundary.vue'
import PresenceBar from './PresenceBar.vue'
import SealDock, { type SealId } from './SealDock.vue'
import RoutePageDrawer from './RoutePageDrawer.vue'
import SelfScrollDrawer from './drawers/SelfScroll.vue'
import PastTimelineDrawer from './drawers/PastTimeline.vue'
import DeskDrawer from './drawers/Desk.vue'
import PreferencesDrawer from './drawers/Preferences.vue'
import { useDayTint } from './composables/useDayTint'
import DayStream from './DayStream.vue'
import { ADVANCED_HOST, COMPOSER_HOST, DESK_HOST, immersiveKey } from './shellContext'

withDefaults(defineProps<{ workspaceReady?: boolean; connectionLabel?: string }>(), { workspaceReady: true, connectionLabel: '' })

const route = useRoute()
const router = useRouter()
const ready = ref(false)
// 流的滚动容器由 DayStream 暴露（el）；壳只转发给嵌入的对话页
const dayStreamRef = ref<InstanceType<typeof DayStream> | null>(null)
const NEAR_BOTTOM_PX = 120

function scroller(): HTMLElement | null {
  return dayStreamRef.value?.el ?? null
}
function isNearBottom(): boolean {
  const el = dayStreamRef.value?.el ?? null
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX
}
provide(immersiveKey, { embedded: true, scroller, composerHost: COMPOSER_HOST, deskHost: DESK_HOST, advancedHost: ADVANCED_HOST, isNearBottom })

const isStream = computed(() => route.path === '/chat' || route.path.startsWith('/c/'))
const inDrawer = computed(() => ready.value && !isStream.value && route.path !== '/')
const drawerTitle = computed(() => (typeof route.meta.title === 'string' ? route.meta.title : '知君'))
// 抽屉：我 / 昔 / 案 由印坞开合；偏 = /settings 路由，页面内容由偏好抽屉承载（其余非流路由仍走 RoutePageDrawer）
const shellRef = ref<HTMLElement | null>(null)
useDayTint(shellRef)
type DrawerId = 'self' | 'past' | 'desk'
const drawer = ref<DrawerId | null>(null)
const isSettings = computed(() => route.path === '/settings')
const prefsOpen = computed(() => inDrawer.value && isSettings.value)
const activeSeal = computed<SealId | null>(() => drawer.value ?? (prefsOpen.value ? 'prefs' : null))

// 首屏在路由就绪之后再落定，避免把深链（/c/:id、/me?claim=…）误判为「/」而改写
void router.isReady().then(() => { ready.value = true })
watch([ready, () => route.path], ([isReady, path]) => {
  if (isReady && path === '/') void router.replace('/chat')
}, { immediate: true })

function onSeal(id: SealId) {
  if (id === 'prefs') {
    drawer.value = null
    if (!isSettings.value) void router.push('/settings')
    return
  }
  drawer.value = drawer.value === id ? null : id
}
// 「昔」点一条回到那天：DayStream 暴露 revealConversation 时经它定位（滚动 + 高亮），否则退回路由
function revealConversation(id: string) {
  drawer.value = null
  const stream = dayStreamRef.value as unknown as { revealConversation?: (id: string) => void } | null
  if (stream?.revealConversation) stream.revealConversation(id)
  else void router.push(`/c/${encodeURIComponent(id)}`)
}
// 换路由（去 /me、/review、/c/:id …）时合上印坞抽屉，避免两层抽屉叠着
watch(() => route.path, () => { drawer.value = null })
function closeDrawer() {
  void router.push('/chat')
}
</script>

<template>
  <div ref="shellRef" class="zj-shell">
    <PresenceBar :workspace-ready="workspaceReady" :connection-label="connectionLabel">
      <template #dock><SealDock :active="activeSeal" @select="onSeal" /></template>
    </PresenceBar>

    <!-- 四个抽屉先于流挂载：#zj-desk-host 在案头里、#zj-prefs-advanced-host 在偏好的「高级」里，对话页渲染前宿主已存在 -->
    <SelfScrollDrawer :open="drawer === 'self'" @close="drawer = null" />
    <PastTimelineDrawer :open="drawer === 'past'" @close="drawer = null" @reveal="revealConversation" />
    <DeskDrawer :open="drawer === 'desk'" @close="drawer = null" />
    <PreferencesDrawer :open="prefsOpen" @close="closeDrawer">
      <template v-if="prefsOpen">
        <ErrorBoundary>
          <slot name="content"><RouterView /></slot>
        </ErrorBoundary>
      </template>
    </PreferencesDrawer>

    <main class="zj-stage">
      <div v-if="!ready" class="zj-stream" aria-hidden="true"></div>
      <!-- 一条按天分节的流：当前块（嵌入的对话页）作为 #active 槽落在今天的末尾；抽屉打开时流留在原地，槽改到抽屉里 -->
      <DayStream v-else ref="dayStreamRef" :muted="inDrawer">
        <template v-if="!inDrawer" #active>
          <ErrorBoundary>
            <slot name="content"><RouterView /></slot>
          </ErrorBoundary>
        </template>
      </DayStream>
      <template v-if="inDrawer && !prefsOpen">
        <RoutePageDrawer open :title="drawerTitle" @close="closeDrawer">
          <ErrorBoundary>
            <slot name="content"><RouterView /></slot>
          </ErrorBoundary>
        </RoutePageDrawer>
      </template>
    </main>

    <div id="zj-composer-host" class="zj-shell__composer"></div>
  </div>
</template>
