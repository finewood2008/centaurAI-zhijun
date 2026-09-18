<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, provide, readonly, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import type { ZhijunDesktopV1 } from '../../../shared/desktop-contract'
import App from '../App.vue'
import { installProductTransport } from '../services/transport'
import { installProductFiles } from '../services/productFiles'
import { createDesktopProductClient } from './productClient'
import type { ProductDesktop } from '../../../shared/product-contract'
import { setProductScope } from '../shared/productScope'
import { DesktopController } from './controller'
import { desktopWorkspaceKey } from './workspace'
import DesktopConnection from './DesktopConnection.vue'
import DesktopTopbar from './DesktopTopbar.vue'
import { connectionLabel } from './connectionPresentation'
import { nextWriteUncertainty, showWriteUncertainty, uncertainWriteMessage, type UncertainWriteOwner } from './writeUncertainty'

const bridge = (window as Window & { zhijunDesktop?: ZhijunDesktopV1 }).zhijunDesktop
const controller = new DesktopController(bridge, { materials: false })
const state = shallowRef(controller.state)
const router = useRouter()
const signedIn = computed(() => !!state.value.snapshot?.subject?.accountId)
const isSettings = computed(() => router.currentRoute.value.path === '/settings')
const phase = computed(() => state.value.snapshot?.phase)
const hasEnteredWorkspace = shallowRef(false)
const enteredScope = shallowRef<string | null>(null)
const uncertainWrite = shallowRef<UncertainWriteOwner | null>(null)
const showUncertainWrite = computed(() => showWriteUncertainty(uncertainWrite.value, state.value.snapshot))
const ready = computed(() => enteredScope.value !== null && enteredScope.value === scopeKey && !!client && state.value.snapshot?.phase === 'ready' && !!state.value.snapshot.subject && !!state.value.snapshot.capabilities.product)
const product = (bridge as (ZhijunDesktopV1 & { product?: ProductDesktop }) | undefined)?.product
const client = product ? createDesktopProductClient(product, () => {
  const snapshot = state.value.snapshot
  const workspaceId = (snapshot?.subject as { workspaceId?: string } | undefined)?.workspaceId
  return snapshot?.phase === 'ready' && workspaceId ? { generation: snapshot.generation, workspaceId } : null
}) : null
const stopTransport = client ? installProductTransport(client.request) : () => {}
const stopFiles = client ? installProductFiles(client) : () => {}
let scopeKey: string | null = null
const stopObserving = controller.observe(next => {
  const snapshot = next.snapshot
  const previous = state.value.snapshot
  if (previous?.subject?.accountId !== snapshot?.subject?.accountId) hasEnteredWorkspace.value = false
  // Capture before scope reset aborts old requests and unmounts their error UI.
  uncertainWrite.value = nextWriteUncertainty(previous, snapshot, uncertainWrite.value,
    !!previous?.subject?.workspaceId && !!client?.hasPendingMutations({
      generation: previous.generation, workspaceId: previous.subject.workspaceId,
    }))
  const workspaceId = (snapshot?.subject as { workspaceId?: string } | undefined)?.workspaceId
  const nextScope = client && snapshot?.phase === 'ready' && snapshot.capabilities.product && snapshot.subject && workspaceId
    ? JSON.stringify([snapshot.subject.accountId, snapshot.subject.deviceId, workspaceId, snapshot.generation]) : null
  if (nextScope !== scopeKey) {
    scopeKey = nextScope
    enteredScope.value = null
    setProductScope(scopeKey, nextScope && snapshot?.subject
      ? JSON.stringify([snapshot.subject.accountId, snapshot.subject.deviceId, workspaceId]) : null)
    if (nextScope) void nextTick(async () => {
      await router.isReady()
      if (scopeKey !== nextScope) return
      // Settings and the bare overview have no owner detail ID. Preserve them
      // across reloads; never carry a previous owner's detail/query into a box.
      const route = router.currentRoute.value
      const bareOverview = route.path === '/me' && Object.keys(route.query).length === 0
      if (!isSettings.value && !bareOverview) await router.replace({ path: '/', force: true })
      if (scopeKey === nextScope) {
        enteredScope.value = nextScope
        hasEnteredWorkspace.value = true
      }
    })
  }
  state.value = next
})
provide(desktopWorkspaceKey, { controller, state, ready, scopeKey: readonly(enteredScope) })
onMounted(() => { void controller.start() })
onBeforeUnmount(() => { stopObserving(); setProductScope(null); client?.dispose(); stopTransport(); stopFiles(); controller.dispose() })
</script>
<template>
  <App v-if="signedIn" :workspace-ready="ready" :connection-label="connectionLabel(phase, ready)">
    <template #content>
      <div v-if="showUncertainWrite" role="alert" data-testid="uncertain-write-notice" class="uncertain-write-notice">{{ uncertainWriteMessage }}</div>
      <RouterView v-if="ready || isSettings" :key="isSettings ? 'settings' : scopeKey ?? ''" />
      <DesktopConnection v-else-if="!hasEnteredWorkspace" embedded />
      <section v-else class="connection-required" data-testid="workspace-unavailable" role="status">
        <h1>需要连接盒子</h1>
        <p>请从侧栏底部的「设置」连接盒子，连接后即可继续使用。</p>
      </section>
    </template>
    <template #topbar="{ toggleMenu }"><DesktopTopbar :workspace-ready="ready" @toggle-menu="toggleMenu" /></template>
  </App>
  <DesktopConnection v-else />
</template>
<style scoped>
.uncertain-write-notice { margin: 16px 24px; padding: 14px 18px; border: 1px solid #c98c6b; border-radius: 12px; background: #fff4e8; color: #713e2a; line-height: 1.7; }
.connection-required { padding: 32px; color: var(--ws-text-secondary-color); }
.connection-required h1 { color: var(--ws-text-color); font: 24px var(--ws-font-display); }
</style>
