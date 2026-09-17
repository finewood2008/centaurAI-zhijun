<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Check, HardDrive, LoaderCircle, Lock, Monitor, Shield, TriangleAlert } from 'lucide-vue-next'
import type { DesktopSnapshot } from '../../../shared/desktop-contract'
import { connectionAttemptKey, connectionPresentation, type ConnectionStagePresentation } from './connectionPresentation'

const props = defineProps<{ snapshot: DesktopSnapshot | null; canCancel: boolean }>()
const emit = defineEmits<{ cancel: [] }>()
const elapsedSeconds = ref(0)
let startedAt: number | null = null
let activeKey = ''
let previousPhase: DesktopSnapshot['phase'] | 'idle' = 'idle'
let timer: ReturnType<typeof setInterval> | undefined

function stopTimer(): void {
  if (timer !== undefined) clearInterval(timer)
  timer = undefined
}

function updateElapsed(): void {
  if (startedAt === null) return
  elapsedSeconds.value = Math.max(0, Math.floor((performance.now() - startedAt) / 1000))
}

function startTimer(): void {
  if (timer !== undefined) return
  timer = setInterval(updateElapsed, 1000)
}

watch(() => props.snapshot, snapshot => {
  const nextPhase = snapshot?.phase ?? 'idle'
  const nextKey = connectionAttemptKey(snapshot)
  const productReady = snapshot?.phase === 'ready' && snapshot.capabilities.product === true
  const shouldTime = nextPhase === 'connecting' || nextPhase === 'authorizing'
    || nextPhase === 'disconnecting' || productReady
  const recoveryRestart = nextPhase === 'connecting'
    && ['failed', 'ready', 'disconnecting'].includes(previousPhase)
  if (nextKey !== activeKey || recoveryRestart || startedAt === null) {
    activeKey = nextKey
    startedAt = performance.now()
    elapsedSeconds.value = 0
  }
  previousPhase = nextPhase
  if (!snapshot || nextPhase === 'failed' || !shouldTime) {
    if (snapshot && nextPhase === 'failed') updateElapsed()
    stopTimer()
  } else {
    startTimer()
  }
}, { immediate: true })

onBeforeUnmount(stopTimer)

const view = computed(() => connectionPresentation(props.snapshot, elapsedSeconds.value, props.canCancel))
const stageIcon = (item: ConnectionStagePresentation) => item.id === 'connect' ? Shield
  : item.id === 'authorize' ? Lock : Monitor
const statusIcon = (item: ConnectionStagePresentation) => item.state === 'complete' || item.state === 'simulated' ? Check
  : item.state === 'active' ? LoaderCircle : item.state === 'error' ? TriangleAlert : null
</script>

<template>
  <section class="scp-card" :class="`is-${view.phase}`" :data-phase="view.phase"
    data-testid="secure-connection-progress" aria-labelledby="scp-title">
    <p class="scp-live" aria-live="polite" aria-atomic="true">{{ view.announcement }}</p>
    <header class="scp-header">
      <span class="scp-emblem" aria-hidden="true"><Shield :size="21" :stroke-width="1.8" /></span>
      <div class="scp-heading">
        <p class="scp-eyebrow">安全连接</p>
        <h2 id="scp-title">{{ view.title }}</h2>
        <p class="scp-detail">{{ view.detail }}</p>
      </div>
      <div class="scp-device" data-testid="secure-connection-device">
        <HardDrive :size="15" aria-hidden="true" />
        <span>{{ view.deviceLabel }}</span>
      </div>
    </header>

    <p v-if="view.simulationNotice" class="scp-simulation" data-testid="connection-simulation-note">
      {{ view.simulationNotice }}
    </p>

    <p class="scp-security" data-testid="connection-security-note">{{ view.securityNotice }}</p>

    <ol class="scp-stages" aria-label="连接进度">
      <li v-for="item in view.stages" :key="item.id" class="scp-stage" :class="`is-${item.state}`"
        :data-state="item.state" :data-testid="`connection-stage-${item.id}`">
        <span class="scp-stage-icon" aria-hidden="true">
          <component :is="stageIcon(item)" :size="18" :stroke-width="1.8" />
          <span class="scp-status-mark">
            <component :is="statusIcon(item)" v-if="statusIcon(item)" :size="11" :stroke-width="2.5" />
          </span>
        </span>
        <span class="scp-stage-copy"><strong>{{ item.label }}</strong><small>{{ item.detail }}</small></span>
      </li>
    </ol>

    <footer class="scp-footer">
      <div class="scp-status-copy">
        <p v-if="view.pathLabel" class="scp-path" data-testid="connection-path">{{ view.pathLabel }}</p>
        <p v-if="view.elapsedLabel" class="scp-elapsed" data-testid="connection-elapsed" aria-hidden="true">{{ view.elapsedLabel }}</p>
        <p v-if="view.longWaitMessage" class="scp-wait" data-testid="connection-wait-note">{{ view.longWaitMessage }}</p>
        <p v-if="view.retryMessage" class="scp-retry" data-testid="connection-retry-note">{{ view.retryMessage }}</p>
      </div>
      <button v-if="props.canCancel && view.cancellablePhase" type="button" class="scp-cancel"
        data-testid="connection-cancel" @click="emit('cancel')">取消连接</button>
    </footer>
  </section>
</template>

