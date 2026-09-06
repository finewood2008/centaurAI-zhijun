<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, provide, shallowRef } from 'vue'
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

const bridge = (window as Window & { zhijunDesktop?: ZhijunDesktopV1 }).zhijunDesktop
const controller = new DesktopController(bridge, { materials: false })
const state = shallowRef(controller.state)
const router = useRouter()
const enteredScope = shallowRef<string | null>(null)
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
  const workspaceId = (snapshot?.subject as { workspaceId?: string } | undefined)?.workspaceId
  const nextScope = client && snapshot?.phase === 'ready' && snapshot.capabilities.product && snapshot.subject && workspaceId
    ? JSON.stringify([snapshot.subject.accountId, snapshot.subject.deviceId, workspaceId, snapshot.generation]) : null
  if (nextScope !== scopeKey) {
    scopeKey = nextScope
    enteredScope.value = null
    setProductScope(scopeKey)
    if (nextScope) void nextTick(async () => {
      if (scopeKey !== nextScope) return
      // Start each owner at their own home/onboarding. Never carry another owner's detail ID.
      await router.replace({ path: '/', force: true })
      if (scopeKey === nextScope) enteredScope.value = nextScope
    })
  }
  state.value = next
})
provide(desktopWorkspaceKey, { controller, state })
onMounted(() => { void controller.start() })
onBeforeUnmount(() => { stopObserving(); setProductScope(null); client?.dispose(); stopTransport(); stopFiles(); controller.dispose() })
</script>
<template>
  <App v-if="ready" :key="scopeKey ?? ''">
    <template #topbar="{ toggleMenu }"><DesktopTopbar @toggle-menu="toggleMenu" /></template>
  </App>
  <DesktopConnection v-else />
</template>
