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
import { nextWriteUncertainty, showWriteUncertainty, uncertainWriteMessage, type UncertainWriteOwner } from './writeUncertainty'

const bridge = (window as Window & { zhijunDesktop?: ZhijunDesktopV1 }).zhijunDesktop
const controller = new DesktopController(bridge, { materials: false })
const state = shallowRef(controller.state)
const router = useRouter()
const signedIn = computed(() => !!state.value.snapshot?.subject?.accountId)
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
  <App v-if="signedIn">
    <template #content>
      <div v-if="showUncertainWrite" role="alert" data-testid="uncertain-write-notice" class="uncertain-write-notice">{{ uncertainWriteMessage }}</div>
      <RouterView v-if="ready" :key="scopeKey ?? ''" />
      <DesktopConnection v-else embedded />
    </template>
    <template #topbar="{ toggleMenu }"><DesktopTopbar :workspace-ready="ready" @toggle-menu="toggleMenu" /></template>
  </App>
  <DesktopConnection v-else />
</template>
<style scoped>
.uncertain-write-notice { margin: 16px 24px; padding: 14px 18px; border: 1px solid #c98c6b; border-radius: 12px; background: #fff4e8; color: #713e2a; line-height: 1.7; }
</style>