<style scoped>
.scp-card {
  width: 100%; min-width: 0; overflow: hidden; padding: 20px 22px 18px;
  border: 1px solid #e6dbcd; border-radius: 16px; background: #fffdfa;
  color: #302e2a; box-shadow: 0 12px 32px rgba(78, 57, 39, .06);
}
.scp-live { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.scp-header { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: start; gap: 12px; }
.scp-emblem { display: grid; place-items: center; width: 38px; height: 38px; border-radius: 11px; background: #f8ebe4; color: #a6452e; }
.scp-heading { min-width: 0; }
.scp-eyebrow { margin: 0 0 1px; color: #98654b; font-size: 11px; font-weight: 650; letter-spacing: .13em; }
.scp-heading h2 { margin: 0; font-family: 'Songti SC', STSong, serif; font-size: 20px; line-height: 1.35; font-weight: 600; overflow-wrap: anywhere; }
.scp-detail { margin: 4px 0 0; color: #726d64; font-size: 12px; line-height: 1.55; overflow-wrap: anywhere; }
.scp-device { display: inline-flex; align-items: center; gap: 6px; max-width: 230px; min-width: 0; padding: 6px 9px; border: 1px solid #e4ddd2; border-radius: 999px; color: #625e56; background: #faf7f1; font-size: 11px; }
.scp-device span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.scp-simulation { margin: 13px 0 0; padding: 8px 10px; border-radius: 8px; background: #fbecd8; color: #7e4b1b; font-size: 11px; line-height: 1.5; }
.scp-security { margin: 12px 0 0; color: #6f685f; font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.scp-stages { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 17px 0 0; padding: 0; list-style: none; }
.scp-stage { position: relative; display: flex; min-width: 0; gap: 9px; padding: 11px; border: 1px solid #e7e0d6; border-radius: 11px; background: #faf8f3; }
.scp-stage::after { content: ''; position: absolute; top: 50%; left: calc(100% + 1px); width: 8px; height: 1px; background: #ddd4c7; }
.scp-stage:last-child::after { display: none; }
.scp-stage-icon { position: relative; display: grid; place-items: center; flex: 0 0 auto; width: 29px; height: 29px; border-radius: 9px; color: #8a877e; background: #efede8; }
.scp-status-mark { position: absolute; right: -4px; bottom: -4px; display: grid; place-items: center; width: 16px; height: 16px; border: 2px solid #faf8f3; border-radius: 50%; background: #d9d5ce; color: #fff; }
.scp-stage-copy { display: block; min-width: 0; }
.scp-stage-copy strong { display: block; font-size: 12px; line-height: 1.35; }
.scp-stage-copy small { display: block; margin-top: 3px; color: #79756c; font-size: 10px; line-height: 1.45; overflow-wrap: anywhere; }
.scp-stage.is-active { border-color: #d6a793; background: #fff7f2; }
.scp-stage.is-active .scp-stage-icon { color: #a6452e; background: #f6e2d8; }
.scp-stage.is-active .scp-status-mark { background: #a6452e; }
.scp-stage.is-active .scp-status-mark svg { animation: scp-spin 1.1s linear infinite; }
.scp-stage.is-complete .scp-stage-icon { color: #477053; background: #e8f0e8; }
.scp-stage.is-complete .scp-status-mark { background: #4a7c59; }
.scp-stage.is-error { border-color: #dec0b5; background: #fff3ee; }
.scp-stage.is-error .scp-stage-icon, .scp-stage.is-error .scp-status-mark { color: #9a3f2d; background: #f4dcd3; }
.scp-stage.is-blocked { opacity: .72; }
.scp-stage.is-simulated .scp-stage-icon { color: #8a6331; background: #f5ead8; }
.scp-stage.is-simulated .scp-status-mark { background: #9c7646; }
.scp-footer { display: flex; align-items: end; justify-content: space-between; gap: 16px; margin-top: 14px; padding-top: 13px; border-top: 1px solid #eee7dd; }
.scp-status-copy { min-width: 0; font-size: 11px; line-height: 1.5; }
.scp-status-copy p { margin: 0; overflow-wrap: anywhere; }
.scp-path { display: inline-block; margin-right: 9px !important; color: #83513b; font-weight: 650; }
.scp-elapsed { display: inline-block; color: #858078; }
.scp-wait { margin-top: 3px !important; color: #775b3a; }
.scp-retry { margin-top: 2px !important; color: #8c3828; }
.scp-cancel { flex: 0 0 auto; min-height: 34px; padding: 6px 12px; border: 1px solid #d8c7ba; border-radius: 8px; background: transparent; color: #8f3a26; font: inherit; font-size: 12px; cursor: pointer; }
.scp-cancel:hover { border-color: #a6452e; background: #faf0e8; }
.scp-card.is-failed { border-color: #dec0b5; }
@keyframes scp-spin { to { transform: rotate(360deg); } }
@media (max-width: 620px) {
  .scp-card { padding: 17px 15px 15px; }
  .scp-header { grid-template-columns: auto minmax(0, 1fr); }
  .scp-device { grid-column: 2; max-width: 100%; justify-self: start; }
  .scp-stages { grid-template-columns: 1fr; }
  .scp-stage::after { top: calc(100% + 1px); left: 25px; width: 1px; height: 8px; }
  .scp-footer { align-items: flex-start; flex-direction: column; }
  .scp-cancel { width: 100%; }
}
@media (prefers-reduced-motion: reduce) {
  .scp-stage.is-active .scp-status-mark svg { animation: none; }
  *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
}
</style>
